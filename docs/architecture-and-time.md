# 架构、时间戳与 30 Hz 策略

## 为什么录制阶段不强制“双 30 Hz”

“相机设置为 30 fps”和“每帧实际严格间隔 33.333 ms”不是一回事。UVC 摄像头、驱动、USB 调度和编码器都会造成启动抖动、丢帧或可变间隔；BLE 通知也可能一次批量送来多个 IMU 样本。若在落盘时强制补齐或丢弃，原始时序证据会永久丢失，而且可能把传输抖动误当成传感器运动。

推荐分三层：

1. **采集原始层**：保存通知原始字节、主机 `CLOCK_MONOTONIC` 接收时刻、候选样本和视频真实 PTS；不重采样。
2. **同步标注层**：使用镜头可见且 IMU 可见的动作锚点拟合 `video_time = scale × imu_time + offset`；所有标注都使用录制起点后的纳秒时间，并同时保存最近的视频帧和 IMU 样本索引。
3. **训练冻结层**：只截取已标注 IMU 片段，在低通/抗混叠约束下插值到严格 30 Hz，运行质量门禁，写入可复现的标准 HDF5。原始 H5/MKV 永不被覆盖。

因此，转换不应发生在原始落盘阶段，而应发生在标注冻结之后的 adapter/build 阶段。这样更容易修正协议、同步和标签，也能尝试不同窗口策略而不重拍数据。

## 时钟模型

- IMU 通知回调使用 Linux 单调时钟纳秒值，避免系统时间校准或时区变化导致跳变。
- 视频用 V4L2/FFmpeg 提供的逐帧 PTS；H5 同时保存其映射到同一单调时钟域后的值。
- BLE 包内没有已确认的设备时间戳，因此先把“包末样本”关联到接收时间，再根据所有包做线性回归重建样本间隔。该时间是估计值，必须保留质量字段和拟合残差。
- 同步锚点在 H5 和 API 中都使用“相对本次录制起点的纳秒”，避免把开机时长暴露为界面坐标。至少两个分散在录制前后的物理锚点才能估计 offset 与 drift；一个锚点只能估计 offset。三个或更多锚点才能用残差发现某个锚点的误差。

## 文件组织

```text
~/IMUData/
  <collection_id>/
    <UTC_timestamp>_<participant_unikey>/
      <recording_id>.h5
      <recording_id>.mkv
```

不推荐把整个项目所有录制塞进一个不断增长的 H5/MKV：它会放大损坏范围、妨碍并行上传和增量复核。也不推荐按每次跌倒切成海量几秒小文件：拍摄时边界未知，而且大量容器启动会造成管理和时序问题。一次佩戴会话一个文件对，是两者之间更稳妥的原子单位；H5 内可有多个片段。

## HDF5 核心结构

```text
/
  attrs: participant_id, recording_id, clock_domain, schema_version, ...
  imu/packets/: payload_values, payload_offsets, receive_time_ns, parse_valid
  imu/samples/: raw_counts, trailer, values_si, time_monotonic_ns,
                recording_time_ns, packet_index, sample_in_packet, time_quality
  video/frames/: pts_monotonic_ns, recording_time_ns, duration_ns, key_frame
  video attrs: MKV filename, SHA-256, codec, dimensions, actual median fps
  sync/: anchors, scale, offset, residual, quality
  annotations/segments/: labeled half-open intervals [start_ns, end_ns)
  annotations/events/: onset/impact time plus nearest frame/sample provenance
```

视频体积可按码率预估：6 Mbit/s 约为 2.7 GB/小时，实际还受场景复杂度、编码器和容器开销影响。一个 10–30 分钟录制约 0.45–1.35 GB。Git LFS 技术上能存大文件，但不适合作为持续增长原始视频库：配额、克隆成本、历史不可变和权限治理都不理想。代码、schema、manifest 和小型测试样本进 Git；原始 H5/MKV 走对象/云盘存储。当前规模先用 rclone + Google Drive，未来并发、生命周期或数据量显著上升时再迁移 S3 兼容对象存储。
