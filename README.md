# CW12EU-T IMU 数据采集平台

这是一个面向胸前佩戴 CW12EU-T 的本地数据采集与标注工具。第一版只支持原生 Linux（当前目标环境为 Arch Linux），同时采集 BLE IMU 与 UVC 摄像头视频，在 HDF5 中保留原始数据和实际时间戳，在 Matroska（MKV）中保留 H.264 视频；同步、标注和审核快照保存在相邻的 `review.json`。

项目采用 **时间戳优先** 原则：录制时不把 IMU 或视频强制改造成 30 Hz。原始 IMU 接收时间和视频逐帧 PTS 会被保留；交付 MKV 仅无损重封装为从零开始的浏览器媒体时间轴，H5 同时保存媒体时间和主机单调时钟时间。完成同步与标注、冻结数据集时，才把每个已标注 IMU 片段重采样为严格的 30 Hz 训练输入。视频是标注与审计证据，不生成伪造的“严格 30 fps”训练视频。

## 当前边界

- 设备：CW12EU-T，当前样机 BLE 地址 `83:FC:90:14:1E:A4`；电脑端已验证六轴候选数据从通知特征 `0x2AE1` 到达。
- 传感器语义：当前只确认每个候选帧含 6 个 `int16` 和 4 个尾部字节；轴顺序、正负方向、量程和比例仍需实验验证。
- 采样率：供应商口头信息为 30 Hz，但当前样机短测为每秒一包、每包 25 帧，更符合 25 Hz；仍需至少 30 分钟长测确认。
- 摄像头：UVC 摄像头，默认请求 MJPEG 1920×1080、30 fps；保存 H.264、约 6 Mbit/s、无音频；同时存在兼容的内置与外接相机时默认优先外接相机。
- 产物：一次佩戴录制对应一对同名 `.h5` 与 `.mkv` 和一个外置 `review.json`；原始文件收尾后不再因同步或标注而修改。草稿允许空白，正式定稿前必须把完整时间轴逐段标为 `fall`、`non_fall` 或显式排除。
- 身份：参与者、操作者与标注者共用配置白名单；当前为 9 个已确认 UniKey，WebUI 使用下拉框，后端再次强制校验。
- 数据级别：录制开始时显式选择 `test` 或 `prod`，安全默认值为 `test`；任何 `test` 或旧版未分类数据都永久禁止进入训练集，`prod` 也仍需通过全部质量门禁。
- 存储：原始媒体不进 Git/Git LFS；当前实现本地文件系统对象存储边界和按需无压缩 TAR。未来服务器使用 GCS，当前版本不配置真实云凭据。

完整事实、架构和操作说明见 [docs/](docs/)，当前工作清单见 [TODO.md](TODO.md)。
本轮冻结的数据生命周期、审核状态、30 Hz 派生和服务器配置边界见
[数据生命周期与服务器配置合同](docs/data-lifecycle.md)。

## 开发运行

```bash
uv sync
uv run pytest
uv run ruff check .
cd frontend
npm install
npm run build
cd ..
uv run imu-collector start
```

`start` 会启动后端，并在健康接口就绪后自动打开 `http://127.0.0.1:8765`。终端保持运行时，
摄像头、BLE 和 WebUI 才可用；按 `Ctrl+C` 停止。若不希望自动打开浏览器，可改用
`uv run imu-collector serve`。服务器仅监听本机；除非明确完成隐私和鉴权设计，不应绑定
`0.0.0.0`。

重复执行 `start` 时，如果本项目后端已经健康运行，它只会打开现有页面，不会启动第二个
实例或打断进行中的录制。开发中修改代码后若要加载新版，仍需先在原服务终端按 `Ctrl+C`，
再运行 `start`。

当前不默认常驻：本项目仅供单人本机使用，按需启动可以避免后台长期占用摄像头、BLE、IMU
电量和隐私敏感画面。未来若确认需要开机常驻，应把 `serve` 包装成可选的 systemd 用户服务，
而不是把“启动后端”按钮放进尚未加载的 WebUI。

日常正式同步固定在录制开始和结束各轻拍一次外壳。标注页把视频首接触帧与 IMU 首个明显
响应配对；默认相信共同主机单调时钟，只在首尾锚点一致且平均偏移绝对值达到 100 ms 时，
经人工确认应用一个 `scale=1` 的固定偏移。详细合同见
[正式同步与标注合同](docs/annotation-and-sync.md)。

历史同步方法验证仍保留在“标注”页中的独立逐帧实验区。具体摆放、录制矩阵和保存步骤见
[同步验证协议](docs/sync-validation-protocol.md)。31 个轻拍观察完成后运行：

```bash
uv run imu-collector analyze-sync-experiment \
  ~/IMUData/_diagnostics/sync_validation_01/sync_validation_01.sync-experiment.json
```

该实验不会调用正式同步写入，也不会修改原始 H5/MKV。

常用只读诊断：

```bash
uv run imu-collector doctor
uv run imu-collector devices
uv run imu-collector probe-gatt
uv run imu-collector probe-imu --seconds 15
uv run imu-collector probe-video --seconds 20 --camera-id '<稳定 camera_id>'
uv run imu-collector characterize-imu --operator xfan0282 \
  --stage pipeline_smoke_uncontrolled --seconds 10
uv run imu-collector validate /path/to/recording.h5
```

`characterize-imu` 生成 IMU-only H5 和相邻 JSON 报告，固定落入
`~/IMUData/_diagnostics/`，并写死 `training_eligible=false`。正式姿态表征建议通过
WebUI 的“IMU 表征”页逐阶段操作，详见
[IMU 表征与校准候选](docs/imu-characterization.md)。

配置文件为 [configs/default.yaml](configs/default.yaml)。也可用 `IMU_COLLECTOR_CONFIG` 指向服务器私有 YAML，或用 `IMU_COLLECTOR_DATA_ROOT` 临时覆盖落盘根目录。结构化配置与用户角色放 YAML，真正的秘密只放私有 env；两者都不提交真实值。

## 数据安全

- `.partial.h5` / `.partial.mkv` 表示录制中断或尚未完整收尾，不能当作可训练数据。
- `review.json` 使用原子替换和 revision 乐观锁；原始 H5/MKV 的 SHA-256 会随 sidecar 保存并在打包、导出前复核。
- 原始字节、原始计数和时间戳不可被重采样结果覆盖。
- 不根据目录名或文件名推断训练资格；H5 根属性 `data_tier` 是用途分级的事实来源，普通界面不允许录制后修改。
- 未验证比例时，SI 单位数组保持 `NaN`；代码不会猜测量程。
- 正式 30 Hz 导出同时要求 `prod`、同步 verified、标注定稿、异人审核 accepted、源哈希不变以及加速度计/陀螺仪尺度均 verified；当前真实陀螺仪校准为空，因此门禁会按设计拒绝正式导出。
- 人体跌倒采集必须另行制定安全、知情同意与隐私流程；本软件不替代该流程。
