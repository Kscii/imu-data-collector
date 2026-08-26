# 架构、时间戳与 30 Hz 策略

## 为什么录制阶段不强制“双 30 Hz”

“相机设置为 30 fps”和“每帧实际严格间隔 33.333 ms”不是一回事。UVC 摄像头、驱动、USB 调度和编码器都会造成启动抖动、丢帧或可变间隔；BLE 通知也可能一次批量送来多个 IMU 样本。若在落盘时强制补齐或丢弃，原始时序证据会永久丢失，而且可能把传输抖动误当成传感器运动。

推荐分三层：

1. **采集原始层**：保存通知原始字节、主机 `CLOCK_MONOTONIC` 接收时刻、候选样本和视频真实 PTS；不重采样。
2. **同步标注层**：录制首尾各使用一次镜头可见、IMU 可见的外壳轻拍；人工配对“视频首接触帧”和“IMU 首个明显响应”。默认保留共同主机时钟，只有满足条件时才应用 `video_time = imu_time + fixed_offset`。当前同步、标注和审核快照写入外置 `review.json`。
3. **训练冻结层**：在 IMU/视频公共有效区间上插值到严格 30 Hz，保留 activity/onset/impact/exclude 标注，运行质量门禁，写入可复现的三表 HDF5。训练窗口跳过 exclude；原始 H5/MKV 永不被覆盖。

录制前的设备预览是独立的内存态：连接后持续解析 IMU 通知并输出摄像头低帧率 MJPEG，
只向本机 WebUI 提供实时画面、最近 120 秒六轴曲线、频率、最后一包距今时间、解析错误
和回调丢弃，不创建 H5/MKV。业务状态拆为“设备是否预览”和“文件是否录制”；开始正式
录制时，以新的录制起点清空计数并落盘，结束后自动恢复预览，因此预检样本不会混入原始
采集文件。V4L2 摄像头通常只能由一个 FFmpeg 进程占用，切换落盘状态时允许约一秒内部
进程切换，但页面和设备监看语义保持一致。

因此，转换不应发生在原始落盘阶段，而应发生在标注冻结之后的 adapter/build 阶段。这样更容易修正协议、同步和标签，也能尝试不同窗口策略而不重拍数据。

## 时钟模型

- IMU 通知回调使用 Linux 单调时钟纳秒值，避免系统时间校准或时区变化导致跳变。
- 视频用 V4L2/FFmpeg 提供的逐帧 PTS；H5 同时保存其映射到同一单调时钟域后的值。
- 采集中的临时 MKV 保留 V4L2 单调时钟 PTS。收尾时先把精确 PTS 写入 H5，再以 `-c copy`
  无损重封装交付 MKV，使首帧媒体 PTS 为零。H5 的 `media_time_ns` 用于浏览器 seek，
  `pts_monotonic_ns` 和 `recording_time_ns` 用于同步与标注；重封装前后逐帧时间间隔必须一致。
- 视频编码禁用 B 帧并使用 VFR：保留具有唯一源时间戳的真实帧，不为满足名义 30 FPS
  而复制画面。光线不足导致自动曝光降低真实 FPS 时，应显示并告警实际值，而不是补帧。
- BLE 包内没有已确认的设备时间戳，因此先把“包末样本”关联到接收时间，再根据所有包做线性回归重建样本间隔。该时间是估计值，必须保留质量字段和拟合残差。
- 同步锚点在 H5 和 API 中都使用“相对本次录制起点的纳秒”，避免把开机时长暴露为界面坐标。正式流程恰好使用开始和结束两个轻拍，检查同一连接内偏移是否稳定；当前实证不足以支持把两个点拟合成任意 `scale`，因此正式模型固定 `scale=1`。

### 条件式固定偏移

对开始和结束锚点分别计算 `d_start = video_start - imu_start` 和
`d_end = video_end - imu_end`，估计值为两者平均数。规则固定为：

- `|d_start - d_end| > 100 ms`：锚点不一致，必须重选，不能自动补偿；
- 首尾一致且 `|estimated_offset| < 100 ms`：保留主机时间，不做补偿；
- 首尾一致且 `|estimated_offset| >= 100 ms`：只给出建议，必须由标注者确认后才应用固定偏移；
- 误差区间上界目标不超过 200 ms，超过 500 ms 直接拒绝定稿。

界面同时显示秒、按实际视频中位 FPS 换算的近似帧数，以及按实际 IMU 频率换算的近似样本
数。帧数和样本数只是帮助理解，纳秒时间与来源索引才是保存的事实。

### 正式同步与同步实验的隔离

schema `1.3.0` 起保留以下不可变或可重建证据：

- `imu/packets/receive_time_ns`：Bleak 回调记录的整包到达时间；
- `imu/samples/time_monotonic_ns`：根据全部包到达时间重建的候选样本时间；
- `imu/samples/recording_time_ns`：上述样本时间减去本条录制起点，正式同步不会改写；
- `imu/samples/aligned_video_time_ns`：旧版内嵌同步的兼容列；新同步不再更新它；
- `video/frames/pts_monotonic_ns`：视频帧 PTS 所在的单调时钟时间；
- 根属性 `recording_start_monotonic_ns`：本条录制的相对时间原点。

在同步方法尚未验证期间，逐帧人工观察写入独立的
`_diagnostics/sync_validation_01/*.sync-experiment.json`，不得调用正式 `/sync` 写入。实验分析
始终从 `time_monotonic_ns - recording_start_monotonic_ns` 重新得到 host-only 时间。正式同步、
标注和审核只更新相邻 `review.json`，不能修改原始包、原始计数、主机时间或 MKV。

## 文件组织

```text
~/IMUData/
  _diagnostics/
    <UTC_timestamp>_<operator_unikey>_imu_characterization/
      <recording_id>.h5
      <recording_id>.characterization.json
  <collection_id>/
    <UTC_timestamp>_<participant_unikey>/
      <recording_id>.h5
      <recording_id>.mkv
      review.json
      aligned30.h5                 # 仅通过全部门禁后按需生成
      <recording_id>.capture.tar   # 按需生成，不包含 review.json
```

`_diagnostics` 与正式 collection 分离，诊断 H5 根属性含
`recording_kind=imu_characterization`、`video_status=not_requested` 和
`training_eligible=false`。它用于验证协议、尺度候选和稳定性，不能直接作为训练样本。

不推荐把整个项目所有录制塞进一个不断增长的 H5/MKV：它会放大损坏范围、妨碍并行上传和增量复核。也不推荐按每次跌倒切成海量几秒小文件：拍摄时边界未知，而且大量容器启动会造成管理和时序问题。一次佩戴会话一个文件对，是两者之间更稳妥的原子单位；H5 内可有多个片段。

## HDF5 核心结构

```text
/
  attrs: participant_id, recording_id, data_tier, clock_domain, schema_version, ...
  imu/packets/: payload_values, payload_offsets, receive_time_ns, parse_valid,
                fitted_packet_end_time_ns, fit_residual_ns
  imu/connection_events/: event, time_monotonic_ns, notes
  imu/samples/: raw_counts, trailer, values_si, time_monotonic_ns,
                recording_time_ns, aligned_video_time_ns,
                packet_index, sample_in_packet, time_quality
  video/frames/: pts_monotonic_ns, recording_time_ns, media_time_ns,
                 duration_ns, key_frame
  video attrs: MKV filename, SHA-256, codec, dimensions, actual median fps,
               FFmpeg non-fatal diagnostics
  sync/: 录制结束时的兼容初始快照；后续编辑在 review.json
  annotations/: 录制结束时的兼容初始快照；后续编辑在 review.json
  experiment/stages/: 物理实验阶段、起止时间、可靠性和备注
```

从 schema `1.1.0` 开始，新录制必须包含 `data_tier=test|prod`。从 schema `1.2.0` 开始，
视频必须同时包含主机单调时钟 PTS、录制相对时间和从零开始的 MKV 媒体时间。旧版缺少该属性的
H5 只按 `legacy_unclassified` 读取，不能进入训练集。`test` 是不可升级的数据用途；
文件名即使含有或不含有 `test` 都不参与判定。`prod` 只表示正式采集意图，不等于已经
通过校准、同步、标注和完整性质量门禁。从 schema `1.3.0` 开始，主机相对时间与对齐时间
分列保存，并增加包拟合诊断、BLE 连接事件和显式排除区间。

FFmpeg 的诊断文本会以 JSON 列表随视频元数据保存。例如罗技 C930c 在 MJPEG 启动时
稳定报告一次 `overread 8`，但重复 20 秒实测均为 601 帧、30.0 FPS，且 PTS 无重复或
倒退。因此它暂按非致命诊断保留；帧数、时间戳或进程退出状态异常仍由质量门禁独立判定。

视频体积可按码率预估：6 Mbit/s 约为 2.7 GB/小时，实际还受场景复杂度、编码器和容器开销影响。一个 10–30 分钟录制约 0.45–1.35 GB。Git LFS 技术上能存大文件，但不适合作为持续增长原始视频库：配额、克隆成本、历史不可变和权限治理都不理想。代码、schema、manifest 和小型测试样本进 Git；原始 H5/MKV 走对象存储。当前只实现 `LocalFilesystemStore`；未来服务器使用 Sydney 区域 GCS，部署前再接入真实鉴权和可续传上传。
