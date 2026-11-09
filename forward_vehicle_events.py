"""Forward locally committed MySQL events to a multipart HTTP endpoint."""
import argparse
import hashlib
import mimetypes
import time
from contextlib import ExitStack
from datetime import timedelta, timezone
from getpass import getpass
from pathlib import Path
from urllib.parse import urlsplit

import mysql.connector
import requests


def submit(session, url, source, event):
    event_id = f"{source}:{event['id']}"
    fields = {name: (None, event[name]) for name in
              ("camera_id", "plate_num", "direction", "report_type")}
    recognized = event["recognized_at"].replace(tzinfo=timezone(timedelta(hours=8)))
    fields["recognized_at"] = (None, recognized.isoformat())
    fields["event_id"] = (None, event_id)
    with ExitStack() as stack:
        for name in ("plate_photo", "scene_photo"):
            if event[name]:
                path = Path(event[name])
                photo = stack.enter_context(path.open("rb"))
                fields[name] = (path.name, photo, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        response = session.post(url, files=fields, headers={"Idempotency-Key": event_id},
                                timeout=(5, 30), allow_redirects=False)
        if response.status_code not in (200, 201, 204):
            raise RuntimeError(f"后端返回 HTTP {response.status_code}")


def main():
    parser = argparse.ArgumentParser(description="上传车牌记录及照片")
    parser.add_argument("--url", required=True)
    parser.add_argument("--source", required=True, help="本地数据库固定唯一编号，例如 gate-laptop-01")
    parser.add_argument("--from-id", type=int, required=True, help="从哪个本地记录编号开始（含）")
    args = parser.parse_args()
    target = urlsplit(args.url)
    if not (target.scheme == "https" or
            (target.scheme == "http" and target.hostname in ("localhost", "127.0.0.1"))):
        parser.error("远程后端使用 HTTPS，本机测试可使用 HTTP")
    if not args.source.isascii() or not all(c.isalnum() or c in "-_" for c in args.source) or not args.source:
        parser.error("来源编号仅使用英文字母、数字、连字符或下划线")
    destination = hashlib.sha256((args.source + "|" + args.url).encode()).hexdigest()
    options = dict(host="127.0.0.1", database="parking", user="parking_uploader",
                   password=getpass("parking_uploader 的 MySQL 密码："), charset="utf8mb4",
                   connection_timeout=5, autocommit=True)
    with requests.Session() as session:
        token = getpass("后端 Bearer Token（无需认证时回车）：")
        if token:
            session.headers["Authorization"] = f"Bearer {token}"
        while True:
            try:
                with mysql.connector.connect(**options) as db, db.cursor(dictionary=True) as cur:
                    cur.execute("""
                        SELECT e.id,e.camera_id,e.plate_num,e.recognized_at,e.direction,
                               e.report_type,e.plate_photo,e.scene_photo
                        FROM vehicle_events e
                        LEFT JOIN upload_receipts r ON r.event_id=e.id AND r.destination=%s
                        WHERE e.id >= %s AND r.event_id IS NULL ORDER BY e.id LIMIT 20
                    """, (destination, args.from_id))
                    events = cur.fetchall()
                    for event in events:
                        print(f"正在提交记录 {event['id']}", flush=True)
                        submit(session, args.url, args.source, event)
                        cur.execute("INSERT INTO upload_receipts(destination,event_id) VALUES(%s,%s)",
                                    (destination, event["id"]))
                        print(f"后端已接收记录 {event['id']}", flush=True)
                if not events:
                    time.sleep(3)
            except (mysql.connector.Error, requests.RequestException, OSError, RuntimeError) as error:
                print(f"上传暂停，15 秒后重试：{error}", flush=True)
                time.sleep(15)


if __name__ == "__main__":
    main()
