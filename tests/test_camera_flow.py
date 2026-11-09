"""仅使用回环地址的模拟相机/后端和临时数据库，绝不访问现场计费接口。"""
import base64
import http.client
import json
import logging
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

import uvicorn

from camera_api import create_app
from camera_receiver import ENTRY_CAMERA, EXIT_CAMERA
from camera_service import MAX_BODY, CameraService, parse_event
from camera_storage import ACK, OPEN_GATE, ROUTE, save_photo, save_sqlite
from camera_transport import CameraHTTPServer, CameraHandler
from camera_voice import VoiceSettings, build_open_reply, validate_camera_reply
from gate_backend import GateBackend
from receiver_audit import ReceiverAudit

logging.getLogger('gate.receiver').addHandler(logging.NullHandler())
logging.getLogger('gate.receiver').propagate = False


class FakeBackend(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append((self.path, payload, self.headers['Content-Type']))
        time.sleep(self.server.delay)
        reply = self.server.reply(payload)
        body = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        self.send_response(self.server.status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeBackend)
        self.fake.daemon_threads = True
        self.fake.calls, self.fake.delay, self.fake.status = [], 0, 200
        self.fake.reply = lambda p: {'code': 200, 'data': {'carNo': p['carNo'], 'lift': True, 'orderId': 1}}
        self.fake_thread = threading.Thread(target=self.fake.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        self.fake_thread.start()
        self.backend = GateBackend(f'http://127.0.0.1:{self.fake.server_port}', self.root / 'backend.db')
        self.audit = ReceiverAudit(self.root / 'audit.db')
        self.saved = []
        self.service = CameraService(self.saved.append, self.audit, self.backend,
                                     {ENTRY_CAMERA: 'in', EXIT_CAMERA: 'out'})
        self.api_socket = socket.socket()
        self.api_socket.bind(('127.0.0.1', 0))
        self.api_port = self.api_socket.getsockname()[1]
        self.app = create_app(self.service, 'test-token', budget=0.35)
        self.api = uvicorn.Server(uvicorn.Config(self.app, log_level='critical', access_log=False))
        self.api_thread = threading.Thread(target=self.api.run, kwargs={'sockets': [self.api_socket]}, daemon=True)
        self.api_thread.start()
        deadline = time.monotonic() + 5
        while not self.api.started and self.api_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.api.started)
        self.camera = CameraHTTPServer(('127.0.0.1', 0), self.api_port, 'test-token', self.audit, budget=0.35)
        self.camera_thread = threading.Thread(target=self.camera.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        self.camera_thread.start()

    def tearDown(self):
        self.camera.shutdown()
        self.camera.server_close()
        self.camera_thread.join(2)
        self.api.should_exit = True
        self.api_thread.join(5)
        self.api_socket.close()
        self.fake.shutdown()
        self.fake.server_close()
        self.fake_thread.join(2)
        self.temp.cleanup()

    def post(self, **changes):
        event = {'type': 'online', 'mode': '5', 'proto_ver': '1.12', 'cam_id': ENTRY_CAMERA,
                 'plate_num': '皖MF58780', 'start_time': '1793865600', 'vdc_type': 'in', 'plate_val': 'true'}
        event.update(changes)
        body = urlencode({k: v for k, v in event.items() if v is not None}).encode()
        with closing(http.client.HTTPConnection('127.0.0.1', self.camera.server_port, timeout=4)) as conn:
            conn.request('POST', ROUTE, body, {'Content-Type': 'application/x-www-form-urlencoded'})
            response = conn.getresponse()
            return response.status, response.read()

    def get(self, path):
        with closing(http.client.HTTPConnection('127.0.0.1', self.api_port, timeout=3)) as conn:
            conn.request('GET', path)
            response = conn.getresponse()
            return response.status, json.loads(response.read())

    def test_entry_json_and_open_signal(self):
        self.assertEqual(self.post(), (200, OPEN_GATE))
        path, payload, content_type = self.fake.calls[0]
        self.assertEqual(path, '/gate/entry')
        self.assertEqual(payload, {'carNo': '皖MF58780', 'entryTime': 1793865600000})
        self.assertEqual(content_type, 'application/json')

    def test_exit_json_and_open_signal(self):
        self.assertEqual(self.post(cam_id=EXIT_CAMERA, vdc_type='out'), (200, OPEN_GATE))
        self.assertEqual(self.fake.calls[0][0], '/gate/exit')
        self.assertIn('exitTime', self.fake.calls[0][1])

    def test_voice_entry_exit_and_health(self):
        self.service.voice = VoiceSettings(True, 100, 101, 'welcome', 'goodbye')
        for direction, camera, suffix, address in [('in', ENTRY_CAMERA, b'\x01', 100),
                                                    ('out', EXIT_CAMERA, b'\x02', 101)]:
            status, body = self.post(cam_id=camera, vdc_type=direction, plate_num=' 粤b12345 ')
            self.assertEqual(status, 200)
            self.assertTrue(validate_camera_reply(body))
            reply = json.loads(body)
            self.assertEqual(reply['gpio_data'], [{'ionum': 'io1', 'action': 'on'}])
            frame = bytes.fromhex(reply['rs485_data'][0]['data'])
            self.assertEqual(frame[3], address)
            self.assertEqual(frame[8:-3], bytes.fromhex('D4C1423132333435') + suffix)
            record = self.audit.recent()[0]
            self.assertEqual(record['open_signal'], 1)
            self.assertIn('voice=prepared', record['detail'])
        health = self.get('/health')[1]
        self.assertTrue(health['voice']['enabled'])
        self.assertFalse(health['voice_playback_confirmation'])

    def test_voice_denied_offline_heartbeat_and_invalid_events_are_silent(self):
        self.service.voice = VoiceSettings(True)
        self.fake.reply = lambda p: {'code': 200, 'data': {'carNo': p['carNo'], 'lift': False}}
        for changes in [{}, {'type': 'heartbeat'}, {'type': 'offline'}, {'plate_val': 'false'},
                        {'cam_id': 'unknown'}, {'encrypt_verify': 'false'}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.post(**changes), (200, ACK))

    def test_voice_concurrent_and_persistent_duplicate_are_silent(self):
        self.service.voice = VoiceSettings(True)
        expected = build_open_reply('皖MF58780', 'in', self.service.voice)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.post(), range(4)))
        self.assertEqual(results.count((200, expected)), 1)
        self.assertEqual(results.count((200, ACK)), 3)
        self.service.backend = GateBackend(self.backend.base_url, self.backend.journal)
        self.assertEqual(self.post(), (200, ACK))
        self.assertEqual(len(self.fake.calls), 1)

    def test_voice_deadline_suppresses_both_commands(self):
        self.service.voice = VoiceSettings(True)
        self.test_deadline_does_not_deliver_late_permission()

    def test_voice_delivery_journal_failure_suppresses_both_commands(self):
        self.service.voice = VoiceSettings(True)
        self.test_cannot_open_when_delivery_journal_fails()

    def test_unsupported_voice_character_keeps_authorized_gate_signal(self):
        self.service.voice = VoiceSettings(True)
        self.assertEqual(self.post(plate_num='粤B12345🙂'), (200, OPEN_GATE))
        self.assertIn('voice=skipped', self.audit.recent()[0]['detail'])
        self.assertEqual(self.post(plate_num='粤B12345🙂'), (200, ACK))

    def test_denied(self):
        self.fake.reply = lambda p: {'code': 200, 'data': {'carNo': p['carNo'], 'lift': False}}
        self.assertEqual(self.post(), (200, ACK))

    def test_rejected(self):
        self.fake.reply = lambda p: {'code': 500, 'msg': '重复出库异常'}
        self.assertEqual(self.post(cam_id=EXIT_CAMERA, vdc_type='out'), (200, ACK))
        self.assertEqual(self.audit.recent()[0]['backend_state'], 'rejected')

    def test_duplicate_true_still_uses_lift(self):
        self.fake.reply = lambda p: {'code': 200, 'data': {'carNo': p['carNo'], 'lift': True, 'duplicate': True}}
        self.assertEqual(self.post(), (200, OPEN_GATE))

    def test_malformed_backend_and_plate_mismatch_fail_closed(self):
        for i, reply in enumerate([b'not-json', [], {'code': 200, 'data': {'carNo': 'other', 'lift': True}},
                                  {'code': 200, 'data': {'carNo': '皖MF58780', 'lift': 'true'}}]):
            self.fake.reply = lambda p, r=reply: r
            self.assertEqual(self.post(start_time=str(1793865600 + i)), (200, ACK))
            self.assertEqual(self.audit.recent()[0]['backend_state'], 'uncertain')

    def test_http_error_no_redirect_or_open(self):
        self.fake.status = 302
        self.assertEqual(self.post(), (200, ACK))
        self.assertEqual(len(self.fake.calls), 1)

    def test_duplicate_does_not_resubmit_or_pulse(self):
        self.assertEqual(self.post(), (200, OPEN_GATE))
        self.assertEqual(self.post(), (200, ACK))
        self.assertEqual(len(self.fake.calls), 1)

    def test_concurrent_duplicate(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.post(), range(4)))
        self.assertEqual(results.count((200, OPEN_GATE)), 1)
        self.assertEqual(len(self.fake.calls), 1)

    def test_persistent_duplicate_after_backend_recreation(self):
        self.assertEqual(self.post(), (200, OPEN_GATE))
        self.service.backend = GateBackend(self.backend.base_url, self.backend.journal)
        self.assertEqual(self.post(), (200, ACK))
        self.assertEqual(len(self.fake.calls), 1)

    def test_heartbeat_and_offline(self):
        self.assertEqual(self.post(type='heartbeat', plate_num=None, start_time=None, vdc_type=None), (200, ACK))
        self.assertEqual(self.post(type='offline'), (200, ACK))
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.fake.calls, [])

    def test_wrong_camera_or_direction(self):
        for changes in [{'cam_id': 'unknown'}, {'cam_id': EXIT_CAMERA}, {'vdc_type': 'out'}]:
            self.assertEqual(self.post(**changes), (200, ACK))
        self.assertEqual(self.fake.calls, [])

    def test_invalid_empty_missing_plate_and_encryption(self):
        for changes in [{'plate_num': ''}, {'plate_val': 'false'}, {'plate_val': None}, {'encrypt_verify': 'false'}]:
            self.assertEqual(self.post(**changes), (200, ACK))
        self.assertEqual(self.fake.calls, [])

    def test_encryption_handshake_not_misreported_as_supported(self):
        self.assertEqual(self.post(type='encrypt_verify'), (200, ACK))
        self.assertEqual(self.audit.recent()[0]['state'], 'unsupported_encryption')
        self.assertEqual(self.fake.calls, [])

    def test_bad_photo_does_not_discard_plate(self):
        self.assertEqual(self.post(picture='not-a-photo'), (200, OPEN_GATE))
        self.assertIn('picture 保存失败', self.audit.recent()[0]['detail'])
        self.assertEqual(self.saved[0][-1], None)

    def test_storage_failure_does_not_call_backend(self):
        self.service.save_event = lambda row: (_ for _ in ()).throw(OSError('storage failed'))
        self.assertEqual(self.post(), (200, ACK))
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.audit.recent()[0]['state'], 'processing_error')

    def test_deadline_does_not_deliver_late_permission(self):
        self.fake.delay = 0.65
        started = time.monotonic()
        self.assertEqual(self.post(), (200, ACK))
        self.assertLess(time.monotonic() - started, 0.6)
        time.sleep(0.45)
        record = self.audit.recent()[0]
        self.assertEqual(record['state'], 'deadline')
        self.assertEqual(record['backend_state'], 'allowed')
        self.assertEqual(record['open_signal'], 0)
        self.assertEqual(self.post(), (200, ACK))
        self.assertEqual(len(self.fake.calls), 1)

    def test_backend_read_timeout_is_uncertain(self):
        self.backend.timeout = (0.1, 0.05)
        self.fake.delay = 0.15
        self.assertEqual(self.post(), (200, ACK))
        self.assertEqual(self.audit.recent()[0]['backend_state'], 'uncertain')
        self.assertEqual(self.post(), (200, ACK))
        self.assertEqual(len(self.fake.calls), 1)
        time.sleep(0.2)

    def test_status_and_manual_review_never_open_gate(self):
        self.post()
        status, rows = self.get('/events')
        self.assertEqual(status, 200)
        request_id = rows[0]['request_id']
        with closing(http.client.HTTPConnection('127.0.0.1', self.api_port)) as conn:
            conn.request('POST', f'/events/{request_id}/review', json.dumps({'note': '现场已人工核对'}),
                         {'Content-Type': 'application/json'})
            response = conn.getresponse()
            self.assertFalse(json.loads(response.read())['gate_command_sent'])
        status, detail = self.get('/events/' + request_id)
        self.assertEqual(detail['backend']['state'], 'allowed')
        self.assertEqual(detail['reviews'][0]['note'], '现场已人工核对')
        self.assertEqual(len(self.fake.calls), 1)
        self.assertFalse(self.get('/health')[1]['physical_gate_confirmation'])

    def test_raw_wire_headers_and_actual_content_length(self):
        body = urlencode({'type': 'heartbeat', 'cam_id': ENTRY_CAMERA}).encode()
        request = (f'POST {ROUTE} HTTP/1.1\r\nHost:localhost\r\n'
                   f'Content-Type:application/x-www-form-urlencoded\r\nContent-Length:{len(body)}\r\n\r\n').encode() + body
        with socket.create_connection(('127.0.0.1', self.camera.server_port), timeout=3) as client:
            client.sendall(request)
            raw = bytearray()
            while chunk := client.recv(4096):
                raw.extend(chunk)
        headers, content = bytes(raw).split(b'\r\n\r\n', 1)
        self.assertEqual(content, ACK)
        self.assertTrue(headers.startswith(b'HTTP/1.1 200\r\n'))
        self.assertIn(b'Content-Type:application/json;charset=utf-8', headers)
        self.assertIn(f'Content-Length:{len(content)}'.encode(), headers)
        self.assertNotIn(b'Transfer-Encoding', headers)

    def test_invalid_input_is_json_and_does_not_call_backend(self):
        for change in [{'start_time': 'invalid'}, {'start_time': '-1'}, {'vdc_type': 'bad'}, {'mode': '2'}]:
            status, body = self.post(**change)
            self.assertEqual(status, 400)
            self.assertNotEqual(json.loads(body)['error_num'], 0)
        self.assertEqual(self.fake.calls, [])

    def test_management_not_forwarded_and_internal_route_requires_token(self):
        with closing(http.client.HTTPConnection('127.0.0.1', self.camera.server_port)) as conn:
            conn.request('GET', '/events')
            self.assertEqual(conn.getresponse().status, 501)
        with closing(http.client.HTTPConnection('127.0.0.1', self.api_port)) as conn:
            conn.request('POST', ROUTE, b'type=heartbeat')
            self.assertEqual(conn.getresponse().status, 403)

    def test_cannot_open_when_delivery_journal_fails(self):
        original = self.audit.delivery
        def fail_sending(request_id, state, *args):
            if state == 'sending':
                raise sqlite3.OperationalError('locked')
            return original(request_id, state, *args)
        self.audit.delivery = fail_sending
        self.assertEqual(self.post(), (200, ACK))
        self.assertEqual(self.audit.recent()[0]['backend_state'], 'allowed')

    def test_write_failure_is_recorded_and_does_not_enable_retry(self):
        self.assertEqual(self.post(), (200, OPEN_GATE))
        request_id = self.audit.recent()[0]['request_id']
        handler = CameraHandler.__new__(CameraHandler)
        handler.server = self.camera
        class BrokenConnection:
            def sendall(self, data):
                raise ConnectionResetError('simulated disconnect')
        handler.connection = BrokenConnection()
        handler.reply(200, OPEN_GATE, request_id)
        self.assertEqual(self.audit.recent()[0]['delivery_state'], 'failed_or_unknown')
        self.assertEqual(self.post(), (200, ACK))
        self.assertEqual(len(self.fake.calls), 1)

    def test_audit_failure_does_not_call_backend(self):
        with patch.object(self.audit, 'begin', side_effect=sqlite3.OperationalError('locked')):
            status, body = self.post()
        self.assertEqual(status, 503)
        self.assertNotIn('gpio_data', json.loads(body))
        self.assertEqual(self.fake.calls, [])

    def test_oversize_and_chunked_rejected_before_read(self):
        for header, expected in [(f'Content-Length:{MAX_BODY + 1}', 413),
                                 ('Transfer-Encoding:chunked', 400),
                                 ('Content-Length:0', 400)]:
            with socket.create_connection(('127.0.0.1', self.camera.server_port), timeout=2) as client:
                client.sendall((f'POST {ROUTE} HTTP/1.1\r\nHost:localhost\r\n'
                                f'Content-Type:application/x-www-form-urlencoded\r\n{header}\r\n\r\n').encode())
                self.assertTrue(client.recv(4096).startswith(f'HTTP/1.1 {expected}\r\n'.encode()))
        self.assertEqual(self.fake.calls, [])


class StorageTests(unittest.TestCase):
    def test_special_base64_photo_and_existing_sqlite_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / 'events.db'
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE events (camera TEXT,timestamp INTEGER,plate TEXT,direction TEXT,kind TEXT)')
                db.execute("INSERT INTO events VALUES ('old',1,'OLD','in','online')")
            data = b'\xff\xd8\xff\xfb\xff'
            encoded = base64.b64encode(data).decode().replace('+', '-').replace('/', '_').replace('=', '.')
            with patch('camera_storage.PHOTO_DIR', root / 'photos'), patch('camera_storage.DATABASE', database):
                photo = save_photo(encoded)
                self.assertEqual(Path(photo).read_bytes(), data)
                save_sqlite(('new', 1793865600, '皖MF58780', 'in', 'online', photo, None))
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 2)

    def test_reject_duplicate_fields_and_oversize(self):
        with self.assertRaises(ValueError):
            parse_event(b'type=online&type=offline&cam_id=x')
        with self.assertRaises(ValueError):
            parse_event(b'x' * (MAX_BODY + 1))


if __name__ == '__main__':
    unittest.main(verbosity=2)
