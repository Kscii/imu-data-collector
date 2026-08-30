# 实施 TODO

状态约定：`[ ]` 未完成；`[~]` 已实现但还缺真实设备、浏览器或云端验收。已经完成且无需继续
行动的历史不留在 TODO，证据写入对应文档和不可变数据清单。

## 边界

- 本仓库负责 CW12EU-T 的真实 IMU/视频采集、同步、标注、校准门禁、版本化导出和不可变团队
  快照；训练、模型比较与 Android 推理属于其他仓库。
- `test` 录制永久禁止进入训练快照；真实参与者的 H5、视频、OAuth 凭据和私有回放样例不得
  进入 Git。
- TODO 不是删除数据、扩大摄像范围、执行高风险跌倒、推送远程或部署生产的长期授权。
- 设备、浏览器、云端和训练仓库的验收分别记录，不能用自动化测试代替真实链路证据。

## P0：本轮生产闭环

- [ ] 在 Arch 使用罗技摄像头与 CW12EU-T 完成一条至少 30 分钟的 `test` 录制；记录摄像头输入
  FPS、视频帧数、IMU 包/样本率、最大间隙、时间拟合残差、解析/回调丢弃、收尾耗时、上传
  重试和资源占用。验收后保留或删除由用户决定，不自动清理。
- [ ] 独立执行一次 BLE 真实断连恢复：预览中断开或关闭 IMU，确认单会话所有权、有限重连、
  页面状态和重新接入；正式录制期间断连必须停止录制并保留可诊断 partial，不能静默拼接。
- [ ] 独立执行一次上传网络中断恢复：上传中断网后恢复，确认 resumable session 继续、三件制品
  大小/SHA-256 一致、manifest-last，且不产生重复录制或错误 `published` 状态。
- [~] 修复英文模式中已报告的同步、完整 IMU 与数据集目录残留中文；自动化测试与双前端构建
  已通过，等待生产部署后用英语浏览器做截图复核。
- [~] 当前主线已由 GitHub Actions 原子部署，生产 systemd、回环/公网健康、团队 Current、H5
  拉取和 ONNX 实验目录扫描均通过；仍需在中文与英文浏览器各做一次 IAP 页面和下载人工复核。

## P0：真实采集安全

- [ ] 在扩大正式 ADL/跌倒采集前冻结参与者知情同意、摄像范围、原视频访问与删除申请流程。
- [ ] 冻结高风险跌倒动作的场地、软垫、保护人员、停止条件和禁止动作；未完成前只允许安全的
  平地 ADL、平台联调和非危险动作。
- [ ] 冻结 `collection/session/recording/trial/segment` 编号与补录/重录语义，避免多人采集后再
  迁移 ID。

## P1：桌面平台验收

- [ ] Windows 11 x64：安装/卸载、托盘打开与退出、30 分钟录制、BLE 断连恢复、上传断点续传。
- [ ] macOS 13+ Intel：摄像头/蓝牙权限、CoreBluetooth 固定设备、罗技 AVFoundation、录制
  与 VideoToolbox 回退、Keychain 登录上传、DMG 安装和故障恢复。
- [ ] macOS Apple Silicon：在真机重复同一验收；当前只有 CI 构建证据，未公证测试版不能描述
  为正式发行版。
- [ ] 评估 WSL2 采集只关注 USB 摄像头与 BLE 透传；不得把 benchmark 在 WSL2 可运行当成采集
  硬件已兼容。

## P1：训练仓库交接

- [ ] 冻结团队快照撤回与替换操作手册：旧 snapshot 永不原地覆盖，`current.json` 只切换到
  已完整校验的不可变版本；误发布通过新指针和撤回记录处理。
- [ ] 在第一批正式团队数据冻结前单独讨论 `dataset_handoff` 是否需要 0.2：包括一条 recording
  多个跌倒区间、每个区间恰好一个 onset/impact、`is_fall` 与事件表一致性。达成跨仓库决议前
  继续使用 0.1.0，不静默迁移或改写已发布快照。

## 非阻塞研究

- [ ] 只有厂商文档或新实验能给出可验证语义时再研究每个 16-byte 候选帧末尾 4 字节；此前
  原样保存，不暴露猜测字段。
- [ ] 若第二种 IMU 或回放测试需求出现，再抽象通用 BLE IMU/模拟源；当前不为假设需求重构。

## 当前证据入口

- 设备与协议事实：[docs/device-facts.md](docs/device-facts.md)
- 坐标系与校准：[docs/device-coordinate-system.md](docs/device-coordinate-system.md)
- 时间与同步：[docs/architecture-and-time.md](docs/architecture-and-time.md)、
  [docs/annotation-and-sync.md](docs/annotation-and-sync.md)
- 采集前检查：[docs/pre-collection-checklist.md](docs/pre-collection-checklist.md)
- 桌面平台：[docs/desktop-platforms.md](docs/desktop-platforms.md)、
  [docs/macos-desktop.md](docs/macos-desktop.md)
- 数据生命周期与生产验收：[docs/data-lifecycle.md](docs/data-lifecycle.md)、
  [docs/production-acceptance.md](docs/production-acceptance.md)、
  [2026-08-30 自动化与 WSL2 验收记录](docs/production-acceptance-session-2026-08-30.md)
