# 道闸接收服务：FastAPI + 相机 HTTP 兼容入口

入口相机 `1803002542bd`，出口相机 `18030025462d`。实际流程：

```text
相机模式 5 表单
  → 本机 8080/api/camera/events（严格匹配设备 HTTP 响应格式）
  → 本机 FastAPI（127.0.0.1:18081）
  → 保存车牌记录，入口调用 /gate/entry，出口调用 /gate/exit
  → code=200、车牌一致、data.lift=true 才在本次 HTTP 回复携带 io1/on
  → 启用语音时，同一回复携带 rs485_data，控制卡通过 0x22 播报车牌
```

8080 只接收相机上报，不转发管理接口。18081 只监听回环地址，提供状态、审计记录与接口文档。两者由一个启动命令管理。保留设备兼容层是因为模式 5 文档要求响应头大小写及冒号格式，而通用 ASGI 服务的默认输出不同。

## 立即进行现场测试

1. 关闭旧的 8080 接收程序，避免端口冲突。
2. 双击 `start_backend_receiver.cmd`，在本机输入 `parking_writer` 的 MySQL 密码。
3. 启动日志应显示“后端判定”，两台相机映射，以及两个服务地址。
4. 浏览器打开 <http://127.0.0.1:18081/health> 检查配置，再打开 <http://127.0.0.1:18081/events> 查看最近上报。
5. 按下面相机配置表测试：先用一辆车完成入口识别，再用同一辆车完成出口识别。v0.2 后端在没有入场订单或已离场时会拒绝出口请求。

| 配置项 | 入口 | 出口 |
| --- | --- | --- |
| 相机 ID | 1803002542bd | 18030025462d |
| 出入口类型 | in / 入口 | out / 出口 |
| HTTP POST 模式 | 5 | 5 |
| 服务器地址 | 192.168.55.101（接收电脑实际 IP） | 同左 |
| 端口 | 8080 | 8080 |
| 路径 | /api/camera/events | /api/camera/events |
| 开闸方式 | 反馈开闸 | 反馈开闸（按该型号界面配置） |
| 开闸输出 | IO1 | IO1 |

请核对电脑当前 IP；程序默认监听所有本机网卡，不会为电脑设置静态地址。出口上报的 `vdc_type` 必须是 `out`。不要把 18081 配给相机。测试时确保现场具备抬杆条件。

本次环境已安装到项目 `.venv`。换电脑或缺失环境时先运行 `setup_fastapi.cmd`。它使用 `D:\codex\envs\gate-project\python.exe` 创建项目虚拟环境，再安装 `requirements.txt`；迁移电脑时需要调整这个基础解释器路径。

## 后端与运行参数

**日常配置统一修改项目根目录的 `config.ini`，不再修改 Python 文件或启动 CMD 中的 IP。** 配置文件已填写当前后端 `http://192.168.74.85:83` 和两台相机 ID。MySQL 密码仍通过 `getpass` 在启动时输入。

后端服务器换 IP 时，用记事本打开 `config.ini`，修改：

```ini
[backend]
url = http://192.168.74.85:83
```

把地址改成实际后端根地址即可，不追加 `/gate/entry`、`/gate/exit` 或 `/index.html`。保存后，先在旧接收程序窗口按 Ctrl+C 停止，再双击 `start_backend_receiver.cmd`。运行中的进程不会自动重读配置。

| 换网络后哪一项变了 | 修改位置 | 是否需要修改相机网页 |
| --- | --- | --- |
| 后端服务器 IP 或端口 | `[backend] url` | 接收电脑地址不变时不需要 |
| 接收电脑连接相机的网卡 IP | `[receiver] advertised_host`，仅更新日志展示 | 需要，把两台相机的 HTTP POST 服务器地址改为接收电脑的新 IP |
| 相机上报的接收端口 | `[receiver] port` | 需要同步修改 HTTP POST 端口 |
| MySQL 所在服务器或库名 | `[mysql] host / port / database / user` | 不需要 |
| 相机设备更换 | `[cameras] entry_id / exit_id` | 同时确认设备方向配置 |

`[receiver] host = 0.0.0.0` 表示监听接收电脑的所有 IPv4 网卡，一般保持不变；不能把后端 IP 填在这里。`advertised_host` 不会修改系统网卡，也不会自动修改相机设置；不确定时可以留空。管理端口 `api_port` 始终只绑定 `127.0.0.1`。

双击 `check_config.cmd` 可以检查格式并打印最终生效参数，不连接数据库、后端或相机。它不能证明网络连通；启动后可在 `/health` 核对实际后端地址。配置字段缺失、重复、拼写错误或端口不合法时，程序会给出错误并停止，不会悄悄回退到其他模式。

命令行参数仍可临时覆盖文件配置，例如：

```cmd
start_backend_receiver.cmd --processing-budget 6
```

完整启动命令：

```cmd
.venv\Scripts\python.exe -s camera_receiver.py --config config.ini
```

未指定 `--config` 时也会读取 Python 程序所在目录的 `config.ini`，不受当前工作目录影响。可通过 `--backend-url`、`--host`、`--port`、`--api-port` 等临时覆盖参数；常规双击启动不会覆盖文件配置。不要并行启动多个接收实例或使用 reload、多 worker。

`start_receiver.cmd` 自动加上 `--collection`，读取相同配置但不调用后端，仍是仅采集模式。只有显式指定 `--collection --open-camera 设备ID` 才启用旧的直接开闸测试。正常入口/出口测试使用 `start_backend_receiver.cmd`。`[mysql] user` 留空表示使用 SQLite 采集，不是匿名连接 MySQL。

旧的 multipart 上传练习 `start_uploader.cmd` 和 `start_local_api.cmd` 不使用本配置，不参与当前实时开闸流程。先前 Word 代码手册是修改前的源码快照；配置读取与启动方式以这里和带中文注释的 `config.ini` 为准。

## 语音播报配置与测试

完整学习文档：[道闸语音播报功能使用与代码详解](output/documents/道闸语音播报功能使用与代码详解.md)，包含流程图、协议拆包、关键函数说明、CMD 启动方式和排查步骤。

已接入调试工具验证成功的 FK 控制卡 `0x22` 即时播报协议，当前默认启用，入口和出口均只读车牌。
程序只在原有流程允许本次开闸时生成播报，使用同一份 HTTP 回复中的 `gpio_data` 和 `rs485_data`。
这表示发出开闸指令时播报，不表示闸杆已经完全抬起；程序没有机械动作或喇叭播放完成反馈。

日常只需修改 `config.ini` 的以下分区，保存后重启：

```ini
[voice]
enabled = true
entry_address = 100
exit_address = 100
entry_suffix = none
exit_suffix = none
```

| 字段 | 含义 |
| --- | --- |
| enabled | true 启用；false 关闭语音，仍按原逻辑判断开闸 |
| entry_address / exit_address | 各自控制卡的十进制 RS485 地址，0～255；当前按调试工具设置为 100，即十六进制 64 |
| entry_suffix / exit_suffix | none 只读车牌；welcome 追加“欢迎光临”；goodbye 追加“一路平安” |

如需“入口车牌＋欢迎光临、出口车牌＋一路平安”，把 `entry_suffix` 改为 `welcome`，
`exit_suffix` 改为 `goodbye`。这里不能填写任意中文句子，也没有启用本次未调通的 `0xFD` TTS。
两台相机连接各自的控制卡，地址可以相同；电脑 COM4 不是此配置的一部分。
当前实现针对未加密的控制卡。旧的自定义 INI 文件也需要补齐 `[voice]` 五个字段，否则启动检查会提示错误。

现场测试步骤：

1. 保持调试成功的相机 RS485 通道、9600 波特率、控制卡地址和音量设置；程序不修改这些硬件设置。
2. 断开调试工具的相机连接，避免手动播报与业务播报互相中断。
3. 在旧接收程序窗口按 Ctrl+C 停止，运行 `check_config.cmd`，确认“语音：启用”和控制卡地址。
4. 双击 `start_backend_receiver.cmd`，正常输入 MySQL 密码；`/health` 的 `voice` 可查看已加载的设置。
5. 进行一次正常的新入口识别，后端允许时检查开闸和车牌播报；再用同一车牌进行出口识别。
6. 查看 `receiver.log`：`voice=prepared` 表示业务层已生成指令，`IO1=True voice=True response=written`
   表示组合回复已写入网络连接。两者都不是设备播放确认；最终以现场听到的内容为准。

如果开闸但没有声音，先查看实际发送行是否为 `voice=True`，再核对相机 HTTP 模式 5 的 RS485 透传路径。
目前是通过文档规定的 `rs485_data` 发送，不存在自定义 `COM` 或 `rs485_port` 字段。
相机调试工具播放成功已经验证相机至控制卡的链路，但 HTTP 回复转发仍需实机确认。

语音沿用原有事件去重：相同事件重发、重启后再次收到同一事件不会再开闸或再播报；新的识别时间可能属于新事件。
拒绝通行、超时、离线补传、心跳及发送意图无法持久化时都不发送语音。
普通语音目录不支持的车牌字符（例如表情、任意标点）会记录 `voice=skipped` 并跳过整条播报，
不改变后端已批准的放行决定。没有自动重试播报，也不会在每次识别时写入音量、广告或配置命令。

实现位置：`camera_voice.py` 负责车牌 GBK 编码、语音目录提示代码、CRC16、完整指令和回复白名单；
`camera_service.py` 在允许开闸后组合回复；`camera_transport.py` 对动态回复继续执行超时、审计和格式检查。
厂家 CRC 包含两个未加密的 `00` 占位字节，结果高字节在前，代码中已注释。
固定测试样本包括你成功播放的 `AA55016400220009D4C1423132333435018CCFAF`（粤B12345＋欢迎光临）。

## 保留的协议与业务行为

- 相机请求保持 `application/x-www-form-urlencoded`；不是 JSON，也不是文件上传 multipart。
- `start_time` 是秒，后端 `entryTime` / `exitTime` 为秒数乘 1000。车牌发送前去除两端空格并转大写。
- `plate_val` 必须明确为 `true`；空车牌、`false` 或缺少该字段不会请求后端。可选 `encrypt_verify` 缺省兼容普通相机，明确为 `false` 时不放行。
- `online` 才进入实时判定。`offline` 仅保存；心跳、IO、RS485 等非识别事件正常确认，不改变停车订单。
- 相机 ID 必须配置，且上报方向与配置一致。两台相机均回复各自连接的 `io1/on`。
- `duplicate=true` 是后端订单复用信息，不代替 `lift` 判定。
- 收到正常事件但后端拒绝、超时、响应异常或车牌不一致时，返回普通 ACK，不带 GPIO。
- 保留特殊图片 Base64（`-`、`_`、`.`）和原照片哈希命名方式。可选图片损坏或保存失败会留警告，继续处理有效车牌；车牌核心数据库写入失败则不开闸，持久化接收审计留作人工排查。
- 请求体上限仍为 16 MiB；过大、非法表单、错误路径和错误内容类型返回错误 JSON，不会返回开闸指令。
- 相机响应使用紧凑 JSON、`HTTP/1.1 200`、精确字节数 `Content-Length` 和 `Content-Type:application/json;charset=utf-8`，头名保留大小写，冒号后无空格，连接关闭，不使用分块传输。

## 超时、重发与异常恢复

默认 FastAPI 处理预算为 6 秒，从内部收到请求开始计时；外层另有本机转发及发送余量。相机上传请求体单次阻塞读取超时为 5 秒。该预算不等于设备端整个网络往返的硬实时保证，需核对相机实际等待时限。

后台 HTTP 连接/读取超时分别为 2/3 秒；MySQL 连接/读取/写入超时分别为 2/3/3 秒。处理预算到期后本次仅回复 ACK；同步 I/O 可能仍在结束，晚到的后端结果会保留，不会补发开闸。不能通过减小客户端超时撤销已经提交的停车订单。

`backend_events.sqlite3` 继续按“后端地址 + 相机 ID + 识别秒 + 规范化车牌 + 方向”持久化去重。相同事件重发不会再请求后端或再输出 IO1，重启也一样。同秒同车牌的两次识别可能被合并；不同秒的重新识别可能是新事件。

若后台已经处理但相机未收到应答，当前设备协议没有可靠的执行回执，不能同时保证“绝不重复动作”和“必定开闸”。本版保留一次下发策略，并增加人工核对留痕：

1. 在 `/events` 找到对应 `request_id`，打开 `/events/{request_id}`，查看关联后端订单响应。
2. 对 `pending`、`uncertain`、`deadline`、`processing_error`、`sending` 或 `failed_or_unknown` 记录，核对后端停车订单与现场车辆/杆状态。进程中断时也可能留在 `processing`。
3. 需要放行时由现场人员根据核对结果使用现场控制完成；**不要删除去重表、自动重放 exit 或修改时间戳来绕过去重**。
4. 在本机 `/docs` 使用 `POST /events/{request_id}/review`，填写例如 `{"note":"已核对订单，现场人工放行"}`。此操作仅保存核对说明，不请求后端、不下发开闸，也不解除去重。

当前未实现加密相机的签名握手。若收到 `type=encrypt_verify`，状态为 `unsupported_encryption`，需要厂商签名算法后补充。`encrypt_verify=false` 不会开闸。

## 诊断接口与日志

仅在接收电脑本机访问：

- <http://127.0.0.1:18081/health>：配置和服务存活；不会主动探测数据库/后端，不表示它们一定可用。
- <http://127.0.0.1:18081/events>：最近 30 次上报，可加 `?limit=100`。
- `GET /events/{request_id}`：具体上报、后端请求与响应、人工核对记录。
- <http://127.0.0.1:18081/docs>：管理接口文档。相机路由需要内部令牌，请通过 8080 测试设备请求。

日志写入 `receiver.log`，单文件 5 MiB，保留 4 个轮转文件。终端同时显示相机、车牌、后端结果与 `IO1=True/False`。

| 字段/状态 | 含义 |
| --- | --- |
| state=allowed | 后端允许且业务处理生成开闸响应 |
| state=no_signal | 本次不发指令，结合 backend_state 和 detail 查看拒绝/重复原因 |
| state=camera_mismatch | 相机 ID 未配置或方向不符 |
| state=invalid_plate | 空车牌、虚假车牌或 plate_val 缺失 |
| state=deadline | 本次处理超时；后端最终可能仍成功 |
| backend_state=allowed/denied/rejected | 后端允许/不允许/业务失败 |
| backend_state=pending/uncertain/not_sent | 请求结果待核对/结果不确定/截止前未发送 |
| delivery_state=written, open_signal=1 | 开闸响应已写入网络连接，**不是设备执行确认** |
| delivery_state=written, open_signal=0 | 普通 ACK 或错误响应已写出 |
| delivery_state=sending/failed_or_unknown | 发送中断或结果无法确认，不能据此自动重发 |

非法请求可能只有日志和发送记录，不会创建业务事件；正常心跳会出现在事件列表中。

## 文件与原有数据

| 文件 | 用途 |
| --- | --- |
| camera_receiver.py | 启动参数、日志、两个监听服务的生命周期 |
| config.ini | 集中配置后端地址、接收端口、相机 ID、MySQL 和语音 |
| receiver_config.py | 读取配置、命令行覆盖、校验与配置摘要 |
| check_config.cmd | 只检查配置，不连接现场设备或数据库 |
| camera_transport.py | 面向相机的严格 HTTP 格式适配 |
| camera_api.py | FastAPI 请求入口、预算控制、诊断与人工核对接口 |
| camera_service.py | 相机事件校验、保存、后端判定 |
| camera_voice.py | 0x22 语音编码、CRC、开闸组合回复和协议白名单 |
| camera_storage.py | 原有图片、SQLite/MySQL 保存与直接测试去重 |
| gate_backend.py | entry/exit 调用、持久化去重及后端记录 |
| receiver_audit.py | 接收、发送及人工核对记录 |

现有 `backend_events.sqlite3`、`gate_replies.sqlite3`、`vehicle_records.sqlite3`、`postman_vehicle_test.sqlite3` 和 `camera_photos` 保留；新增 `receiver_audit.sqlite3` 用于诊断。未清理旧去重记录，未更改 MySQL 表结构。MySQL 中历史图片绝对路径可能仍指向原目录，不要删除原照片。

修改前的源码、启动脚本及依赖清单已备份在 `backups/before_fastapi_*.zip`。这些 ZIP 不包含业务数据库；回退代码时不要覆盖或删除正在使用的数据文件。

原 `local_vehicle_api.py` 和 `forward_vehicle_events.py` 是独立的 multipart 上传练习，不参与本次实时开闸链路，原启动脚本仍可使用。

## 自动化测试与依赖复现

```cmd
.venv\Scripts\python.exe -s -m unittest discover -s tests -v
.venv\Scripts\python.exe -s -m pip check
```

测试只访问随机回环端口的模拟后端，使用临时数据库，不连接真实相机/MySQL/计费服务。覆盖入口/出口、允许/拒绝、并发重复、重启去重、异常响应、超时、心跳、离线、图片异常、核心存储失败、响应写出失败和真实 HTTP 字节格式。

`requirements.txt` 固定直接依赖版本；`requirements-lock.txt` 固定本次验证环境全部依赖，并已移除原清单中的本机构建路径。需要精确复现时在新环境执行 `python -m pip install -r requirements-lock.txt`。

模拟测试通过不代表真实 MySQL 权限、相机固件、等待时限、IO 接线以及机械动作已经验证。后端 v0.2 文档注明 demo 成功时恒返回 `lift=true`，现场拒绝分支需要后端配合或使用本项目自动化测试验证。
