"""启动 FastAPI 业务服务和相机模式 5 HTTP 兼容入口。"""
import logging
import secrets
import socket
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from camera_api import create_app
from camera_service import CameraService
from camera_storage import ROUTE, mysql_writer, save_sqlite
from camera_transport import CameraHTTPServer
from gate_backend import GateBackend
from receiver_audit import ReceiverAudit
from receiver_config import ENTRY_CAMERA, EXIT_CAMERA, describe_settings, parse_settings

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    args = parse_settings(argv)
    if args.check_config:
        print('\n'.join(describe_settings(args)))
        print('配置格式检查通过；未连接数据库、后端或相机。')
        return
    directions = {}
    if args.backend_url:
        directions = {args.entry_camera: 'in', args.exit_camera: 'out'}
    logger = logging.getLogger('gate.receiver')
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    for handler in (logging.StreamHandler(), RotatingFileHandler(
            ROOT / 'receiver.log', maxBytes=5 * 1024 * 1024, backupCount=4, encoding='utf-8')):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    for line in describe_settings(args):
        logger.info('%s', line)
    backend = GateBackend(args.backend_url) if args.backend_url else None
    save_event = (mysql_writer(args.mysql_user, host=args.mysql_host, port=args.mysql_port,
                              database=args.mysql_database) if args.mysql_user else save_sqlite)
    audit = ReceiverAudit(ROOT / 'receiver_audit.sqlite3')
    service = CameraService(save_event, audit, backend, directions, args.open_camera, voice=args.voice)
    token = secrets.token_hex(32)
    app = create_app(service, token, args.processing_budget)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as api_socket:
        api_socket.bind(('127.0.0.1', args.api_port))
        with CameraHTTPServer((args.host, args.port), args.api_port, token, audit, args.processing_budget) as camera:
            api = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.api_port,
                                              access_log=False, log_level='warning'))
            api_thread = threading.Thread(target=api.run, kwargs={'sockets': [api_socket]}, daemon=True)
            api_thread.start()
            startup_deadline = time.monotonic() + 10
            while not api.started:
                if not api_thread.is_alive() or time.monotonic() >= startup_deadline:
                    api.should_exit = True
                    raise RuntimeError('FastAPI 启动失败，请检查端口和日志')
                time.sleep(0.05)
            logger.info('相机入口：http://%s:%s%s（监听 %s）',
                        args.advertised_host or '<接收电脑实际IP>', args.port, ROUTE, args.host)
            logger.info('本机状态：http://127.0.0.1:%s/health；记录：/events；文档：/docs', args.api_port)
            logger.info('运行模式=%s 相机=%s 存储=%s', '后端判定' if backend else '采集/直接测试',
                        directions, 'MySQL' if args.mysql_user else 'SQLite')
            try:
                camera.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                logger.info('正在停止接收服务')
            finally:
                api.should_exit = True
                api_thread.join(timeout=10)


if __name__ == '__main__':
    main()
