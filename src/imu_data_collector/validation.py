"""对采集产物和标注执行自动验证。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from imu_data_collector.constants import CAPTURE_SCHEMA_NAME, CAPTURE_SCHEMA_VERSION
from imu_data_collector.models import AnnotationDocument, BinaryLabel, EventKind


@dataclass(frozen=True, slots=True)
class ValidationReport:
    ready: bool
    issues: tuple[str, ...]
    metrics: dict[str, int | float | str]


def validate_annotations(
    document: AnnotationDocument,
    taxonomy: dict[str, Any],
    duration_ns: int | None,
) -> list[str]:
    issues: list[str] = []
    taxonomy_codes = {
        label: {item["code"] for item in taxonomy[label]} for label in ("fall", "non_fall")
    }
    if document.taxonomy_id != taxonomy["taxonomy_id"]:
        issues.append("annotation taxonomy_id does not match configured taxonomy")
    if document.taxonomy_version != taxonomy["version"]:
        issues.append("annotation taxonomy_version does not match configured taxonomy")

    ordered = sorted(document.segments, key=lambda segment: (segment.start_ns, segment.end_ns))
    if ordered != document.segments:
        issues.append("segments must be sorted by start time")
    segment_by_id = {segment.segment_id: segment for segment in document.segments}
    if len(segment_by_id) != len(document.segments):
        issues.append("segment_id values must be unique")
    previous_end = 0
    for segment in ordered:
        if segment.start_ns < previous_end:
            issues.append(f"segment {segment.segment_id} overlaps the previous segment")
        previous_end = max(previous_end, segment.end_ns)
        allowed = taxonomy_codes[segment.binary_label.value]
        if segment.activity_code not in allowed:
            issues.append(
                f"segment {segment.segment_id} activity {segment.activity_code!r} "
                f"is not a {segment.binary_label.value} taxonomy entry"
            )
        if duration_ns is not None and segment.end_ns > duration_ns:
            issues.append(f"segment {segment.segment_id} lies outside the recording")

    events_by_segment: dict[str, dict[EventKind, list[int]]] = {}
    for event in document.events:
        segment = segment_by_id.get(event.segment_id)
        if segment is None:
            issues.append(f"event references unknown segment {event.segment_id}")
            continue
        if not (segment.start_ns <= event.time_ns < segment.end_ns):
            issues.append(f"{event.kind.value} for {event.segment_id} lies outside its segment")
        events_by_segment.setdefault(event.segment_id, {}).setdefault(event.kind, []).append(
            event.time_ns
        )

    for segment in document.segments:
        grouped = events_by_segment.get(segment.segment_id, {})
        onset = grouped.get(EventKind.ONSET, [])
        impact = grouped.get(EventKind.IMPACT, [])
        if segment.binary_label == BinaryLabel.NON_FALL and (onset or impact):
            issues.append(f"non_fall segment {segment.segment_id} cannot contain fall events")
        if segment.binary_label == BinaryLabel.FALL:
            if len(onset) > 1 or len(impact) > 1:
                issues.append(f"fall segment {segment.segment_id} has duplicate onset/impact")
            if document.finalized and (len(onset) != 1 or len(impact) != 1):
                issues.append(
                    f"finalized fall segment {segment.segment_id} requires onset and impact"
                )
            if len(onset) == 1 and len(impact) == 1 and onset[0] >= impact[0]:
                issues.append(f"fall segment {segment.segment_id} must satisfy onset < impact")
    return issues


def validate_capture_h5(
    path: Path,
    taxonomy: dict[str, Any] | None = None,
    *,
    require_video: bool = True,
    require_sync: bool = False,
    require_calibration: bool = False,
) -> ValidationReport:
    issues: list[str] = []
    metrics: dict[str, int | float | str] = {}
    try:
        handle = h5py.File(path, "r")
    except (OSError, ValueError) as error:
        return ValidationReport(False, (f"cannot open HDF5: {error}",), metrics)

    with handle:
        if handle.attrs.get("capture_schema_name") != CAPTURE_SCHEMA_NAME:
            issues.append("invalid capture_schema_name")
        if handle.attrs.get("capture_schema_version") != CAPTURE_SCHEMA_VERSION:
            issues.append("invalid capture_schema_version")
        required = (
            "imu/packets/payload_values",
            "imu/packets/payload_offsets",
            "imu/packets/receive_time_ns",
            "imu/packets/sample_count",
            "imu/samples/raw_counts",
            "imu/samples/trailer",
            "imu/samples/packet_index",
            "imu/samples/sample_in_packet",
            "imu/samples/time_monotonic_ns",
            "imu/samples/recording_time_ns",
            "imu/samples/values_si",
        )
        for name in required:
            if name not in handle:
                issues.append(f"missing dataset {name}")
        if issues:
            return ValidationReport(False, tuple(issues), metrics)

        payload_values = handle["imu/packets/payload_values"]
        offsets = np.asarray(handle["imu/packets/payload_offsets"], dtype=np.int64)
        receive = np.asarray(handle["imu/packets/receive_time_ns"], dtype=np.int64)
        sample_counts = np.asarray(handle["imu/packets/sample_count"], dtype=np.int64)
        raw_counts = handle["imu/samples/raw_counts"]
        trailer = handle["imu/samples/trailer"]
        packet_index = handle["imu/samples/packet_index"]
        sample_in_packet = handle["imu/samples/sample_in_packet"]
        sample_times = np.asarray(handle["imu/samples/time_monotonic_ns"], dtype=np.int64)
        recording_times = np.asarray(handle["imu/samples/recording_time_ns"], dtype=np.int64)
        values_si = handle["imu/samples/values_si"]

        packet_count = len(receive)
        sample_count = len(raw_counts)
        metrics.update(packet_count=packet_count, sample_count=sample_count)
        if len(offsets) != packet_count + 1 or offsets[0] != 0:
            issues.append("packet payload offsets have invalid length or origin")
        elif np.any(np.diff(offsets) < 0) or offsets[-1] != len(payload_values):
            issues.append("packet payload offsets are invalid")
        if len(sample_counts) != packet_count:
            issues.append("packet sample_count length mismatch")
        if packet_count == 0:
            issues.append("recording contains no IMU packets")
        if sample_count == 0:
            issues.append("recording contains no parsed IMU samples")
        sample_lengths = {
            sample_count,
            len(trailer),
            len(packet_index),
            len(sample_in_packet),
            len(sample_times),
            len(recording_times),
            len(values_si),
        }
        if len(sample_lengths) != 1:
            issues.append("IMU sample datasets have inconsistent lengths")
        if sample_count and np.any(np.diff(sample_times) <= 0):
            issues.append("IMU estimated timestamps are not strictly increasing")
        if sample_count and np.any(np.diff(recording_times) <= 0):
            issues.append("IMU recording timestamps are not strictly increasing")
        if raw_counts.shape != (sample_count, 6):
            issues.append("raw_counts must have shape (N, 6)")
        if trailer.shape != (sample_count, 4):
            issues.append("trailer must have shape (N, 4)")
        if values_si.shape != (sample_count, 6):
            issues.append("values_si must have shape (N, 6)")
        calibrated = bool(handle.attrs.get("calibration_verified", False))
        metrics["calibration_verified"] = str(calibrated).lower()
        if require_calibration and not calibrated:
            issues.append("device calibration is not verified")

        if require_video:
            if "video/frames/pts_monotonic_ns" not in handle:
                issues.append("missing finalized video frame timestamps")
            else:
                video_pts = np.asarray(handle["video/frames/pts_monotonic_ns"], dtype=np.int64)
                video_recording = np.asarray(
                    handle["video/frames/recording_time_ns"], dtype=np.int64
                )
                metrics["video_frame_count"] = len(video_pts)
                if not len(video_pts):
                    issues.append("recording contains no video frames")
                if len(video_pts) != len(video_recording):
                    issues.append("video timestamp dataset length mismatch")
                if len(video_pts) > 1 and np.any(np.diff(video_pts) <= 0):
                    issues.append("video PTS are not strictly increasing")
                mkv_path = Path(str(handle["video"].attrs.get("path", "")))
                if not mkv_path.name:
                    issues.append("video path is missing")
                if not handle["video"].attrs.get("sha256"):
                    issues.append("video SHA-256 is missing")

        sync_quality = str(handle["sync"].attrs.get("quality", "missing"))
        metrics["sync_quality"] = sync_quality
        if require_sync and sync_quality != "verified":
            issues.append(f"synchronization is not verified: {sync_quality}")

        duration_ns = int(handle.attrs.get("duration_ns", 0)) or None
        if duration_ns is not None:
            metrics["duration_ns"] = duration_ns
        if taxonomy is not None and "annotations" in handle:
            document = read_annotation_document(handle)
            issues.extend(validate_annotations(document, taxonomy, duration_ns))

    return ValidationReport(not issues, tuple(issues), metrics)


def _text_list(dataset: h5py.Dataset) -> list[str]:
    return [item.decode() if isinstance(item, bytes) else str(item) for item in dataset]


def read_annotation_document(handle: h5py.File) -> AnnotationDocument:
    from imu_data_collector.models import ActivitySegment, AnnotationEvent

    group = handle["annotations"]
    segments_group = group["segments"]
    events_group = group["events"]
    segment_ids = _text_list(segments_group["segment_id"])
    segment_labels = _text_list(segments_group["binary_label"])
    activity_codes = _text_list(segments_group["activity_code"])
    annotators = _text_list(segments_group["annotator_id"])
    notes = _text_list(segments_group["notes"])
    segments = [
        ActivitySegment(
            segment_id=segment_ids[index],
            start_ns=int(segments_group["start_ns"][index]),
            end_ns=int(segments_group["end_ns"][index]),
            binary_label=segment_labels[index],
            activity_code=activity_codes[index],
            annotator_id=annotators[index],
            confidence=float(segments_group["confidence"][index]),
            notes=notes[index],
        )
        for index in range(len(segment_ids))
    ]
    event_segment_ids = _text_list(events_group["segment_id"])
    kinds = _text_list(events_group["kind"])
    event_annotators = _text_list(events_group["annotator_id"])
    events = [
        AnnotationEvent(
            segment_id=event_segment_ids[index],
            kind=kinds[index],
            time_ns=int(events_group["time_ns"][index]),
            source_video_frame=(
                None
                if int(events_group["source_video_frame"][index]) < 0
                else int(events_group["source_video_frame"][index])
            ),
            source_imu_sample=(
                None
                if int(events_group["source_imu_sample"][index]) < 0
                else int(events_group["source_imu_sample"][index])
            ),
            annotator_id=event_annotators[index],
        )
        for index in range(len(event_segment_ids))
    ]
    return AnnotationDocument(
        taxonomy_id=str(group.attrs["taxonomy_id"]),
        taxonomy_version=str(group.attrs["taxonomy_version"]),
        revision=int(group.attrs.get("revision", 1)),
        finalized=bool(group.attrs.get("finalized", False)),
        segments=segments,
        events=events,
    )
