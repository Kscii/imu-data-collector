# 2026-08-30 生产、数据交接与模型目录验收

本文只记录本轮实际观察到的自动化、云端和 WSL2 证据。真实摄像头/IMU 长录制、BLE 断连和
上传断网仍是独立的人工验收，不能由本文替代。

## 代码与部署

- `imu-fall-benchmark` PR #6 已通过 CI 并合并，merge commit 为
  `0ff04e3c490371bf71fbf20dd8997b32a39939d8`。
- `imu-data-collector` PR #25 已通过 Python、前端、Windows installer 和 macOS 两种架构构建，
  merge commit 为 `54546530e7c5539d74fd7ead34aabe5680027695`。
- GitHub Actions production deployment run `33286825805` 通过部署确认、IAP 上传、原子切换、
  systemd 和公网健康门禁。生产 release 指向 collector merge commit。
- VM 上 `imu-annotation.service`、`imu-upload-broker.service` 和
  `imu-annotation-gc.timer` 均为 active；部署工作流验证了标注端和上传端公网健康接口。

## 不可变团队快照

生产服务使用共享合同 `0.1.0` 生成了
`snapshot-3db91b058ac190d007f718b0`。快照身份包含合同版本和规范化录制清单，因此不会与旧合同下
内容相同的快照 ID 冲突，也没有原地覆盖旧对象。

- HDF5 schema：`3.1.0`；采样率：25 Hz；用途：`training_only`；
- 2 条 sequence、6,430 行、39 条 annotation、3 个 impact event；
- HDF5 SHA-256：
  `caa6ec167be723e3412467fb59756dde2781e2ad8f2ea077853b7aee4abf383b`；
- 逻辑内容 SHA-256：
  `6d9cfcf16b8f6ee558fff53b881c4ccb24f0e42894f03c262060d9832a70f547`；
- manifest SHA-256：
  `8a75c0954b8e3ea7167414b8b127db5d756abe26b4c594d0f38878c49fe9ece8`。

激活前，WSL2 从 benchmark clean source snapshot
`0ff04e3c490371bf71fbf20dd8997b32a39939d8` 显式拉取该快照并通过：

1. `data pull --team-snapshot snapshot-3db91b058ac190d007f718b0`；
2. `validate-data`，base 9 个文件和 team 1 个文件全部通过 HDF5 v3.1 校验；
3. `doctor`，RTX 4070 SUPER、CUDA 12.9、PyTorch 2.8.0+cu129 和七种模型后端通过。

随后生产服务通过 CAS 将 team `current.json` 切换到该快照。WSL2 再次不指定快照 ID 执行
`data pull`，确认 Current 解析、缓存校验和 active manifest 均指向新快照。

## ONNX 实验目录

正式 temporal-core run
`formal_baseline_temporal_core_onnx_v1-bfef2ab3d903` 已发布为独立实验目录：

- evidence level：`formal_cv`；
- 65 个 ONNX artifact、13 个方法汇总、65 个计划作业；
- benchmark 端 `experiments verify` 返回 `PASS`；
- 标注平台使用生产 `ModelCatalog` 强制刷新后显示 1 个 available experiment、
  0 个 invalid publication；
- 本轮没有创建产品 `model_release`，也没有把实验目录描述为可直接部署的最终模型。

## 尚未完成的人工验收

- Arch 上至少 30 分钟的摄像头 + CW12EU-T `test` 录制及资源/质量统计；
- 预览和短录制中的 BLE 真实断电恢复；
- 上传期间网络中断、恢复和 manifest-last/无重复对象检查；
- 中文与英文浏览器中的 IAP 页面、目录和文件下载人工复核。
