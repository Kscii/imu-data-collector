# GCP 生产标注服务部署与访问

## 已冻结的生产拓扑

```text
浏览器
  -> https://imu.kscii.tech
  -> Google Cloud 外部 HTTPS Load Balancer
  -> Identity-Aware Proxy（Google 账号登录与 IAM 白名单）
  -> soft3888-label:8766
  -> 应用验证 IAP 签名 JWT，再把私有邮箱映射为 UniKey
  -> gs://soft3888-label
```

- GCP project：`project-51b589c7-8d5e-4e78-a10`
- VM：`soft3888-label`，`australia-southeast1-a`
- 对象存储：`gs://soft3888-label`
- 生产域名：`imu.kscii.tech`
- OAuth 应用显示名：`IMU Data Collector`
- 支持邮箱：`xuejian.fang.kscii@gmail.com`

Cloudflare 只新增 `imu` 子域，不修改当前根域记录。`imu` 使用指向负载均衡静态 IPv4 的 A
记录并保持 **DNS only（灰云）**，让 Google 托管证书和 IAP 直接终止 HTTPS。

VM 目标标签使用 `imu-annotation`。专用防火墙只允许 Google Front End/健康检查来源
`35.191.0.0/16`、`130.211.0.0/22` 访问 8766。当前 VM 同时承载根域既有服务，因此不增加
覆盖整台 VM 的 deny，也不修改供其他服务/VM 使用的 22、80、443、8000 或共享默认规则；
这些既有规则本身并没有开放 8766。

## 双重身份门禁

IAP IAM 是第一层：只授予确切 Google 账号 `roles/iap.httpsResourceAccessor`。应用是第二层：
验证 `X-Goog-IAP-JWT-Assertion` 的签名、issuer 与 backend service audience，再查服务器私有
YAML 的 `identity.email_to_unikey`。两层任意一层没有该账号都拒绝访问。

第一位成员映射为：

```yaml
identity:
  email_to_unikey:
    "xuejian.fang.kscii@gmail.com": "xfan0282"
```

真实映射保存在 `/etc/imu-annotation/config.yaml`，权限 `0640`，不提交 Git。公网请求体里的
`actor_id`、`annotator_id` 或 `reviewer_id` 不是可信身份；保存时服务端一律覆盖为当前会话
UniKey。`POST /api/v1/index/refresh` 只允许配置中的管理员调用。

## 对象布局和保留

```text
gs://soft3888-label/
  captures/<recording_id>/capture.h5|video.mkv|preview.mp4|manifest.json
  reviews/<recording_id>/review.json
  diagnostics/sync-experiments/<experiment_id>.json
  exports/<recording_id>/review-<revision>/aligned30-<digest>.h5
  releases/<release_id>/*.tar|manifest.json
  release-tombstones/<release_id>.json
```

存储桶启用 uniform bucket-level access、public access prevention 和 7 天 soft delete。原始媒体
不进 Git/Git LFS。`test` 可以上传、标注并下载不可变原始 `capture.h5` 和当前 `review.json`，
用于联调与结构展示；不能生成训练 `aligned30.h5`，也不会进入 release 或 SOFT3888 `raw`。
训练发布位于 `releases/`，相同内容用指纹去重。撤销后有效 TAR 和 manifest 进入软删除，轻量
墓碑继续保留。

每日 systemd timer 运行孤儿清理：删除超过 7 天仍无 manifest 的中断 capture、没有当前
review/release 引用的旧导出，以及缺少 manifest 的 release TAR。当前有效导出、有效发布引用
和完整录制不自动清理。

## GitHub Actions

公开仓库 `github.com/Kscii/imu-data-collector` 使用三类 workflow：

1. `CI`：PR 和 main push 执行 Ruff、全部 Pytest、temporal v3 合同和两套前端构建。
2. `部署生产环境`：只接受 main 上手动 `workflow_dispatch`，并要求输入
   `DEPLOY PRODUCTION`；重新跑全部门禁后打最小部署包，经 WIF/OIDC 和 IAP TCP 转发部署。
3. `回滚生产环境`：手动输入已存在的 40 位 commit 和 `ROLLBACK PRODUCTION`，原子切回。

`production` environment 不要求第二位审批者，符合当前单人阶段；仍应限制 deployment branch
为 `main`。仓库需要以下 Variables，不需要 GCP JSON key 或 OAuth client secret：

```text
GCP_PROJECT_ID=project-51b589c7-8d5e-4e78-a10
GCP_WIF_PROVIDER=projects/<project-number>/locations/global/workloadIdentityPools/github/providers/imu-data-collector
GCP_DEPLOY_SERVICE_ACCOUNT=imu-github-deployer@project-51b589c7-8d5e-4e78-a10.iam.gserviceaccount.com
GCP_ZONE=australia-southeast1-a
GCP_VM=soft3888-label
```

WIF provider 的 attribute condition 必须同时限制不可变 repository ID `1347062318` 和
`ref == refs/heads/main`。目标 VM 单独启用 OS Login；部署服务账号授予 IAP tunnel、OS Admin
Login，并仅在 VM 附加的运行时服务账号上授予 Service Account User。部署账号没有直接的 GCS
角色，workflow 也不下载 H5/MKV；但 OS Admin 可以在 VM 内取得该 VM 运行时身份，因此仍应把
部署身份视为生产特权身份，保持 WIF 仓库和 main ref 限制。

OpenSSH 默认 SFTP 模式在 GitHub Runner 经 IAP/OS Login 上传时会建立远端空文件但不继续传输。
生产 workflow 明确向 `gcloud compute scp` 传入 `--scp-flag=-O` 使用传统 SCP；真实探针必须以
远端文件大小和 SHA-256 一致为通过条件，不能只依据 SSH 会话已建立。

## VM 原子部署

服务器私有配置固定为 `/etc/imu-annotation/config.yaml`。代码目录为：

```text
/opt/imu-annotation/releases/<git-commit>/
/opt/imu-annotation/current -> releases/<git-commit>
/var/lib/imu-annotation/catalog.sqlite3
/var/lib/imu-annotation/cache/
```

一次性 bootstrap 把仓库中的 `scripts/deploy/imu-annotation-deploy` 和
`imu-annotation-rollback` 安装到 `/usr/local/sbin/`。部署入口验证文件名、commit、SHA-256 和
TAR 路径，只从 `/opt/imu-annotation/python/cpython-3.12.*-linux-x86_64-gnu/` 中选择最新的
完整补丁版本并再次核对解释器版本。它不会把 `cpython-3.12-linux-x86_64-gnu` 这类通用目录
误判为第二套解释器。源码先进入临时目录，移动到最终 release 路径后才使用锁文件建立
虚拟环境，避免入口脚本和可编辑安装残留 `.incoming-*` 绝对路径。入口、Python 导入、当前私有
配置和上一版本回滚兼容性全部预检通过后，才写入 `.deployment-ready` 完成标记并原子切换
symlink、重启和检查回环健康接口。自动部署包固定文件顺序、时间戳和属主，并排除 Python
缓存。新版本
失败时自动尝试回到上一版本。回滚 workflow 只能切到服务器已保留的 release 目录。

服务运行身份 `kscii` 通过 VM 专用服务账号访问 GCS，不保存服务账号 JSON key。应用绑定
`0.0.0.0:8766` 是为了接收负载均衡流量；防火墙仅给 Google Front End 来源开放该端口。
健康检查 `/api/v1/health` 不包含敏感数据；其余 `/api/v1/*` 都要求可信身份。

## 发布验收和故障处理

每次发布至少检查：

```bash
sudo systemctl status imu-annotation.service
sudo systemctl status imu-annotation-gc.timer
curl --fail http://127.0.0.1:8766/api/v1/health
```

浏览器访问 `https://imu.kscii.tech` 时应先进入 Google 登录；未加入 IAP IAM 的账号被拒绝，
加入 IAP 但没有私有邮箱映射的账号也被拒绝。合法账号进入后，页首应显示自己的 UniKey，且
页面不能切换成别人。

SQLite catalog 是可重建索引。服务器默认每 10 秒扫描一次 Bucket，新 manifest 会自动进入
索引；管理员也可以在页面点击“立即扫描 Bucket”强制刷新。`review.json` 和不可变采集制品
不依赖 VM 启动盘。不要覆盖对象来“回滚”标注，正式修改通过 revision、重开和重新导出完成。

Cloud Monitoring 至少为实例不可达、HTTPS 5xx 和磁盘空间设置基础告警；Billing Budget 建议
设置 A$50、A$80、A$100 三档通知。Budget 是告警，不会自动停止资源。
