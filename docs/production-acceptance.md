# 生产验收与监控

## 上线结论的分级

必须分别记录三种结论，不能把自动化测试等同于生产可用：

1. **代码验收**：Ruff、全部 Pytest、采集前端构建、标注前端构建通过；
2. **服务器验收**：systemd、回环健康、GCS 读写、后台扫描和垃圾回收 timer 正常；
3. **人工业务验收**：真实 IAP 登录、任务领取/接管、同步、完整标注、完成、下载和训练快照。

只有三项都通过，才标记版本“可用于正式团队采集”。

## 发布前自动门禁

```bash
uv run ruff check src tests
uv run pytest -q
cd frontend
npm ci
npm run build
```

还必须确认工作树中没有真实录制、邮箱映射、OAuth secret、GCP key 或服务器私有 YAML。

## 服务器检查

```bash
sudo systemctl is-active imu-annotation.service
sudo systemctl is-active imu-upload-broker.service
sudo systemctl is-active imu-annotation-gc.timer
curl --fail http://127.0.0.1:8766/api/v1/health
curl --fail http://127.0.0.1:8770/health
sudo journalctl -u imu-annotation.service -n 100 --no-pager
sudo journalctl -u imu-upload-broker.service -n 100 --no-pager
sudo journalctl -u imu-annotation-gc.service -n 100 --no-pager
```

检查当前 symlink 指向预期 commit，服务日志没有反复重启、GCS 权限错误、catalog 扫描错误或
校准证据不一致。垃圾回收先执行 dry-run，不能在发布验收中顺带清理未知对象。

## 浏览器业务冒烟

使用管理员账号：

- Windows 安装包内不存在 `google_oauth_client_secret`；首次 Google 登录经上传代理成功，刷新或
  重启本机服务后仍可用系统凭据库中的 refresh token 获取短期 ID token；
- macOS 两个 DMG 的 CPU 架构、最低系统版本、ad-hoc 签名、随包 FFmpeg/x264 和 SHA-256
  均通过 CI；Intel 真机按 [macOS 桌面测试版](macos-desktop.md) 完成权限、BLE、摄像头、
  完整 test 录制和 Keychain 上传。Apple Silicon 未做真机验收时必须明确记录为 CI-only；
- token exchange 拒绝非 loopback 回调，错误页和服务日志不包含 code、verifier、refresh token
  或 client secret；
- IAP 登录后页首显示正确 UniKey，不能切换身份；
- 刷新后仍停留在 URL 指定的当前页面；
- 刷新 Bucket 后新 manifest 进入列表，异常条目显示稳定错误码和录制 ID；
- 未领取时轻拍、同步和标注按钮明确禁用，并显示“先领取任务”；
- 领取后按钮可用；另一成员只能查看，明确接管后才可编辑；
- 使用一条专门的 prod 验收录制完成首尾轻拍、同步、全覆盖、impact 和完成；
- 完成后任务只读且能下载新的 aligned30；重开会移除当前导出入口；
- 再次完成生成不同 revision 的不可变 aligned30，旧对象不会被误下载；
- 生成训练快照，重复点击相同内容复用同一 snapshot ID；
- test 录制能下载原始 H5/review，但不能完成或进入训练快照；
- 校准证据页的 15 条记录可播放、可下载 H5，且不出现在标注队列。
- 校准证据列表只在自身区域滚动，不得在页面滚动时覆盖右侧视频或实验说明；窄屏时改为上下布局。
- 使用 `?lang=zh-CN` 与 `?lang=en` 各验收一次；无参数时 `zh-*` 浏览器显示中文，其余语言显示
  英文。两种模式都不得出现另一种语言的按钮、提示、校准语义或 API 错误。
- 下载训练 TAR 后运行 SOFT3888 的 `validate-team`；确认内层 manifest `1.0.0`、aligned HDF5
  `3.0.0`、逐文件大小与 SHA-256 全部通过，再执行 `import-team`。

使用未授权账号确认 IAP 拒绝；使用“有 IAP、无应用映射”的测试账号确认应用拒绝。不要用真实
组员账号做破坏性权限实验。

## 必须监控的信号

P0 告警：

- `https://imu.kscii.tech/api/v1/health` 连续 5 分钟不可达；
- HTTPS 5xx 比例连续 5 分钟超过 5%；
- VM 启动盘使用率超过 85%；
- `imu-annotation.service` 反复重启或 10 分钟内无健康响应。

P1 告警：

- catalog 最近成功扫描时间超过 5 分钟；
- 最近扫描存在持续重复的 GCS 权限或 schema 错误；
- GCS bucket 容量或每日增长异常；
- 垃圾回收连续两次失败；
- aligned30 或训练快照构建失败率异常。

Cloud Monitoring 创建 HTTPS uptime check 和上述告警策略。通知渠道属于组织选择，至少配置
管理员邮箱；没有已确认通知渠道时不要创建“无人接收”的假告警。Billing Budget 建议使用
A$50、A$80、A$100 三档；预算只通知，不自动停机。

## 发布记录

每次生产发布至少记录：

- Git commit 与部署时间；
- 自动化门禁结果；
- VM 与公网健康结果；
- 人工业务冒烟使用的专用 recording ID；
- 校准 profile ID 与证据摘要；
- 已知限制和回滚 commit。

不要把参与者姓名、邮箱、视频截图、标注内容或临时登录链接写进公开 Issue/Actions 日志。

## 当前仍需人工完成的首发门禁

- 一次真实 prod 录制的完整云端三态流程；
- 两名实际成员的领取、只读和明确接管验证；
- 15 条校准证据归档后的云端页面复核；
- Cloud Monitoring 通知渠道和告警实际触发测试；
- 知情同意、跌倒动作安全和删除申请流程。

这些项目不是单元测试可以替代的；未完成时可以继续内部试用，但不能宣称“生产闭环已经完全验收”。
