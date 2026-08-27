"""与硬件会话解耦、可重复执行的录制制品收尾。"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from imu_data_collector.config import Settings
from imu_data_collector.hdf5_store import CaptureH5Writer
from imu_data_collector.models import RecordingSummary
from imu_data_collector.validation import ValidationReport, validate_capture_h5
from imu_data_collector.video import normalize_video_timeline, probe_video_frames


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    h5_path: Path
    mkv_path: Path
    ended_at_utc: str
    duration_ns: int
    observed_rate_hz: float
    report: ValidationReport
    recovery_warnings: tuple[str, ...] = ()


def _derived_paths(summary: RecordingSummary) -> tuple[Path, Path, Path, Path]:
    partial_h5 = Path(summary.h5_path or "")
    partial_mkv = Path(summary.mkv_path or "")
    final_h5 = partial_h5.with_name(partial_h5.name.replace(".partial.h5", ".h5"))
    final_mkv = partial_mkv.with_name(partial_mkv.name.replace(".partial.mkv", ".mkv"))
    if final_h5 == partial_h5 or final_mkv == partial_mkv:
        raise ValueError("重新收尾要求录制仍具有 .partial.h5 和 .partial.mkv")
    return partial_h5, partial_mkv, final_h5, final_mkv


def _require_safe_partial(
    summary: RecordingSummary, settings: Settings
) -> tuple[Path, Path, Path, Path]:
    partial_h5, partial_mkv, final_h5, final_mkv = _derived_paths(summary)
    root = settings.data_root.resolve()
    for path in (partial_h5, partial_mkv, final_h5, final_mkv):
        if not path.resolve().is_relative_to(root):
            raise ValueError("录制路径越出数据根目录")
    if not partial_h5.is_file() or not partial_mkv.is_file():
        raise ValueError("录制缺少可恢复的 partial H5/MKV")
    if partial_h5.stat().st_size == 0 or partial_mkv.stat().st_size == 0:
        raise ValueError("partial H5/MKV 为空，不能重新收尾")
    with h5py.File(partial_h5, "r") as handle:
        identity = {
            "recording_id": str(handle.attrs.get("recording_id", "")),
            "collection_id": str(handle.attrs.get("collection_id", "")),
            "participant_id": str(handle.attrs.get("participant_id", "")),
            "data_tier": str(handle.attrs.get("data_tier", "")),
        }
    expected = {
        "recording_id": summary.recording_id,
        "collection_id": summary.collection_id,
        "participant_id": summary.participant_id,
        "data_tier": summary.data_tier.value,
    }
    if identity != expected:
        raise ValueError("partial H5 身份与目录索引不一致")
    return partial_h5, partial_mkv, final_h5, final_mkv


def _video_controls(
    context: dict[str, Any], settings: Settings
) -> tuple[dict[str, int], dict[str, int], list[str], tuple[str, ...]]:
    requested = {
        str(key): int(value)
        for key, value in dict(context.get("camera_controls_requested") or {}).items()
    }
    effective = {
        str(key): int(value)
        for key, value in dict(context.get("camera_controls_effective") or {}).items()
    }
    errors = [str(item) for item in context.get("camera_control_errors") or []]
    if requested or not settings.video.manual_controls_enabled:
        return requested, effective, errors, ()
    # 旧 partial 没有保存运行时读回值；但 prod 能进入 recording 状态说明当时
    # 已经通过 requested == effective 且无错误的启动门禁。这里明确记录恢复来源。
    requested = {
        "auto_exposure": settings.video.auto_exposure,
        "exposure_time_absolute": settings.video.exposure_time_absolute,
        "gain": settings.video.gain,
        "exposure_dynamic_framerate": settings.video.exposure_dynamic_framerate,
        "power_line_frequency": settings.video.power_line_frequency,
    }
    return (
        requested,
        dict(requested),
        [],
        ("旧录制重新收尾：摄像头控制值依据正式录制启动门禁和当前配置恢复",),
    )


def _duration_seconds(summary: RecordingSummary, partial_h5: Path) -> float:
    try:
        started = datetime.fromisoformat(summary.started_at_utc)
        ended = datetime.fromisoformat(summary.ended_at_utc or "")
        return max(0.0, (ended - started).total_seconds())
    except ValueError:
        with h5py.File(partial_h5, "r") as handle:
            values = handle["imu/samples/recording_time_ns"]
            return float(values[-1]) / 1e9 if len(values) else 0.0


async def finalize_recording(
    summary: RecordingSummary,
    context: dict[str, Any],
    settings: Settings,
    taxonomy: dict[str, Any],
    phase: Callable[[str], None],
) -> FinalizationResult:
    """在临时副本中收尾，成功后才发布最终路径。"""

    partial_h5, partial_mkv, final_h5, final_mkv = _require_safe_partial(
        summary, settings
    )
    work_h5 = partial_h5.with_name(f"{summary.recording_id}.finalizing.h5")
    work_mkv = partial_mkv.with_name(f"{summary.recording_id}.finalizing.mkv")

    # 服务可能在两个原子改名之间退出。两个最终文件都存在时直接验证接管；
    # 只存在一个时，它只能是本收尾器生成的未提交制品，清理后从 partial 重做。
    if final_h5.is_file() and final_mkv.is_file():
        report = validate_capture_h5(final_h5, taxonomy)
        with h5py.File(final_h5, "r") as handle:
            return FinalizationResult(
                final_h5,
                final_mkv,
                str(handle.attrs.get("ended_at_utc", summary.ended_at_utc or "")),
                int(handle.attrs.get("duration_ns", 0)),
                float(handle["imu"].attrs.get("observed_rate_hz", 0.0)),
                report,
            )
    if final_h5.exists() != final_mkv.exists():
        final_h5.unlink(missing_ok=True)
        final_mkv.unlink(missing_ok=True)
    work_h5.unlink(missing_ok=True)
    work_mkv.unlink(missing_ok=True)

    length_seconds = _duration_seconds(summary, partial_h5)
    probe_timeout = max(600.0, length_seconds * 1.25)
    remux_timeout = max(300.0, length_seconds * 0.75)
    requested, effective, control_errors, recovery_warnings = _video_controls(
        context, settings
    )
    writer: CaptureH5Writer | None = None
    try:
        phase("copying_h5")
        await asyncio.to_thread(shutil.copy2, partial_h5, work_h5)
        with h5py.File(work_h5, "r") as source:
            frozen_rate = float(
                source["imu"].attrs.get(
                    "expected_rate_hz", settings.imu.expected_rate_hz
                )
            )
        writer = CaptureH5Writer.open_for_finalization(
            work_h5, replace(settings.imu, expected_rate_hz=frozen_rate)
        )
        phase("reconstructing_imu")
        observed_rate_hz, _residual = writer.reconstruct_times()
        if not partial_mkv.is_file() or partial_mkv.stat().st_size == 0:
            raise ValueError("FFmpeg produced no MKV data")

        phase("probing_source_video")
        source_table = await probe_video_frames(
            partial_mkv,
            writer.recording_start_monotonic_ns,
            timeout_seconds=probe_timeout,
            nice_value=settings.background_jobs.subprocess_nice,
        )
        if not len(source_table.pts_monotonic_ns):
            raise ValueError("recorded MKV has no video frames")

        phase("normalizing_video")
        await normalize_video_timeline(
            partial_mkv,
            work_mkv,
            timeout_seconds=remux_timeout,
            nice_value=settings.background_jobs.subprocess_nice,
        )
        phase("probing_normalized_video")
        normalized = await probe_video_frames(
            work_mkv,
            int(source_table.pts_monotonic_ns[0]),
            pts_are_monotonic=False,
            timeout_seconds=probe_timeout,
            nice_value=settings.background_jobs.subprocess_nice,
        )
        if not np.array_equal(
            normalized.pts_monotonic_ns, source_table.pts_monotonic_ns
        ):
            raise RuntimeError("MKV 重封装前后的帧数量或逐帧时间间隔不一致")

        phase("writing_h5")
        writer.write_video_frames(
            pts_monotonic_ns=source_table.pts_monotonic_ns,
            media_time_ns=(
                source_table.pts_monotonic_ns - source_table.pts_monotonic_ns[0]
            ),
            duration_ns=source_table.duration_ns,
            key_frame=source_table.key_frame,
            video_path=work_mkv,
            video_filename=final_mkv.name,
            codec=source_table.codec,
            width=source_table.width,
            height=source_table.height,
            requested_fps=settings.video.requested_fps,
            ffmpeg_diagnostics=[
                str(item) for item in context.get("ffmpeg_diagnostics") or []
            ],
            camera_controls_requested=requested,
            camera_controls_effective=effective,
            camera_control_errors=control_errors,
        )
        writer.handle["video"].attrs["finalization_context_source"] = (
            "capture_runtime" if context.get("camera_controls_requested") else "recovered"
        )
        writer.write_sync([])
        ended_at = str(
            context.get("ended_at_utc")
            or summary.ended_at_utc
            or writer.handle.attrs.get("capture_ended_at_utc", "")
        )
        writer.finish(ended_at_utc=ended_at or None)
        writer = None

        phase("validating")
        report = validate_capture_h5(work_h5, taxonomy)
        with h5py.File(work_h5, "r") as handle:
            ended_at = str(handle.attrs.get("ended_at_utc", ended_at))
            duration_ns = int(handle.attrs.get("duration_ns", 0))

        phase("committing")
        work_mkv.replace(final_mkv)
        work_h5.replace(final_h5)
        return FinalizationResult(
            final_h5,
            final_mkv,
            ended_at,
            duration_ns,
            observed_rate_hz,
            report,
            recovery_warnings,
        )
    except BaseException:
        if writer is not None:
            writer.abort_close()
        work_h5.unlink(missing_ok=True)
        work_mkv.unlink(missing_ok=True)
        raise


def cleanup_partial_inputs(summary: RecordingSummary) -> None:
    """目录索引已提交最终路径后，才清理冻结的采集输入。"""

    partial_h5, partial_mkv, _final_h5, _final_mkv = _derived_paths(summary)
    partial_h5.unlink(missing_ok=True)
    partial_mkv.unlink(missing_ok=True)
