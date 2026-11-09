"""接收、后端决定、HTTP 写出和人工核对分别留痕；不宣称机械开闸成功。"""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


class ReceiverAudit:
    def __init__(self, path):
        self.path = path
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS receiver_events (
                request_id TEXT PRIMARY KEY, received_at TEXT NOT NULL,
                camera TEXT, timestamp INTEGER, plate TEXT, direction TEXT, kind TEXT,
                state TEXT NOT NULL, detail TEXT, backend_state TEXT,
                elapsed_ms INTEGER, finished_at TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS receiver_deliveries (
                request_id TEXT PRIMARY KEY, state TEXT, open_signal INTEGER,
                detail TEXT, updated_at TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS receiver_reviews (
                id INTEGER PRIMARY KEY, request_id TEXT, note TEXT, reviewed_at TEXT)""")

    def connect(self):
        return sqlite3.connect(self.path, timeout=0.5)

    def begin(self, request_id, event):
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO receiver_events
                (request_id,received_at,camera,timestamp,plate,direction,kind,state)
                VALUES (?,?,?,?,?,?,?,'processing')""",
                (request_id, now(), event.get('cam_id'), event.get('start_time'),
                 event.get('plate_num'), event.get('vdc_type'), event.get('type')))

    def finish(self, request_id, state, detail, elapsed_ms):
        with closing(self.connect()) as db, db:
            db.execute("""UPDATE receiver_events SET state=?,detail=?,elapsed_ms=?,finished_at=?
                WHERE request_id=? AND state='processing'""",
                (state, detail, elapsed_ms, now(), request_id))

    def backend_result(self, request_id, state):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE receiver_events SET backend_state=? WHERE request_id=?", (state, request_id))

    def delivery(self, request_id, state, open_signal=False, detail=None):
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO receiver_deliveries VALUES (?,?,?,?,?)
                ON CONFLICT(request_id) DO UPDATE SET state=excluded.state,
                open_signal=excluded.open_signal,detail=excluded.detail,updated_at=excluded.updated_at""",
                (request_id, state, int(open_signal), detail, now()))

    def recent(self, limit=30, request_id=None):
        with closing(self.connect()) as db:
            db.row_factory = sqlite3.Row
            sql = """SELECT e.*,d.state AS delivery_state,d.open_signal,d.detail AS delivery_detail
                FROM receiver_events e LEFT JOIN receiver_deliveries d USING(request_id)"""
            args = []
            if request_id:
                sql += " WHERE e.request_id=?"
                args.append(request_id)
            rows = db.execute(sql + " ORDER BY e.rowid DESC LIMIT ?", (*args, limit)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item['reviews'] = [dict(r) for r in db.execute(
                    "SELECT note,reviewed_at FROM receiver_reviews WHERE request_id=? ORDER BY id",
                    (item['request_id'],))]
                result.append(item)
            return result

    def review(self, request_id, note):
        with closing(self.connect()) as db, db:
            if not db.execute("SELECT 1 FROM receiver_events WHERE request_id=?", (request_id,)).fetchone():
                return False
            db.execute("INSERT INTO receiver_reviews(request_id,note,reviewed_at) VALUES (?,?,?)",
                       (request_id, note, now()))
        return True
