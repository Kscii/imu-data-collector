# 采集、标注、上传与恢复

## 为双模 CW12EU-T 显式启用 BLE 承载

本机 BlueZ 5.87 的 `Device1.PreferredBearer` 与 `Bearer.LE1` 仍属于实验 D-Bus 接口。CW12EU-T 同时提供经典蓝牙与 BLE，默认连接可能错误地选择经典蓝牙，因此需要为 `bluetoothd` 持久增加 `--experimental`。

推荐使用项目脚本：

```bash
cd /home/kscii/Codes/imu-data-collector
sudo ./scripts/install-bluetooth-experimental.sh
```

脚本把 [bluetooth-experimental.conf](../configs/systemd/bluetooth-experimental.conf) 安装到：

```text
/etc/systemd/system/bluetooth.service.d/experimental.conf
```

drop-in 中第一条空的 `ExecStart=` 是 systemd 覆盖列表型设置的必要语法，用于清除软件包服务文件里的旧启动命令；第二条再设置带 `--experimental` 的完整命令。不要直接修改 `/usr/lib/systemd/system/bluetooth.service`，否则软件包升级可能覆盖修改。

首次修复当前样机的错误经典蓝牙记录并验证 LE 偏好：

```bash
bluetoothctl remove 83:FC:90:14:1E:A4
uv run imu-collector probe-gatt
uv run imu-collector probe-imu --seconds 15
bluetoothctl info 83:FC:90:14:1E:A4
```

项目连接代码会在每次重连时显式设置 `PreferredBearer=le` 并调用 `Bearer.LE1.Connect()`。正常停止后，`bluetoothctl info` 应显示 `PreferredBearer: le`，且 `BREDR.Paired`、`BREDR.Bonded` 均为 `no`。

安装会重启蓝牙服务，正在使用的耳机、手柄等会断开，需要重新连接。验证实际生效：

```bash
systemctl show bluetooth.service -p ExecStart -p ActiveState -p SubState
```

需要回滚时运行：

```bash
sudo ./scripts/uninstall-bluetooth-experimental.sh
```

## 采集前

1. 确认参与者 UniKey、知情同意、安全人员和动作清单。
2. 固定 IMU 胸前方向，记录设备与佩戴位置，不在会话中途调整。
3. 手机 nRF Connect 断开 CW12EU-T，避免 BLE 单连接设备被占用。
4. `imu-collector doctor` 检查 BlueZ、摄像头、FFmpeg、磁盘和 rclone。
5. 检查镜头范围和背景隐私；v1 不录音。
6. 开始后做一次明显同步动作，结束前再做一次。

## 录制中 WebUI 应关注

- 视频是否连续且构图正确。
- 实际编码 FPS，而不是只看请求 FPS。
- IMU 六轴曲线是否随动作变化、最后通知时间是否持续更新。
- BLE 包数/样本数、回调丢弃数、磁盘剩余空间、编码器错误。
- 任何设备滑动、重新佩戴或异常都应在会话备注中记录。

## 标注语义

- 一个 recording 可有多个不重叠 segment；未确认区间保持空白，不强迫归入 non-fall。
- fall segment 必须在最终确认前包含 `onset`（跌倒开始）和 `impact`（撞击）事件，且事件位于该 segment 内。
- non-fall segment 不要求这两个事件。
- 时间区间使用半开区间 `[start_ns, end_ns)`，避免相邻片段重复一个样本。
- 每次保存增加 revision；H5 是标签事实来源，不维护会漂移的旁路 CSV。

## 上传边界

Google Drive/rclone 是阶段一远端副本方案，不是 Git 仓库。后台任务必须按以下顺序：上传临时远端名、比较大小/哈希、原子改名或标记完成、写本地 catalog 状态。网络中断只应留下可重试状态，不能删除本地原件。下载恢复也必须核对 H5 中记录的 MKV SHA-256。

建议远端结构与本地 `collection_id/recording_id/` 一致。Google Drive 账户、共享范围和保留周期必须在真人视频开始规模化采集前确认。

## 中断与恢复

- 正常录制先写 `.partial.h5` 与 `.partial.mkv`，完整收尾和验证后才改为最终文件名。
- 崩溃后的 partial 文件只做隔离和诊断：H5 可能仍有完整 chunk，MKV 可能需要 remux；任何自动修复都写到新文件。
- 如果 H5 与 MKV 只剩一个，或哈希不一致，该记录标为 `needs_attention`，禁止进入冻结训练集。
- 原始文件的修复、解析升级和训练派生都必须保留来源 SHA-256 与软件版本。
