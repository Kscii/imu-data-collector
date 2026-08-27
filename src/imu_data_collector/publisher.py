"""把一次完整录制作为不可变制品手动发布到标注存储。"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import h5py

from imu_data_collector.config import Settings
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    ArtifactDescriptor,
    CalibrationProfile,
    CaptureManifestV2,
    DataTier,
    RecordingSummary,
)
from imu_data_collector.storage import ObjectConflictError, ObjectStore


async def build_preview_mp4(mkv_path: Path, output_path: Path) -> Path:
    """无重编码生成浏览器代理；原始 MKV 始终保留。"""

    if output_path.is_file() and output_path.stat().st_mtime_ns >= mkv_path.stat().st_mtime_ns:
        return output_path
    temporary = output_path.with_name(f".{output_path.stem}.{os.getpid()}.partial.mp4")
    temporary.unlink(missing_ok=True)
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(mkv_path),
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(temporary),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            "生成 MP4 浏览代理失败：" + stderr.decode("utf-8", errors="replace").strip()
        )
    if not temporary.is_file() or temporary.stat().st_size == 0:
        raise RuntimeError("FFmpeg 没有生成有效 MP4 浏览代理")
    temporary.replace(output_path)
    return output_path


def _descriptor(
    recording_id: str,
    role: str,
    path: Path,
    content_type: str,
) -> ArtifactDescriptor:
    filename = {
        "capture_h5": "capture.h5",
        "video_mkv": "video.mkv",
        "preview_mp4": "preview.mp4",
    }[role]
    return ArtifactDescriptor(
        role=role,
        object_key=f"captures/{recording_id}/{filename}",
        filename=filename,
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
        content_type=content_type,
    )


def _put_idempotent(store: ObjectStore, path: Path, artifact: ArtifactDescriptor) -> None:
    current = store.stat(artifact.object_key)
    if current is not None:
        if (
            current.size_bytes == artifact.size_bytes
            and current.metadata.get("sha256") == artifact.sha256
        ):
            return
        raise ObjectConflictError(f"远端对象与本地制品不同：{artifact.object_key}")
    store.put_file(
        path,
        artifact.object_key,
        content_type=artifact.content_type,
        metadata={
            "sha256": artifact.sha256,
            "recording_id": artifact.object_key.split("/")[1],
            "artifact_role": artifact.role,
        },
        if_absent=True,
    )


async def publish_recording(
    summary: RecordingSummary,
    settings: Settings,
    store: ObjectStore,
) -> CaptureManifestV2:
    """先上传三个制品，最后写 manifest 作为原子可见标记。"""

    if not summary.h5_path or not summary.mkv_path:
        raise ValueError("录制缺少 H5 或 MKV")
    if summary.data_tier == "legacy_unclassified":
        raise ValueError("旧版未分类录制禁止发布")
    h5_path = Path(summary.h5_path)
    mkv_path = Path(summary.mkv_path)
    if not h5_path.is_file() or not mkv_path.is_file():
        raise ValueError("录制源文件不存在")
    proxy_path = await build_preview_mp4(mkv_path, h5_path.parent / "preview.mp4")
    paths = {
        "capture_h5": h5_path,
        "video_mkv": mkv_path,
        "preview_mp4": proxy_path,
    }
    artifacts = [
        _descriptor(summary.recording_id, "capture_h5", h5_path, "application/x-hdf5"),
        _descriptor(summary.recording_id, "video_mkv", mkv_path, "video/x-matroska"),
        _descriptor(summary.recording_id, "preview_mp4", proxy_path, "video/mp4"),
    ]
    with h5py.File(h5_path, "r") as handle:
        source_schema = str(handle.attrs.get("capture_schema_version", "unknown"))
        body_location = str(handle.attrs.get("body_location", "chest"))
        imu_attrs = handle["imu"].attrs
        calibration = CalibrationProfile(
            profile_id=str(
                imu_attrs.get(
                    "calibration_profile_id",
                    handle.attrs.get("calibration_profile_id", "unverified"),
                )
            ),
            verified=bool(handle.attrs.get("calibration_verified", False)),
            accel_counts_per_g=(
                float(imu_attrs["accel_counts_per_g"])
                if "accel_counts_per_g" in imu_attrs
                else None
            ),
            gyro_counts_per_dps=(
                float(imu_attrs["gyro_counts_per_dps"])
                if "gyro_counts_per_dps" in imu_attrs
                else None
            ),
            accel_bias_counts=tuple(
                json.loads(str(imu_attrs.get("accel_bias_counts_json", "[0, 0, 0]")))
            ),
            gyro_bias_counts=tuple(
                json.loads(str(imu_attrs.get("gyro_bias_counts_json", "[0, 0, 0]")))
            ),
            raw_axis_order=tuple(
                json.loads(str(imu_attrs.get("raw_axis_order_json", "[0, 1, 2]")))
            ),
            axis_signs=tuple(
                json.loads(str(imu_attrs.get("axis_signs_json", "[1, 1, 1]")))
            ),
            method=str(imu_attrs.get("calibration_method", "unverified")),
            evidence_sha256=(
                str(imu_attrs["calibration_evidence_sha256"])
                if "calibration_evidence_sha256" in imu_attrs
                else None
            ),
        )
    manifest = CaptureManifestV2(
        recording_id=summary.recording_id,
        collection_id=summary.collection_id,
        participant_id=summary.participant_id,
        data_tier=DataTier(summary.data_tier),
        body_location=body_location,
        captured_at_utc=summary.started_at_utc,
        duration_ns=int(summary.duration_ns or 0),
        source_h5_schema_version=source_schema,
        software_revision=os.environ.get("IMU_PLATFORM_REVISION", "working-tree"),
        calibration=calibration,
        artifacts=artifacts,
    )
    for artifact in artifacts:
        await asyncio.to_thread(
            _put_idempotent,
            store,
            paths[artifact.role],
            artifact,
        )
    manifest_key = f"captures/{summary.recording_id}/manifest.json"
    current = await asyncio.to_thread(store.stat, manifest_key)
    if current is None:
        await asyncio.to_thread(
            store.write_json,
            manifest_key,
            manifest.model_dump(mode="json"),
            if_generation_match=0,
        )
    else:
        existing, _generation = await asyncio.to_thread(store.read_json, manifest_key)
        if CaptureManifestV2.model_validate(existing) != manifest:
            raise ObjectConflictError("远端 manifest 已存在且内容不同")
    return manifest


def published_at() -> str:
    return datetime.now(UTC).isoformat()
