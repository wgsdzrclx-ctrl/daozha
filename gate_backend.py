"""计费服务 v0.7：凭证、停车场、持久化去重及有截止时间的接口限流。"""
import json
import logging
import math
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import requests

log = logging.getLogger('gate.receiver')


def validate_lot_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', value):
        raise ValueError('停车场编号仅允许 1～32 位字母、数字、下划线或中划线')
    return value


def validate_token(value):
    # 不在异常信息中包含凭证；同时拒绝换行等非法 HTTP 头字符。
    if not isinstance(value, str) or not value or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError('后端访问凭证为空或包含非法字符，请检查凭证文件/环境变量')
    return value


class GateBackend:
    # 同一接收进程内，同一后端同一路径共享锁，不按相机或 lotId 分锁。
    # 多台接收电脑共享出口 IP 时，仍需由部署方统筹后端限流。
    _locks_guard = threading.Lock()
    _route_locks = {}

    def __init__(self, base_url, journal=None, connect_timeout=2, read_timeout=3, *,
                 api_token='', lot_id='LOT-DEFAULT', min_interval=3,
                 legacy_lot_id='LOT-DEFAULT', verify_tls=True):
        url = urlsplit(base_url)
        if (url.scheme not in ('http', 'https') or not url.hostname or
                url.path not in ('', '/') or url.query or url.fragment or
                url.username is not None or url.password is not None):
            raise ValueError('后端地址请填 http(s)://主机:端口，不附加接口路径或凭证')
        self.base_url = base_url.rstrip('/')
        self.api_token = validate_token(api_token)
        self.lot_id = validate_lot_id(lot_id)
        self.legacy_lot_id = validate_lot_id(legacy_lot_id)
        if not math.isfinite(min_interval) or not 0 <= min_interval <= 30:
            raise ValueError('后端最小调用间隔必须在 0～30 秒之间')
        if type(verify_tls) is not bool:
            raise ValueError('verify_tls 必须为 true 或 false')
        self.min_interval = min_interval
        self.verify_tls = verify_tls
        self.timeout = (connect_timeout, read_timeout)
        self.journal = Path(journal or Path(__file__).with_name('backend_events.sqlite3'))
        self._initialize_journal()

    @staticmethod
    def _create_events(db):
        db.execute('''CREATE TABLE backend_events (
            backend TEXT, lot_id TEXT NOT NULL, camera TEXT, timestamp INTEGER,
            plate TEXT, direction TEXT, state TEXT NOT NULL, request_json TEXT NOT NULL,
            response_json TEXT, detail TEXT, http_status INTEGER, retry_after INTEGER,
            PRIMARY KEY (backend,lot_id,camera,timestamp,plate,direction))''')

    def _initialize_journal(self):
        with closing(sqlite3.connect(self.journal)) as db, db:
            # 同一事务内迁移，失败即回滚。旧表保留，不清空去重历史。
            db.execute('BEGIN IMMEDIATE')
            columns = {r[1] for r in db.execute('PRAGMA table_info(backend_events)')}
            if not columns:
                self._create_events(db)
            elif 'lot_id' not in columns:
                db.execute('ALTER TABLE backend_events RENAME TO backend_events_v02')
                self._create_events(db)
                db.execute('''INSERT INTO backend_events
                    (backend,lot_id,camera,timestamp,plate,direction,state,request_json,response_json,detail)
                    SELECT backend,?,camera,timestamp,plate,direction,state,request_json,response_json,detail
                    FROM backend_events_v02''', (self.legacy_lot_id,))
                log.info('旧后端记录已迁移到停车场 %s；原表保留为 backend_events_v02', self.legacy_lot_id)
            db.execute('''CREATE TABLE IF NOT EXISTS backend_rate_limits (
                backend TEXT, route TEXT, next_allowed REAL NOT NULL,
                PRIMARY KEY (backend,route))''')

    def _key(self, camera, timestamp, plate, direction, *, backend_url=None, lot_id=None):
        return (backend_url or self.base_url, lot_id or self.lot_id,
                camera, timestamp, plate.strip().upper(), direction)

    def _redact(self, value):
        if isinstance(value, str):
            return value.replace(self.api_token, '[REDACTED]')
        if isinstance(value, dict):
            return {self._redact(k): self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        return value

    def _next_allowed(self, route):
        with closing(sqlite3.connect(self.journal, timeout=0.5)) as db:
            row = db.execute('SELECT next_allowed FROM backend_rate_limits WHERE backend=? AND route=?',
                             (self.base_url, route)).fetchone()
        return row[0] if row else 0

    def _cooldown(self, route, seconds):
        with closing(sqlite3.connect(self.journal, timeout=0.5)) as db, db:
            db.execute('''INSERT INTO backend_rate_limits VALUES (?,?,?)
                ON CONFLICT(backend,route) DO UPDATE SET
                next_allowed=max(next_allowed,excluded.next_allowed)''',
                (self.base_url, route, time.time() + seconds))

    def _acquire_route(self, route, deadline):
        with self._locks_guard:
            lock = self._route_locks.setdefault((self.base_url, route), threading.Lock())
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not lock.acquire(timeout=remaining):
            return None
        try:
            delay = max(0, self._next_allowed(route) - time.time())
            if delay >= deadline - time.monotonic():
                lock.release()
                return None
            if delay:
                # 仅在当前请求预算内等待；绝不在请求过期后由队列补发。
                time.sleep(delay)
            if time.monotonic() >= deadline:
                lock.release()
                return None
            return lock
        except BaseException:
            lock.release()
            raise

    def decide(self, camera, timestamp, plate, direction, deadline=None):
        route, time_field = {'in': ('/gate/entry', 'entryTime'), 'out': ('/gate/exit', 'exitTime')}[direction]
        plate = plate.strip().upper()
        # 保留相机识别时间，秒转毫秒，不替换成排队结束时的时间。
        payload = {'carNo': plate, 'lotId': self.lot_id, time_field: timestamp * 1000}
        key = self._key(camera, timestamp, plate, direction)
        deadline = deadline if deadline is not None else time.monotonic() + 6
        with closing(sqlite3.connect(self.journal)) as db, db:
            inserted = db.execute('''INSERT OR IGNORE INTO backend_events
                (backend,lot_id,camera,timestamp,plate,direction,state,request_json)
                VALUES (?,?,?,?,?,?,'pending',?)''',
                (*key, json.dumps(payload, ensure_ascii=False))).rowcount
        if not inserted:
            log.info('重复上报已跳过：lot=%s %s %s %s %s', self.lot_id, camera, plate, timestamp, direction)
            return False

        raw, http_status, retry_after, lock = None, None, None, None
        try:
            lock = self._acquire_route(route, deadline)
            if lock is None:
                self._finish(key, 'not_sent', None, '截止时间内无法取得接口发送时隙；未请求后端，需人工核对')
                return False
            if time.monotonic() >= deadline:
                self._finish(key, 'not_sent', None, '请求预算已到；未请求后端')
                return False
            # 先持久化冷却时刻。收到响应后再延长到响应后 3 秒，比仅按发送起点
            # 间隔更保守，避免网络抖动缩短服务端收到两次请求的间隔。
            self._cooldown(route, self.min_interval)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._finish(key, 'not_sent', None, '登记发送时隙后预算已到；未请求后端')
                return False
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    with session.post(self.base_url + route, json=payload,
                                      headers={'X-Gate-Token': self.api_token},
                                      timeout=tuple(min(t, remaining) for t in self.timeout),
                                      verify=self.verify_tls, allow_redirects=False) as response:
                        http_status = response.status_code
                        try:
                            raw = self._redact(response.json())
                        except ValueError:
                            raw = None
                        if http_status == 429:
                            header = response.headers.get('Retry-After', '').strip()
                            if re.fullmatch(r'[0-9]{1,9}', header):
                                retry_after = int(header)
            finally:
                self._cooldown(route, max(self.min_interval, retry_after or 0))

            message = str(raw.get('msg', '')) if isinstance(raw, dict) else ''
            if http_status == 401:
                self._finish(key, 'auth_failed', raw, 'HTTP 401：后端访问凭证缺失或错误；' + message,
                             http_status=http_status)
                log.warning('后端认证失败：lot=%s %s；请检查凭证配置', self.lot_id, plate)
                return False
            if http_status == 429:
                self._finish(key, 'rate_limited', raw,
                             f'HTTP 429：后端限流；Retry-After={retry_after}；本事件不自动重试；' + message,
                             http_status=http_status, retry_after=retry_after)
                log.warning('后端限流：lot=%s %s Retry-After=%s；本次不开闸', self.lot_id, plate, retry_after)
                return False
            if not 200 <= http_status < 300:
                raise ValueError(f'HTTP {http_status}')
            if not isinstance(raw, dict):
                raise ValueError('后端返回值必须为 JSON 对象')
            if raw.get('code') != 200:
                self._finish(key, 'rejected', raw, message or '业务失败', http_status=http_status)
                log.warning('后端拒绝处理：lot=%s %s %s', self.lot_id, plate, message)
                return False
            data = raw.get('data')
            if not isinstance(data, dict) or type(data.get('lift')) is not bool:
                raise ValueError('data.lift 必须为布尔值 true 或 false')
            if data.get('carNo') != plate or data.get('lotId') != self.lot_id:
                raise ValueError('后端返回的 carNo 或 lotId 与本次请求不一致')
            # needPay=true、duplicate=true 都不能代替 lift。demo 可需付费但仍放行。
            lift = data['lift']
            self._finish(key, 'allowed' if lift else 'denied', raw, None, http_status=http_status)
            log.info('后端判定：lot=%s %s %s orderId=%s lift=%s',
                     self.lot_id, plate, direction, data.get('orderId'), lift)
            return lift
        except (requests.RequestException, ValueError) as error:
            detail = self._redact(str(error))
            self._finish(key, 'uncertain', raw, detail, http_status=http_status, retry_after=retry_after)
            log.warning('后端结果待核对，本次不开闸：lot=%s %s %s', self.lot_id, plate, detail)
            return False
        finally:
            if lock is not None:
                lock.release()

    def lookup(self, camera, timestamp, plate, direction, *, backend_url=None, lot_id=None):
        with closing(sqlite3.connect(self.journal, timeout=0.5)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('''SELECT state,request_json,response_json,detail,lot_id,http_status,retry_after
                FROM backend_events WHERE backend=? AND lot_id=? AND camera=? AND timestamp=? AND plate=? AND direction=?''',
                self._key(camera, timestamp, plate, direction, backend_url=backend_url, lot_id=lot_id)).fetchone()
        return dict(row) if row else None

    def _finish(self, key, state, response, detail, *, http_status=None, retry_after=None):
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute('''UPDATE backend_events SET state=?,response_json=?,detail=?,http_status=?,retry_after=?
                WHERE backend=? AND lot_id=? AND camera=? AND timestamp=? AND plate=? AND direction=?''',
                (state, json.dumps(self._redact(response), ensure_ascii=False), self._redact(detail),
                 http_status, retry_after, *key))
