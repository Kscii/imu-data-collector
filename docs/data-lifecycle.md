# 数据生命周期与服务器配置合同

## 本地录制原子单位

每个 `recording_id` 有独立本地目录。正常收尾后事实层是同名 H5/MKV；两者不再因同步、
标注或审核而修改。用户在采集页确认后手动发布 `capture.h5`、`video.mkv`、可重建的
`preview.mp4`，最后写入 `manifest.json`。只有 manifest 已出现且三个对象的大小与 SHA-256
一致，标注端才会索引该录制。

`review.json` 只保存当前同步、标注、工作流状态、最新审核意见和单调增加的 revision。并发
保存必须携带预期 revision；过期写入返回冲突，不覆盖新结果。不保留完整编辑历史。

工作流固定为：

```text
unassigned -> in_progress -> submitted -> accepted -> exported
                    ^            |
                    +-- reject --+
```

`review_policy=single_user` 时 submit 直接进入 accepted；`two_person` 时才经过 submitted 且
标注者不能审核自己的提交。管理员只可重开 accepted/exported，重开会使当前导出状态失效。

## 文件产物

- `captures/<recording_id>/manifest.json`：采集端与标注端唯一稳定交接合同，引用原始 H5、
  MKV 和 MP4 代理及其 SHA-256。
- `aligned30.h5`：通过全部门禁后生成的单录制训练文件，只有 `/samples`、`/sequences`、
  `/annotations` 三个根数据集。
- `cw12eu_training_release_NNNN.tar`：把已导出的单录制 H5 汇总成不可变训练发布；manifest
  保存逐文件 SHA-256，SOFT3888 导入器安全校验后拼接为 `cw12eu.h5`。

文件名不编码可变状态。状态来自 catalog 和 `review.json`。已经进入训练发布的录制不能逐条
硬删除；未发布且未被当前会话占用的本地录制，只有输入完整 recording ID 才能永久删除。

## 30 Hz 时间网格

IMU 使用外置同步决定映射到视频时间，取 IMU 与视频公共有效区间作为起点。第 `k` 行的概念
时间严格为 `k/30 s`。数值由已校准 SI 样本按真实时间线性插值，不外推。区间边界使用首个
不早于边界的网格点（ceil）；点事件取最近网格点，恰好半格时向后取整。

本轮只生成 IMU 训练文件。未来视频帧对齐必须使用单独的 multimodal schema，使视频帧 `k`
与 IMU 行 `k` 共用同一时间定义，不能修改当前 SOFT3888 v3 合同。

## 配置与秘密

服务器结构化配置放 `/etc/imu-annotation/config.yaml`，建议 root 所有、服务可读、权限
`0640` 或更严格。九名 UniKey、角色、审核策略、bucket 和路径属于结构化配置，不属于环境变量。

真正的 OAuth client secret 等秘密放单独 `secret.env`，权限同样为 `0600`。仓库只提交
`configs/default.yaml` 与 `configs/secret.env.example`，不提交真实配置或秘密。未来 VM 使用
绑定服务账号访问 GCS，不下载服务账号 JSON key。

当前已实现 `LocalFilesystemStore` 与使用 ADC 的 `GcsObjectStore`、浏览器 MP4 代理、对象
generation 并发控制和 manifest-last 发布。服务器通过专用服务账号访问 Sydney GCS；本轮
仅通过 SSH 隧道单人使用。后台队列、上传进度、自动重试和团队身份认证仍是后续迭代。
