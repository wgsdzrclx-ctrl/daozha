"""v0.7 迁移/限流回归：临时 SQLite 和模拟 HTTP，不连接真实后端。"""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import MagicMock, patch

from gate_backend import GateBackend
from receiver_audit import ReceiverAudit


class BackendV07Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'backend.db'
        self.backend = self.make_backend()
        self.calls = []
        self.status, self.headers, self.data = 200, {}, None
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.session.post.side_effect = self.respond
        patcher = patch('gate_backend.requests.Session', return_value=self.session)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_backend(self, **kwargs):
        return GateBackend('http://127.0.0.1:12345', self.path, api_token='private-test-token',
                           **({'min_interval': 0} | kwargs))

    def respond(self, url, **kwargs):
        self.calls.append((url, time.monotonic(), kwargs))
        result = MagicMock()
        result.__enter__.return_value = result
        result.status_code, result.headers = self.status, self.headers
        payload = kwargs['json']
        result.json.return_value = self.data if self.data is not None else {
            'code': 200, 'data': {'carNo': payload['carNo'], 'lotId': payload['lotId'], 'lift': True}}
        return result

    def decide(self, timestamp=1793865600, direction='in', backend=None, budget=1):
        return (backend or self.backend).decide('camera', timestamp, '皖MF58780', direction,
                                               deadline=time.monotonic() + budget)

    def test_two_lots_have_separate_keys_and_restart_keeps_dedup(self):
        self.assertTrue(self.decide())
        lot_b = self.make_backend(lot_id='LOT-B')
        self.assertTrue(self.decide(backend=lot_b))
        self.assertFalse(self.decide(backend=self.make_backend()))
        self.assertFalse(self.decide(backend=self.make_backend(lot_id='LOT-B')))
        self.assertEqual(len(self.calls), 2)
        for _, _, kwargs in self.calls:
            self.assertEqual(kwargs['headers'], {'X-Gate-Token': 'private-test-token'})
            self.assertNotIn('token', kwargs['json'])

    def test_tls_verification_is_enabled_by_default_and_can_be_disabled_per_backend(self):
        self.assertTrue(self.decide())
        self.assertIs(self.calls[-1][2]['verify'], True)
        testing = self.make_backend(verify_tls=False)
        self.assertTrue(self.decide(timestamp=1793865601, backend=testing))
        self.assertIs(self.calls[-1][2]['verify'], False)
        self.assertTrue(self.decide(timestamp=1793865602))
        self.assertIs(self.calls[-1][2]['verify'], True)
        with self.assertRaises(ValueError):
            self.make_backend(verify_tls='false')

    def test_interval_shared_between_lots_and_camera_requests(self):
        first, second = self.make_backend(min_interval=0.08), self.make_backend(lot_id='LOT-B', min_interval=0.08)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda b: self.decide(backend=b), (first, second)))
        self.assertEqual(results, [True, True])
        self.assertGreaterEqual(self.calls[1][1] - self.calls[0][1], 0.08)

    def test_routes_independent_and_wait_cannot_outlive_deadline(self):
        self.backend = self.make_backend(min_interval=3)
        self.assertTrue(self.decide())
        self.assertTrue(self.decide(direction='out'))
        started = time.monotonic()
        self.assertFalse(self.decide(timestamp=1793865601, budget=0.05))
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(len(self.calls), 2)
        record = self.backend.lookup('camera', 1793865601, '皖MF58780', 'in')
        self.assertEqual(record['state'], 'not_sent')
        self.assertIsNone(record['http_status'])

    def test_waiting_for_inflight_request_expires_without_send(self):
        lock = threading.Lock()
        lock.acquire()
        with patch.dict(GateBackend._route_locks, {(self.backend.base_url, '/gate/entry'): lock}):
            self.assertFalse(self.decide(budget=0.03))
        lock.release()
        self.assertEqual(self.calls, [])

    def test_retry_after_persists_and_does_not_retry_failed_event(self):
        self.status, self.headers = 429, {'Retry-After': '3'}
        self.data = {'code': 429, 'msg': 'too frequent'}
        self.assertFalse(self.decide())
        self.assertFalse(self.decide())
        reopened = self.make_backend()
        self.assertFalse(self.decide(timestamp=1793865601, backend=reopened, budget=0.05))
        self.assertEqual(len(self.calls), 1)
        record = reopened.lookup('camera', 1793865600, '皖MF58780', 'in')
        self.assertEqual((record['state'], record['http_status'], record['retry_after']), ('rate_limited', 429, 3))

    def test_bad_retry_after_and_non_json_auth_still_fail_closed(self):
        for index, header in enumerate(('bad', '-1', '')):
            self.status, self.headers = 429, {'Retry-After': header}
            self.assertFalse(self.decide(timestamp=1793865600 + index))
            self.assertIsNone(self.backend.lookup('camera', 1793865600 + index, '皖MF58780', 'in')['retry_after'])
        response = MagicMock(status_code=401, headers={})
        response.__enter__.return_value = response
        response.json.side_effect = ValueError('not JSON')
        self.session.post.side_effect = None
        self.session.post.return_value = response
        self.assertFalse(self.decide(timestamp=1793865700))
        self.assertEqual(self.backend.lookup('camera', 1793865700, '皖MF58780', 'in')['state'], 'auth_failed')

    def test_response_and_exception_credentials_are_redacted(self):
        self.status = 401
        self.data = {'msg': 'private-test-token', 'echo': {'private-test-token': ['private-test-token']}}
        with self.assertLogs('gate.receiver', level='WARNING') as logs:
            self.assertFalse(self.decide())
        row = self.backend.lookup('camera', 1793865600, '皖MF58780', 'in')
        self.assertNotIn('private-test-token', json.dumps(row) + str(logs.output))
        self.assertIn('[REDACTED]', row['response_json'])

    def test_legacy_migration_preserves_all_rows_and_is_idempotent(self):
        legacy = Path(self.temp.name) / 'legacy.db'
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute('''CREATE TABLE backend_events (
                backend TEXT,camera TEXT,timestamp INTEGER,plate TEXT,direction TEXT,
                state TEXT NOT NULL,request_json TEXT NOT NULL,response_json TEXT,detail TEXT,
                PRIMARY KEY(backend,camera,timestamp,plate,direction))''')
            for i, state in enumerate(('allowed', 'pending', 'uncertain', 'denied')):
                db.execute('INSERT INTO backend_events VALUES (?,?,?,?,?,?,?,?,?)',
                           (self.backend.base_url, 'camera', 1793865600+i, '皖MF58780', 'in', state, '{}', '{}', 'old'))
        for _ in range(2):
            migrated = GateBackend(self.backend.base_url, legacy, api_token='test-only', min_interval=0)
            for i in range(4):
                self.assertFalse(self.decide(timestamp=1793865600+i, backend=migrated))
        self.assertEqual(self.calls, [])
        with closing(sqlite3.connect(legacy)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM backend_events_v02').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT count(*) FROM backend_events').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT DISTINCT lot_id FROM backend_events').fetchall(), [('LOT-DEFAULT',)])

    def test_legacy_audit_gains_context_without_fabricating_history(self):
        path = Path(self.temp.name) / 'audit.db'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('''CREATE TABLE receiver_events (request_id TEXT PRIMARY KEY, received_at TEXT NOT NULL,
                camera TEXT,timestamp INTEGER,plate TEXT,direction TEXT,kind TEXT,state TEXT NOT NULL,
                detail TEXT,backend_state TEXT,elapsed_ms INTEGER,finished_at TEXT)''')
            db.execute("INSERT INTO receiver_events (request_id,received_at,state) VALUES ('old','old','allowed')")
        audit = ReceiverAudit(path)
        ReceiverAudit(path)
        self.assertIsNone(audit.recent()[0]['lot_id'])
        self.assertIsNone(audit.recent()[0]['backend_url'])
