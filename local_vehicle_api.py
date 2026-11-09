"""本地 Postman 练习：接收车牌表单和可选照片。"""
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.json.ensure_ascii = False
DATABASE = Path(__file__).with_name("postman_vehicle_test.sqlite3")


@app.post("/api/vehicle-events")
def receive_event():
    required = ("event_id", "camera_id", "plate_num", "recognized_at", "direction", "report_type")
    missing = [key for key in required if key not in request.form or
               (key != "plate_num" and not request.form[key].strip())]
    if missing:
        return jsonify(error="缺少表单参数", fields=missing), 400
    event = {key: request.form[key] for key in required}
    try:
        datetime.fromisoformat(event["recognized_at"])
    except ValueError:
        return jsonify(error="recognized_at 请填写 ISO 格式时间"), 400
    if event["direction"] not in ("in", "out") or event["report_type"] not in ("online", "offline"):
        return jsonify(error="请检查 direction 和 report_type"), 400

    photos = [request.files[key].read() if key in request.files else None
              for key in ("plate_photo", "scene_photo")]
    with sqlite3.connect(DATABASE) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY, camera_id TEXT, plate_num TEXT, recognized_at TEXT,
            direction TEXT, report_type TEXT, plate_photo BLOB, scene_photo BLOB)""")
        result = db.execute("""INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(event_id) DO NOTHING""",
                            (*event.values(), *photos))
        created = result.rowcount == 1
        plate, = db.execute("SELECT plate_num FROM events WHERE event_id=?",
                            (event["event_id"],)).fetchone()
    return jsonify(event_id=event["event_id"], plate_num=plate,
                   status="created" if created else "already_received"), 201 if created else 200


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=18080)
