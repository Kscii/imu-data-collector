# v0.3.0 多设备正式版本检查清单

更新时间：2026-09-09。本文区分“仓库本机验证完成”“CI 构建完成”“真实设备人工验收完成”与
“已正式发布”；任一项不能替代下一项。

## 本次版本范围

- 以项目 SN 选择设备，不再使用写死的默认设备 ID；未选择 SN 时，后端在打开 BLE 前拒绝预览和录制。
- `IMU-0001-R01` 保留原 CW12EU-T 正式协议和校准档案。
- `IMU-0002-R01` 接入 22-byte ABF2 协议、六轴原始计数和设备本地 uint64 LE 毫秒计数器；当前约 50 Hz。
- 新设备仍为 `commissioning`、`prod_capture_enabled=false`。16384 counts/g 只是诊断候选，正式 HDF5 `values_si` 保持 NaN。
- 每个 SN 可有独立的本机候选尺度、零偏和轴映射；候选只能用于实时诊断和 test 元数据，不能获得正式校准权限。
- 完整设备配置使用 Snapshot v2：工作区、本机快照、团队 candidate/approved/revoked、独立 Current 和审计事件。SI Profile 随 Snapshot 冻结；纯校准换 `si-…`，固件变化换 SN revision。
- HDF5 1.9 和 manifest 3.2 冻结 Snapshot/content SHA-256、SN 和 SI Profile ID；旧 HDF5 1.8 与 manifest 3.0/3.1 继续可读。
- 采集页只保留高频场次/设备/预览/录制操作，健康状态持续可见；配置维护进入“设备与设置”，团队审批进入标注端“设备配置”。

## 仓库与本机门禁

- [x] 完整 Python、HDF5、API、Snapshot/LKG、防回滚和两种协议测试通过。
- [x] 最终 Ruff、Python 编译检查和 `git diff --check` 通过。
- [x] 采集端/标注端新版前端构建和现有前端合同测试通过。
- [x] 前端在后端缺少 `imu_profiles` 时显示“前后端版本不一致”的可操作错误，不再因为直接解引用旧响应而渲染空白页。
- [x] PyInstaller 构建输入包含 v2 配置、协议、注册表和 broker 客户端源码；Windows x64、macOS arm64/x86_64 工作流包含 v2 定向冒烟测试。
- [ ] 对最终 diff 做人工代码/协议审查，并只提交约定文件。

本机验证记录（2026-09-09）：前端 6 项合同测试及 capture/annotation 双构建通过；Python 全量
279 项通过（含 TestClient、视频、HDF5、API、Snapshot/LKG、防回滚和两种协议），Ruff、Python
编译检查与 `git diff --check` 通过。受限执行环境会禁止 asyncio 用于跨线程唤醒的本地
`socketpair.send()`，TestClient 在该环境内会假性阻塞；解除这一沙箱限制后全量测试在 6.18 秒内
完成。FastAPI 下载路由的 GET/HEAD OpenAPI 重复 Operation ID 已清理。正常 CI 与三平台打包工作流
仍须独立给出结论。

## 配置发布门禁

- [ ] 在生产部署前备份并审查私有配置；不得把 OAuth secret、服务账号或用户 token 写入仓库或安装包。
- [ ] 把 v2 Snapshot/identity broker 接口和标注端审批接口部署到生产环境；验证未登录 401、成员提交/分配、非成员拒绝、只有管理员审批/revoke/Current。
- [ ] 从已审查的 Git bootstrap lock 提交第一份团队候选并核对 Snapshot/content SHA-256；本轮到此为止，不批准、不撤销、不设置团队 Current，也不得直接改 Bucket 指针。
- [ ] 在独立审批验收中复核候选内容与证据，之后才可批准；是否设置第一份 Current 仍是另一个显式决策。
- [ ] 桌面端登录后刷新 v2 配置；断网、坏哈希、未知 schema 继续使用 LKG，未知 protocol 只禁用对应设备。
- [ ] 验证人工固定 Snapshot 不随刷新变化，空闲时可恢复 Current；local/candidate 只允许 test，revoked 不允许新采集。
- [ ] 正式 SI 证据不可变；相同 SN 的纯重新校准产生新 `si-…`，固件/协议/时序变化保留资产号并分配下一 revision SN。
- [ ] 审查 `configs/device-config-bootstrap.lock.json` 的源文件哈希、Snapshot 哈希和 `cloud_objects_written=0` 迁移报告。

## 真实设备人工验收

- [ ] Linux：两个 SN 分别完成扫描、连接、预览、test 短录制、H5/MKV 收尾和退出释放。
- [ ] `IMU-0002-R01`：确认一条通知就是一个 22-byte 样本；静止模长、设备时钟约 20 ms 步进、重启归零/回绕边界和约 50 Hz 结果写入审计记录。
- [ ] Windows 10/11 x64：安装器全新安装后，验证 bootstrap v2、设置页、两个 SN 的 BLE/摄像头/test 录制、登录、Snapshot 刷新和上传；检查托盘单实例与退出。
- [ ] macOS arm64：DMG 全新安装后，验证 bootstrap v2、设置页、权限、按 SN 隔离的 CoreBluetooth 绑定、摄像头、test 录制、Snapshot 刷新和上传。
- [ ] macOS x86_64：重复同一验收。CI 构建成功不能替代 Intel 真机验收。
- [ ] 新设备完成正式多姿态加速度和受控陀螺仪校准前，保持 commissioning/test-only；若固件改变协议、时序或单位，为同一资产分配下一 revision SN。

## 构建与发布顺序

1. 完成人工审查并提交；推送后等待普通 CI 全绿。
2. 手动运行桌面打包工作流的 `all`，下载 Windows x64、macOS arm64、macOS x86_64 三套 artifact，核对 SHA-256 并执行上述真机验收。
3. 只有验收通过后创建 `desktop-v0.3.0` 标签。标签工作流会重新构建三平台并创建同一个稳定 GitHub Release。
4. 下载 Release 资产再次核对版本、架构和 SHA-256；保留验收记录。当前 Windows 未签名，macOS 为 ad-hoc 签名且未公证，这些限制必须继续写在发布说明中。

## 空白页故障说明

截图中的空白页来自同一 `127.0.0.1:8765` 进程同时提供了新版静态前端和旧版后端 API：新版
页面读取旧 `/api/v1/devices` 响应中不存在的 `imu_profiles`，React 初始化阶段异常后只剩背景。
本版本增加响应合同检查并给出一键更新提示。部署本机源码变更时仍必须运行
`./scripts/update-local-capture.sh`，让后端源码与临时构建后再原子替换的 `dist-capture` 保持同一
build ID；只重启 systemd 服务不能修复旧静态文件。
