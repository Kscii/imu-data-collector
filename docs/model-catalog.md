# ONNX 模型目录

数据标注平台的“研究模型与交叉验证证据”页面只负责浏览、校验状态和下载，不在服务器或浏览器中执行推理，
也不会给任何发布自动标记 `current`、`recommended` 或 `best`。

## 两类发布

- 交叉验证证据：正式 5-fold 交叉验证和受控的 1-fold 工程验证分开显示。表格默认按
  `model_id + training_recipe` 排列，显示各 test fold 的均值和样本标准差；单个 fold 的
  ONNX、统一 metadata 和既有完整 result bundle 仍可下载复核。
- 研究候选模型：面向后续运行时集成研究的独立两文件制品，只包含 `model.onnx` 和
  `metadata.json`。metadata 固定阈值与触发策略；Python parity 不代表 Android Runtime
  或实体机已经通过。

CV test-fold 指标始终是 `selection_eligible=false` 的审计证据。页面不会默认按这些指标
排名；用户主动排序也不能把排序结果解释为最佳、推荐或可部署模型。研究候选模型必须内嵌
validation OOF、participant-once、阈值和触发策略选择摘要。

对象前缀固定为：

```text
benchmark-model-catalog/experiments/<publication_id>/
benchmark-model-catalog/models/<release_id>/
```

每个发布的有效载荷不可变。`state.json` 只允许 `available` 和 `deprecated` 两种状态；
弃用不会删除文件。发布 manifest/publication 必须最后写入，因此目录扫描不会展示半成品。

## 权限与发布链路

- IAP 白名单成员都能查看和下载。
- 标注平台管理员可以在网页把发布标记为已弃用。
- 白名单成员可以在 benchmark CLI 使用当前 `gcloud` Google 登录发布。
- 只有管理员可以通过 benchmark CLI 恢复已弃用发布。

Benchmark 不需要 Bucket 写权限。CLI 获取短期 Google ID token 后请求
`https://upload.imu.kscii.tech`；代理检查邮箱白名单，根据发布 JSON 反推出唯一允许的对象键，
签发受限的 resumable upload 会话，随后逐个复核大小和 SHA-256，再写 `state.json` 和最终
`metadata.json`。令牌和上传会话 URL 不得写入日志或持久化。

上传代理默认接受 Google Cloud SDK 的 OAuth audience：

```yaml
cloud:
  model_publish_google_audiences:
    - 32555940559.apps.googleusercontent.com
```

如 Google Cloud SDK 的身份 audience 后续发生变化，应在服务器私有配置中显式更新，或用
逗号分隔的 `IMU_MODEL_PUBLISH_GOOGLE_AUDIENCES` 注入；不要关闭 audience 校验。

## 目录缓存和异常隔离

服务端目录缓存 60 秒，页面提供“刷新目录”按钮强制重扫。详情按需加载。扫描时同时核对发布标记、
`state.json`、对象大小和对象 `sha256` metadata。缺文件、缺 SHA 或对象身份不一致的发布会
进入 `invalid_publications`，不会作为可下载模型展示。

冻结前发布的 `imu_experiment_catalog_v0 / 0.1.0` 只按 `legacy_pre_v1` 解释，缺失的指标范围
一律保守视为 `metric_split=test`、`selection_eligible=false`。它可以通过
`include_deprecated=true` 审计，但 v1 迁移验证后会从普通页面隐藏；云端对象不删除、不覆盖。

标注 API：

```text
GET  /api/v1/model-catalog
GET  /api/v1/model-catalog?refresh=true
GET  /api/v1/model-catalog?include_deprecated=true
GET  /api/v1/model-catalog/{experiment|model}/{id}
GET  /api/v1/model-catalog/{experiment|model}/{id}/marker/download
GET  /api/v1/model-catalog/{experiment|model}/{id}/files/{file_id}/download
POST /api/v1/model-catalog/{experiment|model}/{id}/deprecate
```

文件下载使用稳定 `file_id`，支持单段 HTTP Range，并返回 `X-Content-SHA256`。实验详情的下载
区只保留 `metadata.json` 与完整 result bundle；所选方法的 fold ONNX 在方法详情内按 0–4 排序。

跨仓库的规范源及版本策略见同步副本
[`docs/contracts/annotation-benchmark-contract.zh-CN.md`](contracts/annotation-benchmark-contract.zh-CN.md)，
其来源 commit 和 SHA-256 固定在
`configs/contracts/annotation-benchmark-contract.lock.json`。
