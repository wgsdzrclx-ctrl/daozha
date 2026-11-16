"""只验证配置读取和依赖装配；不访问现场 MySQL、相机或后端。"""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import camera_receiver
from camera_storage import mysql_writer
from receiver_config import ROOT, parse_settings


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.ini'
        self.template = (Path(__file__).parent / 'fixtures/config.ini').read_text(encoding='utf-8')
        self.token_file = Path(self.temp.name) / 'backend_token.txt'
        self.token_file.write_text('local-test-token', encoding='utf-8')
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.path.write_text(self.template, encoding='utf-8')

    def read(self, *extra):
        return parse_settings(['--config', str(self.path), *extra])

    def invalid(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            self.read()
        self.assertEqual(error.exception.code, 2)

    def test_defaults_are_independent_of_working_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.temp.name)
            with patch('receiver_config.ROOT', Path(self.temp.name)):
                args = parse_settings([])
        finally:
            os.chdir(previous)
        self.assertEqual(args.config, self.path)
        self.assertEqual(args.backend_url, 'http://192.168.74.85:83')
        self.assertEqual(args.entry_camera, '1803002542bd')
        self.assertEqual(args.exit_camera, '18030025462d')
        self.assertTrue(args.voice.enabled)
        self.assertEqual(args.voice.entry_suffix, 'none')
        self.assertEqual(args.voice.exit_address, 100)

    def test_edited_file_and_cli_override(self):
        self.path.write_text(self.template.replace('192.168.74.85:83', '192.0.2.10:9000')
                             .replace('port = 3306', 'port = 3307'), encoding='utf-8-sig')
        args = self.read('--port', '8088')
        self.assertEqual(args.backend_url, 'http://192.0.2.10:9000')
        self.assertEqual(args.mysql_port, 3307)
        self.assertEqual(args.port, 8088)
        args = self.read('--backend-url', 'http://192.0.2.11:9001/')
        self.assertEqual(args.backend_url, 'http://192.0.2.11:9001')

    def test_tls_option_keeps_old_config_secure_and_accepts_explicit_test_override(self):
        self.assertTrue(self.read().backend_verify_tls)
        self.path.write_text(self.template.replace('[backend]', '[backend]\nverify_tls = false'),
                             encoding='utf-8')
        self.assertFalse(self.read().backend_verify_tls)
        self.assertTrue(self.read('--backend-verify-tls', 'true').backend_verify_tls)
        self.path.write_text(self.template.replace('[backend]', '[backend]\nverify_tls = maybe'),
                             encoding='utf-8')
        self.invalid()

    def test_collection_does_not_inherit_backend(self):
        args = self.read('--collection')
        self.assertIsNone(args.backend_url)
        self.assertIsNone(args.open_camera)
        self.assertEqual(args.mysql_user, 'parking_writer')
        args = self.read('--collection', '--open-camera', 'test-camera')
        self.assertEqual(args.open_camera, 'test-camera')

    def test_missing_config_fails_instead_of_starting_other_mode(self):
        self.path.unlink()
        self.invalid()

    def test_typo_duplicate_and_invalid_values_fail(self):
        replacements = [
            ('api_port = 18081', 'api_prt = 18081'),
            ('port = 8080', 'port = 8080\nport = 8081'),
            ('port = 8080', 'port = wrong'),
            ('api_port = 18081', 'api_port = 8080'),
            ('port = 3306', 'port = 70000'),
            ('processing_budget = 6', 'processing_budget = nan'),
            ('url = http://192.168.74.85:83', 'url ='),
            ('url = http://192.168.74.85:83', 'url = http://192.0.2.1:83/gate/entry'),
            ('url = http://192.168.74.85:83', 'url = http://192.0.2.1:99999'),
            ('exit_id = 18030025462d', 'exit_id = 1803002542bd'),
            ('advertised_host = 192.168.55.101', 'advertised_host = 0.0.0.0'),
            ('enabled = true', 'enabled = maybe'),
            ('entry_address = 100', 'entry_address = 256'),
            ('exit_address = 100', 'exit_address = -1'),
            ('entry_suffix = none', 'entry_suffix = 欢迎光临'),
            ('lot_id = LOT-DEFAULT', 'lot_id = 停车场'),
            ('min_interval_seconds = 3', 'min_interval_seconds = nan'),
            ('min_interval_seconds = 3', 'min_interval_seconds = -1'),
            ('token_env = GATE_API_TOKEN', 'token_env = INVALID-NAME'),
        ]
        for before, after in replacements:
            with self.subTest(after=after):
                self.path.write_text(self.template.replace(before, after), encoding='utf-8')
                self.invalid()

    def test_direct_open_is_never_implicitly_enabled(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.read('--open-camera', 'test-camera')

    def test_blank_optional_values(self):
        self.path.write_text(self.template.replace('user = parking_writer', 'user =')
                             .replace('advertised_host = 192.168.55.101', 'advertised_host ='), encoding='utf-8')
        args = self.read()
        self.assertIsNone(args.mysql_user)
        self.assertEqual(args.advertised_host, '')

    def test_check_config_does_not_connect_or_create_services(self):
        with patch.object(camera_receiver, 'mysql_writer') as mysql, \
                patch.object(camera_receiver, 'GateBackend') as backend, \
                patch.object(camera_receiver, 'ReceiverAudit') as audit, \
                patch.object(camera_receiver, 'CameraHTTPServer') as server, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            camera_receiver.main(['--config', str(self.path), '--check-config'])
        for dependency in (mysql, backend, audit, server):
            dependency.assert_not_called()
        self.assertIn('http://192.168.74.85:83', output.getvalue())
        self.assertIn('语音：启用', output.getvalue())

    def test_voice_config_overrides_and_explicit_false(self):
        args = self.read('--voice-enabled', 'false', '--voice-entry-suffix', 'welcome',
                         '--voice-exit-suffix', 'goodbye', '--voice-exit-address', '101')
        self.assertFalse(args.voice.enabled)
        self.assertEqual(args.voice.entry_suffix, 'welcome')
        self.assertEqual(args.voice.exit_suffix, 'goodbye')
        self.assertEqual(args.voice.exit_address, 101)

    def test_backend_credentials_precedence_validation_and_redaction(self):
        args = self.read()
        self.assertEqual(args.backend_token, 'local-test-token')
        with patch.dict(os.environ, {'GATE_API_TOKEN': 'env-test-token'}):
            self.assertEqual(self.read().backend_token, 'env-test-token')
        for invalid in ['', 'secret\r\ninvalid', '中文凭证']:
            with patch.dict(os.environ, {'GATE_API_TOKEN': invalid}):
                self.invalid()
        self.token_file.unlink()
        self.invalid()
        self.assertIsNone(self.read('--collection').backend_token)
        with patch.dict(os.environ, {'GATE_API_TOKEN': 'DO-NOT-PRINT-THIS'}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            camera_receiver.main(['--config', str(self.path), '--check-config'])
        self.assertNotIn('DO-NOT-PRINT-THIS', output.getvalue())
        self.assertIn('环境变量 GATE_API_TOKEN', output.getvalue())

    def test_mysql_options_reach_initial_and_event_connections(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with patch('camera_storage.getpass', return_value='test-only'), \
                patch('mysql.connector.connect', return_value=connection) as connect:
            save = mysql_writer('test_user', host='192.0.2.30', port=3307, database='test_db')
            save(('camera', 1700000000, 'TEST', 'in', 'online', None, None))
        self.assertEqual(connect.call_count, 2)
        for call in connect.call_args_list:
            self.assertEqual(call.kwargs['host'], '192.0.2.30')
            self.assertEqual(call.kwargs['port'], 3307)
            self.assertEqual(call.kwargs['database'], 'test_db')
            self.assertEqual(call.kwargs['user'], 'test_user')
        connection.commit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
