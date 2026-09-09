# 设备配置 Snapshot v2

本文定义 v0.3.0 起的设备配置、SN、SI 系数和发布治理。它同时回答“哪些内容可以在设置页修改”
以及“哪些本机/服务器配置不应该进入团队设备 Snapshot”。原始 BLE 字节与实际时间戳仍是事实
来源；配置只能说明如何解释它们，不能改写既有录制。

## 1. 配置分层

| 层 | 内容 | 存储位置 | 可变性 | 页面入口 |
|---|---|---|---|---|
| 团队设备 Snapshot | 全部设备的 SN、身份、固件证据、协议、预期频率、SI Profile | `device-config/v2/snapshots/<cfg-id>.json` | 不可变 | 采集端“设备与设置”、标注端“设备配置” |
| 审批状态 | `candidate → approved → revoked`、revision、操作者、时间 | `device-config/v2/reviews/<cfg-id>.json` | 受控可变 | 标注端管理员 |
| 团队 Current | 当前推荐的 approved Snapshot | `device-config/v2/current.json` | CAS 更新 | 标注端管理员 |
| 审计事件 | 提交、批准、撤销、切换 Current、分配 SN | `device-config/v2/events/` | 只追加 | 标注端详情 |
| 本机工作区 | 尚未冻结的整份 v2 配置 | 用户数据目录 `device-config/workspace.json` | 自动保存 | 采集端“配置快照” |
| 本机 Snapshot | 操作者手动冻结、只供 test 的配置 | 用户数据目录 `device-config/local/` | 不可变 | 采集端“配置快照” |
| 团队缓存 / LKG | 已下载并完整校验的远端 Snapshot、review、Current | 用户缓存目录 `device-config/` | 原子替换 | 采集端“配置快照” |
| 本机 BLE 绑定 | SN 到 CoreBluetooth UUID / 本机标识的连接加速信息 | 用户数据目录 `device-bindings.json` | 可忘记、可重建 | 采集端技术详情 |
| 运行配置 | 数据目录、端口、视频、OAuth broker URL、对象存储、白名单 | `configs/default.yaml` 或私有 YAML/env | 重启生效 | 设置页只读显示 |
| 动作标签 | 动作 code、名称和版本种子 | `configs/activities.yaml`，线上另有版本化对象 | 独立治理 | 标注端“标签管理” |

因此配置文件不只有 SI 和设备注册。设备解释数据属于 Snapshot；采集主机、视频、身份、存储、
标签和秘密具有不同生命周期，不得为了“一个设置页”而合并成同一个可发布对象。

## 2. Snapshot 内容与排除项

每份 Snapshot 是完整设备集合，不是补丁。内容 schema 为 `2.0`，每台设备包含：

- 固定 `sensor_sn`、物理资产号、revision 和上一 revision；
- 广播名、可选公共地址、地址类型、服务 UUID、GATT fingerprint；
- 固件版本及其证据状态；
- 代码审查过的 `protocol_id`、预期采样率和证据状态；
- 可用数据级别；
- 独立的 `si-…` Profile：尺度、零偏、轴顺序、符号、方法、证据 SHA-256 与证据摘要；
- 审计文档路径。

以下内容明确排除：主机路径、摄像头选择、OAuth/服务账号/token、团队成员、标签 taxonomy、
SQLite catalog、SN 到本机蓝牙 UUID 的绑定，以及实际协议解析代码。Snapshot 可以引用
`protocol_id`，但不会从云端下载或执行代码。

未知 Snapshot schema 会整份拒绝并继续使用 LKG。已知 schema 内出现未知 `protocol_id` 时，
只禁用对应设备，其他设备仍可工作。

## 3. SN 与 SI 规则

SN 使用 `IMU-NNNN-RNN`：

- `IMU-NNNN` 永久代表一个物理资产；
- 固件升级或任何可能改变字节、时序、范围、单位的变更使用下一 `RNN`；
- 纯 SI 重新校准不换 SN，在新 Snapshot 中写新的 `si-…`；
- `si-…` 由 SN 和归一化 SI 内容自动计算，操作者不需要手工生成或维护哈希；
- SN/revision 由 broker 中的原子分配器永久保留，允许编号出现空洞，不允许复用。

`prod` 同时要求 Snapshot 状态为 approved、设备允许 prod、SI Profile verified。操作员创建的
local 和 candidate 只能 test；revoked 保留历史引用但不能开始新预览或录制。唯一例外是随代码
审查并由 `device-config-bootstrap.lock.json` 精确锁定的安装包 bootstrap：它保留旧设备的离线
正式采集能力，标注端也只在自己的 bootstrap lock 得出完全相同 `local-cfg-…` 时承认该权限。

## 4. 本机交互

采集端选择逻辑如下：

1. 默认跟随最近一次完整校验的团队 Current；没有缓存时使用 Git bootstrap。
2. 操作者可以人工选择 local、candidate 或非 Current 的 approved Snapshot。
3. 人工选择持久化，不会因后台刷新在采集中途变化；设置页明确显示“人工固定”。
4. “恢复跟随 Current”只能在空闲且设备已释放时执行。
5. 工作区以结构化表单为默认入口，字段变更在停止输入约 900 ms 后自动保存；高级完整 JSON
   仅用于批量迁移和排障。格式或字段错误会保留编辑内容并显示错误。
6. “保存本机快照”只冻结本地 test 配置；“发布为团队候选”才经过 OAuth broker 写入团队对象。

“查找附近 IMU”只执行一次约 5 秒的 BLE 广播发现：不建立 GATT 连接、不订阅数据、不修改
Snapshot，也不会自动登记设备。采集页显示简要结果，完整候选、服务 UUID、RSSI、适配器状态和错误
保留在“诊断与运行环境”。

本机候选 SI 在“设备档案与 SI”中使用表单维护。候选用于实时诊断，并作为非权威候选元数据写入
test H5；原始 BLE 帧始终保留。候选可显式复制到工作区，但该操作会强制 `verified=false`、清除
权威证据 SHA 并将允许级别限制为 `test`，不会自动批准或切换团队 Current。

配置 Snapshot 和 IMU 切换不会在录制/预览中发生；实时预览期间仍可使用既有的摄像头热切换。
页面禁用操作只是第一层提示，后端对配置和 IMU 执行同一空闲门禁。

## 5. 审批和 Current

所有授权成员可提交 Snapshot、保留新资产 SN 或下一 revision。只有管理员可以：

- 把 candidate 批准为 approved；
- 把非 Current 的 approved 撤销为 revoked；
- 把 approved Snapshot 设为 Current。

Snapshot 内容重复时，即使名称或说明不同也拒绝提交。review 与 Current 使用 revision/generation
乐观锁；冲突必须刷新后重试。Snapshot 永不覆盖或删除。

## 6. 录制证据合同

capture HDF5 `1.9.0` 在根属性中冻结：

- `configuration_snapshot_id`、Snapshot SHA-256、内容 SHA-256；
- 配置来源、采集开始时看到的审批状态和最近检查时间；
- `sensor_sn`、设备 Profile SHA-256 和 `si_profile_id`。

manifest `3.2.0` 重复这些引用。标注/训练导出不使用“现在的审批状态”替代历史事实，而是根据
只追加事件判断该 Snapshot 在 `captured_at_utc` 是否 approved。采集后撤销不会删除原始录制，
但未在采集开始时获批的配置不能成为正式训练导出。

旧 HDF5 `1.8.0` 和 manifest `3.0/3.1` 保持可读，不会伪造 v2 引用。

## 7. Git bootstrap 与迁移

`configs/device-config-bootstrap.lock.json` 是安装包的精确 v2 引导快照，包含 v1 注册表、引用校准
证据的输入 SHA-256，以及完整 Snapshot 哈希。运行时会重新校验输入和锁；不匹配时停止使用该
引导配置。

迁移命令默认只输出计划，不写文件或云端：

```bash
uv run imu-collector migrate-device-configuration-v2
```

审查 `plan_token`、输出路径和 Snapshot 后，使用输出中给出的确认文本执行 `--apply`。该 apply
只写本机 bootstrap lock，报告中的 `cloud_objects_written` 必须为 `0`。团队 Snapshot、审批和
Current 必须通过产品页面及已鉴权 broker 流程操作。

## 8. 发布验收

- 修改 v1 引导输入后重新生成 lock，并运行完整 Python/前端测试；
- Windows 与 macOS 配置生成步骤必须复制 lock，PyInstaller 必须包含 v2 实现源码；
- 用干净用户目录验证 bootstrap、断网 LKG、坏 hash、未知 schema、未知 protocol；
- 验证 local/candidate/approved/revoked 的 test/prod 门禁；
- 验证手工固定、恢复 Current、冲突提示和录制中禁止切换；
- 检查 H5/manifest 三组哈希、SI ID、采集时审批判断与旧数据兼容；
- 只有普通 CI、三平台构建和真实设备人工验收都完成后才创建正式版本标签。
