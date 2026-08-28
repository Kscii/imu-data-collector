# 桌面跨平台采集

## 支持矩阵

| 平台 | BLE | 摄像头 | 安装形式 | 当前结论 |
| --- | --- | --- | --- | --- |
| Arch/Linux | Bleak + BlueZ LE | FFmpeg + V4L2 | `uv` 或 systemd 用户服务 | 已验收生产基线 |
| Windows 10/11 x64 | Bleak + WinRT | FFmpeg + DirectShow | PyInstaller onedir + Inno Setup | Windows 10 已验收真实 BLE、摄像头、短录制、登录和上传；Windows 11 与长时故障恢复待验收 |
| macOS 13+ Intel / Apple Silicon | Bleak + CoreBluetooth | FFmpeg + AVFoundation | 原生分架构 `.app` + DMG | CI 构建路径已实现；Intel 真机完整验收待完成，不宣称已生产可用 |
| WSL2 | 不承诺 | 不承诺 | 无 | 暂不支持；不要把 USB/BLE 透传当作原生 Windows |
| Android | 不适用当前桌面包 | 不适用 | 无 | 暂不属于采集端范围 |

Windows 10 与 Windows 11 共用 WinRT、DirectShow 和 x64 安装包代码路径；“预期兼容”不等于
已经在 Windows 11 真机验收。macOS CI 只证明代码可导入和构建，不能替代摄像头、BLE、权限
弹窗和完整录制验收。当前 macOS 包采用 ad-hoc 签名且不做 Apple 公证，这是小组测试版的明确
限制，不等同于正式对外分发。

## 不变的数据原则

各平台只替换设备接入层，不改变数据语义：

- Bleak 回调进入进程时立即读取 Python `monotonic_ns()`；原始通知、约 25 Hz 样本和接收时间不重采样。
- 视频保留 FFmpeg 报告的真实逐帧 PTS；Windows/macOS 把第一帧源 PTS 映射到本次 FFmpeg 启动时的主机单调时钟，Linux 继续保存 V4L2 的单调 PTS。
- 严格 25 Hz 只在同步、标注完成后的 `aligned.h5` 中派生，不能覆盖原始 H5。
- 一次录制仍是同名 H5/MKV 原子文件对，不因操作系统改变目录层级或 manifest 2.1 合同。

capture H5 schema 1.6 新增或冻结以下运行时事实：

- 根属性：`host_os`、`host_os_version`、`host_architecture`、`monotonic_implementation`、`clock_domain`；
- `imu` 属性：`ble_backend`、`local_device_id`；
- `video` 属性：`video_backend`、`timestamp_mapping`、`camera_control_policy`。

标注端同时接受 1.5 与 1.6。1.5 是只读兼容输入；新采集必须写 1.6，不能把旧文件静默改版本。

## 摄像头策略

平台枚举系统后端公开的所有模式，筛选实际声明达到目标 30 FPS 的模式，然后选择像素数最高的
分辨率，不设置最低分辨率。若没有任何 30 FPS 模式，摄像头显示为不可用于正式采集，不通过
复制帧伪造 30 FPS。

- Linux：优先 MJPEG，使用 V4L2 设置并读回手动曝光；`camera_control_policy=fixed_verified`。
- Windows：使用 DirectShow；摄像头厂商与驱动不统一，第一版不伪装成已经锁定曝光，使用
  `camera_control_policy=observed_fps_gate`，由录前滚动 FPS 和整段 PTS 门禁判断。
- macOS：通过 PyObjC 读取 AVFoundation 的稳定 `uniqueID` 与真实支持格式，再映射到本次
  FFmpeg 枚举索引；按实际 FPS/PTS 门禁。录制启动时实测 `h264_videotoolbox`，硬编码不可用
  则回退随包 `libx264`，不把编码器存在误当作硬件可用。

浏览器预览主动限制约 10 FPS，只降低 JPEG 和页面绘图负担；MKV 输入目标仍是 30 FPS。

## Windows 构建与使用

GitHub tag `desktop-vX.Y.Z` 会：

1. 运行 Windows 完整测试并构建采集端 WebUI；
2. 下载固定版本的 BtbN GPL FFmpeg 并核对 SHA-256；
3. 用锁定的 Python 3.12 与 PyInstaller 生成 onedir；
4. 用 Inno Setup 生成当前用户安装器；
5. 静默安装并执行打包 CLI 冒烟；
6. 发布带标签版本号的未签名单安装器 EXE 与 `SHA256SUMS.txt`，并更新 GitHub Latest Release。

手动运行工作流只生成供开发者下载的 Actions artifact，不会替换组员使用的 Latest Release。
稳定标签必须同时通过 Windows、macOS arm64 和 macOS x86_64 三套构建，才发布同一个 Latest
Release。这里的“单安装器”表示 Windows 组员
只需下载和双击一个 EXE；安装后 Python、HDF5、WebUI 和 FFmpeg 仍以 onedir 形式存放，避免
onefile 每次启动重新解压大型运行时。

未签名试用包会触发 Windows SmartScreen，适合小组试点，不适合公开分发。安装后双击桌面或
开始菜单的“IMU 数采平台”即可启动无控制台托盘程序：它只允许一个实例，自动打开浏览器，
关闭浏览器不会断开设备；双击托盘图标可重新打开页面，右键“退出”才会优雅释放设备。录制
进行中时托盘会拒绝退出，必须先在 WebUI 结束录制。

所有 FFmpeg、ffprobe 与 rclone 后台子进程在 Windows 下均使用无窗口模式；摄像头列表在后端
会话内缓存，只有首次使用或人工点击“重新扫描摄像头”才重新运行完整设备探测。诊断入口仍
保留为独立控制台程序：

```powershell
imu-collector doctor
imu-collector devices
```

托盘后端仍只监听 `127.0.0.1:8765`，不安装 Windows Service、不随登录自启，也不自动更新。
托盘启动失败会弹出错误，并把详细信息写入用户缓存目录的 `logs/tray.log`。开发者仍可运行
`imu-collector start` 使用原有控制台生命周期。

## macOS 构建边界

GitHub Actions 在 `macos-15` 原生 arm64 Runner 和 `macos-15-intel` 原生 x86_64 Runner 分别
构建，不制作 Rosetta 通用包。两套构建都从锁定源码归档编译 FFmpeg n8.1.2 与 x264，将
SHA-256、架构、编码器、动态链接、Info.plist、ad-hoc 签名和 DMG 挂载作为自动门禁。应用最低
系统版本为 macOS 13，不启用 App Sandbox，不申请麦克风权限，也不随登录自启或自动更新。

CoreBluetooth 不公开 MAC 地址。首次连接按精确名称扫描；同名设备唯一时，只有成功订阅
`0x2AE1` 并收到通知后才保存该 Mac 的 CoreBluetooth UUID。发现多个同名设备时必须人工选择；
后续优先按保存 UUID 连接，失效则退回扫描。WebUI 可忘记绑定，绑定文件位于 Application
Support，不写入 H5 的物理设备校准身份。详细安装与真机清单见
[macOS 桌面测试版](macos-desktop.md)。

## 2026-08-28 Windows 10 实测边界

局域网主机 `Windows 10 x64 build 19045` 已完成：

- Python 3.12 锁定依赖安装；
- 138 项 Python 测试与 Ruff；
- React 采集前端构建；
- PyInstaller onedir 构建；
- 打包 EXE 的 `doctor`，并确认 FFmpeg/FFprobe 来自安装包内部；
- 使用全新数据目录启动打包 EXE，确认目录自动创建，`/api/v1/health` 与 `/api/v1/cloud/status` 均返回 200；
- 无控制台托盘 EXE 进入真实图形 Session 1，健康接口返回 200；从同一会话再次启动仍只有一个托盘进程；
- DirectShow 设备枚举。

托盘右键菜单和人工点击退出仍需在 Windows 桌面上完成一次观察验收；进程生命周期与“录制中
拒绝退出”已有自动化测试，不能用它替代真实点击确认。

测试机随后已枚举罗技 C930c 的 MJPEG 1920×1080、30 FPS 模式，并在托盘后端观察到约 30 FPS
输入；仍需用新安装器复核完整录制。同一时刻 Linux 可持续扫描到 CW12EU-T（RSSI 约
-53～-65 dBm），Windows 的 Realtek/WinRT 主动扫描却没有返回该广播。按固定 public address
构造 WinRT 设备并关闭 GATT 服务缓存后，Windows 已成功取得 4 个服务并订阅 `0x2AE1`：8 秒
收到 10 个通知包，包长为 400 或 10 字节。400 字节通知可继续按 25 个 16 字节原始帧解析。

因此 Windows 正式路径不依赖扫描结果：当前固定样机优先使用 public address 直连，扫描只用
于人工诊断。设备从短探针断开后可能退出可连接状态；重新按键进入匹配即可开始新的长连接。
默认 WinRT 服务缓存会使 `get_gatt_services_async()` 超时，不能重新启用。

## BLE 实机验收顺序

CW12EU-T 是单连接设备。Windows 测试前必须先在手机与 Arch 上停止通知并断开设备，然后：

1. 关闭手机自动重连，停止 Arch 预览，把当前固定样机重新上电并进入匹配状态；
2. 在 WebUI 连接设备；正式连接会绕过 WinRT 扫描，直接使用固定 public address；
3. 检查 GATT 服务发现、`0x2AE1` 通知与约 25 Hz 样本；主动扫描结果仅供诊断，不能作为
   Windows 是否可连接当前样机的门禁；
4. 若 WinRT 明确报告认证或访问拒绝，再在 Windows 蓝牙设置完成一次系统配对；
5. 录制 test 数据，检查 H5 的 WinRT 后端、主机时钟属性、原始包、视频 PTS 与收尾；
6. 断电、超距、后端强制退出各做一次，确认不会产生两个并行 BLE 客户端。

不能让 Arch、手机和 Windows 同时抢连来测试“稳定性”，否则得到的是单连接冲突而非平台故障。
