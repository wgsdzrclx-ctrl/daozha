"""相机 HTTP 兼容边界。只转发固定上报路径，管理接口不对局域网开放。"""
import http.client
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from camera_service import MAX_BODY
from camera_storage import ACK, ROUTE
from camera_voice import validate_camera_reply

log = logging.getLogger('gate.receiver')


def wire_response(status, body):
    # 设备文档 5.8：保留头名大小写，冒号后不加空格，明确字节长度，不用 chunked。
    return (f'HTTP/1.1 {status}\r\nContent-Type:application/json;charset=utf-8\r\n'
            f'Content-Length:{len(body)}\r\nConnection:close\r\n\r\n').encode('ascii') + body


class CameraHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, api_port, token, audit, budget=6.0):
        self.api_port, self.token, self.audit, self.budget = api_port, token, audit, budget
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, CameraHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(wire_response(503, b'{"error_num":3,"error_str":"busy"}'))
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class CameraHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format, *args):
        log.debug('camera client=%s %s', self.client_address[0], format % args)

    def send_error(self, code, message=None, explain=None):
        self.reply(code, b'{"error_num":1,"error_str":"invalid request"}')

    def reply(self, status, body, request_id=None):
        self.close_connection = True
        # 动态语音回复也必须登记开闸发送意图；不能再与固定 OPEN_GATE 字节串比较。
        opened = False
        if status == 200:
            try:
                opened = validate_camera_reply(body)
            except ValueError:
                log.exception('%s 非法相机回复；抑制开闸和语音', request_id)
                body = ACK
        if opened:
            try:
                self.server.audit.delivery(request_id, 'sending', True)
            except Exception:
                log.exception('%s 无法登记发送意图；抑制开闸', request_id)
                body, opened = ACK, False
        try:
            self.connection.sendall(wire_response(status, body))
            if request_id:
                self.server.audit.delivery(request_id, 'written', opened,
                                           '仅表示写入网络连接；没有机械开闸确认')
            log.info('%s HTTP=%s IO1=%s voice=%s response=written', request_id or '-', status,
                     opened, b'"rs485_data"' in body)
        except Exception as error:
            log.error('%s HTTP 响应失败：%s；需要核对，不能证明设备未收到', request_id, type(error).__name__)
            if request_id:
                try:
                    self.server.audit.delivery(request_id, 'failed_or_unknown', opened, type(error).__name__)
                except Exception:
                    log.exception('无法记录响应失败')

    def do_POST(self):
        if self.path != ROUTE:
            return self.send_error(404)
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            return self.send_error(400)
        try:
            size = int(self.headers['Content-Length'])
            if not 0 < size <= MAX_BODY:
                return self.send_error(413 if size > MAX_BODY else 400)
            if self.headers.get_content_type() != 'application/x-www-form-urlencoded':
                return self.send_error(415)
            body = self.rfile.read(size)
            if len(body) != size:
                return self.send_error(400)
        except (ValueError, OSError):
            return self.send_error(400)
        request_id = str(uuid.uuid4())
        started = time.monotonic()
        conn = http.client.HTTPConnection('127.0.0.1', self.server.api_port, timeout=self.server.budget + 1)
        try:
            conn.request('POST', ROUTE, body=body, headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-Gate-Token': self.server.token, 'X-Gate-Request-ID': request_id})
            response = conn.getresponse()
            content = response.read(4097)
            if len(content) > 4096:
                raise ValueError('内部响应过长')
            opened = validate_camera_reply(content) if response.status == 200 else False
            if opened and time.monotonic() - started >= self.server.budget + 0.5:
                log.error('%s 开闸结果到达过晚，抑制输出', request_id)
                content = ACK
            self.reply(response.status, content, request_id)
        except (OSError, http.client.HTTPException, ValueError):
            log.exception('%s 内部处理未能确认；不开闸，不自动重试', request_id)
            self.reply(200, ACK, request_id)
        finally:
            conn.close()
