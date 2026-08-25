# CW12EU-T IMU 数据采集平台

这是一个面向胸前佩戴 CW12EU-T 的本地数据采集与标注工具。第一版只支持原生 Linux（当前目标环境为 Arch Linux），同时采集 BLE IMU 与 UVC 摄像头视频，在 HDF5 中保留原始数据、实际时间戳和标注，在 Matroska（MKV）中保留 H.264 视频。

项目采用 **时间戳优先** 原则：录制时不把 IMU 或视频强制改造成 30 Hz。原始 IMU 接收时间和视频逐帧 PTS 会被保留；完成同步与标注、冻结数据集时，才把每个已标注 IMU 片段重采样为严格的 30 Hz 训练输入。视频是标注与审计证据，不生成伪造的“严格 30 fps”训练视频。

## 当前边界

- 设备：CW12EU-T，当前样机 BLE 地址 `83:FC:90:14:1E:A4`；电脑端已验证六轴候选数据从通知特征 `0x2AE1` 到达。
- 传感器语义：当前只确认每个候选帧含 6 个 `int16` 和 4 个尾部字节；轴顺序、正负方向、量程和比例仍需实验验证。
- 采样率：供应商口头信息为 30 Hz，但当前样机短测为每秒一包、每包 25 帧，更符合 25 Hz；仍需至少 30 分钟长测确认。
- 摄像头：UVC 摄像头，默认请求 MJPEG 1920×1080、30 fps；保存 H.264、约 6 Mbit/s、无音频。
- 产物：一次佩戴录制对应一对同名 `.h5` 与 `.mkv`，建议 10–30 分钟；一个录制可包含多个互不重叠的标注片段，片段间空白保持未标注。
- 身份：参与者、操作者与标注者共用配置白名单；当前为 9 个已确认 UniKey，WebUI 使用下拉框，后端再次强制校验。
- 上传：原始媒体不进 Git/Git LFS；首选通过 rclone 自动复制到 Google Drive，并在上传后校验。

完整事实、架构和操作说明见 [docs/](docs/)，当前工作清单见 [TODO.md](TODO.md)。

## 开发运行

```bash
uv sync
uv run pytest
uv run ruff check .
cd frontend
npm install
npm run build
cd ..
uv run imu-collector serve
```

然后访问 `http://127.0.0.1:8765`。服务器仅监听本机；除非明确完成隐私和鉴权设计，不应绑定 `0.0.0.0`。

常用只读诊断：

```bash
uv run imu-collector doctor
uv run imu-collector devices
uv run imu-collector probe-gatt
uv run imu-collector probe-imu --seconds 15
uv run imu-collector characterize-imu --operator xfan0282 \
  --stage pipeline_smoke_uncontrolled --seconds 10
uv run imu-collector validate /path/to/recording.h5
```

`characterize-imu` 生成 IMU-only H5 和相邻 JSON 报告，固定落入
`~/IMUData/_diagnostics/`，并写死 `training_eligible=false`。正式姿态表征建议通过
WebUI 的“IMU 表征”页逐阶段操作，详见
[IMU 表征与校准候选](docs/imu-characterization.md)。

配置文件为 [configs/default.yaml](configs/default.yaml)。也可用 `IMU_COLLECTOR_CONFIG` 指向另一个 YAML，或用 `IMU_COLLECTOR_DATA_ROOT` 临时覆盖落盘根目录。

## 数据安全

- `.partial.h5` / `.partial.mkv` 表示录制中断或尚未完整收尾，不能当作可训练数据。
- 标注与同步编辑使用“复制、验证、原子替换”，尽量避免损坏唯一副本。
- 原始字节、原始计数和时间戳不可被重采样结果覆盖。
- 未验证比例时，SI 单位数组保持 `NaN`；代码不会猜测量程。
- 人体跌倒采集必须另行制定安全、知情同意与隐私流程；本软件不替代该流程。
