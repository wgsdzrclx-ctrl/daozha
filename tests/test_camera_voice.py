"""用厂家/现场日志固定样本核对协议；所有测试均不连接设备。"""
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from camera_storage import ACK, OPEN_GATE, ROUTE
from camera_transport import CameraHandler, wire_response
from camera_voice import (VoiceSettings, build_open_reply, build_voice_frame, crc16,
                          validate_camera_reply)


class VoiceProtocolTests(unittest.TestCase):
    def test_vendor_and_captured_crc_vectors(self):
        # 附件二原始校验样本，含末尾的未加密占位 0000。
        self.assertEqual(crc16(bytes.fromhex('01640011000B01B5DAD2BBD0D0B9E3B8E60000')).hex(), 'e560')
        # 用户调试工具实测成功：粤B12345 + 欢迎光临，流水号 01。
        self.assertEqual(build_voice_frame('粤B12345', suffix='welcome', sequence=1).hex().upper(),
                         'AA55016400220009D4C1423132333435018CCFAF')
        # 用户第二条日志：流水号 02，单独欢迎光临。
        self.assertEqual(crc16(bytes.fromhex('026400220001010000')).hex(), 'f542')

    def test_plate_only_normalization_and_wire_length(self):
        self.assertEqual(build_voice_frame(' 皖es6077 ').hex().upper(),
                         'AA55006400220008CDEE455336303737D9C4AF')
        body = build_open_reply('皖ES6077', 'in', VoiceSettings(True))
        self.assertTrue(validate_camera_reply(body))
        headers, actual = wire_response(200, body).split(b'\r\n\r\n', 1)
        self.assertEqual(body, actual)
        self.assertIn(f'Content-Length:{len(actual)}'.encode(), headers)
        self.assertNotIn(b'Transfer-Encoding', headers)
        self.assertEqual(build_open_reply('皖ES6077', 'in', VoiceSettings(False)), OPEN_GATE)
        self.assertFalse(validate_camera_reply(ACK))

    def test_reject_invalid_configuration_and_unsupported_text(self):
        for changes in [{'enabled': 'false'}, {'entry_address': -1}, {'exit_address': 256},
                        {'entry_suffix': '欢迎光临'}, {'exit_suffix': 'unknown'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                VoiceSettings(**changes)
        for plate in ['', '粤B12\x0134', '粤B-12345', '粤B🙂', 'A' * 101]:
            with self.subTest(plate=plate), self.assertRaises(ValueError):
                build_voice_frame(plate)

    def test_reply_whitelist_rejects_extra_commands_and_broken_frames(self):
        body = build_open_reply('粤B12345', 'in', VoiceSettings(True))
        original = json.loads(body)
        mutations = []
        for key, value in [('gpio_data', [{'ionum': 'io2', 'action': 'on'}]),
                           ('triger_data', {'action': 'on'}), ('error_num', False),
                           ('rs485_data', original['rs485_data'] * 2)]:
            reply = dict(original)
            reply[key] = value
            mutations.append(json.dumps(reply, separators=(',', ':')).encode())
        voice_only = dict(original)
        del voice_only['gpio_data']
        mutations.append(json.dumps(voice_only, separators=(',', ':')).encode())
        mutations.extend([body.replace(b'"error_num":0', b'"error_num":0,"error_num":0'),
                          b'null', b'[]', b'invalid', body.replace(b'hex2string', b'base64')])
        frame = bytes.fromhex(original['rs485_data'][0]['data'])
        for position, value in [(0, 0), (5, 0xF0), (7, 0), (-2, frame[-2] ^ 1), (-1, 0)]:
            broken = bytearray(frame)
            broken[position] = value
            # 连 CRC 正确的配置命令也必须拒绝，不能扩大为任意 RS485 透传。
            if position == 5:
                broken[-3:-1] = crc16(broken[2:-3] + b'\x00\x00')
            mutations.append(body.replace(frame.hex().upper().encode(), broken.hex().upper().encode()))
        for invalid in mutations:
            with self.subTest(body=invalid), self.assertRaises(ValueError):
                validate_camera_reply(invalid)

    def test_transport_suppresses_late_combined_reply(self):
        handler = CameraHandler.__new__(CameraHandler)
        handler.path = ROUTE
        event = b'type=heartbeat&cam_id=test'
        handler.headers = Mock()
        handler.headers.get.return_value = None
        handler.headers.get_all.return_value = [str(len(event))]
        handler.headers.__getitem__ = Mock(return_value=str(len(event)))
        handler.headers.get_content_type.return_value = 'application/x-www-form-urlencoded'
        handler.rfile = io.BytesIO(event)
        handler.server = SimpleNamespace(api_port=1, budget=1, token='test')
        handler.reply = Mock()
        response = Mock(status=200)
        response.read.return_value = build_open_reply('粤B12345', 'in', VoiceSettings(True))
        with patch('camera_transport.http.client.HTTPConnection') as connection, \
                patch('camera_transport.time.monotonic', side_effect=[0, 2]):
            connection.return_value.getresponse.return_value = response
            handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[:2], (200, ACK))
        connection.return_value.close.assert_called_once()

    def test_combined_reply_write_failure_is_audited_as_open_attempt(self):
        handler = CameraHandler.__new__(CameraHandler)
        audit = Mock()
        handler.server = SimpleNamespace(audit=audit)
        handler.connection = Mock()
        handler.connection.sendall.side_effect = ConnectionResetError('test')
        handler.reply(200, build_open_reply('粤B12345', 'in', VoiceSettings(True)), 'request')
        self.assertEqual(audit.delivery.call_args_list[0].args, ('request', 'sending', True))
        self.assertEqual(audit.delivery.call_args_list[-1].args[:3], ('request', 'failed_or_unknown', True))


if __name__ == '__main__':
    unittest.main()
