"""读取集中配置并校验；命令行参数可以临时覆盖配置文件。"""
import argparse
import configparser
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from camera_voice import VoiceSettings

ROOT = Path(__file__).resolve().parent
ENTRY_CAMERA = '1803002542bd'
EXIT_CAMERA = '18030025462d'


def parse_bool(value):
    if value.lower() not in ('true', 'false'):
        raise ValueError('请填写 true 或 false')
    return value.lower() == 'true'

# 配置文件字段 → 启动参数名称、转换类型。所有字段必须存在，避免拼写错误被忽略。
FIELDS = {
    'backend': {'url': ('backend_url', str)},
    'receiver': {'host': ('host', str), 'port': ('port', int),
                 'api_port': ('api_port', int), 'advertised_host': ('advertised_host', str),
                 'processing_budget': ('processing_budget', float)},
    'cameras': {'entry_id': ('entry_camera', str), 'exit_id': ('exit_camera', str)},
    'mysql': {'host': ('mysql_host', str), 'port': ('mysql_port', int),
              'database': ('mysql_database', str), 'user': ('mysql_user', str)},
    'voice': {'enabled': ('voice_enabled', parse_bool),
              'entry_address': ('voice_entry_address', int), 'exit_address': ('voice_exit_address', int),
              'entry_suffix': ('voice_entry_suffix', str), 'exit_suffix': ('voice_exit_suffix', str)},
}


def load_config(path):
    config = configparser.ConfigParser(interpolation=None)
    with Path(path).open(encoding='utf-8-sig') as file:
        config.read_file(file)
    if config.defaults() or set(config.sections()) != set(FIELDS):
        raise ValueError('配置必须包含 backend、receiver、cameras、mysql、voice 五个分区，不支持 DEFAULT 或额外分区')
    values = {}
    for section, fields in FIELDS.items():
        if set(config[section]) != set(fields):
            raise ValueError(f'[{section}] 字段不完整或拼写错误；应包含：{", ".join(fields)}')
        for key, (name, convert) in fields.items():
            try:
                values[name] = convert(config[section][key].strip())
            except ValueError as error:
                raise ValueError(f'[{section}] {key} 格式错误：{error}') from None
    return values


def parse_settings(argv=None):
    parser = argparse.ArgumentParser(description='启动道闸接收服务，默认读取程序目录的 config.ini')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.ini', help='配置文件路径')
    parser.add_argument('--check-config', action='store_true', help='只检查配置，不连接数据库或启动服务')
    parser.add_argument('--collection', action='store_true', help='仅采集，不请求 entry/exit 后端')
    for fields in FIELDS.values():
        for name, convert in fields.values():
            parser.add_argument('--' + name.replace('_', '-'), type=convert,
                                default=argparse.SUPPRESS, help='临时覆盖 config.ini 中的对应值')
    parser.add_argument('--open-camera', help='旧入口直接开闸测试；仅与 --collection 一起使用')
    args = parser.parse_args(argv)
    path = args.config.expanduser().resolve()
    try:
        values = load_config(path)
        # 只覆盖明确传入的命令行选项；未传入的值来自文件。
        values.update(vars(args))
        args = argparse.Namespace(**values)
        args.config = path
        args.voice = VoiceSettings(args.voice_enabled, args.voice_entry_address, args.voice_exit_address,
                                   args.voice_entry_suffix, args.voice_exit_suffix)
        for name in ('port', 'api_port', 'mysql_port'):
            if not 1 <= getattr(args, name) <= 65535:
                raise ValueError(f'{name} 必须在 1 到 65535 之间')
        if args.port == args.api_port:
            raise ValueError('相机端口与本机管理端口不能相同')
        if not 1 <= args.processing_budget <= 30:
            raise ValueError('processing_budget 必须在 1 到 30 秒之间')
        ipaddress.IPv4Address(args.host)
        if args.advertised_host:
            address = ipaddress.IPv4Address(args.advertised_host)
            if address.is_unspecified or address.is_loopback or address.is_multicast:
                raise ValueError('advertised_host 请填写相机可访问的接收电脑 IPv4 地址，或留空')
        if args.mysql_user and (not args.mysql_host or not args.mysql_database):
            raise ValueError('使用 MySQL 时 host 和 database 不能为空')
        if args.collection:
            args.backend_url = None
        else:
            url = urlsplit(args.backend_url)
            if (url.scheme not in ('http', 'https') or not url.hostname or
                    url.path not in ('', '/') or url.query or url.fragment or
                    url.username is not None or url.password is not None):
                raise ValueError('[backend] url 请填写 http(s)://主机:端口，不要附加接口路径')
            if url.port is not None and not 1 <= url.port <= 65535:
                raise ValueError('后端端口必须在 1 到 65535 之间')
            args.backend_url = args.backend_url.rstrip('/')
            if args.open_camera:
                raise ValueError('后端模式不能同时启用直接开闸测试；旧测试需使用 --collection')
            if (not args.entry_camera or not args.exit_camera or
                    args.entry_camera == args.exit_camera):
                raise ValueError('入口和出口相机 ID 必须非空且不同')
        args.mysql_user = args.mysql_user or None
    except (OSError, UnicodeError, configparser.Error, ValueError) as error:
        parser.error(f'配置错误（{path}）：{error}')
    return args


def describe_settings(args):
    address = args.advertised_host or '<接收电脑连接相机的实际IP>'
    storage = (f'MySQL {args.mysql_host}:{args.mysql_port}/{args.mysql_database} 用户={args.mysql_user}'
               if args.mysql_user else 'SQLite 本地采集')
    return [f'配置文件：{args.config}',
            f'运行模式：{"后端判定" if args.backend_url else "采集/直接测试"}',
            f'后端地址：{args.backend_url or "不调用后端"}',
            f'相机上报地址：http://{address}:{args.port}/api/camera/events（监听 {args.host}）',
            f'本机管理：http://127.0.0.1:{args.api_port}/health',
            f'相机 ID：入口={args.entry_camera} 出口={args.exit_camera}',
            f'采集存储：{storage}', f'处理预算：{args.processing_budget} 秒',
            f'语音：{"启用" if args.voice.enabled else "关闭"}；0x22；'
            f'入口地址={args.voice.entry_address} 提示语={args.voice.entry_suffix}；'
            f'出口地址={args.voice.exit_address} 提示语={args.voice.exit_suffix}']
