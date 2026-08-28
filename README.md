# CW12EU-T IMU 数据采集平台

这是一个面向胸前佩戴 CW12EU-T 的数据采集与标注项目。采集端以原生 Linux 为已验收生产基线，原生 Windows 10/11 x64 已具备独立安装包和系统后端适配，macOS 提供尚待真机与签名验收的实验包；WSL2 与 Android 暂不属于采集端支持范围。同一仓库提供两个独立应用：本机采集端连接 BLE IMU 与摄像头，云端标注端只读取已发布制品，不初始化任何采集硬件。HDF5 保留原始数据和实际时间戳，Matroska（MKV）保留 H.264 原始视频；同步、标注、负责人和当前有效导出单独保存在对象存储的 `review.json`。

项目采用 **时间戳优先** 原则：录制时不把 IMU 或视频强制改造成 30 Hz。原始 IMU 接收时间和视频逐帧 PTS 会被保留；交付 MKV 仅无损重封装为从零开始的浏览器媒体时间轴，H5 同时保存媒体时间和主机单调时钟时间。完成同步与标注、冻结数据集时，才把每个已标注 IMU 片段重采样为严格的 30 Hz 训练输入。视频是标注与审计证据，不生成伪造的“严格 30 fps”训练视频。

## 当前边界

- 设备：CW12EU-T，当前样机 BLE 地址 `83:FC:90:14:1E:A4`；电脑端已验证六轴候选数据从通知特征 `0x2AE1` 到达。
- 传感器语义：当前样机已用六面静止和八次三轴旋转冻结工程校准档案；原始列映射为 `[raw_ax, -raw_ay, raw_az]`，加速度为 4096 counts/g、陀螺仪为 32.8 counts/(°/s)。4-byte trailer 仍保持未知并原样保存。
- 采样率：供应商口头信息为 30 Hz，但当前样机多次短测和两次 10 分钟静态均稳定在约 25 Hz；平台按 25 Hz 保存实际时间戳，不把原始 IMU 伪造成 30 Hz。
- 摄像头：Linux 罗技 C930c 已验收 MJPEG 1920×1080、30 fps，固定手动曝光后实测输入约 30 FPS；Windows/macOS 从系统后端选择能够达到 30 FPS 的最高分辨率，并以实际 FPS/PTS 门禁代替不可移植的 UVC 控件假设。浏览器预览独立限为约 10 FPS。保存 H.264、约 6 Mbit/s、无音频；同时存在兼容的内置与外接相机时默认优先外接相机。
- 产物：一次佩戴录制对应本地同名 `.h5`/`.mkv`；后台发布时增加可重建 `preview.mp4` 和不可变 `manifest.json`。标注端另写当前 `review.json` 快照，原始制品不再因同步或标注而修改。
- 身份：本机采集端从 9 个 UniKey 白名单选择参与者；公网标注端由 Google IAP 验证登录账号，
  再由服务器私有邮箱映射得到唯一 UniKey。操作者身份不能由浏览器请求体自报。
- 数据级别：录制开始时显式选择 `test` 或 `prod`，安全默认值为 `test`；缺失级别的旧条目
  也按 `test` 处理，`prod` 仍需通过全部质量门禁。
- 存储：原始媒体不进 Git/Git LFS；Linux 管理机保留 ADC/GCS 发布能力，团队桌面安装包改由 Google Desktop OAuth + PKCE 登录，经独立上传代理完成 token exchange 并获得单录制 GCS 可恢复上传会话。安装包不含 client secret；refresh token 只进入操作系统凭据库，OAuth secret 与 GCS 服务账号只存在于代理 VM；标注端从 `gs://soft3888-label` 索引 manifest。
- 发布状态：上传成功只表示制品已经进入 Bucket；标注端校验完成后会另写索引回执。采集页分别
  展示“已上传 Bucket”“标注端已接收”和“标注端拒绝”，拒绝时保留具体错误码与原因。

完整事实、架构和操作说明见 [docs/](docs/)，当前工作清单见 [TODO.md](TODO.md)。
本轮冻结的数据生命周期、三态任务、30 Hz 派生和服务器配置边界见
[数据生命周期与服务器配置合同](docs/data-lifecycle.md)。
桌面系统的支持状态、安装包与真机验收边界见
[桌面跨平台采集](docs/desktop-platforms.md)；无云密钥桌面上传见
[桌面 OAuth 与上传代理](docs/desktop-upload.md)。

Windows 安装包提供两个入口：普通用户双击“IMU 数采平台”启动系统托盘并自动打开浏览器；
关闭浏览器不退出后端，双击托盘图标可重新打开，右键“退出”才释放设备。`imu-collector.exe`
继续作为 `doctor`、`devices` 等诊断命令的控制台入口。托盘不会随登录自启，录制进行中也会
阻止误退出。组员从 GitHub Latest Release 下载单个未签名安装器 EXE；稳定版只由
`desktop-vX.Y.Z` 标签产生，普通 `main` 提交不会自动替换组员使用的版本。

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
实例或打断进行中的录制。推荐首次安装按需使用的用户级 systemd 服务：

```bash
./scripts/install-user-service.sh
systemctl --user restart imu-data-collector.service
```

日常仅重启已经安装且前后端版本一致的服务时，可重复第二条命令；修改或拉取代码后必须运行：

```bash
./scripts/update-local-capture.sh
```

该脚本会在确认当前没有录制/收尾后安装锁定依赖、暂存构建前端、校验前后端版本号、原子替换
静态资源、重启服务并等待健康检查。单独执行 `systemctl restart` 不会重新构建前端，因此不能
修复由旧 `dist-capture` 引起的版本不一致。查看状态使用
`systemctl --user status imu-data-collector.service --no-pager`。该单元默认不启用开机自启，
但配置文件会持久保留，因此电脑重启后同一条 `restart` 命令仍可启动服务。WebUI 会比较
前后端源码哈希，发现不一致时会阻止设备操作并明确提示运行一键更新脚本，避免旧页面控制新
状态机或新页面控制旧后端。

当前不默认开机常驻：本项目仅供单人本机使用，按需启动可以避免后台长期占用摄像头、BLE、
IMU 电量和隐私敏感画面。systemd 用户服务只负责提供稳定的启动、重启和日志入口；没有执行
`enable` 时不会自动随登录启动，也不把“启动后端”按钮放进尚未加载的 WebUI。

标注端是另一套进程和前端。本机开发可运行 `uv run imu-annotation start`；生产入口为
`https://imu.kscii.tech`，由 Google IAP 登录和服务器私有白名单双重限制，具体见
[GCP 标注服务部署与访问](docs/gcp-deployment.md)。采集端
的“记录与发布”只负责查看后台收尾/上传、人工重试和测试数据手动交接；标注、同步、aligned30
和训练快照不出现在采集页。新 `prod` 录制通过收尾门禁后自动加入上传队列，`test` 仍需人工发布。

日常正式同步固定在录制开始和结束各轻拍一次外壳。标注页把视频首接触帧与 IMU 首个明显
响应配对；默认相信共同主机单调时钟，只在首尾锚点一致且平均偏移绝对值达到 100 ms 时，
经人工确认应用一个 `scale=1` 的固定偏移。详细合同见
[正式同步与标注合同](docs/annotation-and-sync.md)。

早期同步方法验证录制已在流程确定后按用户确认清理，不再作为生产证据或普通标注任务展示。
生产流程只使用每条录制首尾两次轻拍和共同主机时间；历史方法与阈值设计仍可查阅
[同步验证协议](docs/sync-validation-protocol.md)，但不得据此虚构已删除实验的观察结果。

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
当前冻结坐标、换算公式与证据边界见
[CW12EU-T 坐标系与工程校准档案](docs/device-coordinate-system.md)。
开始第一条正式数据前，应逐项执行
[每场次正式采集检查清单](docs/pre-collection-checklist.md)。
生产部署后的代码、服务器和人工业务验收见
[生产验收与监控](docs/production-acceptance.md)。首次归档校准证据前还要逐项核对
[2026-08-27 首次生产数据迁移清单](docs/production-data-migration-2026-08-27.md)。

配置文件为 [configs/default.yaml](configs/default.yaml)。也可用 `IMU_COLLECTOR_CONFIG` 指向服务器私有 YAML，或用 `IMU_COLLECTOR_DATA_ROOT` 临时覆盖落盘根目录。结构化配置与用户角色放 YAML，真正的秘密只放私有 env；两者都不提交真实值。

## 数据安全

- `.partial.h5` / `.partial.mkv` 表示录制中断或尚未完整收尾，不能当作可训练数据。
- 停止录制只冻结并关闭 `.partial.*`、持久化收尾任务并恢复设备预览；视频探测、重封装、H5
  校验和发布在单一低优先级后台 worker 中串行执行。首次失败后按 5/30/120 秒自动重试，耗尽
  四次尝试后由“记录与发布”页面人工选择“重新收尾”或“重新上传”。
- `review.json` 使用对象 generation 与 revision 双重乐观锁；所有公开写接口必须携带预期
  revision。原始 H5/MKV/MP4 的 SHA-256 必须写入对象 metadata，并在索引、缓存和导出前
  与 manifest 逐一复核；缺少哈希不能降级为只比较大小。
- 原始字节、原始计数和时间戳不可被重采样结果覆盖。
- `imu/packets/packet_kind` 保存每条原始 BLE 通知的类型码：`1` 为 IMU 样本，`2` 为已确认包族
  但字段语义仍未知的 10 字节辅助状态，`255` 为未知无效通知。辅助通知保留原始 payload、样本数
  为 0、不进入时间拟合且不算解析失败；未知通知才会阻断正式发布。
- 不根据目录名或文件名推断训练资格；H5 根属性 `data_tier` 是用途分级的事实来源，普通界面不允许录制后修改。
- 当前样机的新录制会把版本化工程校准 profile、轴映射、零偏、比例与证据 SHA-256 冻结进 H5；原始计数始终另外保留。未知或旧 profile 的 SI 数组仍保持 `NaN`，代码不会静默猜测。
- 正式 30 Hz 导出同时要求 `prod`、同步 verified、标注定稿、源哈希不变以及加速度计/陀螺仪
  尺度均 verified；manifest、H5 冻结属性和服务器校准证据档案必须一致。完成操作自动生成导出。
  导出使用包含 review revision 与
  内容摘要的不可变对象键，`review.json.active_export` 只指向当前有效版本；重开会清除该指针。
- 跌倒起始由 fall 区间起点自动派生；每个跌倒区间必须单独人工标记一个位于区间内部的撞击时刻。
- 九名白名单成员采用同权领取：只有当前负责人可编辑，其他成员可显式接管；完成后负责人或
  管理员可重开。录制删除必须二次输入 `DELETE <recording_id>`，管理员清理历史训练快照必须
  输入 `DELETE <snapshot_id>`；GCS 仍提供 7 天软删除恢复窗口。
- 每日任务清理超过 7 天的无 manifest 中断上传、已失去源 manifest 的索引回执、无当前
  review 引用的旧导出和无有效清单的孤儿训练快照；当前 `active_export` 和完整录制不会被
  自动删除。
- 人体跌倒采集必须另行制定安全、知情同意与隐私流程；本软件不替代该流程。
