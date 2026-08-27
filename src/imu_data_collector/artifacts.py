"""生成不可变源包与经过门禁的 30 Hz 训练 HDF5。"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import tarfile
from pathlib import Path

import h5py
import numpy as np

from imu_data_collector.config import ImuSettings
from imu_data_collector.cw12eu import calibrate_counts
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    BinaryLabel,
    ReviewDocument,
    ReviewWorkflowState,
)
from imu_data_collector.review import verify_source_artifacts
from imu_data_collector.sync import assess_conditional_fixed_offset
from imu_data_collector.validation import validate_annotations

TRAINING_SCHEMA_VERSION = "3.0.0"
TARGET_RATE_HZ = 30
NANOSECONDS_PER_SECOND = 1_000_000_000


def estimate_capture_package_bytes(h5_path: Path, mkv_path: Path) -> int:
    """无压缩 TAR 的保守空间估计，包含块对齐和结束块。"""

    payload = h5_path.stat().st_size + mkv_path.stat().st_size + 16_384
    return math.ceil(payload / 512) * 512 + 1_024


def create_capture_package(
    document: ReviewDocument,
    h5_path: Path,
    mkv_path: Path,
    output_path: Path,
) -> Path:
    issues = verify_source_artifacts(document, h5_path, mkv_path)
    if issues:
        raise ValueError("；".join(issues))
    manifest = {
        "schema_version": "1.0.0",
        "recording_id": document.recording_id,
        "contents": [item.model_dump(mode="json") for item in document.sources],
        "review_sidecar_included": False,
        "compression": "none",
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.partial")
    try:
        with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest_bytes)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(manifest_bytes))
            archive.add(h5_path, arcname="capture.h5", recursive=False)
            archive.add(mkv_path, arcname="video.mkv", recursive=False)
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return output_path


def create_training_snapshot_archive(
    files: list[tuple[str, str, Path]], output_path: Path
) -> Path:
    """把逐录制训练文件打包成一个不可变 TAR。"""

    if not files:
        raise ValueError("没有已完成的 aligned30.h5 可加入训练快照")
    manifest_files = []
    archive_items: list[tuple[str, Path]] = []
    for participant_id, recording_id, path in sorted(files):
        if not path.is_file():
            raise ValueError(f"训练文件缺失：{path}")
        archive_path = f"recordings/{participant_id}/{recording_id}/aligned30.h5"
        archive_items.append((archive_path, path))
        manifest_files.append(
            {
                "path": archive_path,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest = {
        "schema_version": "1.0.0",
        "dataset_id": "cw12eu",
        "files": manifest_files,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.partial")
    try:
        with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest_bytes)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(manifest_bytes))
            for archive_path, source in archive_items:
                info = archive.gettarinfo(str(source), arcname=archive_path)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                info.mode = 0o644
                with source.open("rb") as handle:
                    archive.addfile(info, handle)
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return output_path


def _ceil_grid_index(relative_ns: int) -> int:
    return (relative_ns * TARGET_RATE_HZ + NANOSECONDS_PER_SECOND - 1) // NANOSECONDS_PER_SECOND


def _nearest_grid_index_half_up(relative_ns: int) -> int:
    return (relative_ns * TARGET_RATE_HZ + NANOSECONDS_PER_SECOND // 2) // NANOSECONDS_PER_SECOND


def _text_dtype() -> np.dtype:
    return h5py.string_dtype("utf-8")


def _sequence_dtype() -> np.dtype:
    text = _text_dtype()
    return np.dtype(
        [
            ("sample_start", "<i8"),
            ("sample_stop", "<i8"),
            ("source_file", text),
            ("participant_id", text),
            ("recording_id", text),
            ("body_location", text),
            ("activity_code", text),
            ("is_fall", "?"),
            ("supervision_kind", text),
            ("source_sampling_rate_hz", "<f8"),
        ]
    )


def _annotation_dtype() -> np.dtype:
    text = _text_dtype()
    return np.dtype(
        [
            ("sequence_index", "<i4"),
            ("kind", text),
            ("start_sample", "<i8"),
            ("stop_sample", "<i8"),
            ("code", text),
        ]
    )


def _annotation_rows(
    document: ReviewDocument, grid_origin_ns: int, sample_count: int
) -> np.ndarray:
    rows: list[tuple[int, str, int, int, str]] = []
    activity_by_segment = {
        item.segment_id: item.activity_code for item in document.annotations.segments
    }
    for segment in document.annotations.segments:
        start = max(0, min(sample_count, _ceil_grid_index(segment.start_ns - grid_origin_ns)))
        stop = max(0, min(sample_count, _ceil_grid_index(segment.end_ns - grid_origin_ns)))
        if start < stop:
            rows.append((0, "activity", start, stop, segment.activity_code))
    for exclusion in document.annotations.exclusions:
        start = max(0, min(sample_count, _ceil_grid_index(exclusion.start_ns - grid_origin_ns)))
        stop = max(0, min(sample_count, _ceil_grid_index(exclusion.end_ns - grid_origin_ns)))
        if start < stop:
            rows.append((0, "exclude", start, stop, exclusion.reason.value))
    for event in document.annotations.events:
        relative_ns = event.time_ns - grid_origin_ns
        index = (
            _ceil_grid_index(relative_ns)
            if event.kind.value == "onset"
            else _nearest_grid_index_half_up(relative_ns)
        )
        if 0 <= index < sample_count:
            rows.append(
                (
                    0,
                    event.kind.value,
                    index,
                    index,
                    activity_by_segment[event.segment_id],
                )
            )
    kind_order = {"activity": 0, "onset": 1, "impact": 2, "exclude": 3}
    rows.sort(key=lambda item: (item[2], kind_order[item[1]], item[3], item[4]))
    return np.asarray(rows, dtype=_annotation_dtype())


def _logical_digest(
    values: np.ndarray,
    sequence: np.ndarray,
    annotations: np.ndarray,
) -> str:
    row = sequence[0]

    def text(value: object) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    metadata = {
        "dataset_id": "cw12eu",
        "source_file": text(row["source_file"]),
        "participant_id": text(row["participant_id"]),
        "recording_id": text(row["recording_id"]),
        "body_location": text(row["body_location"]),
        "activity": text(row["activity_code"]),
        "is_fall": bool(row["is_fall"]),
        "sampling_rate_hz": 30.0,
        "original_sampling_rate_hz": float(row["source_sampling_rate_hz"]),
        "supervision_kind": text(row["supervision_kind"]),
        "annotations": [
            {
                "kind": text(item["kind"]),
                "start_sample": int(item["start_sample"]),
                "stop_sample": int(item["stop_sample"]),
                "code": text(item["code"]),
            }
            for item in annotations
        ],
    }
    encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    little_endian_values = np.asarray(values, dtype="<f4", order="C")
    digest = hashlib.sha256()
    digest.update(len(encoded).to_bytes(8, "little"))
    digest.update(encoded)
    digest.update(np.asarray(little_endian_values.shape, dtype="<i8").tobytes())
    digest.update(little_endian_values.tobytes())
    return digest.hexdigest()


def export_aligned30(
    document: ReviewDocument,
    h5_path: Path,
    mkv_path: Path,
    output_path: Path,
    imu_settings: ImuSettings,
    taxonomy: dict,
    *,
    source_hashes_verified: bool = False,
) -> Path:
    if document.workflow.state not in {
        ReviewWorkflowState.IN_PROGRESS,
        ReviewWorkflowState.COMPLETED,
    }:
        raise ValueError("只有进行中或已完成的录制可以导出训练数据")
    if not imu_settings.accel_counts_per_g or not imu_settings.gyro_counts_per_dps:
        raise ValueError("真实 IMU 尺度校准尚未验证，禁止导出正式训练数据")
    if not source_hashes_verified:
        issues = verify_source_artifacts(document, h5_path, mkv_path)
        if issues:
            raise ValueError("；".join(issues))
    assessment = assess_conditional_fixed_offset(document.sync)
    if assessment.quality != "verified":
        raise ValueError("同步尚未验证，禁止导出正式训练数据")

    with h5py.File(h5_path, "r") as source:
        if str(source.attrs.get("data_tier", "test")) != "prod":
            raise ValueError("只有 prod 录制可以导出正式训练数据")
        duration_ns = int(source.attrs["duration_ns"])
        annotation_issues = validate_annotations(
            document.annotations, taxonomy, duration_ns
        )
        if not document.annotations.finalized:
            annotation_issues.append("标注尚未定稿")
        if annotation_issues:
            raise ValueError("；".join(annotation_issues))

        raw = np.asarray(source["imu/samples/raw_counts"], dtype=np.int16)
        imu_time_ns = np.asarray(
            source["imu/samples/recording_time_ns"], dtype=np.int64
        ) + assessment.applied_offset_ns
        video_time_ns = np.asarray(
            source["video/frames/recording_time_ns"], dtype=np.int64
        )
        source_rate_hz = float(source["imu"].attrs.get("observed_rate_hz", 0.0))
        participant_id = f"cw12eu:{source.attrs['participant_id']}"
        recording_id = f"cw12eu:{source.attrs['recording_id']}"
        body_location = str(source.attrs["body_location"])

    if len(raw) < 2 or len(video_time_ns) < 2:
        raise ValueError("IMU 或视频样本不足，无法生成公共 30 Hz 时间轴")
    start_ns = max(int(imu_time_ns[0]), int(video_time_ns[0]))
    stop_ns = min(int(imu_time_ns[-1]), int(video_time_ns[-1]))
    if stop_ns <= start_ns:
        raise ValueError("IMU 与视频没有公共有效时间区间")
    sample_count = (stop_ns - start_ns) * TARGET_RATE_HZ // NANOSECONDS_PER_SECOND + 1
    if sample_count < 2:
        raise ValueError("IMU 与视频公共有效区间不足两个 30 Hz 样本")
    relative_grid_ns = (
        np.arange(sample_count, dtype=np.int64) * NANOSECONDS_PER_SECOND // TARGET_RATE_HZ
    )
    target_ns = start_ns + relative_grid_ns
    calibrated = calibrate_counts(
        raw,
        imu_settings.accel_counts_per_g,
        imu_settings.gyro_counts_per_dps,
        accel_bias_counts=imu_settings.accel_bias_counts,
        gyro_bias_counts=imu_settings.gyro_bias_counts,
        raw_axis_order=imu_settings.raw_axis_order,
        axis_signs=imu_settings.axis_signs,
    ).astype(np.float64)
    values = np.column_stack(
        [np.interp(target_ns, imu_time_ns, calibrated[:, axis]) for axis in range(6)]
    ).astype(np.float32)
    if not np.isfinite(values).all():
        raise ValueError("校准或插值产生了非有限值")

    activity_codes = sorted(
        {item.activity_code for item in document.annotations.segments}
    )
    sequence = np.asarray(
        [
            (
                0,
                sample_count,
                h5_path.name,
                participant_id,
                recording_id,
                body_location,
                activity_codes[0] if len(activity_codes) == 1 else "mixed",
                any(
                    item.binary_label == BinaryLabel.FALL
                    for item in document.annotations.segments
                ),
                "temporal",
                source_rate_hz,
            )
        ],
        dtype=_sequence_dtype(),
    )
    annotations = _annotation_rows(document, start_ns, sample_count)
    logical_digest = _logical_digest(values, sequence, annotations)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.partial")
    try:
        with h5py.File(temporary, "w", libver="latest") as output:
            output.attrs.update(
                {
                    "imu_schema_version": TRAINING_SCHEMA_VERSION,
                    "dataset_id": "cw12eu",
                    "sampling_rate_hz": 30.0,
                    "axis_frame": "sensor_local",
                    "hdf5_compatibility": "1.14",
                    "feature_columns": json.dumps(
                        [
                            "acceleration_x_mps2",
                            "acceleration_y_mps2",
                            "acceleration_z_mps2",
                            "angular_velocity_x_rad_s",
                            "angular_velocity_y_rad_s",
                            "angular_velocity_z_rad_s",
                        ]
                    ),
                    "recording_id": recording_id,
                    "grid_origin_recording_time_ns": start_ns,
                    "grid_definition": "row k occurs at exactly k/30 seconds",
                    "sequence_count": 1,
                    "sample_count": sample_count,
                    "annotation_count": len(annotations),
                    "logical_content_sha256": logical_digest,
                }
            )
            output.create_dataset(
                "samples",
                data=values,
                maxshape=(None, 6),
                chunks=(4096, 6),
                compression="gzip",
                compression_opts=4,
                shuffle=True,
                fletcher32=True,
            )
            output.create_dataset(
                "sequences", data=sequence, maxshape=(None,), chunks=(1,)
            )
            output.create_dataset(
                "annotations",
                data=annotations,
                maxshape=(None,),
                chunks=(max(1, min(1024, max(1, len(annotations)))),),
            )
            output["samples"].attrs.update(
                {
                    "columns": output.attrs["feature_columns"],
                    "units": json.dumps(
                        ["m/s^2", "m/s^2", "m/s^2", "rad/s", "rad/s", "rad/s"]
                    ),
                    "axis_frame": "sensor_local",
                }
            )
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return output_path
