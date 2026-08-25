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
    calibrate_counts,
    parse_notification,
    reconstruct_sample_times,
)
from imu_data_collector.models import AnnotationDocument, RecordingStartRequest, SyncAnchor
from imu_data_collector.sync import SyncModel, fit_sync_model
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
        self.training_eligible = training_eligible
        self.video_status = video_status
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = h5py.File(path, "w", libver="latest")
        self.packet_count = 0
        self.sample_count = 0
        self.parse_errors: list[str] = []
        self._initialize()

    def _initialize(self) -> None:
        handle = self.handle
        handle.attrs.update(
            {
                "capture_schema_name": CAPTURE_SCHEMA_NAME,
                "capture_schema_version": CAPTURE_SCHEMA_VERSION,
                "recording_id": self.recording_id,
                "collection_id": self.request.collection_id,
                "participant_id": self.request.participant_id,
                "body_location": self.request.body_location,
                "protocol_id": self.request.protocol_id,
                "started_at_utc": datetime.now(UTC).isoformat(),
                "recording_start_monotonic_ns": self.recording_start_monotonic_ns,
                "clock_domain": "linux_clock_monotonic",
                "feature_columns": json.dumps(FEATURE_COLUMNS),
                "feature_units": json.dumps(FEATURE_UNITS),
                "calibration_verified": bool(
                    self.imu_settings.accel_counts_per_g
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
                "byte_order": "big_endian_hypothesis",
                "frame_layout_status": "hypothesis_pending_characterization",
            }
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
            ("sample_count", "u2"),
            ("parse_valid", "?"),
        ):
            packets.create_dataset(name, shape=(0,), maxshape=(None,), dtype=dtype, chunks=(1_024,))

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
                "unverified_semantics": "NaN until both device scales are verified",
            }
        )
        for name, dtype in (
            ("packet_index", "i8"),
            ("sample_in_packet", "u2"),
            ("time_monotonic_ns", "i8"),
            ("recording_time_ns", "i8"),
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
        sync.attrs.update({"quality": "missing", "scale": 1.0, "offset_ns": 0.0})
        sync.create_dataset("imu_anchor_ns", shape=(0,), maxshape=(None,), dtype="i8")
        sync.create_dataset("video_anchor_ns", shape=(0,), maxshape=(None,), dtype="i8")
        self._write_annotations_group(
            AnnotationDocument(
                taxonomy_id=self.taxonomy["taxonomy_id"],
                taxonomy_version=self.taxonomy["version"],
            )
        )
        handle.flush()

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

        try:
            parsed = parse_notification(payload, self.imu_settings.frame_size_bytes)
        except ValueError as error:
            _resize_append(packets["sample_count"], np.asarray([0], dtype=np.uint16))
            _resize_append(packets["parse_valid"], np.asarray([False], dtype=np.bool_))
            self.parse_errors.append(str(error))
            self.packet_count += 1
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
            samples["time_quality"], np.full(parsed.sample_count, 1, dtype=np.uint8)
        )
        self.packet_count += 1
        self.sample_count += parsed.sample_count
        self.handle.attrs["packet_count"] = self.packet_count
        self.handle.attrs["sample_count"] = self.sample_count
        self.handle.flush()
        return parsed.sample_count

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
        self.handle["imu"].attrs["observed_rate_hz"] = rate
        self.handle["imu"].attrs["packet_end_fit_residual_rms_ns"] = residual
        self.handle.flush()
        return rate, residual

    def write_video_frames(
        self,
        *,
        pts_monotonic_ns: np.ndarray,
        duration_ns: np.ndarray,
        key_frame: np.ndarray,
        video_path: Path,
        codec: str,
        width: int,
        height: int,
        requested_fps: float,
    ) -> None:
        video = self.handle["video"]
        frames = video["frames"]
        for name in tuple(frames.keys()):
            del frames[name]
        pts = np.asarray(pts_monotonic_ns, dtype=np.int64)
        durations = np.asarray(duration_ns, dtype=np.int64)
        keys = np.asarray(key_frame, dtype=np.bool_)
        if not (len(pts) == len(durations) == len(keys)):
            raise ValueError("video frame arrays must have equal length")
        frames.create_dataset("pts_monotonic_ns", data=pts, chunks=True)
        frames.create_dataset("duration_ns", data=durations, chunks=True)
        frames.create_dataset("key_frame", data=keys, chunks=True)
        frames.create_dataset(
            "recording_time_ns",
            data=pts - self.recording_start_monotonic_ns,
            chunks=True,
        )
        deltas = np.diff(pts)
        actual_fps = float(1e9 / np.median(deltas)) if len(deltas) else 0.0
        video.attrs.update(
            {
                "path": video_path.name,
                "sha256": sha256_file(video_path),
                "codec": codec,
                "width": width,
                "height": height,
                "requested_fps": requested_fps,
                "actual_median_fps": actual_fps,
                "frame_count": len(pts),
            }
        )
        self.handle.flush()

    def write_sync(self, anchors: list[SyncAnchor]) -> SyncModel:
        imu_anchor = np.asarray([anchor.imu_time_ns for anchor in anchors], dtype=np.int64)
        video_anchor = np.asarray([anchor.video_time_ns for anchor in anchors], dtype=np.int64)
        model = fit_sync_model(imu_anchor, video_anchor)
        sync = self.handle["sync"]
        sync.attrs["anchor_clock_domain"] = "recording_relative_ns"
        if "labels" in sync:
            del sync["labels"]
        sync.create_dataset(
            "labels",
            data=np.asarray(
                [anchor.label for anchor in anchors], dtype=h5py.string_dtype("utf-8")
            ),
            maxshape=(None,),
        )
        for name, values in (("imu_anchor_ns", imu_anchor), ("video_anchor_ns", video_anchor)):
            dataset = sync[name]
            dataset.resize((len(values),))
            dataset[:] = values
        sync.attrs.update(
            {
                "scale": model.scale,
                "offset_ns": model.offset_ns,
                "residual_rms_ns": model.residual_rms_ns,
                "quality": model.quality,
            }
        )
        sample_times = np.asarray(
            self.handle["imu/samples/time_monotonic_ns"], dtype=np.int64
        )
        if len(sample_times):
            imu_recording_time = sample_times - self.recording_start_monotonic_ns
            self.handle["imu/samples/recording_time_ns"][:] = model.imu_to_video(
                imu_recording_time
            )
        self.handle.flush()
        return model

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
                "parse_error_count": len(self.parse_errors),
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
    issues = validate_annotations(document, taxonomy, duration_ns)
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
    anchors: list[SyncAnchor],
    taxonomy: dict[str, Any],
) -> SyncModel:
    temporary = path.with_suffix(".syncing.h5")
    shutil.copy2(path, temporary)
    try:
        with h5py.File(temporary, "r+") as handle:
            writer = object.__new__(CaptureH5Writer)
            writer.handle = handle
            writer.recording_start_monotonic_ns = int(
                handle.attrs["recording_start_monotonic_ns"]
            )
            model = writer.write_sync(anchors)
            handle.attrs["sync_updated_at_utc"] = datetime.now(UTC).isoformat()
            handle.flush()
        report = validate_capture_h5(temporary, taxonomy, require_sync=False)
        if not report.ready:
            raise ValueError("; ".join(report.issues))
        os.replace(temporary, path)
        return model
    finally:
        temporary.unlink(missing_ok=True)
