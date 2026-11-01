from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
import sqlite3
import argparse
import base64
import hashlib
import os
from tempfile import NamedTemporaryFile
from getpass import getpass
from datetime import datetime, timezone, timedelta

ADDRESS = "192.168.55.101"
ROUTE = "/api/camera/events"
DATABASE = Path(__file__).with_name("vehicle_records.sqlite3")
PHOTO_DIR = DATABASE.parent / "camera_photos"


def save_photo(encoded):
    if not encoded:
        return None
    data = base64.b64decode(encoded.replace(".", "="), altchars=b"-_", validate=True)
    if data.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    else:
        raise ValueError("Unsupported photo format")
    PHOTO_DIR.mkdir(exist_ok=True)
    target = PHOTO_DIR / (hashlib.sha256(data).hexdigest() + suffix)
    if not target.exists():
        with NamedTemporaryFile(dir=PHOTO_DIR, delete=False) as temp:
            try:
                temp.write(data)
                temp.close()
                os.replace(temp.name, target)
            finally:
                temp.close()
                Path(temp.name).unlink(missing_ok=True)
    return str(target)


def save_sqlite(row):
    with sqlite3.connect(DATABASE) as db:
        db.execute("CREATE TABLE IF NOT EXISTS events "
                   "(camera TEXT, timestamp INTEGER, plate TEXT, direction TEXT, kind TEXT)")
        db.execute("BEGIN IMMEDIATE")
        columns = {item[1] for item in db.execute("PRAGMA table_info(events)")}
        for column in ("plate_photo", "scene_photo"):
            if column not in columns:
                db.execute(f"ALTER TABLE events ADD COLUMN {column} TEXT")
        db.execute("INSERT INTO events (camera,timestamp,plate,direction,kind,plate_photo,scene_photo) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)", row)


def mysql_writer(user):
    import mysql.connector
    options = dict(host="127.0.0.1", database="parking", user=user,
                   password=getpass("MySQL 密码："), charset="utf8mb4", connection_timeout=5)
    sql = ("INSERT INTO vehicle_events (camera_id, recognized_at, plate_num, direction, report_type) "
           "VALUES (%s, %s, %s, %s, %s)")
    with mysql.connector.connect(**options):
        pass

    def save(row):
        camera, timestamp, plate, direction, kind, plate_photo, scene_photo = row
        local_time = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))).replace(tzinfo=None)
        try:
            with mysql.connector.connect(**options) as db, db.cursor() as cursor:
                values = (camera, local_time, plate, direction, kind)
                if plate_photo or scene_photo:
                    cursor.execute("INSERT INTO vehicle_events "
                                   "(camera_id, recognized_at, plate_num, direction, report_type, plate_photo, scene_photo) "
                                   "VALUES (%s, %s, %s, %s, %s, %s, %s)", (*values, plate_photo, scene_photo))
                else:
                    cursor.execute(sql, values)
                db.commit()
        except mysql.connector.Error as error:
            raise OSError(f"MySQL 写入失败 ({error.errno})") from error
    return save


class Receiver(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        if self.path != ROUTE:
            return self.send_error(404)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16 * 1024 * 1024:
                return self.send_error(400, "Invalid body size")
            fields = parse_qs(self.rfile.read(size).decode("utf-8"), keep_blank_values=True)
            event = {key: values[0] for key, values in fields.items()}
            kind = event["type"]
            camera = event["cam_id"]
            if kind in ("online", "offline"):
                row = (camera, int(event["start_time"]), event["plate_num"],
                       event["vdc_type"], kind, save_photo(event.get("closeup_pic")),
                       save_photo(event.get("picture")))
                self.server.save_event(row)
                print(f"车牌 {row[2] or '（空）'} | 相机 {camera} | {row[3]} | {kind}", flush=True)
            else:
                print(f"{kind} | 相机 {camera}", flush=True)
        except (KeyError, ValueError):
            return self.send_error(400, "Invalid event")
        except (sqlite3.Error, OSError) as error:
            self.log_error("%s", error)
            return self.send_error(500, "Cannot store event")
        body = b'{"error_num":0,"error_str":"noerror"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json;charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mysql-user", help="使用该账户写入本机 MySQL parking 数据库")
    args = parser.parse_args()
    save_event = mysql_writer(args.mysql_user) if args.mysql_user else save_sqlite
    with ThreadingHTTPServer((ADDRESS, 8080), Receiver) as server:
        server.save_event = save_event
        print(f"等待相机推送：{ADDRESS}:8080{ROUTE}", flush=True)
        server.serve_forever()
