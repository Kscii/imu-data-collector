# 桌面 OAuth 与上传代理

## 为什么不能把服务账号放进安装包

Windows/macOS 安装包会复制到每名组员的电脑。若内置 GCP 服务账号 JSON，任何拿到安装包的
人都能提取长期私钥并绕过 WebUI，因此桌面端禁止使用 ADC 文件或共享服务账号。

当前方案分为三层：

1. 浏览器通过 Google Desktop OAuth Authorization Code + PKCE 登录，权限仅为
   `openid email profile`；
2. refresh token 保存到 Windows Credential Manager、macOS Keychain 或系统 keyring，短期
   ID token 只在进程内存中；
3. 上传代理校验 Google 签名、client ID、已验证邮箱和服务器私有邮箱到 UniKey 映射，再签发
   单次、单录制、固定对象键的 GCS resumable upload URL。

现有 `imu.kscii.tech` 的 IAP 继续保护标注平台；它与桌面 OAuth 是两个独立边界，不能把 IAP
cookie 当成本地程序凭据。

## 数据完整性边界

桌面端先在本地生成 H5、原始 MKV 和可重建 MP4，计算大小与 SHA-256，并提交 manifest 2.1
草案。上传代理只允许：

```text
captures/<recording_id>/capture.h5
captures/<recording_id>/video.mkv
captures/<recording_id>/preview.mp4
```

三个对象都上传后，代理从 GCS 重新流式读取并计算 SHA-256；只有大小和摘要全部与草案一致，
才以 `if_generation_match=0` 写入 `manifest.json`。因此不完整上传不会出现在标注索引中，重试
也不能覆盖另一个已发布录制。

## 配置边界

组员不需要编辑 YAML。`desktop-v*` 的 GitHub Actions 构建从两个公开仓库变量生成安装包内配置：

- `DESKTOP_UPLOAD_BROKER_URL=https://upload.imu.kscii.tech`
- `DESKTOP_GOOGLE_OAUTH_CLIENT_ID=<desktop-client-id>.apps.googleusercontent.com`

桌面端通过 PKCE 取得一次性授权码，再把授权码、verifier 和固定 loopback 回调地址发给上传代理。
代理从服务器私有环境变量读取 Google client secret 并完成 token exchange；后续 refresh token
刷新也走同一个代理。安装包和 GitHub Release 因此不包含 client secret，更绝不包含 GCP 服务账号
私钥。真正的授权边界还包括代理对 ID token 的签名、audience、邮箱和白名单校验。

生成后的公开配置等价于：

```yaml
publish:
  mode: broker

storage:
  backend: local

cloud:
  broker_url: "https://upload.imu.kscii.tech"
  google_oauth_client_id: "<desktop-client-id>.apps.googleusercontent.com"
  scopes: [openid, email, profile]
```

上传代理使用同一个 client ID，并配置 GCS 与白名单：

```yaml
storage:
  backend: gcs
  bucket: soft3888-label
  project: <gcp-project-id>

cloud:
  broker_server_host: "127.0.0.1"
  broker_server_port: 8770

identity:
  email_to_unikey:
    member@example.com: rkim6933
```

生产环境不要把新版本专用的 OAuth 字段写入标注服务和上传代理共用的 YAML，否则旧版本在
部署回滚预检时可能无法解析。Client ID 由仅上传代理读取的
`/etc/imu-annotation/upload-broker.env` 提供；该文件必须仅允许受控运维用户读取：

```dotenv
IMU_GOOGLE_OAUTH_CLIENT_ID=<desktop-client-id>.apps.googleusercontent.com
IMU_GOOGLE_OAUTH_CLIENT_SECRET=<Google 桌面客户端签发的 client secret>
IMU_UPLOAD_BROKER_HOST=0.0.0.0
```

systemd 单元通过 `EnvironmentFile=-/etc/imu-annotation/upload-broker.env` 加载该文件；减号表示
开发环境中该文件缺失时仍允许启动，但生产环境必须配置并由验收检查确认。生产进程监听
`0.0.0.0:8770` 是为了接收负载均衡器从 VM 内网地址发来的请求；GCP 防火墙仍只允许负载均衡
和健康检查来源访问该端口，不把 8770 直接开放给公网。

邮箱映射、client secret 和项目私有值不提交仓库。Desktop OAuth client ID 是公开标识，可以进入
安装包；client secret 只由上传代理读取。代理 VM 的服务账号至少需要创建对象、读取对象与读取
对象 metadata 的权限，不把该身份授予桌面用户。

当前 Arch 管理机是受控运维环境，私有配置显式使用 `publish.mode=direct_gcs` 和 ADC；不能把该
配置复制给组员。

## 启动与验收

代理入口：

```bash
IMU_GOOGLE_OAUTH_CLIENT_ID=<desktop-client-id>.apps.googleusercontent.com \
IMU_GOOGLE_OAUTH_CLIENT_SECRET=<desktop-client-secret> \
  uv run imu-upload-broker --config /etc/imu-annotation/config.yaml
```

本地默认监听 `127.0.0.1:8770`，与标注服务的 `8766` 分离；生产环境通过上述专属环境变量
监听 VM 网卡，并且外部只暴露 HTTPS 负载均衡入口。

部署时应由 HTTPS 负载均衡或反向代理终止 TLS，进程本身继续监听回环地址。生产验收至少包括：

- 非白名单 Google 邮箱得到 403；错误 audience、过期 token 和未验证邮箱得到 401/403；
- 安装包、GitHub Release、WebUI 配置接口和日志中不存在 client secret；
- token exchange 拒绝非 loopback 回调，并且错误响应不回显 code、verifier 或 refresh token；
- 对象键越界、schema 不支持、同 ID 不同摘要得到 409/422；
- 断网后本地后台任务保留并可重试，不删除 H5/MKV；
- 三个对象任一缺失或 SHA-256 不匹配时没有 manifest；
- 成功写 manifest 后标注端扫描并产生 indexed 回执；
- Windows 注销后系统凭据库中 token 被删除，源码目录和日志中没有 token 或 resumable URL。

默认配置 `publish.mode=local`，因此源码开发环境只会归档到本机；正式桌面安装包由 CI 注入 `publish.mode=broker` 和公开的 OAuth 客户端配置。

## 组员实际操作

1. 安装并双击打开数采平台；录制和预览不要求登录。
2. 第一次发布时点击按钮，浏览器弹出 Google 登录；只申请身份信息，不申请 Drive/GCS 权限。
3. 登录成功窗口自动关闭，原发布任务继续；refresh token 由系统凭据库持久保存。
4. `prod` 通过收尾门禁后自动进入后台上传，`test` 由成员手动点击上传。
5. 页面把“仅保存在本机”“等待 Google 登录”“已上传团队 Bucket”“标注端已接收”分开显示。

退出账号会尽力撤销 Google refresh token，并无条件删除本机凭据。上传失败只暂停或重试后台
任务，不删除原始 H5/MKV，也不阻塞新的录制。
