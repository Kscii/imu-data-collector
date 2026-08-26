# 采集、标注、上传与恢复

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

项目连接代码会在每次重连时显式设置 `PreferredBearer=le` 并调用 `Bearer.LE1.Connect()`。正常停止后，`bluetoothctl info` 应显示 `PreferredBearer: le`，且 `BREDR.Paired`、`BREDR.Bonded` 均为 `no`。

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

内置摄像头在画面近乎全黑、镜头被遮挡或光线不足时，可能因自动曝光把真实输入降到
约 10 FPS，即使 UVC profile 宣称 30 FPS。开始正式试采前必须确认预览曝光正常，并以
WebUI/离线 PTS 的实际 FPS 为准；平台不通过复制画面伪装成 30 FPS。

每次录制还必须在开始前选择数据级别：

- `test`：软件、设备或流程验收数据，永久禁止进入训练集；默认使用这一档。
- `prod`：正式采集意图，但只有后续全部质量门禁通过后才可能进入训练冻结流程。

数据级别写入 H5 和本地 catalog，不能依靠批次名中的字符串推断，也不在普通界面提供
录制后的修改入口。旧版缺少该字段的数据按 `legacy_unclassified` 处理并禁止训练。

当前允许的参与者配置在 `configs/default.yaml` 的 `identity.allowed_unikeys`。本机采集 WebUI
使用下拉框减少录入错误。云端标注平台不允许手动切换操作者；Google IAP 登录邮箱必须同时
存在于服务器私有 `identity.email_to_unikey` 映射，得到的 UniKey 还必须在允许名单中。

摄像头使用 udev 的序列号与 USB interface 组成稳定 `camera_id`。以当前内置摄像头为例，
WebUI 应选择彩色 1080p30 MJPEG 主节点，而不是同一设备的灰度或 metadata 辅助节点。
`/dev/videoN` 只作为本次启动时解析出的实际路径，不作为长期身份。

## 录制中 WebUI 应关注

- 视频是否连续且构图正确。
- 实际编码 FPS，而不是只看请求 FPS。
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
- 每次保存增加 revision；相邻 `review.json` 是当前同步、标注和审核快照，原始 H5/MKV 不再修改。

## 手动发布与训练导出边界

采集端“记录与发布”先显示预计读取/上传量，再生成无重编码 MP4 浏览代理并手动发布 H5、
原始 MKV、代理 MP4，最后写 manifest。完成后也不会自动删除本地原件。采集端不再提供同步、
标注、审核或训练导出。

标注端独立完成同步、标注、审核和 `aligned30.h5`。任何 `test` 数据或真实校准缺失都会被硬
门禁拒绝。当前 GCS 客户端使用 SDK 的可续传上传、对象写入前置条件、大小与 SHA-256 校验；
网络中断不能删除本地原件。后台队列、自动重试和进度条后续实现。

## 中断与恢复

- 正常录制先写 `.partial.h5` 与 `.partial.mkv`，完整收尾和验证后才改为最终文件名。
- 记录页可扫描 `.partial.*`、空媒体及临时打包文件，并把经确认的对象移动到
  `_quarantine/`；当前版本不尝试通用修复。
- catalog 可从最终 H5 目录重建；隔离目录不会被重新导入。
- 如果 H5 与 MKV 只剩一个，或哈希不一致，该记录标为 `needs_attention`，禁止进入冻结训练集。
- 原始文件的修复、解析升级和训练派生都必须保留来源 SHA-256 与软件版本。
