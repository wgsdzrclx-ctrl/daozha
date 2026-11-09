"""模式 5 业务处理：持久化后同步判定，任何晚到的结果都不能补发开闸。"""
import logging
import time
from urllib.parse import parse_qs

from camera_storage import ACK, OPEN_GATE, first_gate_reply, save_photo
from camera_voice import VoiceSettings, build_open_reply

MAX_BODY = 16 * 1024 * 1024
log = logging.getLogger('gate.receiver')


def parse_event(body):
    if not 0 < len(body) <= MAX_BODY:
        raise ValueError('请求体必须为 1 字节到 16 MiB')
    fields = parse_qs(body.decode('utf-8'), keep_blank_values=True,
                      encoding='utf-8', errors='strict', max_num_fields=100)
    if any(len(values) != 1 for values in fields.values()):
        raise ValueError('不接受重复的表单字段')
    event = {key: values[0] for key, values in fields.items()}
    for field in ('type', 'cam_id'):
        if not event.get(field, '').strip():
            raise ValueError(f'缺少 {field}')
    if len(event['cam_id']) > 128 or len(event['type']) > 64:
        raise ValueError('相机 ID 或事件类型过长')
    if event.get('mode', '5') != '5':
        raise ValueError('仅支持模式 5')
    if event['type'] in ('online', 'offline'):
        for field in ('start_time', 'plate_num', 'vdc_type'):
            if field not in event:
                raise ValueError(f'缺少 {field}')
        timestamp = int(event['start_time'])
        if not 0 < timestamp <= 253402271999:
            raise ValueError('start_time 必须是有效的秒时间戳')
        if event['vdc_type'] not in ('in', 'out'):
            raise ValueError('vdc_type 必须为 in 或 out')
        if len(event['plate_num']) > 64:
            raise ValueError('车牌过长')
        event['start_time'] = timestamp
    else:
        # 非识别事件的时间字段不是停车订单键。
        event.pop('start_time', None)
    return event


class CameraService:
    def __init__(self, save_event, audit, backend=None, camera_directions=None, open_camera=None,
                 photo_saver=save_photo, reply_claimer=first_gate_reply, voice=None):
        self.save_event = save_event
        self.audit = audit
        self.backend = backend
        self.camera_directions = camera_directions or {}
        self.open_camera = open_camera
        self.photo_saver = photo_saver
        self.reply_claimer = reply_claimer
        self.voice = voice if voice is not None else VoiceSettings()

    def process(self, request_id, event, deadline, started):
        def done(state, detail='', opened=False):
            reply = ACK
            if opened:
                try:
                    reply = build_open_reply(event['plate_num'], event['vdc_type'], self.voice)
                    if self.voice.enabled:
                        detail += '; voice=prepared（已组包，不代表喇叭已播报）'
                except ValueError as error:
                    # 语音目录不支持的特殊车牌只跳过语音，不改变后端已经作出的放行决定。
                    reply = OPEN_GATE
                    detail += f'; voice=skipped：{error}'
            if time.monotonic() >= deadline:
                state, detail, opened = 'deadline', '超过处理时限；晚到结果仅留档，不下发开闸', False
                reply = ACK  # 语音与开闸一起撤下，不能在超时后单独补发播报。
            elapsed = int((time.monotonic() - started) * 1000)
            self.audit.finish(request_id, state, detail, elapsed)
            log.info('%s camera=%s plate=%s type=%s state=%s elapsed=%sms %s',
                     request_id, event['cam_id'], event.get('plate_num', ''), event['type'], state, elapsed, detail)
            return reply

        try:
            kind, camera = event['type'], event['cam_id']
            if kind not in ('online', 'offline'):
                if kind == 'encrypt_verify':
                    return done('unsupported_encryption', '需要厂商签名算法；本程序未完成加密握手')
                return done('acknowledged', '非识别事件，不调用计费后端')

            photos, warnings = [], []
            for field in ('closeup_pic', 'picture'):
                try:
                    photos.append(self.photo_saver(event.get(field)))
                except (ValueError, OSError) as error:
                    # 图片不是 entry/exit 的必填数据；保留警告，不丢弃有效车牌。
                    photos.append(None)
                    warnings.append(f'{field} 保存失败：{type(error).__name__}')
            row = (camera, event['start_time'], event['plate_num'], event['vdc_type'], kind, *photos)
            if time.monotonic() >= deadline:
                return done('deadline')
            self.save_event(row)
            warning = '; '.join(warnings)
            if kind == 'offline':
                return done('offline_saved', warning)
            if not event['plate_num'].strip() or event.get('plate_val', '').lower() != 'true':
                return done('invalid_plate', '空车牌、虚假车牌或缺少 plate_val；' + warning)
            if event.get('encrypt_verify', 'true').lower() != 'true':
                return done('encryption_failed', '相机上报验证未通过；' + warning)
            if time.monotonic() >= deadline:
                return done('deadline')
            if self.backend:
                if self.camera_directions.get(camera) != row[3]:
                    return done('camera_mismatch', '相机未配置或出入口方向不符；' + warning)
                opened = self.backend.decide(camera, row[1], row[2], row[3], deadline=deadline)
                record = self.backend.lookup(camera, row[1], row[2], row[3])
                state = record['state'] if record else 'unknown'
                self.audit.backend_result(request_id, state)
                if opened:
                    return done('allowed', warning, opened=True)
                return done('no_signal', f'后端状态={state}；拒绝、重复或结果待核对；' + warning)
            if camera == self.open_camera and row[3] == 'in' and self.reply_claimer(camera, row[1], row[2]):
                return done('direct_test_allowed', warning, opened=True)
            return done('collected', warning)
        except Exception:
            log.exception('%s 处理失败；不开闸', request_id)
            return done('processing_error', '处理异常，详见 receiver.log；不开闸')
