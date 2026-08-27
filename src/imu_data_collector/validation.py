"""对采集产物和标注执行自动验证。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from imu_data_collector.constants import (
    CAPTURE_SCHEMA_NAME,
    CAPTURE_SCHEMA_VERSION,
    SUPPORTED_CAPTURE_SCHEMA_VERSIONS,
)
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

    ordered_exclusions = sorted(
        document.exclusions, key=lambda item: (item.start_ns, item.end_ns)
    )
    if ordered_exclusions != document.exclusions:
        issues.append("exclusions must be sorted by start time")
    exclusion_ids = {item.exclusion_id for item in document.exclusions}
    if len(exclusion_ids) != len(document.exclusions):
        issues.append("exclusion_id values must be unique")
    previous_exclusion_end = 0
    for exclusion in ordered_exclusions:
        if exclusion.start_ns < previous_exclusion_end:
            issues.append(
                f"exclusion {exclusion.exclusion_id} overlaps the previous exclusion"
            )
        previous_exclusion_end = max(previous_exclusion_end, exclusion.end_ns)
        if duration_ns is not None and exclusion.end_ns > duration_ns:
            issues.append(
                f"exclusion {exclusion.exclusion_id} lies outside the recording"
            )

    coverage = sorted(
        [
            (item.start_ns, item.end_ns, f"segment {item.segment_id}")
            for item in document.segments
        ]
        + [
            (item.start_ns, item.end_ns, f"exclusion {item.exclusion_id}")
            for item in document.exclusions
        ],
        key=lambda item: (item[0], item[1]),
    )
    coverage_end = 0
    for start_ns, end_ns, label in coverage:
        if start_ns < coverage_end:
            issues.append(f"{label} overlaps another annotation interval")
        if document.finalized and start_ns > coverage_end:
            issues.append(
                f"finalized annotation has an uncovered interval "
                f"[{coverage_end}, {start_ns})"
            )
        coverage_end = max(coverage_end, end_ns)
    if document.finalized and duration_ns is not None and coverage_end < duration_ns:
        issues.append(
            f"finalized annotation has an uncovered interval "
            f"[{coverage_end}, {duration_ns})"
        )

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
            if len(onset) == 1 and onset[0] != segment.start_ns:
                issues.append(
                    f"fall segment {segment.segment_id} onset must equal segment start"
                )
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
        schema_version = str(handle.attrs.get("capture_schema_version", ""))
        metrics["capture_schema_version"] = schema_version
        if schema_version not in SUPPORTED_CAPTURE_SCHEMA_VERSIONS:
            issues.append("unsupported capture_schema_version")
        data_tier = str(handle.attrs.get("data_tier", "legacy_unclassified"))
        training_eligible = bool(handle.attrs.get("training_eligible", False))
        metrics["data_tier"] = data_tier
        metrics["training_eligible"] = str(training_eligible).lower()
        if schema_version == CAPTURE_SCHEMA_VERSION and "data_tier" not in handle.attrs:
            issues.append("missing data_tier for current schema")
        if schema_version == CAPTURE_SCHEMA_VERSION:
            if "calibration_profile_id" not in handle.attrs:
                issues.append("missing calibration_profile_id for current schema")
            if "imu" in handle:
                for name in (
                    "accel_bias_counts_json",
                    "gyro_bias_counts_json",
                    "raw_axis_order_json",
                    "axis_signs_json",
                ):
                    if name not in handle["imu"].attrs:
                        issues.append(f"missing IMU calibration attribute {name}")
        if data_tier not in {"test", "prod", "legacy_unclassified"}:
            issues.append(f"invalid data_tier: {data_tier}")
        if training_eligible and data_tier != "prod":
            issues.append("only prod data may be marked training_eligible")
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
        if schema_version == CAPTURE_SCHEMA_VERSION:
            required += (
                "imu/packets/packet_kind",
                "imu/packets/fitted_packet_end_time_ns",
                "imu/packets/fit_residual_ns",
                "imu/connection_events/event",
                "imu/connection_events/time_monotonic_ns",
                "imu/samples/aligned_video_time_ns",
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
        packet_kinds = (
            np.asarray(handle["imu/packets/packet_kind"], dtype=np.uint8)
            if "imu/packets/packet_kind" in handle
            else None
        )
        parse_valid = (
            np.asarray(handle["imu/packets/parse_valid"], dtype=np.bool_)
            if "imu/packets/parse_valid" in handle
            else None
        )
        fitted_packet_end = (
            handle["imu/packets/fitted_packet_end_time_ns"]
            if "imu/packets/fitted_packet_end_time_ns" in handle
            else None
        )
        packet_fit_residual = (
            handle["imu/packets/fit_residual_ns"]
            if "imu/packets/fit_residual_ns" in handle
            else None
        )
        raw_counts = handle["imu/samples/raw_counts"]
        trailer = handle["imu/samples/trailer"]
        packet_index = handle["imu/samples/packet_index"]
        sample_in_packet = handle["imu/samples/sample_in_packet"]
        sample_times = np.asarray(handle["imu/samples/time_monotonic_ns"], dtype=np.int64)
        recording_times = np.asarray(handle["imu/samples/recording_time_ns"], dtype=np.int64)
        aligned_times = (
            np.asarray(handle["imu/samples/aligned_video_time_ns"], dtype=np.int64)
            if "imu/samples/aligned_video_time_ns" in handle
            else recording_times
        )
        values_si = handle["imu/samples/values_si"]

        packet_count = len(receive)
        sample_count = len(raw_counts)
        metrics.update(packet_count=packet_count, sample_count=sample_count)
        callback_drops = int(handle["imu"].attrs.get("callback_drops", -1))
        metrics["callback_drops"] = callback_drops
        metrics["observed_rate_hz"] = float(
            handle["imu"].attrs.get("observed_rate_hz", 0.0)
        )
        expected_rate_hz = float(handle["imu"].attrs.get("expected_rate_hz", 0.0))
        metrics["expected_rate_hz"] = expected_rate_hz
        fit_rms_ns = float(
            handle["imu"].attrs.get("packet_end_fit_residual_rms_ns", 0.0)
        )
        fit_max_ns = int(
            handle["imu"].attrs.get("packet_end_fit_residual_max_abs_ns", 0)
        )
        metrics["packet_fit_residual_rms_ms"] = fit_rms_ns / 1e6
        metrics["packet_fit_residual_max_abs_ms"] = fit_max_ns / 1e6
        metrics["ffmpeg_diagnostic_count"] = int(
            handle["video"].attrs.get("ffmpeg_diagnostic_count", 0)
        )
        if len(receive) > 1:
            metrics["max_packet_gap_ms"] = float(np.diff(receive).max() / 1e6)
        if len(offsets) != packet_count + 1 or offsets[0] != 0:
            issues.append("packet payload offsets have invalid length or origin")
        elif np.any(np.diff(offsets) < 0) or offsets[-1] != len(payload_values):
            issues.append("packet payload offsets are invalid")
        if len(sample_counts) != packet_count:
            issues.append("packet sample_count length mismatch")
        if packet_kinds is not None and len(packet_kinds) != packet_count:
            issues.append("packet kind length mismatch")
        if parse_valid is not None and len(parse_valid) != packet_count:
            issues.append("packet parse_valid length mismatch")
        if fitted_packet_end is not None and len(fitted_packet_end) != packet_count:
            issues.append("fitted packet timestamp length mismatch")
        if packet_fit_residual is not None and len(packet_fit_residual) != packet_count:
            issues.append("packet timestamp residual length mismatch")
        if packet_count == 0:
            issues.append("recording contains no IMU packets")
        if sample_count == 0:
            issues.append("recording contains no parsed IMU samples")
        if callback_drops > 0:
            issues.append(f"BLE callback dropped {callback_drops} notifications")
        parse_error_count = int(handle.attrs.get("parse_error_count", 0))
        metrics["parse_error_count"] = parse_error_count
        if parse_error_count > 0:
            issues.append(f"IMU packet parsing failed {parse_error_count} times")
        if schema_version == CAPTURE_SCHEMA_VERSION:
            assert packet_kinds is not None
            assert parse_valid is not None
            allowed_kinds = {1, 2, 255}
            actual_kinds = {int(value) for value in np.unique(packet_kinds)}
            if not actual_kinds.issubset(allowed_kinds):
                issues.append("packet_kind contains unsupported values")
            imu_mask = packet_kinds == 1
            auxiliary_mask = packet_kinds == 2
            unknown_mask = packet_kinds == 255
            imu_packet_count = int(np.count_nonzero(imu_mask))
            auxiliary_count = int(np.count_nonzero(auxiliary_mask))
            unknown_count = int(np.count_nonzero(unknown_mask))
            metrics.update(
                imu_packet_count=imu_packet_count,
                auxiliary_notification_count=auxiliary_count,
                unknown_notification_count=unknown_count,
            )
            for name, observed in (
                ("imu_packet_count", imu_packet_count),
                ("auxiliary_notification_count", auxiliary_count),
                ("unknown_notification_count", unknown_count),
            ):
                if name not in handle.attrs:
                    issues.append(f"missing notification count attribute {name}")
                elif int(handle.attrs[name]) != observed:
                    issues.append(f"notification count attribute mismatch: {name}")
            if not np.array_equal(parse_valid, imu_mask):
                issues.append("parse_valid must be true only for IMU sample packets")
            if np.any(sample_counts[~imu_mask] != 0):
                issues.append("non-IMU notifications must have zero sample_count")
            if np.any(sample_counts[imu_mask] <= 0):
                issues.append("IMU sample packets must have positive sample_count")
            if parse_error_count != unknown_count:
                issues.append("parse_error_count must equal unknown notification count")
            observed_rate_hz = float(metrics["observed_rate_hz"])
            if expected_rate_hz > 0 and not (
                expected_rate_hz * 0.95
                <= observed_rate_hz
                <= expected_rate_hz * 1.05
            ):
                issues.append(
                    f"IMU observed rate {observed_rate_hz:.3f} Hz is outside "
                    f"the expected ±5% range"
                )
            if len(receive) > 1 and np.max(np.diff(receive)) > 1_500_000_000:
                issues.append("IMU notification gap exceeds 1.5 seconds")
            if fit_rms_ns > 100_000_000:
                issues.append("IMU packet timestamp fit RMS exceeds 0.1 seconds")
            if fit_max_ns > 200_000_000:
                issues.append("IMU packet timestamp maximum residual exceeds 0.2 seconds")
        sample_lengths = {
            sample_count,
            len(trailer),
            len(packet_index),
            len(sample_in_packet),
            len(sample_times),
            len(recording_times),
            len(aligned_times),
            len(values_si),
        }
        if len(sample_lengths) != 1:
            issues.append("IMU sample datasets have inconsistent lengths")
        if sample_count and np.any(np.diff(sample_times) <= 0):
            issues.append("IMU estimated timestamps are not strictly increasing")
        if sample_count and np.any(np.diff(recording_times) <= 0):
            issues.append("IMU recording timestamps are not strictly increasing")
        if sample_count and np.any(np.diff(aligned_times) <= 0):
            issues.append("IMU aligned timestamps are not strictly increasing")
        if raw_counts.shape != (sample_count, 6):
            issues.append("raw_counts must have shape (N, 6)")
        if trailer.shape != (sample_count, 4):
            issues.append("trailer must have shape (N, 4)")
        if values_si.shape != (sample_count, 6):
            issues.append("values_si must have shape (N, 6)")
        calibrated = bool(handle.attrs.get("calibration_verified", False))
        metrics["calibration_verified"] = str(calibrated).lower()
        if calibrated and sample_count and not np.isfinite(values_si[:]).all():
            issues.append("verified calibration produced non-finite values_si")
        if calibrated and schema_version == CAPTURE_SCHEMA_VERSION:
            try:
                order = json.loads(str(handle["imu"].attrs["raw_axis_order_json"]))
                signs = json.loads(str(handle["imu"].attrs["axis_signs_json"]))
                if sorted(order) != [0, 1, 2] or any(item not in (-1, 1) for item in signs):
                    raise ValueError
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                issues.append("invalid verified calibration axis transform")
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
                media_time = (
                    np.asarray(handle["video/frames/media_time_ns"], dtype=np.int64)
                    if "video/frames/media_time_ns" in handle
                    else None
                )
                metrics["video_frame_count"] = len(video_pts)
                pts_deltas = np.diff(video_pts)
                metrics["video_nonpositive_pts_delta_count"] = int(
                    np.count_nonzero(pts_deltas <= 0)
                )
                metrics["video_actual_span_fps"] = (
                    float((len(video_pts) - 1) * 1e9 / (video_pts[-1] - video_pts[0]))
                    if len(video_pts) > 1 and video_pts[-1] > video_pts[0]
                    else 0.0
                )
                if (
                    schema_version == CAPTURE_SCHEMA_VERSION
                    and data_tier == "prod"
                    and float(metrics["video_actual_span_fps"]) < 27.0
                ):
                    issues.append("prod video actual span FPS is below 27")
                if schema_version == CAPTURE_SCHEMA_VERSION and data_tier == "prod":
                    try:
                        requested_controls = json.loads(
                            str(handle["video"].attrs["camera_controls_requested_json"])
                        )
                        effective_controls = json.loads(
                            str(handle["video"].attrs["camera_controls_effective_json"])
                        )
                        control_errors = json.loads(
                            str(handle["video"].attrs["camera_control_errors_json"])
                        )
                        if (
                            not requested_controls
                            or requested_controls != effective_controls
                            or control_errors
                        ):
                            issues.append("prod video fixed camera controls are not verified")
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        issues.append("prod video fixed camera controls are missing or invalid")
                metrics["video_max_frame_gap_ms"] = (
                    float(np.max(pts_deltas) / 1e6) if len(pts_deltas) else 0.0
                )
                if not len(video_pts):
                    issues.append("recording contains no video frames")
                if len(video_pts) != len(video_recording):
                    issues.append("video timestamp dataset length mismatch")
                if schema_version == CAPTURE_SCHEMA_VERSION:
                    if media_time is None:
                        issues.append("current schema is missing video media_time_ns")
                    elif len(media_time) != len(video_pts):
                        issues.append("video media timestamp dataset length mismatch")
                    elif len(media_time) and media_time[0] != 0:
                        issues.append("video media timeline must start at zero")
                    elif len(media_time) > 1 and np.any(np.diff(media_time) <= 0):
                        issues.append("video media timestamps are not strictly increasing")
                if len(video_pts) > 1 and np.any(pts_deltas <= 0):
                    issues.append("video PTS are not strictly increasing")
                if (
                    schema_version == CAPTURE_SCHEMA_VERSION
                    and len(pts_deltas)
                    and np.max(pts_deltas) > 200_000_000
                ):
                    issues.append("video frame gap exceeds 0.2 seconds")
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
    from imu_data_collector.models import (
        ActivitySegment,
        AnnotationEvent,
        ExclusionInterval,
    )

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
    exclusions: list[ExclusionInterval] = []
    if "exclusions" in group:
        exclusions_group = group["exclusions"]
        exclusion_ids = _text_list(exclusions_group["exclusion_id"])
        reasons = _text_list(exclusions_group["reason"])
        exclusion_annotators = _text_list(exclusions_group["annotator_id"])
        exclusion_notes = _text_list(exclusions_group["notes"])
        exclusions = [
            ExclusionInterval(
                exclusion_id=exclusion_ids[index],
                start_ns=int(exclusions_group["start_ns"][index]),
                end_ns=int(exclusions_group["end_ns"][index]),
                reason=reasons[index],
                annotator_id=exclusion_annotators[index],
                notes=exclusion_notes[index],
            )
            for index in range(len(exclusion_ids))
        ]
    return AnnotationDocument(
        taxonomy_id=str(group.attrs["taxonomy_id"]),
        taxonomy_version=str(group.attrs["taxonomy_version"]),
        revision=int(group.attrs.get("revision", 1)),
        finalized=bool(group.attrs.get("finalized", False)),
        segments=segments,
        events=events,
        exclusions=exclusions,
    )
