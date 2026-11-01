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
    """解码并保存相机推送的 Base64 图片。

    参数:
        encoded: 相机上传的 Base64 编码字符串，"." 会被还原为 "="；
                 为空（None 或空串）时表示本次没有图片。
    返回:
        图片保存后的完整路径字符串；没有图片时返回 None。
    异常:
        ValueError: 图片数据既不是 JPEG 也不是 PNG 时抛出。
    说明:
        文件名取内容 SHA-256 值，因此同一张图片重复推送不会重复写入磁盘。
    """
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
    """把一条过车事件写入本地 SQLite 数据库（默认写入方式）。

    参数:
        row: 事件元组，字段顺序为
             (camera, timestamp, plate, direction, kind, plate_photo, scene_photo)，
             其中 timestamp 为秒级整数时间戳，后两列为图片路径。
    说明:
        首次调用会自动建表；若旧表缺少 plate_photo / scene_photo 列则自动补列，
        便于兼容历史数据库。使用 BEGIN IMMEDIATE 立即加写锁，避免多线程重复写入冲突。
    """
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
    """创建一个把过车事件写入本机 MySQL parking 库的保存函数。

    参数:
        user: MySQL 用户名，密码在调用时通过 getpass 交互输入，不写在代码里。
    返回:
        save(row) 函数，参数与 save_sqlite 的 row 元组一致。
    说明:
        创建时先连接一次，用于提前校验账号密码是否可用、数据库是否可访问；
        真正的写入发生在返回的 save 函数中，时间戳会转换为东八区本地时间。
    """
    import mysql.connector
    options = dict(host="127.0.0.1", database="parking", user=user,
                   password=getpass("MySQL 密码："), charset="utf8mb4", connection_timeout=5)
    sql = ("INSERT INTO vehicle_events (camera_id, recognized_at, plate_num, direction, report_type) "
           "VALUES (%s, %s, %s, %s, %s)")
    with mysql.connector.connect(**options):
        pass

    def save(row):
        """把单条事件写入 MySQL 的 vehicle_events 表。

        参数:
            row: 与 save_sqlite 相同的 7 元组事件数据。
        异常:
            OSError: MySQL 写入出错时抛出，并携带原始错误码，供上层返回 500。
        """
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
    """接收相机 HTTP 推送请求的处理器。

    相机把过车事件以表单形式 POST 到 ROUTE；本类解析请求后调用
    server.save_event（由 main 注入的保存函数）落库，并按相机要求回复 JSON。
    使用 HTTP/1.1 以支持长连接，减少相机反复建连的开销。
    """

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        """处理一次 POST 推送（HTTP 入口）。

        处理流程：
            1. 路径不是 ROUTE 时返回 404；
            2. 校验 Content-Length，超过 16 MB 或非正数返回 400；
            3. 解析表单字段为字典，取出 type / cam_id 等关键字段；
            4. type 为 online / offline 时保存两张图片并写库，
               其他类型（如心跳）只打印日志不落库；
            5. 字段缺失或数据非法返回 400，存储失败返回 500；
            6. 正常时回复 {"error_num":0,"error_str":"noerror"}，表示接收成功。
        """
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
    # 命令行入口：默认写入本地 SQLite；传入 --mysql-user 时改写本机 MySQL
    parser = argparse.ArgumentParser()
    parser.add_argument("--mysql-user", help="使用该账户写入本机 MySQL parking 数据库")
    args = parser.parse_args()
    save_event = mysql_writer(args.mysql_user) if args.mysql_user else save_sqlite
    # 启动多线程 HTTP 服务，把保存函数挂到 server 上供 Receiver 调用
    with ThreadingHTTPServer((ADDRESS, 8080), Receiver) as server:
        server.save_event = save_event
        print(f"等待相机推送：{ADDRESS}:8080{ROUTE}", flush=True)
        server.serve_forever()
