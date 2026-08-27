# 采集、标注、上传与恢复

## 按需启动和重启本机采集后端

首次安装持久的用户级服务定义（不启用开机自启）：

```bash
cd /home/kscii/Codes/imu-data-collector
./scripts/install-user-service.sh
```

日常最常用的重启命令：

```bash
systemctl --user restart imu-data-collector.service
```

这条命令只重启已经安装的版本，不会重新构建网页。修改或拉取源码后使用：

```bash
./scripts/update-local-capture.sh
```

脚本会拒绝打断正在录制或收尾的会话，完成锁定依赖安装、前端暂存构建、前后端版本校验、
静态目录原子切换、服务重启和健康检查。只有没有源码变化的日常故障恢复才直接使用
`systemctl --user restart`。

状态和日志分别使用：

```bash
systemctl --user status imu-data-collector.service --no-pager
journalctl --user -u imu-data-collector.service -n 100 --no-pager
```

该服务只监听 `127.0.0.1:8765`。不要同时运行前台 `uv run imu-collector start`，否则第二个
进程会因端口占用退出。页面检测到前后端源码哈希不一致时会禁用设备按钮并提示执行一键更新
脚本；单纯重启不能把旧静态文件变成新构建。

浏览器会长期保持 WebSocket 和 MJPEG 连接，后端为此设置 5 秒优雅关机上限：重启时先等待
活动请求结束，随后取消仍占用的预览连接，再执行应用 shutdown 释放 BLE 通知和 FFmpeg。
systemd 的 `TimeoutStopSec=25` 只是外层保险，正常重启不应再走到 SIGKILL；若日志出现
`State 'stop-sigterm' timed out`，应先按设备泄漏处理，不能把它视为正常停止。

## 为双模 CW12EU-T 显式启用 BLE 承载

本机 BlueZ 5.87 的 `Device1.PreferredBearer` 与 `Bearer.LE1` 仍属于实验 D-Bus 接口。CW12EU-T 同时提供经典蓝牙与 BLE，默认连接可能错误地选择经典蓝牙，因此需要为 `bluetoothd` 持久增加 `--experimental`。

推荐使用项目脚本：

```bash
cd /home/kscii/Codes/imu-data-collector
sudo ./scripts/install-bluetooth-experimental.sh
```

脚本把 [bluetooth-experimental.conf](../configs/systemd/bluetooth-experimental.conf) 安装到：

```text
/etc/systemd/system/bluetooth.service.d/experimental.conf
```

drop-in 中第一条空的 `ExecStart=` 是 systemd 覆盖列表型设置的必要语法，用于清除软件包服务文件里的旧启动命令；第二条再设置带 `--experimental` 的完整命令。不要直接修改 `/usr/lib/systemd/system/bluetooth.service`，否则软件包升级可能覆盖修改。

首次修复当前样机的错误经典蓝牙记录并验证 LE 偏好：

```bash
bluetoothctl remove 83:FC:90:14:1E:A4
uv run imu-collector probe-gatt
uv run imu-collector probe-imu --seconds 15
bluetoothctl info 83:FC:90:14:1E:A4
```

项目连接代码会先从 BlueZ ObjectManager 查找当前地址的缓存设备路径：若设备已经连接或已经被
BlueZ 发现，直接使用该路径附着 GATT，不再要求设备重新广播；只有路径不存在时才启动扫描。
随后仍显式设置 `PreferredBearer=le` 并调用 `Bearer.LE1.Connect()`。这可以接管应用异常退出后
BlueZ 暂时保留的 LE 连接，也减少日常重复扫描，但不等于接受只有传输层 `Connected` 的假阳性；
WebUI 仍需通知订阅与真实样本到达。正常停止后，`bluetoothctl info` 应显示
`PreferredBearer: le`，且 `BREDR.Paired`、`BREDR.Bonded` 均为 `no`。

安装会重启蓝牙服务，正在使用的耳机、手柄等会断开，需要重新连接。验证实际生效：

```bash
systemctl show bluetooth.service -p ExecStart -p ActiveState -p SubState
```

需要回滚时运行：

```bash
sudo ./scripts/uninstall-bluetooth-experimental.sh
```

## 采集前

1. 确认参与者 UniKey、知情同意、安全人员和动作清单。
2. 固定 IMU 胸前方向，记录设备与佩戴位置，不在会话中途调整。
3. 手机 nRF Connect 断开 CW12EU-T，避免 BLE 单连接设备被占用。
4. `imu-collector doctor` 检查 BlueZ、摄像头、FFmpeg、磁盘和 rclone。
5. 检查镜头范围和背景隐私；v1 不录音。
6. 开始后做一次明显同步动作，结束前再做一次。

摄像头启用自动曝光时，画面近乎全黑、镜头被遮挡或光线不足可能使真实输入降到约
15 FPS，即使 UVC profile 宣称 30 FPS。当前罗技 C930c 配置会在每次启动 FFmpeg 前写入并
回读手动曝光、曝光时长、增益、动态帧率和电源频率。若控件不支持、回读不一致或正式录制前
最近窗口的摄像头输入低于 29 FPS，`prod` 预检会拒绝开始；整段视频按 PTS 低于 27 FPS 也会
在收尾质量门禁中失败。平台不通过复制画面伪装成 30 FPS。

WebUI 的“摄像头输入 FPS”来自 FFmpeg 对 V4L2 源帧 PTS 的滚动统计，正常目标约 30；
“浏览器预览 FPS”是为了降低 JPEG 编码、网络和绘图开销而主动限制的输出，正常约 10。
因此预览显示 10 不表示 H.264 落盘只有 10。两者必须分开判断。

IMU 包时间最大拟合残差按以下级别解释：

- `≤ 200 ms`：正常；
- `> 200 ms` 且 `≤ 500 ms`：显示“质量警告（允许发布）”，保留在目录和发布记录中；
- `> 500 ms`：阻止发布，录制进入 `needs_attention`；
- 无论最大残差是多少，拟合 RMS `> 100 ms`、通知间隔 `> 1.5 s`、回调丢包、解析失败仍会
  阻止发布。

服务启动时会自动复检尚未上传的 `needs_attention` 录制，使旧版 200 ms 门禁产生的误判按
当前规则恢复；缺少 H5/MKV、已经上传或含其他操作错误的记录不会被自动放行。

## 设备预览与录制切换

日常流程只需连接一次设备：

1. 点击“连接预览设备”，等待摄像头画面与 IMU 最近一包时间都开始更新。
2. 点击“开始录制”。BLE 不会断开或重新枚举 GATT；只有摄像头 FFmpeg 在预览与落盘模式间
   切换，页面会短暂显示“摄像头正在切换”，并保留上一帧。
3. 点击“结束录制”。IMU 曲线和同一 BLE 通知会话继续运行，摄像头切回不落盘预览；不应再
   依靠 Ctrl+R 恢复画面。
4. 当天不再采集时才点击“释放预览设备”。

后端保证同一时刻最多一个 BLE/GATT 打开操作。若重复点击或旧请求还在清理，界面会提示
“已有设备连接操作正在进行”，等待当前操作完成即可，不能同时另开前台命令或手机连接。
预览中的意外 IMU 或摄像头故障分别按 1、2、4 秒自动重试三次；三次失败后健康的另一设备
继续运行，使用“重试失败设备”手动恢复。系统托盘显示 Bluetooth `Connected` 只证明 LE
链路存在；WebUI 还要求 GATT 服务发现、通知订阅和真实通知包持续到达，二者不能混为一谈。

每次录制还必须在开始前选择数据级别：

- `test`：软件、设备或流程验收数据，永久禁止进入训练集；默认使用这一档。
- `prod`：正式采集意图，但只有后续全部质量门禁通过后才可能进入训练冻结流程。

数据级别写入 H5 和本地 catalog，不能依靠批次名中的字符串推断，也不在普通界面提供
录制后的修改入口。缺少该字段的本地旧条目统一按安全的 `test` 处理；系统不保留另一种
legacy 业务状态。

当前允许的参与者配置在 `configs/default.yaml` 的 `identity.allowed_unikeys`。本机采集 WebUI
使用下拉框减少录入错误。云端标注平台不允许手动切换操作者；Google IAP 登录邮箱必须同时
存在于服务器私有 `identity.email_to_unikey` 映射，得到的 UniKey 还必须在允许名单中。

摄像头使用 udev 的序列号与 USB interface 组成稳定 `camera_id`。以当前内置摄像头为例，
WebUI 应选择彩色 1080p30 MJPEG 主节点，而不是同一设备的灰度或 metadata 辅助节点。
`/dev/videoN` 只作为本次启动时解析出的实际路径，不作为长期身份。

## 录制中 WebUI 应关注

- 视频是否连续且构图正确。
- 摄像头输入 FPS 和浏览器预览 FPS；前者用于判断采集质量，后者约 10 属于设计值。
- 固定曝光控件是否显示已应用；任何不支持或回读不一致都要先解决再录 `prod`。
- IMU 六轴曲线是否随动作变化、最后通知时间是否持续更新。
- BLE 包数/样本数、回调丢弃数、磁盘剩余空间、编码器错误。
- 任何设备滑动、重新佩戴或异常都应在会话备注中记录。

第一次联合技术验收使用 `participant_id=xfan0282`、
`collection_id=xfan0282_test_01`、`data_tier=test`，具体时序与验收记录见
[联合试采 test-01](joint-pilot-test-01.md)。

## 同步锚点操作

标注页同时显示视频和 IMU 曲线。正式流程使用录制开头和结尾各一次外壳轻拍：

1. 把视频逐帧停在手首次接触 IMU 外壳的画面，不选已经压稳或开始离开的帧。
2. 在附近 IMU 曲线上选同一轻拍的首个明显响应，不选振幅最大的后续峰。
3. 分别保存 `start_tap` 和 `end_tap`，核对界面显示的首尾偏移与不一致量。
4. 若建议“保留主机时间”，直接保存；若建议“应用固定偏移”，必须人工确认；若要求重选，
   返回检查两个锚点，不能为通过门禁强行选择别的峰。

正式同步的 `scale` 永远为 1。只有 `quality=verified` 才允许标注定稿；它只表示当前时间对齐
满足阈值，不等于传感器尺度已经校准。完整阈值和数据字段见
[正式同步与标注合同](annotation-and-sync.md)。

## 标注语义

- 草稿允许未标注空洞；定稿时完整时间轴必须恰好被 `fall`、`non_fall` 或显式排除覆盖，
  不能重叠，也不能留下默认含义不明的空白。
- `fall` 从首次明确失去平衡开始，到撞击后身体姿态稳定结束；`onset` 由区间起点自动派生，
  每个跌倒区间仍必须通过视频人工确认一个严格位于区间内的 `impact`。
- `non_fall` 包含正常日常活动和不构成跌倒的其他动作；同步轻拍不是日常动作，必须排除。
- 轻拍、摘戴设备、重新调整、质量异常、含义模糊和隐私片段使用带原因的显式排除区间。
- 时间区间使用半开区间 `[start_ns, end_ns)`，避免相邻片段重复一个样本。
- 每次保存增加 revision；相邻 `review.json` 是当前同步、标注和负责人快照，原始 H5/MKV 不再修改。

## 手动发布与训练导出边界

采集端“记录与发布”先显示预计读取/上传量，再生成无重编码 MP4 浏览代理并手动发布 H5、
原始 MKV、代理 MP4，最后写 manifest。完成后也不会自动删除本地原件。采集端不再提供同步、
标注或训练导出。

标注端独立完成同步和标注；完成操作通过门禁后自动生成不可变 `aligned30.h5`。任何 `test`
数据或真实校准缺失都会被硬门禁拒绝。当前 GCS 客户端使用 SDK 的可续传上传、对象写入前置
条件、大小与 SHA-256 校验；
网络中断不能删除本地原件。后台队列、自动重试和进度条后续实现。

## 中断与恢复

- 正常录制先写 `.partial.h5` 与 `.partial.mkv`，完整收尾和验证后才改为最终文件名。
- 记录页可扫描 `.partial.*`、空媒体及临时打包文件，并把经确认的对象移动到
  `_quarantine/`；当前版本不尝试通用修复。
- catalog 可从最终 H5 目录重建；隔离目录不会被重新导入。
- 如果 H5 与 MKV 只剩一个，或哈希不一致，该记录标为 `needs_attention`，禁止进入冻结训练集。
- 原始文件的修复、解析升级和训练派生都必须保留来源 SHA-256 与软件版本。
