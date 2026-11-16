"""FK 控制卡 0x22 语音组包，以及带语音的模式 5 开闸回复。

只发送即时播报，不自动修改音量、广告或控制卡参数。
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import json
import re
import string

from camera_storage import ACK, OPEN_GATE

SUFFIXES = {'none': b'', 'welcome': b'\x01', 'goodbye': b'\x02'}
# 目录 0x2D 是“金额”，不是任意文字 TTS；目录没有“本次收费”词条。
AMOUNT_PREFIX = b'\x2D'
MAX_VOICE_AMOUNT = Decimal('99999999.99')  # 目录提供十、百、千、万，没有亿。
# 普通语音卡不是任意中文 TTS：只使用厂家语音目录内的车牌字符。
PLATE_CHARACTERS = frozenset(string.ascii_uppercase + string.digits +
    '京黑吉辽苏鲁皖冀豫鄂湘赣陕晋川青琼粤浙闽甘云台贵渝沪津新桂蒙宁'
    '军学警海北沈兰济南广成甲乙丙午未申庚己辛壬寅戍藏港澳领使')


@dataclass(frozen=True)
class VoiceSettings:
    enabled: bool = False
    entry_address: int = 100
    exit_address: int = 100
    entry_suffix: str = 'welcome'
    exit_suffix: str = 'goodbye'

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


def normalize_billing_amount(value):
    """金额按元处理，四舍五入到分；缺失/异常不得转换为免费。"""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError('billingAmount 缺失或类型错误')
    text = str(value)
    if len(text) > 64:
        raise ValueError('billingAmount 过长')
    try:
        amount = Decimal(text)
        if not amount.is_finite() or amount < 0 or amount > MAX_VOICE_AMOUNT:
            raise ValueError('billingAmount 必须为 0 至 99999999.99 元的有限数值')
        with localcontext() as context:
            context.prec = 32
            amount = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return abs(amount)  # -0.00 统一为 0.00。
    except InvalidOperation as error:
        raise ValueError('billingAmount 不是有效金额') from error


def _integer_voice(number):
    """用 ASCII 数字和目录中的 GBK 位数单位读金额，12 应读十二而非一二。"""
    def group(value):
        result = ''
        pending_zero = False
        for divisor, unit in ((1000, '千'), (100, '百'), (10, '十'), (1, '')):
            digit, value = divmod(value, divisor)
            if digit:
                result += ('0' if pending_zero else '') + str(digit) + unit
                pending_zero = False
            elif result and value:
                pending_zero = True
        return result

    high, low = divmod(number, 10000)
    text = (group(high) + '万' if high else '')
    if low:
        text += ('0' if high and low < 1000 else '') + group(low)
    # 只有开头的一十省略“一”，例如十万零一十。
    return (text[1:] if text.startswith('1十') else text) or '0'


def amount_voice_bytes(amount):
    """0x2D(金额) + 整数读法 + 点 + 两位小数 + 元，均来自普通语音目录。"""
    amount = normalize_billing_amount(amount)
    integer, fraction = format(amount, '.2f').split('.')
    return AMOUNT_PREFIX + (_integer_voice(int(integer)) + '点' + fraction + '元').encode('gbk')


def _validate_amount_voice(payload):
    """反解后重新编码，拒绝夹带词条、非规范数字或额外串口命令。"""
    text = payload[1:].decode('gbk')
    match = re.fullmatch(r'([0-9十百千万]+)点([0-9]{2})元', text)
    if not match:
        raise ValueError('金额语音格式错误')
    total = section = digit = 0
    for char in match[1]:
        if char in string.digits:
            digit = digit * 10 + int(char)
        elif char == '万':
            total += (section + digit) * 10000
            section = digit = 0
        else:
            section += (digit or 1) * {'十': 10, '百': 100, '千': 1000}[char]
            digit = 0
    amount = Decimal(f'{total + section + digit}.{match[2]}')
    if amount_voice_bytes(amount) != payload:
        raise ValueError('金额语音编码不规范')


def build_voice_frame(plate, address=100, suffix='none', sequence=0, *, billing_amount=None):
    """生成未加密控制卡的完整二进制帧；流水号按协议允许固定为 0。"""
    if type(address) is not int or not 0 <= address <= 255:
        raise ValueError('控制卡地址必须在 0 到 255 之间')
    if type(sequence) is not int or not 0 <= sequence <= 255 or suffix not in SUFFIXES:
        raise ValueError('无效的流水号或提示语')
    payload = plate_bytes(plate)
    if billing_amount is not None:
        payload += amount_voice_bytes(billing_amount)
    payload += SUFFIXES[suffix]
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


def build_open_reply(plate, direction, settings, *, billing_amount=None):
    """仅供已经判定允许开闸的事件调用；语音与 GPIO 放在同一份 HTTP 回复中。"""
    if not settings.enabled:
        return OPEN_GATE
    if direction not in ('in', 'out'):
        raise ValueError('语音方向必须是 in 或 out')
    address = settings.entry_address if direction == 'in' else settings.exit_address
    suffix = settings.entry_suffix if direction == 'in' else settings.exit_suffix
    # 入口即使误传金额也维持原播报；只有出口允许拼接收费金额。
    amount = billing_amount if direction == 'out' else None
    return _voice_reply(build_voice_frame(plate, address, suffix, billing_amount=amount).hex().upper())


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
        # 0x2D 不会出现在合法车牌 GBK 字节中，因此可作为金额段的唯一分隔符。
        if AMOUNT_PREFIX in payload:
            payload, amount = payload.split(AMOUNT_PREFIX, 1)
            _validate_amount_voice(AMOUNT_PREFIX + amount)
        if plate_bytes(payload.decode('gbk')) != payload:
            raise ValueError('车牌编码不规范')
        # 与规范输出逐字节比较，同时拒绝额外命令、重复 JSON 键、布尔值冒充 0 等。
        if frame_hex != frame.hex().upper() or body != _voice_reply(frame_hex):
            raise ValueError('回复包含不允许的字段或格式')
        return True
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeError) as error:
        raise ValueError('内部响应不是允许的相机协议报文') from error
