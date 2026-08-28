# macOS 桌面测试版

## 当前结论

macOS 13+ 测试版提供两个原生 DMG：`arm64` 用于 Apple Silicon，`x86_64` 用于 Intel Mac。
它们共享采集状态机、H5/MKV、同步、质量门禁和上传协议，只替换 CoreBluetooth、AVFoundation
与菜单栏外壳。CI 构建通过不等于真机通过；首台 Intel Mac 必须完成本文的人工验收后，才能
把 Intel 路径标记为可用于正式采集。Apple Silicon 当前只有 CI 构建证据。

## 下载与首次启动

1. 在 GitHub Latest Release 按 CPU 下载对应 DMG；“关于本机”可查看芯片类型。
2. 核对相邻 `SHA256SUMS-macOS-<arch>.txt`。
3. 打开 DMG，把 `IMU Data Collector.app` 拖入“应用程序”。
4. 当前版本使用 ad-hoc 签名且未公证。首次启动不要直接双击：在 Finder 右键应用，选择
   “打开”，再确认系统提示。后续可正常打开。
5. 应用只出现在菜单栏，不显示 Dock 图标，并自动打开 `http://127.0.0.1:8765`。关闭浏览器
   不会退出后端；从菜单栏重新打开页面或退出。

应用不安装守护进程、不随登录启动、不自动更新，也不监听局域网。录制进行中菜单栏会拒绝
退出，必须先在 WebUI 停止录制。

## 权限

应用只在使用对应硬件时触发权限；不申请麦克风权限，视频始终无音频。

- 相机被拒绝：打开“系统设置 → 隐私与安全性 → 相机”，允许 `IMU Data Collector`。
- 蓝牙被拒绝：打开“系统设置 → 隐私与安全性 → 蓝牙”，允许 `IMU Data Collector`。

WebUI 只显示上述准确路径，不自动跳转系统设置。修改权限后应完全退出应用并重新打开。

## 本机目录

- 原始录制：`~/IMUData`
- 设备绑定与本机应用状态：`~/Library/Application Support/imu-data-collector/`
- 日志：`~/Library/Caches/imu-data-collector/logs/tray.log`
- Google refresh token：macOS Keychain；不写入上述目录或日志

菜单栏提供“打开数据目录”和“打开日志目录”。应用包不包含 Google OAuth client secret、GCP
服务账号或 Bucket 长期密钥；登录仍通过既有上传代理。

## 设备策略

### BLE

CW12EU-T 只允许一个中心设备持有连接。测试前先停止 Arch、Windows 和手机 nRF Connect 的
会话。macOS 不使用配置中的 BLE MAC 地址直连，因为 CoreBluetooth 不公开 MAC：

1. 首次按精确名称 `CW12EU-T` 扫描；
2. 多个同名候选必须在 WebUI 选择本机 UUID；
3. 成功订阅 `0x2AE1` 且收到真实通知后才持久绑定；
4. 后续优先按绑定 UUID 连接，UUID 失效则自动回到精确名称扫描；
5. “忘记已绑定 IMU”只清除本机连接提示，不修改设备校准档案或既有录制。

设备预览、开始录制、停止录制和恢复预览共用一个 BLE owner；不得出现第二个 BleakClient 抢占
当前设备。

### 摄像头

平台读取 AVFoundation 的真实格式，只把报告达到 30 FPS 的模式列为兼容，并选择最高分辨率；
同时存在兼容内置与 USB 相机时优先 USB。浏览器预览上限约 10 FPS，原始输入仍目标 30 FPS。
macOS 没有使用 Linux 的 UVC 固定曝光命令，因此 `camera_control_policy=observed_fps_gate`，正式
录制由启动前滚动 FPS 和整段 PTS 门禁决定。

录制启动会用合成帧真实测试 VideoToolbox 硬编码；失败时回退随包从源码构建的 `libx264`。
两条路径都写 H.264 MKV，不改变真实逐帧 PTS。

## Intel 真机首轮验收

使用与 Linux/Windows 相同的 CW12EU-T 和罗技 USB 摄像头，时长由测试者现场决定：

1. 安装、右键首次打开、菜单栏和单实例；再次打开应用只打开同一 WebUI。
2. 首次相机/蓝牙权限允许；分别拒绝一次，确认只显示准确的系统设置路径，恢复权限后可用。
3. 外接摄像头默认优先，页面显示真实模式；预览输入接近 30 FPS、浏览器预览接近 10 FPS。
4. 首次 BLE 扫描、通知和约 25 Hz 曲线；退出重开后按已验证 UUID 快速连接。
5. 预览开始录制时 BLE 不重连；停止后后台收尾且预览恢复，不产生第二 BLE client。
6. 录一条 `test`，检查原始 H5/MKV、主机时钟、CoreBluetooth UUID、AVFoundation backend、
   VideoToolbox 或 x264 实际编码器、逐帧 PTS、收尾和本地下载。
7. 完成 Google 登录、重启应用后复用 Keychain token，并把 test 数据上传到既有代理。
8. 断电、超距和强制退出各测一次；确认错误可恢复、损坏部分不被当作 ready。
9. 录制中从菜单退出被拒绝；空闲退出后 BLE、摄像头和 8765 端口均释放。
10. 把 macOS 浏览器首选语言分别设为中文和英文，确认整个 WebUI 没有混用另一语言。

验收记录必须写明 macOS 版本、CPU、摄像头名称、应用版本、录制 ID、实际编码器和失败边界。
未完成以上项目时，只能称为“可安装测试版”。
