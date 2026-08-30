# GCP 生产标注服务部署与访问

## 生产拓扑

```text
浏览器
  -> https://imu.kscii.tech
  -> Google Cloud 外部 HTTPS Load Balancer
     -> 5xx 时读取独立维护页 Backend Bucket
  -> Identity-Aware Proxy
  -> soft3888-label:8766
  -> IMU 标注服务
  -> gs://soft3888-label
```

- project：`project-51b589c7-8d5e-4e78-a10`
- VM：`soft3888-label`，zone `australia-southeast1-a`
- backend service：`imu-annotation-backend`
- bucket：`gs://soft3888-label`
- 维护页 bucket：`gs://soft3888-label-maintenance`（只含公开静态维护页）
- 域名：`imu.kscii.tech`

Cloudflare 的 `imu` A 记录必须为 DNS only，让 Google 托管证书和 IAP 终止 HTTPS。VM 目标
标签为 `imu-annotation`，8766 只允许 Google Front End/健康检查来源
`35.191.0.0/16`、`130.211.0.0/22`。

## 身份边界

IAP IAM 授予确切 Google 账号 `roles/iap.httpsResourceAccessor`。应用再验证
`X-Goog-IAP-JWT-Assertion` 的签名、issuer、audience，并从服务器私有
`identity.email_to_unikey` 得到 UniKey。两层任意一层缺失都拒绝访问。

使用应用提供的成员管理命令维护映射，先预演再应用：

```bash
sudo /opt/imu-annotation/current/.venv/bin/imu-annotation \
  --config /etc/imu-annotation/config.yaml member-list

sudo /opt/imu-annotation/current/.venv/bin/imu-annotation \
  --config /etc/imu-annotation/config.yaml \
  member-add --email member@example.com --unikey xabc1234

sudo /opt/imu-annotation/current/.venv/bin/imu-annotation \
  --config /etc/imu-annotation/config.yaml \
  member-add --email member@example.com --unikey xabc1234 --apply
```

命令输出精确 IAP IAM 命令，但不会代为执行。加入成员时先应用 YAML、重启服务、再授予 IAP；
移除成员时先撤销 IAP、再删除 YAML 映射。真实邮箱不进入仓库。

## 对象布局

```text
captures/<recording_id>/{capture.h5,video.mkv,preview.mp4,manifest.json}
reviews/<recording_id>/review.json
exports/<recording_id>/review-<revision>/aligned-<digest>.h5
training-snapshots/<snapshot_id>/{cw12eu_<snapshot_id>.tar,manifest.json}
benchmark-datasets/team/cw12eu/<snapshot_id>/{datasets/cw12eu.h5,manifest.json}
benchmark-datasets/team/cw12eu/current.json
benchmark-datasets/base/<snapshot_id>/{datasets/<dataset_id>.h5,manifest.json}
benchmark-datasets/base/current.json
calibration-evidence/<profile_id>/<recording_id>/
index-receipts/<recording_id>.json
contracts/annotation-capabilities.json
```

bucket 开启 uniform bucket-level access、public access prevention 和 7 天 soft delete。原始媒体
不进 Git/Git LFS。服务器每 10 秒扫描一次新的完整 manifest，管理员也可从页面立即刷新。
benchmark 使用同一个 bucket；`benchmark-datasets/` 已创建为 managed folder，组员的
`roles/storage.objectViewer` 只绑定到该资源，因此不需要也不应访问参与者视频或可变 review。
team HDF5 和 manifest 使用不可变 snapshot 前缀，只有 `current.json` 会用对象 generation
前置条件推进。
标注平台对两个集合只提供经 manifest 解析的只读目录与下载；它不会上传公共数据、删除
benchmark 对象或修改任何 `current.json`。

## GitHub Actions

仓库 `github.com/Kscii/imu-data-collector` 包含：

1. `CI`：PR 和 main push 执行 Ruff、Pytest、数据合同和两套前端构建；
2. `部署生产环境`：仅 main 的手动 `workflow_dispatch`，要求输入
   `DEPLOY PRODUCTION`；
3. `回滚生产环境`：输入服务器已存在的 40 位 commit 与 `ROLLBACK PRODUCTION`。

生产部署保持手动门禁。PR 自动运行 CI 可以避免合并破坏，但不应让每个 PR 自动修改生产。
仓库 Variables：

```text
GCP_PROJECT_ID=project-51b589c7-8d5e-4e78-a10
GCP_WIF_PROVIDER=projects/<project-number>/locations/global/workloadIdentityPools/github/providers/imu-data-collector
GCP_DEPLOY_SERVICE_ACCOUNT=imu-github-deployer@project-51b589c7-8d5e-4e78-a10.iam.gserviceaccount.com
GCP_ZONE=australia-southeast1-a
GCP_VM=soft3888-label
```

WIF provider 同时限制不可变 repository ID `1347062318` 和 `refs/heads/main`。部署身份具有
IAP tunnel、OS Admin Login，并仅能 impersonate VM 运行时服务账号；不保存 GCP JSON key。

## VM 原子部署

```text
/opt/imu-annotation/releases/<git-commit>/
/opt/imu-annotation/current -> releases/<git-commit>
/var/lib/imu-annotation/catalog.sqlite3
/var/lib/imu-annotation/cache/
/etc/imu-annotation/config.yaml
```

`imu-annotation-deploy` 校验 commit、包 SHA-256、Python、私有配置及回滚兼容性后才原子切换
symlink，并重启 `imu-annotation.service`。失败自动尝试上一版本。GitHub Runner 经 IAP 上传
时使用传统 SCP `-O`，并以远端大小和 SHA-256 一致为通过条件。

## 每次发布验收

```bash
sudo systemctl status imu-annotation.service --no-pager
sudo systemctl status imu-annotation-gc.timer --no-pager
curl --fail http://127.0.0.1:8766/api/v1/health
```

随后按 [生产验收与监控](production-acceptance.md) 完成公网、权限、标注、快照和恢复检查。健康
接口不含敏感数据，其余 API 都要求可信身份。SQLite 只是可重建索引，事实数据在 GCS。

## 应用停机时的维护提示

维护页不能由 `imu-annotation.service` 自己提供，否则进程停机或部署失败时页面也会消失。
生产 URL map 仅在 `imu.kscii.tech` 的应用后端返回 `5xx` 时，从独立 Backend Bucket 返回
`scripts/deploy/maintenance.html`；不覆盖原始状态码，因此公网健康检查仍会失败，
`upload.imu.kscii.tech` 也不会收到 HTML 错误页。私有数据 Bucket 继续保持 public access
prevention，只有单独的维护页 Bucket 可公开读取。

首次配置或需要幂等修复时，在已登录正确项目的受控终端运行：

```bash
./scripts/deploy/configure-maintenance-error-page \
  'CONFIGURE MAINTENANCE imu.kscii.tech'
```

脚本先导出当前 URL map，再创建或复用维护 Bucket 与 Backend Bucket，验证修改后的 URL map
后才导入。验收应分别确认：正常服务仍进入 IAP；临时停止 8766 后域名显示维护页且 HTTP
状态仍为 5xx；上传域名 `/health` 不受影响。回滚时从 URL map 删除
`imu-annotation-maintenance-aware` matcher，并把 `imu.kscii.tech` host rule 恢复到原应用 matcher。

## 校准证据迁移

新版本首次部署后先预演：

```bash
sudo -u kscii /opt/imu-annotation/current/.venv/bin/imu-annotation \
  --config /etc/imu-annotation/config.yaml archive-calibration-evidence
```

只有输出精确包含预期的 15 条录制、全部制品可验证时，才运行：

```bash
sudo -u kscii /opt/imu-annotation/current/.venv/bin/imu-annotation \
  --config /etc/imu-annotation/config.yaml \
  archive-calibration-evidence --apply --delete-source
```

迁移后检查校准页可播放视频、下载 H5，并确认这些录制不再出现在动作标注队列。普通测试数据
的批量清理不能和校准迁移混为一个命令。首次执行使用
[2026-08-27 首次生产数据迁移清单](production-data-migration-2026-08-27.md) 核对精确 ID、
预期对象数和恢复边界。
