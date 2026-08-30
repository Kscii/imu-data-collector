# 参与者身份 v3 迁移运行手册

## 不变量

- 普通录制改为 `YYYYMMDDTHHMMSS.ffffffZ`，collection 改为 `YYYYMMDD_session_NN`；校准证据不迁移。
- 原 H5、视频、review、导出、训练快照、benchmark team 指针先进入管理员受限的
  `identity-migrations/<migration_id>/archive/`，再切换活动命名空间。
- H5 只修改身份覆盖层；迁移程序逐数据集比较名称、dtype、shape 和内容 SHA-256。
- 同步和动作标注保留；参与者清空；已完成任务重开；旧导出、快照和 team current 退出活动区。
- 回滚只允许在任何新 review 写入之前；generation 变化后工具必须拒绝。

## 服务器停机迁移

先停掉标注、上传代理和 GC timer，再以 `keep_stopped=true` 调度生产部署工作流。该模式只切换
并预检新 release，不恢复服务；私有 `subject_ids` 配置、迁移 apply、catalog 重建和验收全部
完成后，才统一启用 timer 并启动两个服务。正常部署保持原有自动恢复行为。

```bash
sudo systemctl stop imu-annotation
imu-annotation --config /etc/imu-annotation/config.yaml migrate-participant-identity
imu-annotation --config /etc/imu-annotation/config.yaml migrate-participant-identity \
  --apply --plan-token '<dry-run 输出>' \
  --confirmation 'MIGRATE PARTICIPANT IDENTITY V3'
```

`identity.subject_ids` 必须在服务启动前写入私有配置，且一对一、只追加，例如
`xfan0282: cw12eu:subject-001`。不要把完整映射写进 Issue、Actions、仓库或前端响应。

应用后重建/刷新 catalog，再启动服务。验收至少检查：普通录制数量不变；旧活动 ID 为零；
校准证据数量不变；manifest 不含 `participant_id`；H5 不含该属性；所有 review 身份未分配；
完成数为零；active export、训练快照和 benchmark team current 均为空。

## 当前采集机和其他桌面客户端

桌面 v3 首次启动会先寻找未完成的本地迁移计划，再扫描旧命名录制。它把原目录移动到
`~/IMUData/_identity_migrations/<migration_id>/archive/`，生成身份中立活动副本并重建 catalog。
也可在停服务后手动 dry-run/apply：

```bash
imu-collector --config ~/.config/imu-data-collector/gcs.yaml migrate-participant-identity
imu-collector --config ~/.config/imu-data-collector/gcs.yaml migrate-participant-identity \
  --apply --plan-token '<dry-run 输出>' \
  --confirmation 'MIGRATE PARTICIPANT IDENTITY V3'
```
