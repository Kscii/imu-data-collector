# 数据生命周期与服务器配置合同

## 1. 一次录制就是一个不可变事实单元

每个 `recording_id` 对应一组独立制品：

- `capture.h5`：BLE 原始通知、六轴原始计数、实际时间戳、视频逐帧时间及冻结的校准属性；
- `video.mkv`：H.264 原始视频，不录音；
- `preview.mp4`：由 MKV 无重编码生成的浏览代理，可以重建；
- `manifest.json`：三种制品的对象键、大小、SHA-256、数据级别与校准档案；v3 不含参与者身份。

采集端按 H5、MKV、MP4、manifest 的顺序发布。标注端只在 manifest 出现后索引，并要求三种
制品都具有且匹配 SHA-256 metadata。上传成功与进入标注索引是两个状态；标注端会另写
`index-receipts/<recording_id>.json`。

原始制品一旦完成就不因同步、标注、完成或训练快照而修改。任何派生结果都保留来源对象键、
来源 revision 与 SHA-256。

## 2. 标注工作流

`review.json` 另含身份状态 `unassigned -> selected -> confirmed`。选择必须记录视频帧和纳秒
时间，确认必须是当前任务负责人的第二次显式操作。正式数据只有在身份已确认、且私有
`subject_ids` 存在时才能完成导出；无法从画面确认时必须保持未完成。重开普通任务保留已确认
身份；存量身份 v3 迁移是一次例外，会把所有身份清空并重开全部已完成任务。

工作流只有三个状态：

```text
unassigned -> in_progress -> completed
                    ^              |
                    +--- reopen ---+
```

- 所有白名单成员都能查看任务；
- 未领取任务可由任意成员领取；
- `in_progress` 仅当前负责人可编辑，其他成员必须明确点击“接管”；
- 每次领取或接管都把负责人更新为当前成员，保存时记录最后编辑者；
- 完成状态只读；当前负责人或管理员可以重开；
- 重开清除当前有效训练导出指针，再次完成会生成新的不可变导出；
- 所有写请求都必须携带 `expected_revision`，过期请求返回冲突，不覆盖较新结果。

不设置提交、审核、接受、驳回或复核人状态，也不保留完整编辑历史。`review.json` 只保存当前
同步、标注、负责人、最后编辑者、revision 和当前有效导出。

点击“完成标注并生成训练 H5”是一次受门禁保护的操作：先检查正式数据、完整覆盖、逐个跌倒
impact、同步、来源哈希及校准档案，再生成不可变 `aligned.h5`，最后以一次乐观锁更新把
工作流设为 `completed`。导出失败时任务仍保持可编辑，不会出现“显示已完成但没有文件”。

## 3. 对象存储布局

```text
captures/<recording_id>/
  capture.h5
  video.mkv
  preview.mp4
  manifest.json

reviews/<recording_id>/review.json

exports/<recording_id>/review-<revision>/
  aligned-<logical_digest>.h5

training-snapshots/<snapshot_id>/
  cw12eu_<snapshot_id>.tar
  manifest.json

benchmark-datasets/team/cw12eu/<snapshot_id>/
  datasets/cw12eu.h5
  manifest.json

benchmark-datasets/team/cw12eu/current.json

benchmark-datasets/base/<snapshot_id>/
  datasets/<dataset_id>.h5
  manifest.json

benchmark-datasets/base/current.json

calibration-evidence/<profile_id>/<recording_id>/
  capture.h5
  video.mkv
  preview.mp4
  source-manifest.json
  archive-manifest.json
```

`active_export` 记录实际 aligned 对象键、SHA-256、逻辑摘要、校准证据摘要和来源 revision。
稳定键不覆盖旧文件，因此重开、修改并再次完成后不会下载旧 aligned。历史
`aligned30.h5` 只保留只读下载兼容；创建新快照前必须重开并重新完成这些旧录制。

## 4. 训练快照

训练快照是用户点击时对“当前所有已完成正式录制”的冻结视图。服务按录制 ID、导出 revision、
aligned SHA-256 和逻辑摘要计算内容指纹，并生成 `snapshot-<digest>`：

- 内容相同直接复用已有快照；
- TAR 自包含所有 aligned 和逐文件 SHA-256 manifest；
- 同时合并生成一个可被 benchmark 直接读取的 `cw12eu.h5`；
- 新快照冻结每条录制的 MP4 引用、SHA-256 和样本到视频媒体时间的 `view.json`；
- 可按同一 snapshot ID 在后台生成独立客户 ZIP；训练 TAR/HDF5 本身仍不包含视频；
- 合并 HDF5 与 manifest 写入不可变 snapshot 前缀，再用 generation 前置条件原子推进
  `current.json`；
- 快照生成过程中发生的后续标注不会改变本次快照；
- 所有成员可创建、列出和下载；
- 管理员可二次确认后清理历史快照；
- 当前录制删除不影响已经生成的自包含快照。

快照没有“撤销”状态或墓碑。需要弃用某个快照时，应先生成新的当前快照。平台侧管理员清理
删除 TAR、冻结 view 和已生成的客户 ZIP，最后删除平台 manifest；已经发布到
`benchmark-datasets/` 的不可变 HDF5、manifest 和 current pointer 不随之删除。训练系统解析
受校验的 `current.json` 或显式 snapshot ID，不能把
对象列表中的“最新”当作隐式依赖。

快照存在多层显式版本，不能混为一个字段：平台对象存储侧清单使用 `4.0.0`，TAR 内
`manifest.json` 使用 `2.0.0`，每个 `aligned.h5` 与合并后的 `cw12eu.h5` 使用 IMU HDF5
`3.1.0`，benchmark manifest 使用 `imu_benchmark_dataset_manifest_v1` 并声明
`imu_benchmark_contract_v2`。清单记录规范对象键、字节数、物理 SHA-256 和逻辑内容摘要；
消费者必须先校验 manifest，再校验实际文件。

`imu-fall-benchmark` 直接从 `benchmark-datasets/team/cw12eu/current.json` 解析并校验合并后的
HDF5；TAR 保留给逐录制归档和审计。团队录制不是第三方原始数据源，不需要再次走公共数据集
adapter；它在此平台完成单位换算、同步、标注和严格 25 Hz 重采样后才发布。

标注平台的“数据集”页只读解析公共和团队 `current.json`，并把不可变历史 manifest 折叠展示。
目录只接受 `imu_benchmark_contract_v2`、HDF5 `3.1.0`、25 Hz 以及与集合相符的
`evaluation_role`；下载对象键只能来自已验证 manifest，不能由浏览器传入任意路径。网页允许
下载 manifest 和单个 H5 用于检查，正式训练仍使用 benchmark 仓库的 `./benchmark data pull`
完成整套 SHA-256 校验和原子激活。目录 API 没有上传、删除或推进 current pointer 的能力。

## 5. 校准证据归档

当前样机的 15 条受信校准录制不属于动作标注队列。归档命令先输出计划，显式应用后才复制：

```bash
imu-annotation --config /etc/imu-annotation/config.yaml \
  archive-calibration-evidence

imu-annotation --config /etc/imu-annotation/config.yaml \
  archive-calibration-evidence --apply --delete-source
```

归档要求 H5、MKV、MP4 和原 manifest 全部存在并逐一校验 SHA-256；归档完成后写独立清单。
`--delete-source` 只有在归档复核成功后才删除普通 capture 前缀。校准页只读归档，不能领取、
标注、完成或进入训练快照。页面同时显示共享录制时间游标的原始计数和 SI 两条完整曲线；视频、
两条曲线任一处跳转都会定位另外两处。原始计数、4-byte trailer 和完整 16-byte HEX 直接来自
不可变证据 H5。证据 H5 当时没有可信 SI 时，页面按当前服务器权威 profile 即时推导 SI 并
明确标识来源，不把派生值回写证据文件；profile 门禁失败时仍允许查看原始计数和视频。

## 6. 删除和垃圾回收

录制删除要求输入 `DELETE <recording_id>`。服务先将 catalog 标为 deleting，再按对象
generation 删除原始制品、review、导出和相关诊断引用；对象存储的 7 天 soft delete 是管理员
恢复窗口，不是 WebUI 回收站。

训练快照保留自包含训练数据，并把客户查看所需的视频复制到快照自己的不可变前缀，因此不再
依赖当前录制或 review。快照清理只允许管理员，并要求
`DELETE <snapshot_id>`。删除顺序是 TAR、客户 ZIP、冻结 view，最后是平台清单；失败后可用
同一确认安全重试。

每日垃圾回收只处理超过保留期的中断 capture、已失去源 manifest 的索引回执、无当前引用的
旧导出及缺少有效清单的孤儿训练快照。默认先 dry-run；时间戳、引用或清单不确定时保守跳过。

## 7. test、prod 与 25 Hz

- `test`：联调和结构展示，可下载原始 H5 与 review；不能完成、生成 aligned 或进入快照；
- `prod`：正式采集意图，仍须通过视频、IMU、同步、标注、来源哈希和校准全部门禁。

缺失数据级别的本地旧条目统一按安全的 `test` 处理；系统不提供 legacy 业务状态，也不从文件
名猜测资格。

原始约 25 Hz IMU 和实际视频 PTS 均原样保存。完成标注时，在 IMU/视频公共有效区间上生成严格
25 Hz 网格：第 `k` 行概念时间为 `k/25 s`，SI 数值按真实样本时间线性插值且不外推；区间
边界和 onset 使用 ceil，impact 使用最近网格点、半格向后取整。视频不复制帧伪造成严格 30 FPS。

## 8. 配置和秘密

服务器结构化配置固定在 `/etc/imu-annotation/config.yaml`，建议 root 所有、服务可读、
`0640`。其中包含允许的 UniKey、管理员、私有邮箱映射、bucket、校准 profile 和数值参数。
OAuth 等真正秘密放 `0600` 的独立 env；两者都不提交真实值。

服务使用 VM 绑定身份访问 GCS，不保存服务账号 JSON key。校准 profile、证据文件 SHA-256、
manifest 参数与 H5 冻结属性必须完全一致，不能降级为只比较大小。
