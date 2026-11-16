"""用厂家/现场日志固定样本核对协议；所有测试均不连接设备。"""
import io
import json
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from camera_storage import ACK, OPEN_GATE, ROUTE
from camera_transport import CameraHandler, wire_response
from camera_voice import (VoiceSettings, amount_voice_bytes, build_open_reply, build_voice_frame,
                          crc16, normalize_billing_amount, validate_camera_reply)


class VoiceProtocolTests(unittest.TestCase):
    def test_amount_encoding_uses_catalog_digits_units_and_two_decimal_places(self):
        samples = {'0': '0点00元', '5': '5点00元', '12.50': '十2点50元',
                   '20.06': '2十点06元', '101.01': '1百01点01元',
                   '1001': '1千01点00元', '1010': '1千01十点00元',
                   '10000': '1万点00元', '10010': '1万01十点00元',
                   '110000': '十1万点00元', '100001': '十万01点00元',
                   '99999999.99': '9千9百9十9万9千9百9十9点99元',
                   '1.005': '1点01元', '-0.00': '0点00元'}
        for value, spoken in samples.items():
            with self.subTest(value=value):
                self.assertEqual(amount_voice_bytes(Decimal(value)), b'\x2D' + spoken.encode('gbk'))
                body = build_open_reply('粤B12345', 'out', VoiceSettings(True, exit_suffix='goodbye'),
                                        billing_amount=Decimal(value))
                self.assertTrue(validate_camera_reply(body))
                frame = bytes.fromhex(json.loads(body)['rs485_data'][0]['data'])
                self.assertEqual(int.from_bytes(frame[6:8], 'big'), len(frame) - 11)
                self.assertEqual(frame[-3:-1], crc16(frame[2:-3] + b'\x00\x00'))
                self.assertEqual(frame[8:-3], '粤B12345'.encode('gbk') +
                                 b'\x2D' + spoken.encode('gbk') + b'\x02')

    def test_invalid_amounts_are_not_zero_and_entry_ignores_amount(self):
        for value in (None, True, False, '', 'oops', -1, '-0.001', 'NaN', 'sNaN',
                      'Infinity', '-Infinity', {}, [], '100000000', '1e999999', '1' * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_billing_amount(value)
        self.assertEqual(normalize_billing_amount('12.50'), Decimal('12.50'))
        self.assertEqual(build_open_reply('粤B12345', 'in', VoiceSettings(True), billing_amount=12.5),
                         build_open_reply('粤B12345', 'in', VoiceSettings(True)))
        self.assertEqual(build_open_reply('粤B12345', 'out', VoiceSettings(False), billing_amount=12.5),
                         OPEN_GATE)

    def test_amount_validator_rejects_correct_crc_but_invalid_grammar(self):
        for tail in ('12点50元', '十2点5元', '十2点500元', '-1点00元',
                     '十2.50元', '十2点50元元', '0点00元\x01\x02',
                     '十2点50元\x2D0点00元', '1亿点00元', '点00元'):
            with self.subTest(tail=tail):
                payload = '粤B12345'.encode('gbk') + b'\x2D' + tail.encode('gbk')
                data = bytes((0, 100, 0, 0x22)) + len(payload).to_bytes(2, 'big') + payload
                frame = b'\xAA\x55' + data + crc16(data + b'\x00\x00') + b'\xAF'
                reply = json.loads(OPEN_GATE)
                reply['rs485_data'] = [{'encodetype': 'hex2string', 'data': frame.hex().upper()}]
                with self.assertRaises(ValueError):
                    validate_camera_reply(json.dumps(reply, separators=(',', ':')).encode())

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
        response.read.return_value = build_open_reply('粤B12345', 'out', VoiceSettings(True),
                                                     billing_amount=Decimal('12.50'))
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
