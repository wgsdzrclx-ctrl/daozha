"""只绑定回环地址的 FastAPI：设备请求由 8080 协议适配层转入。"""
import asyncio
import logging
import secrets
import time
import uuid
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from camera_service import MAX_BODY, parse_event
from camera_storage import ACK, ROUTE

log = logging.getLogger('gate.receiver')


def camera_response(body=ACK, status=200):
    return Response(body, status_code=status, headers={'Content-Type': 'application/json;charset=utf-8'})


class Review(BaseModel):
    note: str = Field(min_length=1, max_length=1000)


def create_app(service, token, budget=6.0, lifespan=None):
    app = FastAPI(title='道闸接收服务', version='1.0', lifespan=lifespan)

    @app.post(ROUTE, summary='相机模式 5 上报（请通过相机端口 8080 调用）')
    async def receive(request: Request):
        if not secrets.compare_digest(request.headers.get('x-gate-token', ''), token):
            raise HTTPException(403, '相机上报请使用 8080 兼容端口')
        request_id = request.headers.get('x-gate-request-id', '')
        try:
            request_id = str(uuid.UUID(request_id))
        except ValueError:
            raise HTTPException(400, '无效的请求编号')
        started = time.monotonic()
        try:
            if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/x-www-form-urlencoded':
                raise ValueError('必须使用 application/x-www-form-urlencoded')
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise ValueError('请求超过 16 MiB')
            event = parse_event(body)
        except (ValueError, UnicodeError) as error:
            log.warning('%s 无效表单：%s', request_id, error)
            return camera_response(b'{"error_num":1,"error_str":"invalid event"}', 400)
        try:
            await run_in_threadpool(service.audit.begin, request_id, event)
            remaining = budget - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            result = await asyncio.wait_for(
                run_in_threadpool(service.process, request_id, event, started + budget, started), remaining)
            return camera_response(result)
        except TimeoutError:
            # 同步 I/O 线程可能仍在结束请求；不能取消已提交的停车订单，也不能重试。
            log.error('%s 超时；普通确认，无开闸指令。后台结果仍可能更新。', request_id)
            try:
                await run_in_threadpool(service.audit.finish, request_id, 'deadline',
                                        '处理超时；请核对后端订单，禁止盲目重试', int(budget * 1000))
            except Exception:
                log.exception('超时状态记录失败')
            return camera_response()
        except Exception:
            log.exception('%s 无法持久化处理记录', request_id)
            return camera_response(b'{"error_num":2,"error_str":"storage unavailable"}', 503)

    @app.get('/health', summary='当前配置（不探测或修改后端订单）')
    def health():
        return {'status': 'ok', 'mode': 'backend' if service.backend else 'collection_or_direct_test',
                'cameras': service.camera_directions, 'backend_url': service.backend.base_url if service.backend else None,
                'processing_budget_seconds': budget, 'physical_gate_confirmation': False,
                'voice': asdict(service.voice), 'voice_playback_confirmation': False}

    @app.get('/events', summary='最近上报及响应写出状态')
    def events(limit: int = Query(30, ge=1, le=100)):
        return service.audit.recent(limit)

    @app.get('/events/{request_id}', summary='单次上报及关联后端记录')
    def event_detail(request_id: str):
        rows = service.audit.recent(1, request_id)
        if not rows:
            raise HTTPException(404, '未找到记录')
        row = rows[0]
        if service.backend and row['timestamp'] and row['plate'] and row['direction'] in ('in', 'out'):
            row['backend'] = service.backend.lookup(row['camera'], row['timestamp'], row['plate'], row['direction'])
        return row

    @app.post('/events/{request_id}/review', summary='记录人工核对结果（不重试、不下发开闸）')
    def review(request_id: str, data: Review):
        if not data.note.strip():
            raise HTTPException(400, '核对说明不能为空')
        if not service.audit.review(request_id, data.note.strip()):
            raise HTTPException(404, '未找到记录')
        return {'status': 'reviewed', 'request_id': request_id, 'gate_command_sent': False}

    return app
