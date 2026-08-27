"""仅追加的采集写入器，以及采用原子替换的标注更新。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from imu_data_collector.config import ImuSettings
from imu_data_collector.constants import (
    CAPTURE_SCHEMA_NAME,
    CAPTURE_SCHEMA_VERSION,
    FEATURE_COLUMNS,
    FEATURE_UNITS,
)
from imu_data_collector.cw12eu import (
    NotificationKind,
    calibrate_counts,
    classify_notification,
    parse_notification,
    reconstruct_sample_times,
)
from imu_data_collector.models import (
    AnnotationDocument,
    DataTier,
    RecordingStartRequest,
    SyncAnchor,
    SyncDocument,
)
from imu_data_collector.sync import (
    ConditionalSyncAssessment,
    SyncModel,
    assess_conditional_fixed_offset,
)
from imu_data_collector.validation import (
    read_annotation_document,
    validate_annotations,
    validate_capture_h5,
)

SAMPLE_CHUNK_ROWS = 4_096
BYTE_CHUNK_ROWS = 64 * 1024


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _resize_append(dataset: h5py.Dataset, values: np.ndarray) -> tuple[int, int]:
    start = len(dataset)
    stop = start + len(values)
    dataset.resize((stop, *dataset.shape[1:]))
    if len(values):
        dataset[start:stop] = values
    return start, stop


class CaptureH5Writer:
    def __init__(
        self,
        path: Path,
        request: RecordingStartRequest,
        recording_id: str,
        recording_start_monotonic_ns: int,
        imu_settings: ImuSettings,
        taxonomy: dict[str, Any],
        *,
        recording_kind: str = "capture",
        training_eligible: bool = False,
        video_status: str = "required",
    ) -> None:
        self.path = path
        self.request = request
        self.recording_id = recording_id
        self.recording_start_monotonic_ns = recording_start_monotonic_ns
        self.imu_settings = imu_settings
        self.taxonomy = taxonomy
        self.recording_kind = recording_kind
        if request.data_tier == DataTier.TEST and training_eligible:
            raise ValueError("test 数据永久禁止标记为可训练")
        self.training_eligible = training_eligible
        self.video_status = video_status
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = h5py.File(path, "w", libver="latest")
        self._disable_file_descriptor_inheritance()
        self.packet_count = 0
        self.imu_packet_count = 0
        self.auxiliary_notification_count = 0
        self.unknown_notification_count = 0
        self.sample_count = 0
        self.parse_errors: list[str] = []
        self._initialize()

    def _disable_file_descriptor_inheritance(self) -> None:
        """阻止后续启动的 FFmpeg 等子进程继承 H5 写锁。"""

        try:
            descriptor = self.handle.id.get_vfd_handle()
            if isinstance(descriptor, int):
                os.set_inheritable(descriptor, False)
        except (AttributeError, OSError, TypeError, ValueError):
            # 某些非 sec2 HDF5 驱动不暴露普通 POSIX 文件描述符；此时仍由
            # 子进程的 close_fds=True 提供第二层隔离。
            return

    def _initialize(self) -> None:
        handle = self.handle
        handle.attrs.update(
            {
                "capture_schema_name": CAPTURE_SCHEMA_NAME,
                "capture_schema_version": CAPTURE_SCHEMA_VERSION,
                "recording_id": self.recording_id,
                "collection_id": self.request.collection_id,
                "participant_id": self.request.participant_id,
                "data_tier": self.request.data_tier.value,
                "body_location": self.request.body_location,
                "protocol_id": self.request.protocol_id,
                "started_at_utc": datetime.now(UTC).isoformat(),
                "recording_start_monotonic_ns": self.recording_start_monotonic_ns,
                "clock_domain": "linux_clock_monotonic",
                "feature_columns": json.dumps(FEATURE_COLUMNS),
                "feature_units": json.dumps(FEATURE_UNITS),
                "calibration_profile_id": self.imu_settings.calibration_profile_id,
                "calibration_verified": bool(
                    self.imu_settings.calibration_verified
                    and self.imu_settings.accel_counts_per_g
                    and self.imu_settings.gyro_counts_per_dps
                ),
                "state": "recording",
                "recording_kind": self.recording_kind,
                "training_eligible": self.training_eligible,
                "video_status": self.video_status,
            }
        )
        imu = handle.create_group("imu")
        imu.attrs.update(
            {
                "device_name": self.imu_settings.name,
                "device_address": self.imu_settings.address,
                "notify_uuid": self.imu_settings.notify_uuid,
                "expected_rate_hz": self.imu_settings.expected_rate_hz,
                "expected_rate_status": self.imu_settings.expected_rate_status,
                "frame_size_bytes": self.imu_settings.frame_size_bytes,
                "callback_drops": 0,
                "byte_order": (
                    "big_endian_verified_current_device"
                    if self.imu_settings.calibration_verified
                    else "big_endian_hypothesis"
                ),
                "frame_layout_status": (
                    "six_axis_int16_verified_trailer_unknown"
                    if self.imu_settings.calibration_verified
                    else "hypothesis_pending_characterization"
                ),
                "calibration_profile_id": self.imu_settings.calibration_profile_id,
                "calibration_method": self.imu_settings.calibration_method,
                "accel_bias_counts_json": json.dumps(
                    self.imu_settings.accel_bias_counts
                ),
                "gyro_bias_counts_json": json.dumps(
                    self.imu_settings.gyro_bias_counts
                ),
                "raw_axis_order_json": json.dumps(self.imu_settings.raw_axis_order),
                "axis_signs_json": json.dumps(self.imu_settings.axis_signs),
                "target_coordinate_system": (
                    "+X wearer-right/interface-opposite; "
                    "+Y head/pendant; +Z body-outside/button"
                ),
            }
        )
        if self.imu_settings.calibration_evidence_sha256:
            imu.attrs["calibration_evidence_sha256"] = (
                self.imu_settings.calibration_evidence_sha256
            )
        if self.imu_settings.accel_counts_per_g:
            imu.attrs["accel_counts_per_g"] = self.imu_settings.accel_counts_per_g
        if self.imu_settings.gyro_counts_per_dps:
            imu.attrs["gyro_counts_per_dps"] = self.imu_settings.gyro_counts_per_dps

        packets = imu.create_group("packets")
        packets.create_dataset(
            "payload_values",
            shape=(0,),
            maxshape=(None,),
            dtype="u1",
            chunks=(BYTE_CHUNK_ROWS,),
            compression="gzip",
            compression_opts=1,
            shuffle=True,
        )
        packets.create_dataset(
            "payload_offsets",
            data=np.asarray([0], dtype=np.int64),
            maxshape=(None,),
            chunks=(1_024,),
        )
        for name, dtype in (
            ("receive_time_ns", "i8"),
            ("fitted_packet_end_time_ns", "i8"),
            ("fit_residual_ns", "i8"),
            ("sample_count", "u2"),
            ("parse_valid", "?"),
            ("packet_kind", "u1"),
        ):
            packets.create_dataset(name, shape=(0,), maxshape=(None,), dtype=dtype, chunks=(1_024,))

        connection_events = imu.create_group("connection_events")
        text = h5py.string_dtype("utf-8")
        for name, dtype in (
            ("event", text),
            ("time_monotonic_ns", "i8"),
            ("notes", text),
        ):
            connection_events.create_dataset(
                name, shape=(0,), maxshape=(None,), dtype=dtype
            )

        samples = imu.create_group("samples")
        samples.create_dataset(
            "raw_counts",
            shape=(0, 6),
            maxshape=(None, 6),
            dtype="i2",
            chunks=(SAMPLE_CHUNK_ROWS, 6),
            compression="gzip",
            compression_opts=4,
            shuffle=True,
            fletcher32=True,
        )
        samples.create_dataset(
            "trailer",
            shape=(0, 4),
            maxshape=(None, 4),
            dtype="u1",
            chunks=(SAMPLE_CHUNK_ROWS, 4),
            compression="gzip",
            compression_opts=4,
            shuffle=True,
            fletcher32=True,
        )
        samples.create_dataset(
            "values_si",
            shape=(0, 6),
            maxshape=(None, 6),
            dtype="f4",
            chunks=(SAMPLE_CHUNK_ROWS, 6),
            compression="gzip",
            compression_opts=4,
            shuffle=True,
            fletcher32=True,
        )
        samples["values_si"].attrs.update(
            {
                "columns": json.dumps(FEATURE_COLUMNS),
                "units": json.dumps(FEATURE_UNITS),
                "coordinate_system": (
                    "+X wearer-right/interface-opposite; "
                    "+Y head/pendant; +Z body-outside/button"
                ),
                "calibration_profile_id": self.imu_settings.calibration_profile_id,
                "unverified_semantics": (
                    "NaN until the device-specific calibration profile is verified"
                ),
            }
        )
        for name, dtype in (
            ("packet_index", "i8"),
            ("sample_in_packet", "u2"),
            ("time_monotonic_ns", "i8"),
            ("recording_time_ns", "i8"),
            ("aligned_video_time_ns", "i8"),
            ("time_quality", "u1"),
        ):
            samples.create_dataset(
                name, shape=(0,), maxshape=(None,), dtype=dtype, chunks=(SAMPLE_CHUNK_ROWS,)
            )

        handle.create_group("video").create_group("frames")
        experiment = handle.create_group("experiment")
        experiment.attrs.update(
            {
                "device_frame_definition_zh": (
                    "+X 指向佩戴者右侧；+Y 指向头部/挂绳端；"
                    "+Z 指向身体外侧/按键面"
                ),
                "stage_time_domain": "recording_relative_ns",
            }
        )
        stages = experiment.create_group("stages")
        text = h5py.string_dtype("utf-8")
        for name, dtype in (
            ("stage_code", text),
            ("start_ns", "i8"),
            ("end_ns", "i8"),
            ("reliability", text),
            ("notes", text),
        ):
            stages.create_dataset(name, shape=(0,), maxshape=(None,), dtype=dtype)
        sync = handle.create_group("sync")
        sync.attrs.update(
            {
                "policy": "conditional_fixed_offset_v1",
                "quality": "missing",
                "decision": "host_only",
                "scale": 1.0,
                "estimated_offset_ns": 0,
                "applied_offset_ns": 0,
                "offset_ns": 0,
            }
        )
        sync.create_dataset("imu_anchor_ns", shape=(0,), maxshape=(None,), dtype="i8")
        sync.create_dataset("video_anchor_ns", shape=(0,), maxshape=(None,), dtype="i8")
        self._write_annotations_group(
            AnnotationDocument(
                taxonomy_id=self.taxonomy["taxonomy_id"],
                taxonomy_version=self.taxonomy["version"],
            )
        )
        handle.flush()

    def append_connection_event(
        self, event: str, time_monotonic_ns: int, notes: str = ""
    ) -> None:
        events = self.handle["imu/connection_events"]
        for name, value in (
            ("event", event),
            ("time_monotonic_ns", time_monotonic_ns),
            ("notes", notes),
        ):
            dataset = events[name]
            dataset.resize((len(dataset) + 1,))
            dataset[-1] = value
        self.handle.flush()

    def set_recording_start(
        self, recording_start_monotonic_ns: int, started_at_utc: str
    ) -> None:
        """在设备均已就绪、尚未写入通知时冻结正式采集起点。"""

        if self.packet_count or self.sample_count:
            raise RuntimeError("已有 IMU 数据后不能修改正式采集起点")
        self.recording_start_monotonic_ns = recording_start_monotonic_ns
        self.handle.attrs.update(
            {
                "recording_start_monotonic_ns": recording_start_monotonic_ns,
                "started_at_utc": started_at_utc,
            }
        )
        self.handle.flush()

    def append_experiment_stage(
        self,
        stage_code: str,
        start_ns: int,
        end_ns: int,
        reliability: str,
        notes: str = "",
    ) -> None:
        if end_ns <= start_ns:
            raise ValueError("实验阶段结束时间必须晚于开始时间")
        stages = self.handle["experiment/stages"]
        values: dict[str, Any] = {
            "stage_code": stage_code,
            "start_ns": start_ns,
            "end_ns": end_ns,
            "reliability": reliability,
            "notes": notes,
        }
        for name, value in values.items():
            dataset = stages[name]
            dataset.resize((len(dataset) + 1,))
            dataset[-1] = value
        self.handle.flush()

    def append_notification(self, payload: bytes, receive_time_ns: int) -> int:
        packets = self.handle["imu/packets"]
        payload_values = packets["payload_values"]
        _resize_append(payload_values, np.frombuffer(payload, dtype=np.uint8))
        offsets = packets["payload_offsets"]
        offsets.resize((len(offsets) + 1,))
        offsets[-1] = len(payload_values)
        _resize_append(packets["receive_time_ns"], np.asarray([receive_time_ns], dtype=np.int64))
        _resize_append(
            packets["fitted_packet_end_time_ns"],
            np.asarray([-1], dtype=np.int64),
        )
        _resize_append(packets["fit_residual_ns"], np.asarray([0], dtype=np.int64))

        kind = classify_notification(payload, self.imu_settings.frame_size_bytes)
        _resize_append(
            packets["packet_kind"], np.asarray([int(kind)], dtype=np.uint8)
        )

        if kind == NotificationKind.AUXILIARY_STATUS:
            _resize_append(packets["sample_count"], np.asarray([0], dtype=np.uint16))
            _resize_append(packets["parse_valid"], np.asarray([False], dtype=np.bool_))
            self.packet_count += 1
            self.auxiliary_notification_count += 1
            self._update_notification_counts()
            self.handle.flush()
            return 0

        if kind == NotificationKind.UNKNOWN_INVALID:
            _resize_append(packets["sample_count"], np.asarray([0], dtype=np.uint16))
            _resize_append(packets["parse_valid"], np.asarray([False], dtype=np.bool_))
            self.parse_errors.append(
                f"unknown notification length {len(payload)}: {payload.hex()}"
            )
            self.packet_count += 1
            self.unknown_notification_count += 1
            self._update_notification_counts()
            self.handle.flush()
            return 0

        try:
            parsed = parse_notification(payload, self.imu_settings.frame_size_bytes)
        except ValueError as error:
            _resize_append(packets["sample_count"], np.asarray([0], dtype=np.uint16))
            _resize_append(packets["parse_valid"], np.asarray([False], dtype=np.bool_))
            self.parse_errors.append(str(error))
            self.packet_count += 1
            self.unknown_notification_count += 1
            packets["packet_kind"][-1] = int(NotificationKind.UNKNOWN_INVALID)
            self._update_notification_counts()
            self.handle.flush()
            return 0

        _resize_append(
            packets["sample_count"], np.asarray([parsed.sample_count], dtype=np.uint16)
        )
        _resize_append(packets["parse_valid"], np.asarray([True], dtype=np.bool_))
        samples = self.handle["imu/samples"]
        _resize_append(samples["raw_counts"], parsed.raw_counts)
        _resize_append(samples["trailer"], parsed.trailer)
        _resize_append(
            samples["values_si"],
            calibrate_counts(
                parsed.raw_counts,
                self.imu_settings.accel_counts_per_g,
                self.imu_settings.gyro_counts_per_dps,
                accel_bias_counts=self.imu_settings.accel_bias_counts,
                gyro_bias_counts=self.imu_settings.gyro_bias_counts,
                raw_axis_order=self.imu_settings.raw_axis_order,
                axis_signs=self.imu_settings.axis_signs,
            ),
        )
        _resize_append(
            samples["packet_index"],
            np.full(parsed.sample_count, self.packet_count, dtype=np.int64),
        )
        _resize_append(
            samples["sample_in_packet"], np.arange(parsed.sample_count, dtype=np.uint16)
        )
        spacing_ns = int(round(1e9 / self.imu_settings.expected_rate_hz))
        provisional = receive_time_ns - spacing_ns * np.arange(
            parsed.sample_count - 1, -1, -1, dtype=np.int64
        )
        _resize_append(samples["time_monotonic_ns"], provisional)
        _resize_append(
            samples["recording_time_ns"], provisional - self.recording_start_monotonic_ns
        )
        _resize_append(
            samples["aligned_video_time_ns"],
            provisional - self.recording_start_monotonic_ns,
        )
        _resize_append(
            samples["time_quality"], np.full(parsed.sample_count, 1, dtype=np.uint8)
        )
        self.packet_count += 1
        self.imu_packet_count += 1
        self.sample_count += parsed.sample_count
        self._update_notification_counts()
        self.handle.flush()
        return parsed.sample_count

    def _update_notification_counts(self) -> None:
        self.handle.attrs.update(
            {
                "packet_count": self.packet_count,
                "imu_packet_count": self.imu_packet_count,
                "auxiliary_notification_count": self.auxiliary_notification_count,
                "unknown_notification_count": self.unknown_notification_count,
                "sample_count": self.sample_count,
                "parse_error_count": self.unknown_notification_count,
            }
        )

    def reconstruct_times(self) -> tuple[float, float]:
        packets = self.handle["imu/packets"]
        valid = np.asarray(packets["parse_valid"], dtype=np.bool_)
        receive = np.asarray(packets["receive_time_ns"], dtype=np.int64)[valid]
        counts = np.asarray(packets["sample_count"], dtype=np.int64)[valid]
        times, rate, residual = reconstruct_sample_times(
            receive, counts, self.imu_settings.expected_rate_hz
        )
        dataset = self.handle["imu/samples/time_monotonic_ns"]
        if len(times) != len(dataset):
            raise ValueError("reconstructed sample count does not match parsed samples")
        dataset[:] = times
        self.handle["imu/samples/time_quality"][:] = 2
        relative = times - self.recording_start_monotonic_ns
        self.handle["imu/samples/recording_time_ns"][:] = relative
        self.handle["imu/samples/aligned_video_time_ns"][:] = relative
        valid_indices = np.flatnonzero(valid)
        fitted_packet_end = self.handle["imu/packets/fitted_packet_end_time_ns"]
        fit_residual = self.handle["imu/packets/fit_residual_ns"]
        if len(times):
            valid_ends = np.cumsum(counts, dtype=np.int64) - 1
            fitted_values = times[valid_ends]
            fitted_packet_end[valid_indices] = fitted_values
            fit_residual[valid_indices] = receive - fitted_values
        self.handle["imu"].attrs["observed_rate_hz"] = rate
        self.handle["imu"].attrs["packet_end_fit_residual_rms_ns"] = residual
        valid_residual = np.asarray(fit_residual, dtype=np.int64)[valid]
        self.handle["imu"].attrs["packet_end_fit_residual_max_abs_ns"] = (
            int(np.max(np.abs(valid_residual))) if len(valid_residual) else 0
        )
        self.handle["imu"].attrs["packet_end_fit_residual_p95_abs_ns"] = (
            float(np.percentile(np.abs(valid_residual), 95))
            if len(valid_residual)
            else 0.0
        )
        self.handle["imu"].attrs["max_packet_gap_ns"] = (
            int(np.max(np.diff(receive))) if len(receive) > 1 else 0
        )
        self.handle.flush()
        return rate, residual

    def write_video_frames(
        self,
        *,
        pts_monotonic_ns: np.ndarray,
        media_time_ns: np.ndarray | None = None,
        duration_ns: np.ndarray,
        key_frame: np.ndarray,
        video_path: Path,
        codec: str,
        width: int,
        height: int,
        requested_fps: float,
        ffmpeg_diagnostics: list[str] | None = None,
        camera_controls_requested: dict[str, int] | None = None,
        camera_controls_effective: dict[str, int] | None = None,
        camera_control_errors: list[str] | None = None,
    ) -> None:
        video = self.handle["video"]
        frames = video["frames"]
        for name in tuple(frames.keys()):
            del frames[name]
        pts = np.asarray(pts_monotonic_ns, dtype=np.int64)
        media_times = (
            np.asarray(media_time_ns, dtype=np.int64)
            if media_time_ns is not None
            else pts - pts[0] if len(pts) else np.asarray([], dtype=np.int64)
        )
        durations = np.asarray(duration_ns, dtype=np.int64)
        keys = np.asarray(key_frame, dtype=np.bool_)
        if not (len(pts) == len(media_times) == len(durations) == len(keys)):
            raise ValueError("video frame arrays must have equal length")
        frames.create_dataset("pts_monotonic_ns", data=pts, chunks=True)
        frames.create_dataset("media_time_ns", data=media_times, chunks=True)
        frames.create_dataset("duration_ns", data=durations, chunks=True)
        frames.create_dataset("key_frame", data=keys, chunks=True)
        frames.create_dataset(
            "recording_time_ns",
            data=pts - self.recording_start_monotonic_ns,
            chunks=True,
        )
        deltas = np.diff(pts)
        actual_fps = float(1e9 / np.median(deltas)) if len(deltas) else 0.0
        actual_span_fps = (
            float((len(pts) - 1) * 1e9 / (pts[-1] - pts[0]))
            if len(pts) > 1 and pts[-1] > pts[0]
            else 0.0
        )
        video.attrs.update(
            {
                "path": video_path.name,
                "sha256": sha256_file(video_path),
                "codec": codec,
                "width": width,
                "height": height,
                "requested_fps": requested_fps,
                "actual_median_fps": actual_fps,
                "actual_span_fps": actual_span_fps,
                "frame_count": len(pts),
                "media_timeline_origin": "first_encoded_frame",
                "source_pts_origin_monotonic_ns": int(pts[0]) if len(pts) else -1,
                "ffmpeg_diagnostic_count": len(ffmpeg_diagnostics or []),
                "ffmpeg_diagnostics_json": json.dumps(
                    ffmpeg_diagnostics or [], ensure_ascii=False
                ),
                "camera_controls_requested_json": json.dumps(
                    camera_controls_requested or {}, ensure_ascii=False, sort_keys=True
                ),
                "camera_controls_effective_json": json.dumps(
                    camera_controls_effective or {}, ensure_ascii=False, sort_keys=True
                ),
                "camera_control_errors_json": json.dumps(
                    camera_control_errors or [], ensure_ascii=False
                ),
            }
        )
        self.handle.flush()

    def write_sync(self, anchors: list[SyncAnchor]) -> SyncModel:
        """初始化无锚点状态；旧仿射拟合仅保留为只读兼容实现。"""

        if anchors:
            raise ValueError("正式同步请使用条件式固定偏移接口")
        sync = self.handle["sync"]
        sync.attrs["anchor_clock_domain"] = "recording_relative_ns"
        for name in ("labels", "roles", "reviewer_ids"):
            if name in sync:
                del sync[name]
        for name in ("imu_anchor_ns", "video_anchor_ns"):
            dataset = sync[name]
            dataset.resize((0,))
        sync.attrs.update(
            {
                "policy": "conditional_fixed_offset_v1",
                "scale": 1.0,
                "estimated_offset_ns": 0,
                "applied_offset_ns": 0,
                "offset_ns": 0,
                "residual_rms_ns": float("nan"),
                "quality": "missing",
                "decision": "host_only",
            }
        )
        sample_times = np.asarray(
            self.handle["imu/samples/time_monotonic_ns"], dtype=np.int64
        )
        if len(sample_times):
            relative = sample_times - self.recording_start_monotonic_ns
            self.handle["imu/samples/recording_time_ns"][:] = relative
            if "aligned_video_time_ns" in self.handle["imu/samples"]:
                self.handle["imu/samples/aligned_video_time_ns"][:] = relative
        self.handle.flush()
        return SyncModel(1.0, 0.0, float("nan"), "host_only")

    def write_conditional_sync(
        self, document: SyncDocument
    ) -> ConditionalSyncAssessment:
        assessment = assess_conditional_fixed_offset(document)
        sync = self.handle["sync"]
        sync.attrs["anchor_clock_domain"] = "recording_relative_ns"
        text = h5py.string_dtype("utf-8")
        string_values = {
            "labels": [anchor.label for anchor in document.anchors],
            "roles": [anchor.role for anchor in document.anchors],
            "reviewer_ids": [anchor.reviewer_id or "" for anchor in document.anchors],
        }
        int_values = {
            "imu_anchor_ns": [anchor.imu_time_ns for anchor in document.anchors],
            "video_anchor_ns": [anchor.video_time_ns for anchor in document.anchors],
            "source_video_frame": [
                -1 if anchor.source_video_frame is None else anchor.source_video_frame
                for anchor in document.anchors
            ],
            "source_imu_sample": [
                -1 if anchor.source_imu_sample is None else anchor.source_imu_sample
                for anchor in document.anchors
            ],
            "video_interval_start_ns": [
                anchor.video_time_ns
                if anchor.video_interval_start_ns is None
                else anchor.video_interval_start_ns
                for anchor in document.anchors
            ],
            "imu_interval_start_ns": [
                anchor.imu_time_ns
                if anchor.imu_interval_start_ns is None
                else anchor.imu_interval_start_ns
                for anchor in document.anchors
            ],
        }
        for name, values in string_values.items():
            if name in sync:
                del sync[name]
            sync.create_dataset(name, data=np.asarray(values, dtype=text), maxshape=(None,))
        for name, values in int_values.items():
            if name in sync:
                dataset = sync[name]
                dataset.resize((len(values),))
                dataset[:] = np.asarray(values, dtype=np.int64)
            else:
                sync.create_dataset(
                    name,
                    data=np.asarray(values, dtype=np.int64),
                    maxshape=(None,),
                )
        sync.attrs.update(assessment.as_dict())
        sync.attrs["reviewer_id"] = document.reviewer_id or ""
        sample_times = np.asarray(
            self.handle["imu/samples/time_monotonic_ns"], dtype=np.int64
        )
        relative = sample_times - self.recording_start_monotonic_ns
        samples = self.handle["imu/samples"]
        samples["recording_time_ns"][:] = relative
        aligned = relative + assessment.applied_offset_ns
        if "aligned_video_time_ns" not in samples:
            samples.create_dataset(
                "aligned_video_time_ns",
                data=aligned,
                maxshape=(None,),
                chunks=(SAMPLE_CHUNK_ROWS,),
            )
        else:
            samples["aligned_video_time_ns"][:] = aligned
        self.handle.flush()
        return assessment

    def _write_annotations_group(self, document: AnnotationDocument) -> None:
        if "annotations" in self.handle:
            del self.handle["annotations"]
        group = self.handle.create_group("annotations")
        group.attrs.update(
            {
                "taxonomy_id": document.taxonomy_id,
                "taxonomy_version": document.taxonomy_version,
                "revision": document.revision,
                "finalized": document.finalized,
                "updated_at_utc": datetime.now(UTC).isoformat(),
            }
        )
        text = h5py.string_dtype("utf-8")
        segments = group.create_group("segments")
        segment_columns: dict[str, tuple[Any, Any]] = {
            "segment_id": ([item.segment_id for item in document.segments], text),
            "start_ns": ([item.start_ns for item in document.segments], "i8"),
            "end_ns": ([item.end_ns for item in document.segments], "i8"),
            "binary_label": ([item.binary_label.value for item in document.segments], text),
            "activity_code": ([item.activity_code for item in document.segments], text),
            "annotator_id": ([item.annotator_id for item in document.segments], text),
            "confidence": ([item.confidence for item in document.segments], "f4"),
            "notes": ([item.notes for item in document.segments], text),
        }
        for name, (values, dtype) in segment_columns.items():
            segments.create_dataset(name, data=np.asarray(values, dtype=dtype), maxshape=(None,))
        events = group.create_group("events")
        event_columns: dict[str, tuple[Any, Any]] = {
            "segment_id": ([item.segment_id for item in document.events], text),
            "kind": ([item.kind.value for item in document.events], text),
            "time_ns": ([item.time_ns for item in document.events], "i8"),
            "source_video_frame": (
                [
                    item.source_video_frame
                    if item.source_video_frame is not None
                    else -1
                    for item in document.events
                ],
                "i8",
            ),
            "source_imu_sample": (
                [
                    item.source_imu_sample
                    if item.source_imu_sample is not None
                    else -1
                    for item in document.events
                ],
                "i8",
            ),
            "annotator_id": ([item.annotator_id for item in document.events], text),
        }
        for name, (values, dtype) in event_columns.items():
            events.create_dataset(name, data=np.asarray(values, dtype=dtype), maxshape=(None,))
        exclusions = group.create_group("exclusions")
        exclusion_columns: dict[str, tuple[Any, Any]] = {
            "exclusion_id": ([item.exclusion_id for item in document.exclusions], text),
            "start_ns": ([item.start_ns for item in document.exclusions], "i8"),
            "end_ns": ([item.end_ns for item in document.exclusions], "i8"),
            "reason": ([item.reason.value for item in document.exclusions], text),
            "annotator_id": ([item.annotator_id for item in document.exclusions], text),
            "notes": ([item.notes for item in document.exclusions], text),
        }
        for name, (values, dtype) in exclusion_columns.items():
            exclusions.create_dataset(
                name, data=np.asarray(values, dtype=dtype), maxshape=(None,)
            )

    def finish(self, ended_at_utc: str | None = None) -> None:
        sample_time = np.asarray(self.handle["imu/samples/recording_time_ns"], dtype=np.int64)
        video_time = (
            np.asarray(self.handle["video/frames/recording_time_ns"], dtype=np.int64)
            if "video/frames/recording_time_ns" in self.handle
            else np.empty(0, dtype=np.int64)
        )
        maxima = [int(values[-1]) for values in (sample_time, video_time) if len(values)]
        self.handle.attrs.update(
            {
                "ended_at_utc": ended_at_utc or datetime.now(UTC).isoformat(),
                "duration_ns": max(maxima, default=0),
                "state": "finalized",
                "packet_count": self.packet_count,
                "imu_packet_count": self.imu_packet_count,
                "auxiliary_notification_count": self.auxiliary_notification_count,
                "unknown_notification_count": self.unknown_notification_count,
                "sample_count": self.sample_count,
                "parse_error_count": self.unknown_notification_count,
                "parse_errors": json.dumps(self.parse_errors[:20]),
            }
        )
        self.handle.flush()
        self.handle.close()

    def abort_close(self) -> None:
        if self.handle and self.handle.id.valid:
            self.handle.attrs["state"] = "interrupted"
            self.handle.flush()
            self.handle.close()


def read_annotations(path: Path) -> AnnotationDocument:
    with h5py.File(path, "r") as handle:
        return read_annotation_document(handle)


def replace_annotations_atomic(
    path: Path,
    document: AnnotationDocument,
    taxonomy: dict[str, Any],
) -> None:
    with h5py.File(path, "r") as source:
        duration_ns = int(source.attrs.get("duration_ns", 0)) or None
        sync_quality = str(source["sync"].attrs.get("quality", "missing"))
    issues = validate_annotations(document, taxonomy, duration_ns)
    if document.finalized and sync_quality != "verified":
        issues.append(f"定稿前必须完成同步验证，当前状态为 {sync_quality}")
    if issues:
        raise ValueError("; ".join(issues))
    temporary = path.with_suffix(".annotating.h5")
    shutil.copy2(path, temporary)
    try:
        with h5py.File(temporary, "r+") as handle:
            writer = object.__new__(CaptureH5Writer)
            writer.handle = handle
            writer._write_annotations_group(document)
            handle.flush()
        report = validate_capture_h5(temporary, taxonomy, require_sync=False)
        structural_issues = [
            issue
            for issue in report.issues
            if not issue.startswith("synchronization is not verified")
        ]
        if structural_issues:
            raise ValueError("; ".join(structural_issues))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def replace_sync_atomic(
    path: Path,
    document: SyncDocument,
    taxonomy: dict[str, Any],
) -> ConditionalSyncAssessment:
    temporary = path.with_suffix(".syncing.h5")
    shutil.copy2(path, temporary)
    try:
        with h5py.File(temporary, "r+") as handle:
            writer = object.__new__(CaptureH5Writer)
            writer.handle = handle
            writer.recording_start_monotonic_ns = int(
                handle.attrs["recording_start_monotonic_ns"]
            )
            model = writer.write_conditional_sync(document)
            handle.attrs["sync_updated_at_utc"] = datetime.now(UTC).isoformat()
            handle.flush()
        report = validate_capture_h5(temporary, taxonomy, require_sync=False)
        if not report.ready:
            raise ValueError("; ".join(report.issues))
        os.replace(temporary, path)
        return model
    finally:
        temporary.unlink(missing_ok=True)
