# 客户单 H5 交付与公开只读查看器

## 交付单位

每个客户交付物严格绑定一个不可变训练快照 `snapshot_id`。平台在“训练快照”页面后台直接生成
一个 `cw12eu-client-<snapshot_id>.h5`，不会先创建 ZIP。生成任务不阻塞标注或其他快照操作；
同一快照与同一合同只复用同一不可变对象。

客户 H5 使用 `imu_schema_version = "3.2.0"` 和
`artifact_profile = "client_delivery"`，包含：

- `/samples`、`/sequences`、`/annotations`：25 Hz、六轴 SI 数据与标注；
- `/media/index`、`/media/videos/*`、`/media/timing/*`：逐录制原始 MP4 字节和真实时间映射；
- `/labels/catalog`、`/labels/sequence_versions`：解释历史标注所需的冻结 code/name/version。

交付物不包含原始 BLE 通知、原始计数、采集 H5、review、UniKey、邮箱、可逆身份映射或内嵌说明
文档。可识别视频会进入客户 H5，因此仍受参与者同意、客户授权和保留期限约束。

物理 HDF5 结构以共享规范
[`imu-hdf5-v3.2.md`](contracts/imu-hdf5-v3.2.md) 为准；服务端对象布局和 sidecar 见
[`client-delivery-contract.md`](contracts/client-delivery-contract.md)。

## 生成与完整性

生成器只读取不可变训练 H5、冻结视频、冻结 view 和冻结 taxonomy。它先做磁盘预检，在唯一临时
文件中生成，关闭 H5 后重新校验三张核心表、每段 timing、taxonomy 引用、视频物理范围和逐视频
SHA-256，再计算整个 H5 的 SHA-256。最终 H5 上传并验证成功后才写 manifest。

整个文件的 SHA-256 不写入 H5 自身，而保存在 GCS metadata、交付 manifest 和下载响应头。
下载支持 HTTP Range（200/206/416），便于大文件断点续传。成品不得经过 `h5repack` 或原地修改，
否则视频物理偏移合同失效。

## 公开查看

`https://viewer.imu.kscii.tech` 只接受 HDF5 3.2 的 `training_dataset` 或
`client_delivery` profile。文件只在浏览器本地处理，不上传服务器。客户 profile 可以播放
内嵌视频，并通过逐段线性插值同步视频、25 Hz IMU 和标注；training profile 没有视频，但仍可
移动时间轴查看曲线和稳定 code。

查看器不会在打开数 GiB 文件时自动计算整体 SHA-256。用户可按需启动校验，查看进度并取消。
下载页显示合同版本、文件大小和服务端 SHA-256，接收方应在正式交付时完成一次整体校验。

## 隐私边界

公开查看器不托管客户数据，也不绕过标注平台的 Google/IAP 下载授权。下载后的访问、传输和保留
期限由交付方与客户约定；查看器软件许可证不等于数据许可证。
