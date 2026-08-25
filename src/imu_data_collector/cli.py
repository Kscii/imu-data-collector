"""启动及诊断数采平台的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path

import h5py
import uvicorn

from imu_data_collector.api import create_app
from imu_data_collector.ble import CW12EUBleSource
from imu_data_collector.characterization import (
    correct_characterization_stage,
    recover_interrupted_characterization,
    write_accel_pose_pair_report,
    write_characterization_report,
)
from imu_data_collector.config import load_activity_taxonomy, load_settings
from imu_data_collector.coordinator import RecordingCoordinator
from imu_data_collector.cw12eu import parse_notification
from imu_data_collector.models import (
    CharacterizationStage,
    CharacterizationStageRequest,
    CharacterizationStartRequest,
)
from imu_data_collector.validation import validate_capture_h5
from imu_data_collector.video import (
    FFmpegVideoRecorder,
    discover_video_devices,
    probe_video_frames,
    select_video_device,
)


def _estimate_batched_sample_rate(
    sample_count: int, packet_times_ns: list[int]
) -> tuple[float | None, float | None]:
    """根据批量通知的首末时刻估计其覆盖时长和样本率。"""

    if sample_count <= 0 or len(packet_times_ns) < 2:
        return None, None
    intervals_ns = [
        right - left
        for left, right in zip(packet_times_ns, packet_times_ns[1:], strict=False)
    ]
    median_interval_ns = sorted(intervals_ns)[len(intervals_ns) // 2]
    coverage_ns = packet_times_ns[-1] - packet_times_ns[0] + median_interval_ns
    if coverage_ns <= 0:
        return None, None
    coverage_seconds = coverage_ns / 1e9
    return coverage_seconds, sample_count / coverage_seconds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="imu-collector")
    parser.add_argument("--config", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="启动本地 WebUI 和 API")
    validate = subparsers.add_parser("validate", help="验证一个采集 HDF5")
    validate.add_argument("path", type=Path)
    analyze = subparsers.add_parser(
        "analyze-characterization", help="重新生成 IMU 表征 JSON 报告"
    )
    analyze.add_argument("path", type=Path)
    compare = subparsers.add_parser(
        "compare-accel-pair", help="比较两个相反静态姿态并生成加速度候选报告"
    )
    compare.add_argument("positive_path", type=Path)
    compare.add_argument("negative_path", type=Path)
    compare.add_argument("--positive-stage", required=True)
    compare.add_argument("--negative-stage", required=True)
    compare.add_argument("--axis", choices=("x", "y", "z"), required=True)
    compare.add_argument("--output", type=Path)
    recover = subparsers.add_parser(
        "recover-characterization", help="从中断的 partial 副本保守收尾 IMU 表征"
    )
    recover.add_argument("path", type=Path)
    correct = subparsers.add_parser(
        "correct-characterization-stage", help="原子更正表征阶段并记录审计信息"
    )
    correct.add_argument("path", type=Path)
    correct.add_argument("--old-stage", required=True)
    correct.add_argument("--new-stage", required=True)
    correct.add_argument(
        "--reliability", choices=("candidate", "exploratory"), required=True
    )
    correct.add_argument("--reason", required=True)
    subparsers.add_parser("doctor", help="检查本机运行依赖")
    devices = subparsers.add_parser("devices", help="列出本机采集设备")
    devices.add_argument(
        "--scan-ble",
        action="store_true",
        help="同时执行五秒主动 BLE 扫描；不会配对或连接",
    )
    probe_imu = subparsers.add_parser("probe-imu", help="短时连接并统计 IMU 通知")
    probe_imu.add_argument("--seconds", type=float, default=15.0)
    probe_gatt = subparsers.add_parser(
        "probe-gatt", help="连接 IMU 并枚举实际 GATT 服务树"
    )
    probe_gatt.add_argument(
        "--hold-seconds",
        type=float,
        default=0.0,
        help="返回前保持连接的诊断时长",
    )
    probe_video = subparsers.add_parser(
        "probe-video", help="短时录制到临时目录并统计视频 PTS"
    )
    probe_video.add_argument("--seconds", type=float, default=5.0)
    probe_video.add_argument(
        "--camera-id", default=None, help="按稳定 camera_id 选择要探测的摄像头"
    )
    characterize = subparsers.add_parser(
        "characterize-imu", help="按单个物理阶段录制 IMU-only 表征 HDF5"
    )
    characterize.add_argument("--operator", default="xfan0282")
    characterize.add_argument(
        "--stage",
        choices=[item.value for item in CharacterizationStage],
        default=CharacterizationStage.PIPELINE_SMOKE_UNCONTROLLED.value,
    )
    characterize.add_argument("--seconds", type=float, default=30.0)
    characterize.add_argument("--notes", default="")
    return parser


async def _probe_imu(settings, duration_seconds: float) -> dict:
    if not 1 <= duration_seconds <= 3600:
        raise ValueError("探测时长必须在 1 到 3600 秒之间")
    source = CW12EUBleSource(settings.imu)
    packet_lengths: Counter[int] = Counter()
    packet_times: list[int] = []
    parsed_samples = 0
    parse_errors: list[str] = []
    first_payload_hex: str | None = None
    await source.start()
    started_ns = time.monotonic_ns()
    capture_stopped_ns = started_ns
    deadline = asyncio.get_running_loop().time() + duration_seconds
    try:
        while remaining := deadline - asyncio.get_running_loop().time():
            if remaining <= 0:
                break
            try:
                packet = await asyncio.wait_for(source.queue.get(), timeout=min(0.5, remaining))
            except TimeoutError:
                continue
            if first_payload_hex is None:
                first_payload_hex = packet.payload.hex()
            packet_lengths[len(packet.payload)] += 1
            packet_times.append(packet.receive_time_ns)
            try:
                parsed_samples += parse_notification(
                    packet.payload, settings.imu.frame_size_bytes
                ).sample_count
            except ValueError as error:
                parse_errors.append(str(error))
    finally:
        capture_stopped_ns = time.monotonic_ns()
        await source.stop()
    elapsed_seconds = (capture_stopped_ns - started_ns) / 1e9
    intervals_ms = [
        (right - left) / 1e6
        for left, right in zip(packet_times, packet_times[1:], strict=False)
    ]
    intervals_ms.sort()
    coverage_seconds, estimated_rate_hz = _estimate_batched_sample_rate(
        parsed_samples, packet_times
    )

    def percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        return values[min(len(values) - 1, round((len(values) - 1) * fraction))]

    return {
        "device_name": settings.imu.name,
        "device_address": settings.imu.address,
        "notify_uuid": settings.imu.notify_uuid,
        "elapsed_seconds": elapsed_seconds,
        "packet_count": len(packet_times),
        "packet_length_histogram": dict(sorted(packet_lengths.items())),
        "parsed_candidate_samples": parsed_samples,
        "estimated_sample_coverage_seconds": coverage_seconds,
        "candidate_sample_rate_hz": estimated_rate_hz,
        "packet_interval_median_ms": percentile(intervals_ms, 0.5),
        "packet_interval_p95_ms": percentile(intervals_ms, 0.95),
        "callback_drops": source.dropped_callback_packets,
        "parse_error_count": len(parse_errors),
        "parse_error_examples": parse_errors[:5],
        "first_payload_hex": first_payload_hex,
        "disconnect_reason": source.disconnect_reason,
    }


async def _probe_gatt(settings, hold_seconds: float = 0.0) -> dict:
    if not 0 <= hold_seconds <= 60:
        raise ValueError("GATT 保持连接时长必须在 0 到 60 秒之间")
    source = CW12EUBleSource(settings.imu)
    await source.connect()
    try:
        assert source.client is not None
        if not list(source.client.services):
            await asyncio.sleep(2)
            backend = source.client._backend
            backend.services = None
            await backend._get_services()
        services = []
        notify_candidates = []
        for service in source.client.services:
            characteristics = []
            for characteristic in service.characteristics:
                item = {
                    "uuid": characteristic.uuid,
                    "description": characteristic.description,
                    "properties": list(characteristic.properties),
                    "descriptor_uuids": [
                        descriptor.uuid for descriptor in characteristic.descriptors
                    ],
                }
                characteristics.append(item)
                if "notify" in characteristic.properties:
                    notify_candidates.append(characteristic.uuid)
            services.append(
                {
                    "uuid": service.uuid,
                    "description": service.description,
                    "characteristics": characteristics,
                }
            )
        result = {
            "device_name": settings.imu.name,
            "device_address": settings.imu.address,
            "configured_notify_uuid": settings.imu.notify_uuid,
            "notify_candidates": notify_candidates,
            "services": services,
        }
        if hold_seconds:
            await asyncio.sleep(hold_seconds)
        return result
    finally:
        await source.stop()


async def _probe_video(
    settings, duration_seconds: float, camera_id: str | None = None
) -> dict:
    if not 1 <= duration_seconds <= 60:
        raise ValueError("视频探测时长必须在 1 到 60 秒之间")
    devices = await discover_video_devices(settings.video)
    preferred = select_video_device(devices, settings.video, camera_id)
    with tempfile.TemporaryDirectory(prefix="imu-video-probe-") as directory:
        path = Path(directory) / "probe.mkv"
        recorder = FFmpegVideoRecorder(
            settings.video,
            str(preferred["device"]),
            path,
        )
        await recorder.start()
        try:
            await asyncio.sleep(duration_seconds)
        finally:
            await recorder.stop()
        if recorder.started_monotonic_ns is None:
            raise RuntimeError("视频录制器没有记录启动时刻")
        table = await probe_video_frames(path, recorder.started_monotonic_ns)
        span_seconds = (
            (int(table.pts_monotonic_ns[-1]) - int(table.pts_monotonic_ns[0])) / 1e9
            if len(table.pts_monotonic_ns) > 1
            else 0.0
        )
        actual_fps = (
            (len(table.pts_monotonic_ns) - 1) / span_seconds if span_seconds > 0 else 0.0
        )
        pts_deltas = table.pts_monotonic_ns[1:] - table.pts_monotonic_ns[:-1]
        return {
            "device": preferred["device"],
            "product": preferred["product"],
            "requested_profile": (
                f"{settings.video.width}x{settings.video.height}"
                f"@{settings.video.requested_fps}"
            ),
            "codec": table.codec,
            "width": table.width,
            "height": table.height,
            "frame_count": len(table.pts_monotonic_ns),
            "pts_span_seconds": span_seconds,
            "actual_span_fps": actual_fps,
            "nonpositive_pts_delta_count": int((pts_deltas <= 0).sum()),
            "duplicate_pts_count": int((pts_deltas == 0).sum()),
            "output_bytes": path.stat().st_size,
            "ffmpeg_progress_fps": recorder.progress.fps,
            "ffmpeg_errors": recorder.progress.errors,
        }


async def _characterize_imu(
    settings,
    operator: str,
    stage: str,
    duration_seconds: float,
    notes: str,
) -> dict:
    if not 1 <= duration_seconds <= 3600:
        raise ValueError("单阶段表征时长必须在 1 到 3600 秒之间")
    coordinator = RecordingCoordinator(settings)
    await coordinator.start_characterization(
        CharacterizationStartRequest(operator_id=operator, notes=notes)
    )
    await coordinator.start_characterization_stage(
        CharacterizationStageRequest(stage_code=stage, notes=notes)
    )
    interrupted = False
    try:
        await asyncio.sleep(duration_seconds)
    except asyncio.CancelledError:
        interrupted = True
    finally:
        if coordinator.current_stage is not None:
            await coordinator.stop_characterization_stage()
    result = await coordinator.stop_characterization()
    return {**result, "interrupted_by_user": interrupted}


def main() -> None:
    args = _parser().parse_args()
    settings = load_settings(args.config)
    if args.command == "serve":
        uvicorn.run(create_app(settings), host=settings.server_host, port=settings.server_port)
    elif args.command == "validate":
        with h5py.File(args.path, "r") as handle:
            require_video = (
                str(handle.attrs.get("recording_kind", "capture")) == "capture"
            )
        report = validate_capture_h5(
            args.path,
            load_activity_taxonomy(settings.activity_taxonomy_path),
            require_video=require_video,
            require_sync=False,
        )
        print(
            json.dumps(
                {"ready": report.ready, "issues": report.issues, "metrics": report.metrics},
                indent=2,
                ensure_ascii=False,
            )
        )
        raise SystemExit(0 if report.ready else 1)
    elif args.command == "analyze-characterization":
        report_path = write_characterization_report(args.path)
        print(
            json.dumps(
                {"source_h5": str(args.path), "report_path": str(report_path)},
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "compare-accel-pair":
        report_path = write_accel_pose_pair_report(
            args.positive_path,
            args.negative_path,
            args.positive_stage,
            args.negative_stage,
            args.axis,
            args.output,
        )
        print(json.dumps({"report_path": str(report_path)}, ensure_ascii=False, indent=2))
    elif args.command == "recover-characterization":
        result = recover_interrupted_characterization(
            args.path,
            settings.imu,
            load_activity_taxonomy(settings.activity_taxonomy_path),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "correct-characterization-stage":
        result = correct_characterization_stage(
            args.path,
            args.old_stage,
            args.new_stage,
            args.reliability,
            args.reason,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        commands = {
            name: shutil.which(name)
            for name in ("bluetoothctl", "ffmpeg", "ffprobe", "v4l2-ctl", "rclone")
        }
        print(
            json.dumps(
                {
                    "commands": commands,
                    "data_root": str(settings.data_root),
                    "catalog_path": str(settings.catalog_path),
                    "taxonomy": str(settings.activity_taxonomy_path),
                    "upload_configured": bool(
                        settings.upload.enabled and settings.upload.remote_root
                    ),
                },
                indent=2,
            )
        )
        raise SystemExit(0 if all(commands.values()) else 1)
    elif args.command == "devices":
        async def inspect_devices() -> dict:
            cameras = await discover_video_devices(settings.video)
            ble = (
                await CW12EUBleSource.discover(settings=settings.imu)
                if args.scan_ble
                else []
            )
            return {"cameras": cameras, "ble": ble}

        print(json.dumps(asyncio.run(inspect_devices()), indent=2, ensure_ascii=False))
    elif args.command == "probe-imu":
        print(
            json.dumps(
                asyncio.run(_probe_imu(settings, args.seconds)),
                indent=2,
                ensure_ascii=False,
            )
        )
    elif args.command == "probe-gatt":
        print(
            json.dumps(
                asyncio.run(_probe_gatt(settings, args.hold_seconds)),
                indent=2,
                ensure_ascii=False,
            )
        )
    elif args.command == "characterize-imu":
        print(
            json.dumps(
                asyncio.run(
                    _characterize_imu(
                        settings,
                        args.operator,
                        args.stage,
                        args.seconds,
                        args.notes,
                    )
                ),
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(
            json.dumps(
                asyncio.run(_probe_video(settings, args.seconds, args.camera_id)),
                indent=2,
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
