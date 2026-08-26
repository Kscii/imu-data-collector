# CW12EU-T IMU 数据采集平台

这是一个面向胸前佩戴 CW12EU-T 的数据采集与标注项目。第一版只支持原生 Linux（当前采集环境为 Arch Linux）。同一仓库提供两个独立应用：本机采集端连接 BLE IMU 与 UVC 摄像头，云端标注端只读取已发布制品，不初始化任何采集硬件。HDF5 保留原始数据和实际时间戳，Matroska（MKV）保留 H.264 原始视频；同步、标注和审核快照单独保存在对象存储的 `review.json`。

项目采用 **时间戳优先** 原则：录制时不把 IMU 或视频强制改造成 30 Hz。原始 IMU 接收时间和视频逐帧 PTS 会被保留；交付 MKV 仅无损重封装为从零开始的浏览器媒体时间轴，H5 同时保存媒体时间和主机单调时钟时间。完成同步与标注、冻结数据集时，才把每个已标注 IMU 片段重采样为严格的 30 Hz 训练输入。视频是标注与审计证据，不生成伪造的“严格 30 fps”训练视频。

## 当前边界

- 设备：CW12EU-T，当前样机 BLE 地址 `83:FC:90:14:1E:A4`；电脑端已验证六轴候选数据从通知特征 `0x2AE1` 到达。
- 传感器语义：当前只确认每个候选帧含 6 个 `int16` 和 4 个尾部字节；轴顺序、正负方向、量程和比例仍需实验验证。
- 采样率：供应商口头信息为 30 Hz，但当前样机短测为每秒一包、每包 25 帧，更符合 25 Hz；仍需至少 30 分钟长测确认。
- 摄像头：UVC 摄像头，默认请求 MJPEG 1920×1080、30 fps；保存 H.264、约 6 Mbit/s、无音频；同时存在兼容的内置与外接相机时默认优先外接相机。
- 产物：一次佩戴录制对应本地同名 `.h5`/`.mkv`；手动发布时增加可重建 `preview.mp4` 和不可变 `manifest.json`。标注端另写当前 `review.json` 快照，原始制品不再因同步或标注而修改。
- 身份：参与者、操作者与标注者共用配置白名单；当前为 9 个已确认 UniKey，WebUI 使用下拉框，后端再次强制校验。
- 数据级别：录制开始时显式选择 `test` 或 `prod`，安全默认值为 `test`；任何 `test` 或旧版未分类数据都永久禁止进入训练集，`prod` 也仍需通过全部质量门禁。
- 存储：原始媒体不进 Git/Git LFS；采集端支持本地硬链接交接和 GCS 手动发布，标注端从 `gs://soft3888-label` 索引 manifest。凭据只来自 ADC 或 VM 服务账号，不写入仓库。

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

标注端是另一套进程和前端。本机开发可运行 `uv run imu-annotation start`；云端通过 SSH 隧道
访问 `http://127.0.0.1:8766`，具体见 [GCP 标注服务部署与访问](docs/gcp-deployment.md)。采集端
的“记录与发布”只负责手动交接，标注、同步、审核、aligned30 和训练发布不再出现在采集页。

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
- `review.json` 使用对象 generation 与 revision 双重乐观锁；原始 H5/MKV 的 SHA-256 写入 manifest，并在索引、缓存和导出前复核。
- 原始字节、原始计数和时间戳不可被重采样结果覆盖。
- 不根据目录名或文件名推断训练资格；H5 根属性 `data_tier` 是用途分级的事实来源，普通界面不允许录制后修改。
- 未验证比例时，SI 单位数组保持 `NaN`；代码不会猜测量程。
- 正式 30 Hz 导出同时要求 `prod`、同步 verified、标注定稿、审核策略完成、源哈希不变以及加速度计/陀螺仪尺度均 verified；`single_user` 完成即 accepted，`two_person` 要求异人审核。当前真实陀螺仪校准为空，因此门禁会按设计拒绝正式导出。
- 跌倒起始由 fall 区间起点自动派生；每个跌倒区间必须单独人工标记一个位于区间内部的撞击时刻。
- 九名白名单成员可以永久删除尚未进入训练发布的录制，但必须二次输入 `DELETE <recording_id>`；一旦发布，或存在无法核对内容的旧发布 TAR，就拒绝删除。永久删除没有回收站。
- 人体跌倒采集必须另行制定安全、知情同意与隐私流程；本软件不替代该流程。
