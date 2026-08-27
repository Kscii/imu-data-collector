# 数据生命周期与服务器配置合同

## 本地录制原子单位

每个 `recording_id` 有独立本地目录。正常收尾后事实层是同名 H5/MKV；两者不再因同步、
标注或审核而修改。用户在采集页确认后手动发布 `capture.h5`、`video.mkv`、可重建的
`preview.mp4`，最后写入 `manifest.json`。只有 manifest 已出现且三个对象的大小与 SHA-256
一致，标注端才会索引该录制。

`review.json` 只保存当前同步、标注、工作流状态、最新审核意见和单调增加的 revision。并发
保存必须携带预期 revision；过期写入返回冲突，不覆盖新结果。不保留完整编辑历史。

云端标注服务默认每 10 秒后台扫描一次 `captures/` 中的 manifest。SQLite catalog 保存已见
generation，未变化的录制不会重复下载 manifest 或重新校验三个制品。后台扫描失败时继续提供
已有索引，并在下一轮重试；管理员还可以在标注页面点击“立即扫描 Bucket”强制刷新。

工作流固定为：

```text
unassigned -> in_progress -> submitted -> accepted -> exported
                    ^            |
                    +-- reject --+
```

`review_policy=single_user` 时 submit 直接进入 accepted；`two_person` 时才经过 submitted 且
标注者不能审核自己的提交。当前团队采用同权领取：只有当前领取者可以编辑 `in_progress`
快照，其他白名单成员可显式接管，workflow 的 annotator 随之更新为最后领取者。submitted、
accepted 和 exported 全部只读；所有白名单成员都可重开，重开会增加 revision、恢复编辑并
清除当前有效导出指针。

## 文件产物

- `captures/<recording_id>/manifest.json`：采集端与标注端唯一稳定交接合同，引用原始 H5、
  MKV 和 MP4 代理及其 SHA-256。
- `exports/<recording_id>/review-<revision>/aligned30-<digest>.h5`：通过全部门禁后生成的
  单录制不可变训练文件，只有 `/samples`、`/sequences`、`/annotations` 三个根数据集。
  `review.json.active_export` 保存当前实际对象键、SHA-256、逻辑内容摘要和来源 revision。
- `cw12eu_training_release_NNNN.tar`：把已导出的单录制 H5 汇总成不可变训练发布；manifest
  保存逐文件 SHA-256，SOFT3888 导入器安全校验后拼接为 `cw12eu.h5`。

文件名不编码可变状态。状态来自 catalog 和 `review.json`。每个训练发布除了 TAR 内部 manifest，
还写入对象存储侧车 `releases/<release_id>/manifest.json`，供删除门禁直接核对 recording ID。
已经进入有效训练发布的录制不能逐条删除；若只发现没有侧车清单的旧 TAR，也保守拒绝删除。
尚未发布的录制允许九名白名单成员删除，但必须二次输入 `DELETE <recording_id>`。删除范围
包括原始 H5、MKV、MP4、manifest、review、导出、缓存和共享同步实验引用。应用立即隐藏并
删除活动对象，GCS 存储桶提供 7 天软删除；删除期间 catalog 先标记 `deleting`，并使用对象
generation 防止覆盖并发更新。

所有白名单成员都可以创建、列出和下载训练发布。发布只读取各 review 当前 `active_export`，
并使用录制、导出 revision 与 H5 SHA-256 计算确定性内容指纹；内容完全相同时直接返回现有
发布，清单写入失败后重试也复用已验证的 TAR，不重复占用空间。只有管理员可以撤销；撤销必须输入
`REVOKE <release_id>` 并填写原因：系统先写入墓碑使其立即从列表消失和停止下载，再删除活动
manifest/TAR，最后把墓碑标为 `revoked`。中途失败可用同一确认重试。墓碑只保留发布编号、
操作者、原因、内容指纹和文件哈希等小型审计信息，不保留 TAR。

每日垃圾回收会检查三类对象：超过 7 天仍没有 manifest 的中断 capture、没有任何当前
`active_export` 或有效 release 引用的旧导出，以及没有 manifest 的 release TAR。时间不足、
时间戳未知或仍被引用的对象全部跳过；release manifest 无法解析时保守保护全部导出。先运行
`imu-annotation cleanup-orphans --dry-run` 可只查看候选。

标注页可以随时下载不可变原始 `capture.h5` 和当前 `review.json` 快照。`test` 可使用这两个
制品完成联调与结构展示，但永远不能生成训练 H5、进入训练发布或同步到 SOFT3888 `raw`。
只有 `prod`、同步、标注、审核和物理校准全部通过后，页面才允许生成并下载当前
`active_export` 指向的 aligned30 H5。标注端不会直接写 SOFT3888 工作树或 `data/raw/`，而是先生成不可变训练发布
TAR，再由 SOFT3888 的 `imu-data import-team --release ...` 显式校验和导入到
`data/processed/imu_30hz/cw12eu.h5`。

## 30 Hz 时间网格

IMU 使用外置同步决定映射到视频时间，取 IMU 与视频公共有效区间作为起点。第 `k` 行的概念
时间严格为 `k/30 s`。数值由已校准 SI 样本按真实时间线性插值，不外推。区间边界使用首个
不早于边界的网格点（ceil）；派生 onset 复用同一个 ceil 规则以保持等于 fall 起点，人工
impact 取最近网格点，恰好半格时向后取整。

本轮只生成 IMU 训练文件。未来视频帧对齐必须使用单独的 multimodal schema，使视频帧 `k`
与 IMU 行 `k` 共用同一时间定义，不能修改当前 SOFT3888 v3 合同。

## 配置与秘密

服务器结构化配置放 `/etc/imu-annotation/config.yaml`，建议 root 所有、服务可读、权限
`0640` 或更严格。九名 UniKey、邮箱到 UniKey 的私有映射、角色、审核策略、bucket 和路径
属于结构化配置，不属于环境变量。邮箱映射含个人账号，只写服务器私有 YAML，不提交仓库。
正式导出使用的校准 profile 和数值参数也必须写入该配置；证据 SHA 留空时由服务从部署包内
证据文件计算，显式填写时必须与实际文件完全一致，否则导出硬失败。

真正的 OAuth client secret 等秘密放单独 `secret.env`，权限同样为 `0600`。仓库只提交
`configs/default.yaml` 与 `configs/secret.env.example`，不提交真实配置或秘密。未来 VM 使用
绑定服务账号访问 GCS，不下载服务账号 JSON key。

当前已实现 `LocalFilesystemStore` 与使用 ADC 的 `GcsObjectStore`、浏览器 MP4 代理、对象
generation 并发控制和 manifest-last 发布。服务器通过专用服务账号访问 Sydney GCS；公网
访问使用 Google IAP JWT，再经过应用私有邮箱映射和 UniKey 白名单。后台队列、上传进度和
自动重试仍是后续迭代。
