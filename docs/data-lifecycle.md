# 数据生命周期与服务器配置合同

## 本地录制原子单位

每个 `recording_id` 有独立目录。正常收尾后事实层是同名 H5/MKV；两者不再因同步、标注或
审核而修改。第一次访问旧录制时，平台从 H5 的兼容快照迁移并生成相邻 `review.json`，其中
记录源文件大小和 SHA-256。

`review.json` 只保存当前同步、标注、工作流状态、最新审核意见和单调增加的 revision。并发
保存必须携带预期 revision；过期写入返回冲突，不覆盖新结果。不保留完整编辑历史。

工作流固定为：

```text
unassigned -> in_progress -> submitted -> accepted -> exported
                    ^            |
                    +-- reject --+
```

标注者不能审核自己的提交。管理员可重开 accepted/exported，重开会使当前导出状态失效。

## 文件产物

- `<recording_id>.capture.tar`：手动按需生成的无压缩源包，包含 manifest、capture H5 和 MKV，
  不包含 `review.json`。
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

服务器结构化配置放 `/etc/imu-annotation/config.yaml`，建议 root/服务用户所有、权限 `0600`。
九名用户的已验证 Google 邮箱/subject、UniKey 和角色属于结构化配置，不属于环境变量。

真正的 OAuth client secret 等秘密放单独 `secret.env`，权限同样为 `0600`。仓库只提交
`configs/default.yaml` 与 `configs/secret.env.example`，不提交真实配置或秘密。未来 VM 使用
绑定服务账号访问 GCS，不下载服务账号 JSON key。

当前只实现 `LocalFilesystemStore`。获得服务器后再实现 Sydney 区域 GCS、可续传上传、短期
签名 URL、浏览器 MP4 代理和 IAP JWT 验证；生产 IAP 模式不得回退到本机身份头。
