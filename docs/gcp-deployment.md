# GCP 标注服务部署与访问

## 本轮边界

标注服务部署在 `soft3888-label` VM，数据放在 Sydney 区域的
`gs://soft3888-label`。服务只监听 VM 的 `127.0.0.1:8766`，本轮不新增公网端口，也不把
现有 8000/80/443 防火墙规则当成标注服务入口。开发者通过 SSH 隧道访问；开放团队访问前
必须另行完成身份认证、TLS 和隐私评审。

VM 使用专用服务账号访问 GCS，不保存服务账号 JSON key。服务账号只获得该 bucket 的
`roles/storage.objectUser`。本机采集端使用开发者 Application Default Credentials（ADC）；
凭据不写入仓库和 YAML。

## 对象布局

```text
gs://soft3888-label/
  captures/<recording_id>/
    capture.h5
    video.mkv
    preview.mp4
    manifest.json
  reviews/<recording_id>/review.json
  diagnostics/sync-experiments/<experiment_id>.json
  exports/<recording_id>/aligned30.h5
  releases/<release_id>/*.tar
  releases/<release_id>/manifest.json
```

采集端先校验并上传三个制品，最后以 generation 前置条件写入 manifest。网络中断不会删除本地
H5/MKV；再次发布会按大小和 SHA-256 幂等跳过一致对象，遇到同名不同内容则拒绝覆盖。
`test` 可以发布用于功能验收和标注流程测试，但标注端永远拒绝把它导出到训练集。
发布 TAR 上传成功后再写入同目录侧车 manifest，记录其中的 recording ID 与 H5 哈希。永久
删除会先查询这些清单；已进入发布或遇到缺少侧车的旧 TAR 时一律拒绝删除。

## 服务部署

服务器私有配置路径固定为 `/etc/imu-annotation/config.yaml`，模板见
`configs/server.annotation.example.yaml`。systemd 单元见
`configs/systemd/imu-annotation.service`。发布目录采用：

```text
/opt/imu-annotation/releases/<git-revision>/
/opt/imu-annotation/current -> releases/<git-revision>
/var/lib/imu-annotation/catalog.sqlite3
/var/lib/imu-annotation/cache/
```

更新前在本机完成 `uv run pytest`、`uv run ruff check .` 和前端双构建。部署包必须包含
`frontend/dist-annotation`。受管 Python 固定安装在 `/opt/imu-annotation/python`，不能留在
`/root/.local/share/uv/python`，否则 `ProtectHome=true` 会使服务入口不可执行。每个 release
在部署阶段用这个解释器执行 `uv sync --frozen --no-dev` 建立只读 `.venv`，运行阶段不再解析
或更新依赖；切换 `current` 后重启服务，并检查：

```bash
sudo systemctl status imu-annotation.service
curl --fail http://127.0.0.1:8766/api/v1/health
```

## 推荐的 GitHub Actions 边界

当前继续使用 GCE VM + systemd + GCS。SQLite catalog、按需 H5 缓存和原子 release 都与这套
运行模型匹配；在 catalog、缓存和并发协调完全外置前，不迁移到无状态且可能多实例的 Cloud
Run。

GitHub Actions 适合作为可审计的 CI/CD 编排，但生产部署不应由普通 push 自动触发：

1. Pull request 只运行后端测试、Ruff、前端采集版与标注版双构建、SOFT3888 兼容性检查，
   并上传带 commit SHA 的不可变发布包。
2. 生产 job 只接受 `workflow_dispatch`，绑定受保护的 `production` environment；环境能力允许
   时启用 required reviewer，并禁止触发者自行批准。
3. GitHub 到 GCP 使用 OIDC + Workload Identity Federation 的短期身份，并把 provider 条件
   限定到准确仓库、分支或 tag；不把服务账号 JSON key 放入 GitHub Secrets。
4. 部署身份只获得上传 release、连接目标 VM 和执行受限部署入口所需的最小权限。VM SSH
   后续启用 OS Login/IAP；部署脚本负责解包、锁文件安装、原子切换、健康检查和失败回滚。
5. workflow 只处理程序制品，绝不下载、打包或上传 GCS 中的参与者 H5/MKV/视频数据。

参考：[GitHub OIDC 到 GCP](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-google-cloud-platform)、
[GitHub deployment environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)、
[Google auth action](https://github.com/google-github-actions/auth) 和
[Compute Engine OS Login](https://cloud.google.com/compute/docs/oslogin)。

## 浏览器访问

本机终端保持以下隧道运行：

```bash
gcloud compute ssh soft3888-label \
  --project project-51b589c7-8d5e-4e78-a10 \
  --zone australia-southeast1-a \
  -- -N -L 8766:127.0.0.1:8766
```

然后浏览器打开 `http://127.0.0.1:8766`。本机采集页面仍是
`http://127.0.0.1:8765`。两者是独立进程；停止标注隧道不会影响本机摄像头或 IMU 采集。

## 回滚与恢复

代码回滚只需把 `/opt/imu-annotation/current` 指回上一个 release 并重启服务。SQLite catalog
只是 GCS manifest 的可重建索引，丢失后调用 `POST /api/v1/index/refresh` 即可重建；
`review.json` 和不可变采集制品不依赖 VM 启动盘。不要通过覆盖对象来“回滚”标注，正式修改
应通过 revision、重开和重新导出完成。
