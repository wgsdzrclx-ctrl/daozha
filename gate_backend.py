"""对接道闸计费服务 v0.2：发送 JSON，并保存每次请求的处理记录。"""
import json
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import requests

log = logging.getLogger('gate.receiver')


class GateBackend:
    def __init__(self, base_url, journal=None, connect_timeout=2, read_timeout=3):
        url = urlsplit(base_url)
        if (url.scheme not in ("http", "https") or not url.hostname or
                url.path not in ("", "/") or url.query or url.fragment or url.username):
            raise ValueError("后端地址请填 http(s)://主机:端口，去掉 /index.html")
        self.base_url = base_url.rstrip("/")
        self.timeout = (connect_timeout, read_timeout)
        self.journal = Path(journal or Path(__file__).with_name("backend_events.sqlite3"))
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS backend_events (
                backend TEXT, camera TEXT, timestamp INTEGER, plate TEXT, direction TEXT,
                state TEXT NOT NULL, request_json TEXT NOT NULL,
                response_json TEXT, detail TEXT,
                PRIMARY KEY (backend,camera,timestamp,plate,direction))""")

    def decide(self, camera, timestamp, plate, direction, deadline=None):
        # 相机的 start_time 是秒，后端 v0.2 明确要求毫秒。
        # 不用电脑收到请求的时间，以免网络延迟改变入场/离场时间。
        route, time_field = {
            "in": ("/gate/entry", "entryTime"),
            "out": ("/gate/exit", "exitTime"),
        }[direction]
        plate = plate.strip().upper()
        payload = {"carNo": plate, time_field: timestamp * 1000}
        key = (self.base_url, camera, timestamp, plate, direction)

        # HTTP 文档没有事件 ID，用相机+识别秒+车牌+方向判断同一次上报。
        # 先持久化 pending，再发送：线程竞争、进程重启均不会重复提交同一事件。
        # v0.2 出口重复调用会报错，因此超时后不盲目重试。
        # 若 pending/uncertain，需人工核对后端订单；它不能证明请求未执行。
        with closing(sqlite3.connect(self.journal)) as db, db:
            inserted = db.execute("""INSERT OR IGNORE INTO backend_events
                (backend,camera,timestamp,plate,direction,state,request_json)
                VALUES (?,?,?,?,?,'pending',?)""",
                (*key, json.dumps(payload, ensure_ascii=False))).rowcount
        if not inserted:
            log.info('重复上报已跳过：%s %s %s %s', camera, plate, timestamp, direction)
            return False

        raw = None
        try:
            if deadline is not None and time.monotonic() >= deadline:
                self._finish(key, "not_sent", None, "处理时限已到，未请求后端；需人工核对")
                return False
            # json= 会生成 application/json；禁用重定向，避免页面地址被误当接口。
            # 连接和读取分别限制为 2、3 秒，避免无期限占住相机应答。
            with requests.Session() as session:
                session.trust_env = False
                with session.post(self.base_url + route, json=payload,
                                  timeout=self.timeout, allow_redirects=False) as response:
                    if not 200 <= response.status_code < 300:
                        raise ValueError(f"HTTP {response.status_code}")
                    raw = response.json()
            if not isinstance(raw, dict):
                raise ValueError("返回值必须为 JSON 对象")
            if raw.get("code") != 200:
                self._finish(key, "rejected", raw, str(raw.get("msg", "业务失败")))
                log.warning('后端拒绝处理：%s %s', plate, raw.get('msg'))
                return False
            data = raw.get("data")
            if not isinstance(data, dict) or type(data.get("lift")) is not bool:
                raise ValueError("data.lift 必须为布尔值 true 或 false")
            if data.get("carNo") != plate:
                raise ValueError("后端返回的 carNo 与本次请求不一致")

            # duplicate=true 仅表示复用停车单，不能代替 lift 判定。
            # orderId 和各项计费时长原样保存，便于订单核对。
            # 指令下发前记录决定：若回复中途丢失，重发不会再次输出 IO1。
            lift = data["lift"]
            self._finish(key, "allowed" if lift else "denied", raw, None)
            log.info('后端判定：%s %s orderId=%s lift=%s', plate, direction, data.get('orderId'), lift)
            return lift
        except (requests.RequestException, ValueError) as error:
            self._finish(key, "uncertain", raw, str(error))
            log.warning('后端结果待核对，本次不开闸：%s %s', plate, error)
            return False

    def lookup(self, camera, timestamp, plate, direction):
        with closing(sqlite3.connect(self.journal, timeout=0.5)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("""SELECT state,request_json,response_json,detail FROM backend_events
                WHERE backend=? AND camera=? AND timestamp=? AND plate=? AND direction=?""",
                (self.base_url, camera, timestamp, plate.strip().upper(), direction)).fetchone()
        return dict(row) if row else None

    def _finish(self, key, state, response, detail):
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute("""UPDATE backend_events SET state=?,response_json=?,detail=?
                WHERE backend=? AND camera=? AND timestamp=? AND plate=? AND direction=?""",
                (state, json.dumps(response, ensure_ascii=False), detail, *key))
