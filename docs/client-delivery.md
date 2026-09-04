# 客户数据交付与公开只读查看器

## 交付单位

每个客户交付包严格绑定一个不可变训练快照 `snapshot_id`。平台在“训练快照”页面后台生成
`cw12eu_client_delivery_v2` ZIP64；生成过程不阻塞标注和其他快照操作。相同快照与相同交付
合同只生成同一内容。旧 v1 包不原地升级，也不再显示为当前可下载交付物。

交付 ZIP 包含：

- `dataset/cw12eu.h5`：严格 25 Hz、六轴 SI 单位的合并训练数据，HDF5 schema 3.1.0；
- `recordings/<sequence_index>/video.mp4`：该录制用于复核的原始可识别视频；
- `recordings/<sequence_index>/view.json`：样本、标注和视频媒体时间的冻结映射；
- `taxonomies/<taxonomy_id>/<version>.json`：标注使用的冻结 code、name 与跌倒类别；
- `manifest.json`：快照身份、数据合同、录制列表及文件清单；
- `README.md`、`DATASET_CARD.md` 与 `SHA256SUMS`。

录制目录使用四位 `sequence_index`，不直接使用可能包含 Windows 非法字符的录制 ID。真实
`recording_id` 始终保存在 manifest 和 view 中。

视频不嵌入训练 HDF5，原始 BLE 包和内部采集 H5 也不进入客户包。生成器在服务端核验所有
来源文件的大小与 SHA-256；查看器只负责本地浏览，不替代交付端的完整性门禁。

完整字段和兼容规则见
[`docs/contracts/client-delivery-contract.md`](contracts/client-delivery-contract.md)。

## 公开查看

公开静态站点 `https://viewer.imu.kscii.tech` 支持拖入完整 v2 ZIP 或独立 `cw12eu.h5`。文件只在
浏览器本地处理，不上传到标注服务器，也不读取当前 review。ZIP 模式显示冻结视频、曲线、标注
和 taxonomy name；独立 H5 模式只显示 HDF5 内的 stable code，不用当前在线 taxonomy 猜测
历史名称。

查看器默认不重新计算包内 SHA-256，只进行合同、路径、类型、大小和引用关系检查，并显示
“未执行内容哈希校验”。`SHA256SUMS` 保留给接收方的命令行或审计流程使用。

大文件优先通过 ZIP64 成员范围直接读取。浏览器不能直接使用成员切片时，只把当前视频按块
临时写入 OPFS；刷新后的新页面先清理上次会话目录，且不使用持久视频缓存。

## 时间轴显示

录制查看器共享视频、完整录制峰值概览、当前点附近 2/5/10 秒六轴原始点、标注区间和 impact
事件的当前时间。视频和 IMU 不假定相同帧率，所有跳转使用冻结 `view.json` 的真实时间映射。

## 隐私边界

客户交付包包含可识别参与者的视频，只能在已有参与者同意和交付授权范围内生成、保存和传输。
公开查看器本身不托管客户数据；ZIP 下载仍由 Google/IAP 白名单保护。下载后的访问、传输和
保留期限由交付方与客户约定。查看器的 MIT 软件许可证不等于数据许可证。
