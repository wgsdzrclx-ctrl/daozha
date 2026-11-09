"""复用原有照片与数据库格式，保留旧开闸去重表。"""
from pathlib import Path
import sqlite3
import base64
import hashlib
import os
from contextlib import closing
from tempfile import NamedTemporaryFile
from getpass import getpass
from datetime import datetime, timezone, timedelta

ADDRESS = "192.168.55.101"
ROUTE = "/api/camera/events"
DATABASE = Path(__file__).with_name("vehicle_records.sqlite3")
PHOTO_DIR = DATABASE.parent / "camera_photos"
GATE_LOG = DATABASE.with_name("gate_replies.sqlite3")
ACK = b'{"error_num":0,"error_str":"noerror"}'
OPEN_GATE = b'{"error_num":0,"error_str":"noerror","gpio_data":[{"ionum":"io1","action":"on"}]}'


def first_gate_reply(camera, timestamp, plate):
    # Commit the claim before replying, so retries and concurrent requests
    # cannot issue another pulse for the same recognition.
    with closing(sqlite3.connect(GATE_LOG)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS gate_replies (
            camera TEXT, timestamp INTEGER, plate TEXT,
            PRIMARY KEY (camera, timestamp, plate))""")
        result = db.execute("INSERT OR IGNORE INTO gate_replies VALUES (?, ?, ?)",
                            (camera, timestamp, plate.strip().upper()))
        return result.rowcount == 1


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
    with closing(sqlite3.connect(DATABASE)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS events "
                   "(camera TEXT, timestamp INTEGER, plate TEXT, direction TEXT, kind TEXT)")
        db.execute("BEGIN IMMEDIATE")
        columns = {item[1] for item in db.execute("PRAGMA table_info(events)")}
        for column in ("plate_photo", "scene_photo"):
            if column not in columns:
                db.execute(f"ALTER TABLE events ADD COLUMN {column} TEXT")
        db.execute("INSERT INTO events (camera,timestamp,plate,direction,kind,plate_photo,scene_photo) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)", row)


def mysql_writer(user, *, host='127.0.0.1', port=3306, database='parking'):
    import mysql.connector
    options = dict(host=host, port=port, database=database, user=user,
                   password=getpass("MySQL 密码："), charset="utf8mb4", connection_timeout=2, read_timeout=3, write_timeout=3)
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


