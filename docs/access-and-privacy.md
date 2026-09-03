# 团队访问、隐私与删除边界

## 身份与权限

团队成员必须同时通过两层门禁：

1. Google IAP IAM 允许该 Google 账号访问后端服务；
2. 服务器私有 YAML 把该邮箱唯一映射到一个允许的 UniKey。

后端验证 IAP JWT 后确定当前成员，不信任浏览器自报的操作者。页面不能切换成其他成员。

所有成员可以查看、领取和明确接管任务；只有当前负责人可以修改进行中的任务。完成后只读，
当前负责人或管理员可重开。标签管理域内所有白名单成员同权，可以创建、改名、启停、迁移和
删除未使用标签；变更由不可变 taxonomy 版本记录操作者。管理员另有刷新 Bucket、清理训练快照
等运维权限，这些权限不会因为开放标签管理而下放。

## 成员加入和移除

成员管理命令默认只预演，不修改配置，也不直接执行 IAM 命令：

```bash
sudo /opt/imu-annotation/current/.venv/bin/imu-annotation \
  --config /etc/imu-annotation/config.yaml \
  member-add --email member@example.com --unikey xabc1234
```

确认 JSON 里的配置、邮箱、UniKey 和 `iap_command` 后，增加成员按以下顺序：

1. 使用相同命令加 `--apply` 原子更新私有 YAML；
2. 重启并确认 `imu-annotation.service` 健康；
3. 执行输出的 `gcloud iap web add-iam-policy-binding ...`。

移除成员应先执行预演输出的 `remove-iam-policy-binding` 立即阻断入口，再用
`member-remove --apply` 删除映射并重启。不要把真实邮箱映射提交到 Git。

## 数据用途与可见内容

- `test`：流程联调，可查看视频、曲线并下载原始 H5/review；不能生成训练数据；
- `prod`：通过全部门禁后才能完成并进入训练快照；
- 校准证据：只读展示设备坐标和物理尺度，不进入动作标注队列；
- 视频：标注与质量证据，不进入训练 HDF5，也不提交 Git/Git LFS；
- 训练快照：训练 TAR 和 benchmark HDF5 只包含 25 Hz IMU；快照另外冻结视频引用和时间映射，
  仅在已授权的客户交付 ZIP 与团队只读查看器中使用原始可识别视频。

benchmark 组员只需读取同一 bucket 中已启用的 `benchmark-datasets/` managed folder。不要为了
简化权限而授予整个 bucket 的对象查看权，否则会同时暴露原始视频、review 和身份相关元数据。
具体 IAM 成员仍须在拿到团队 Google 账号清单后由管理员配置，账号不能写进仓库。

成员会看到参与者影像、IMU 数据和标注，因此账号不得共享。人体采集前必须另行完成知情同意、
动作安全、摄像范围和数据保留约定；代码的 MIT License 不等于参与者数据开放授权。

## 删除与恢复

录制和平台侧训练 TAR 都使用精确二次确认。应用删除活动对象后，GCS 仍按 bucket 策略保留
7 天软删除版本；普通 WebUI 不提供恢复入口，恢复只能由 GCP 管理员在保留期内进行。已经发布
到 `benchmark-datasets/` 的不可变 snapshot 不随平台 TAR 清理删除，以免破坏既有实验复现。

校准证据归档是当前样机工程参数的来源，不应作为普通垃圾录制删除。普通同步实验、平台联调和
失败录制在确认不再作为证据后可以清理。任何批量删除都必须先列出精确 recording ID、用途、
对象数、总大小和恢复窗口，再执行。
