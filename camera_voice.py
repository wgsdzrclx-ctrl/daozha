"""FK 控制卡 0x22 语音组包，以及带语音的模式 5 开闸回复。

只发送即时播报，不自动修改音量、广告或控制卡参数。
"""
from dataclasses import dataclass
import json
import string

from camera_storage import ACK, OPEN_GATE

SUFFIXES = {'none': b'', 'welcome': b'\x01', 'goodbye': b'\x02'}
# 普通语音卡不是任意中文 TTS：只使用厂家语音目录内的车牌字符。
PLATE_CHARACTERS = frozenset(string.ascii_uppercase + string.digits +
    '京黑吉辽苏鲁皖冀豫鄂湘赣陕晋川青琼粤浙闽甘云台贵渝沪津新桂蒙宁'
    '军学警海北沈兰济南广成甲乙丙午未申庚己辛壬寅戍藏港澳领使')


@dataclass(frozen=True)
class VoiceSettings:
    enabled: bool = False
    entry_address: int = 100
    exit_address: int = 100
    entry_suffix: str = 'none'
    exit_suffix: str = 'none'

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError('voice.enabled 必须为 true 或 false')
        for address in (self.entry_address, self.exit_address):
            if type(address) is not int or not 0 <= address <= 255:
                raise ValueError('语音控制卡地址必须在 0 到 255 之间')
        for suffix in (self.entry_suffix, self.exit_suffix):
            if suffix not in SUFFIXES:
                raise ValueError('语音提示语必须为 none、welcome 或 goodbye')


def crc16(data):
    """厂家 CRC16：初值 FFFF、反向多项式 A001；返回高字节在前。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc.to_bytes(2, 'big')


def plate_bytes(plate):
    # 与提交后端的车牌规范一致；不能把标点/控制字符当成语音目录编号发送。
    plate = plate.strip().upper()
    if not plate or any(char not in PLATE_CHARACTERS for char in plate):
        raise ValueError('车牌含普通语音目录不支持的字符')
    return plate.encode('gbk')


def build_voice_frame(plate, address=100, suffix='none', sequence=0):
    """生成未加密控制卡的完整二进制帧；流水号按协议允许固定为 0。"""
    if type(address) is not int or not 0 <= address <= 255:
        raise ValueError('控制卡地址必须在 0 到 255 之间')
    if type(sequence) is not int or not 0 <= sequence <= 255 or suffix not in SUFFIXES:
        raise ValueError('无效的流水号或提示语')
    payload = plate_bytes(plate) + SUFFIXES[suffix]
    if not 1 <= len(payload) <= 100:
        raise ValueError('语音内容必须在 1 到 100 字节之间')
    data = bytes((sequence, address, 0, 0x22)) + len(payload).to_bytes(2, 'big') + payload
    # 厂家算法特别要求：排除 AA55/AF，但把未加密的两个 00 占位字节纳入计算。
    # 不能直接套用普通 Modbus 的校验范围或低字节在前的发送顺序。
    return b'\xAA\x55' + data + crc16(data + b'\x00\x00') + b'\xAF'


def _voice_reply(frame_hex):
    reply = json.loads(OPEN_GATE)
    reply['rs485_data'] = [{'encodetype': 'hex2string', 'data': frame_hex}]
    return json.dumps(reply, separators=(',', ':')).encode('ascii')


def build_open_reply(plate, direction, settings):
    """仅供已经判定允许开闸的事件调用；语音与 GPIO 放在同一份 HTTP 回复中。"""
    if not settings.enabled:
        return OPEN_GATE
    if direction not in ('in', 'out'):
        raise ValueError('语音方向必须是 in 或 out')
    address = settings.entry_address if direction == 'in' else settings.exit_address
    suffix = settings.entry_suffix if direction == 'in' else settings.exit_suffix
    return _voice_reply(build_voice_frame(plate, address, suffix).hex().upper())


def validate_camera_reply(body):
    """返回是否含开闸指令；不在白名单内的内部回复抛出 ValueError。

    不因为支持 rs485_data 就允许任意透传：只接受一条 CRC 正确、内容合法的
    0x22 车牌播报，并要求同时带固定 io1/on。配置写入等命令不能通过此边界。
    """
    if body == ACK:
        return False
    if body == OPEN_GATE:
        return True
    try:
        reply = json.loads(body)
        commands = reply['rs485_data']
        if not isinstance(commands, list) or len(commands) != 1:
            raise ValueError('仅允许一条语音指令')
        frame_hex = commands[0]['data']
        frame = bytes.fromhex(frame_hex)
        if (not 12 <= len(frame) <= 111 or frame[:2] != b'\xAA\x55' or frame[-1] != 0xAF
                or frame[4:6] != b'\x00\x22'
                or int.from_bytes(frame[6:8], 'big') != len(frame) - 11
                or frame[-3:-1] != crc16(frame[2:-3] + b'\x00\x00')):
            raise ValueError('语音帧格式或校验错误')
        payload = frame[8:-3]
        # 固定短语只允许出现在车牌末尾，且仅支持当前公开配置的两种提示语。
        if payload[-1:] in (b'\x01', b'\x02'):
            payload = payload[:-1]
        if plate_bytes(payload.decode('gbk')) != payload:
            raise ValueError('车牌编码不规范')
        # 与规范输出逐字节比较，同时拒绝额外命令、重复 JSON 键、布尔值冒充 0 等。
        if frame_hex != frame.hex().upper() or body != _voice_reply(frame_hex):
            raise ValueError('回复包含不允许的字段或格式')
        return True
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeError) as error:
        raise ValueError('内部响应不是允许的相机协议报文') from error
