import json
import os
from pathlib import Path

import h5py
import numpy as np
import pytest

from imu_data_collector.characterization import (
    analyze_characterization,
    compare_accel_pose_pair,
    correct_characterization_stage,
    recover_interrupted_characterization,
    write_characterization_report,
)
from imu_data_collector.config import ImuSettings
from imu_data_collector.cw12eu import pack_test_frame
from imu_data_collector.hdf5_store import (
    CaptureH5Writer,
    read_annotations,
    replace_annotations_atomic,
    replace_sync_atomic,
)
from imu_data_collector.models import (
    ActivitySegment,
    AnnotationDocument,
    AnnotationEvent,
    BinaryLabel,
    DataTier,
    EventKind,
    ExclusionInterval,
    ExclusionReason,
    RecordingStartRequest,
    SyncAnchor,
    SyncDocument,
)
from imu_data_collector.validation import validate_capture_h5


def taxonomy() -> dict:
    return {
        "taxonomy_id": "fall_binary_v1",
        "version": "1.0.0",
        "fall": [{"code": "forward_fall", "display_name_zh": "向前跌倒"}],
        "non_fall": [{"code": "walking", "display_name_zh": "行走"}],
    }


def test_capture_h5_file_descriptor_is_not_inheritable(tmp_path: Path) -> None:
    writer = CaptureH5Writer(
        tmp_path / "descriptor.h5",
        RecordingStartRequest(
            collection_id="descriptor_check",
            participant_id="xfan0282",
        ),
        "descriptor-recording",
        1_000_000_000,
        ImuSettings(),
        taxonomy(),
    )
    try:
        descriptor = writer.handle.id.get_vfd_handle()
        if not isinstance(descriptor, int):
            pytest.skip("当前 HDF5 VFD 不暴露普通 POSIX 文件描述符")
        assert not os.get_inheritable(descriptor)
    finally:
        writer.abort_close()


def formal_anchor(
    role: str,
    imu_time_ns: int,
    video_time_ns: int,
    *,
    source_video_frame: int = 0,
    source_imu_sample: int = 0,
    video_interval_start_ns: int | None = None,
    imu_interval_start_ns: int | None = None,
) -> SyncAnchor:
    return SyncAnchor(
        role=role,
        imu_time_ns=imu_time_ns,
        video_time_ns=video_time_ns,
        source_video_frame=source_video_frame,
        source_imu_sample=source_imu_sample,
        video_interval_start_ns=(
            max(0, video_time_ns - 33_333_333)
            if video_interval_start_ns is None
            else video_interval_start_ns
        ),
        imu_interval_start_ns=(
            max(0, imu_time_ns - 40_000_000)
            if imu_interval_start_ns is None
            else imu_interval_start_ns
        ),
        reviewer_id="xfan0282",
    )


def build_capture(
    tmp_path: Path,
    *,
    data_tier: DataTier = DataTier.TEST,
    video_interval_ns: int = 33_333_333,
    camera_controls_verified: bool = False,
) -> Path:
    start_ns = 1_000_000_000
    path = tmp_path / "capture.h5"
    video = tmp_path / "capture.mkv"
    video.write_bytes("用于测试的伪 MKV 数据".encode())
    writer = CaptureH5Writer(
        path,
        RecordingStartRequest(
            collection_id="pilot",
            participant_id="xfan0282",
            data_tier=data_tier,
        ),
        "recording-1",
        start_ns,
        ImuSettings(),
        taxonomy(),
    )
    for index, receive_time in enumerate((1_100_000_000, 1_180_000_000, 1_260_000_000)):
        payload = b"".join(
            [
                pack_test_frame(
                    (index, index + 1, index + 2, index + 3, index + 4, index + 5),
                    bytes([index, 0, 0, 1]),
                ),
                pack_test_frame(
                    (index + 10,) * 6,
                    bytes([index, 0, 0, 2]),
                ),
            ]
        )
        assert writer.append_notification(payload, receive_time) == 2
    rate, _ = writer.reconstruct_times()
    assert rate == pytest.approx(25.0, rel=0.02)
    writer.write_video_frames(
        pts_monotonic_ns=start_ns
        + 50_000_000
        + np.arange(4, dtype=np.int64) * video_interval_ns,
        duration_ns=np.full(4, video_interval_ns, dtype=np.int64),
        key_frame=np.asarray([True, False, False, False]),
        video_path=video,
        codec="h264",
        width=1920,
        height=1080,
        requested_fps=30.0,
        ffmpeg_diagnostics=["[mjpeg] overread 8"],
        camera_controls_requested=(
            {"auto_exposure": 1, "exposure_time_absolute": 200}
            if camera_controls_verified
            else None
        ),
        camera_controls_effective=(
            {"auto_exposure": 1, "exposure_time_absolute": 200}
            if camera_controls_verified
            else None
        ),
        camera_control_errors=[] if camera_controls_verified else None,
    )
    writer.write_sync([])
    writer.finish()
    return path


def finalized_annotation() -> AnnotationDocument:
    return AnnotationDocument(
        taxonomy_id="fall_binary_v1",
        taxonomy_version="1.0.0",
        revision=2,
        finalized=True,
        segments=[
            ActivitySegment(
                segment_id="fall-1",
                start_ns=80_000_000,
                end_ns=220_000_000,
                binary_label=BinaryLabel.FALL,
                activity_code="forward_fall",
                annotator_id="xfan0282",
            )
        ],
        events=[
            AnnotationEvent(
                segment_id="fall-1",
                kind=EventKind.ONSET,
                time_ns=80_000_000,
                annotator_id="xfan0282",
            ),
            AnnotationEvent(
                segment_id="fall-1",
                kind=EventKind.IMPACT,
                time_ns=180_000_000,
                annotator_id="xfan0282",
            ),
        ],
        exclusions=[
            ExclusionInterval(
                exclusion_id="setup",
                start_ns=0,
                end_ns=80_000_000,
                reason=ExclusionReason.SETUP,
                annotator_id="xfan0282",
            ),
            ExclusionInterval(
                exclusion_id="end_guard",
                start_ns=220_000_000,
                end_ns=260_000_000,
                reason=ExclusionReason.SYNC_TAP,
                annotator_id="xfan0282",
            ),
        ],
    )


def verify_host_sync(path: Path) -> None:
    model = replace_sync_atomic(
        path,
        SyncDocument(
            anchors=[
                formal_anchor("start_tap", 80_000_000, 90_000_000),
                formal_anchor("end_tap", 200_000_000, 210_000_000),
            ]
        ),
        taxonomy(),
    )
    assert model.quality == "verified"


def test_capture_round_trip_and_atomic_annotation_update(tmp_path: Path) -> None:
    path = build_capture(tmp_path)

    initial = validate_capture_h5(path, taxonomy())
    assert initial.ready, initial.issues
    assert initial.metrics["video_nonpositive_pts_delta_count"] == 0
    assert initial.metrics["video_actual_span_fps"] == pytest.approx(30.0, rel=0.02)
    with h5py.File(path, "r") as handle:
        assert np.isnan(handle["imu/samples/values_si"][:]).all()
        assert handle["sync"].attrs["anchor_clock_domain"] == "recording_relative_ns"
        assert str(handle.attrs["data_tier"]) == "test"
        assert not bool(handle.attrs["training_eligible"])
        assert int(handle["video"].attrs["ffmpeg_diagnostic_count"]) == 1
        assert handle["video/frames/media_time_ns"][:].tolist() == [
            0,
            33_333_333,
            66_666_666,
            99_999_999,
        ]
        assert handle["video"].attrs["media_timeline_origin"] == "first_encoded_frame"
        assert json.loads(handle["video"].attrs["ffmpeg_diagnostics_json"]) == [
            "[mjpeg] overread 8"
        ]

    document = finalized_annotation()
    verify_host_sync(path)
    replace_annotations_atomic(path, document, taxonomy())

    stored = read_annotations(path)
    assert stored == document
    assert validate_capture_h5(path, taxonomy()).ready


@pytest.mark.parametrize(
    ("maximum_ns", "ready", "warning_count", "blocking_issue"),
    [
        (200_000_000, True, 0, None),
        (200_000_001, True, 1, None),
        (500_000_000, True, 1, None),
        (
            500_000_001,
            False,
            0,
            "IMU packet timestamp maximum residual exceeds 0.5 seconds",
        ),
    ],
)
def test_packet_timestamp_maximum_residual_uses_tiered_gate(
    tmp_path: Path,
    maximum_ns: int,
    ready: bool,
    warning_count: int,
    blocking_issue: str | None,
) -> None:
    path = build_capture(tmp_path)
    with h5py.File(path, "r+") as handle:
        handle["imu"].attrs["packet_end_fit_residual_rms_ns"] = 10_000_000.0
        handle["imu"].attrs["packet_end_fit_residual_max_abs_ns"] = maximum_ns

    report = validate_capture_h5(path, taxonomy())

    assert report.ready is ready
    assert len(report.warnings) == warning_count
    if warning_count:
        assert f"{maximum_ns / 1e6:.3f} ms" in report.warnings[0]
    if blocking_issue is not None:
        assert blocking_issue in report.issues


def test_packet_timestamp_fit_rms_remains_blocking(tmp_path: Path) -> None:
    path = build_capture(tmp_path)
    with h5py.File(path, "r+") as handle:
        handle["imu"].attrs["packet_end_fit_residual_rms_ns"] = 100_000_001.0
        handle["imu"].attrs["packet_end_fit_residual_max_abs_ns"] = 250_000_000

    report = validate_capture_h5(path, taxonomy())

    assert not report.ready
    assert "IMU packet timestamp fit RMS exceeds 0.1 seconds" in report.issues
    assert len(report.warnings) == 1


def test_auxiliary_notifications_are_preserved_without_blocking_validation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "auxiliary.h5"
    writer = CaptureH5Writer(
        path,
        RecordingStartRequest(
            collection_id="auxiliary_check",
            participant_id="xfan0282",
        ),
        "auxiliary-recording",
        1_000_000_000,
        ImuSettings(),
        taxonomy(),
        video_status="not_requested",
    )
    frame = pack_test_frame((1, 2, 4096, 4, 5, 6), b"\x00\x00\x00\x01")
    writer.append_notification(frame, 1_040_000_000)
    writer.append_notification(
        bytes.fromhex("aa1a0200f0f0f0f00146"), 1_050_000_000
    )
    writer.append_notification(frame, 1_080_000_000)
    writer.reconstruct_times()
    writer.write_sync([])
    writer.finish()

    report = validate_capture_h5(path, taxonomy(), require_video=False)
    assert report.ready, report.issues
    assert report.metrics["imu_packet_count"] == 2
    assert report.metrics["auxiliary_notification_count"] == 1
    assert report.metrics["unknown_notification_count"] == 0
    with h5py.File(path, "r") as handle:
        assert str(handle.attrs["capture_schema_version"]) == "1.5.0"
        assert handle["imu/packets/packet_kind"][:].tolist() == [1, 2, 1]
        assert handle["imu/packets/parse_valid"][:].tolist() == [True, False, True]
        assert handle["imu/packets/sample_count"][:].tolist() == [1, 0, 1]
        offsets = handle["imu/packets/payload_offsets"][:]
        payload = bytes(handle["imu/packets/payload_values"][offsets[1] : offsets[2]])
        assert payload == bytes.fromhex("aa1a0200f0f0f0f00146")


def test_unknown_notification_still_blocks_production_readiness(tmp_path: Path) -> None:
    path = tmp_path / "unknown.h5"
    writer = CaptureH5Writer(
        path,
        RecordingStartRequest(
            collection_id="unknown_check",
            participant_id="xfan0282",
        ),
        "unknown-recording",
        1_000_000_000,
        ImuSettings(),
        taxonomy(),
        video_status="not_requested",
    )
    frame = pack_test_frame((1, 2, 4096, 4, 5, 6), b"\x00\x00\x00\x01")
    writer.append_notification(frame, 1_040_000_000)
    writer.append_notification(b"\x42" * 10, 1_050_000_000)
    writer.append_notification(frame, 1_080_000_000)
    writer.reconstruct_times()
    writer.write_sync([])
    writer.finish()

    report = validate_capture_h5(path, taxonomy(), require_video=False)
    assert not report.ready
    assert report.metrics["unknown_notification_count"] == 1
    assert report.metrics["parse_error_count"] == 1
    assert any("parsing failed 1 times" in issue for issue in report.issues)


def test_prod_capture_requires_verified_fixed_camera_controls(tmp_path: Path) -> None:
    path = build_capture(tmp_path, data_tier=DataTier.PROD)

    report = validate_capture_h5(path, taxonomy())

    assert not report.ready
    assert "prod video fixed camera controls are not verified" in report.issues


def test_prod_capture_rejects_low_actual_span_fps(tmp_path: Path) -> None:
    path = build_capture(
        tmp_path,
        data_tier=DataTier.PROD,
        video_interval_ns=66_666_667,
        camera_controls_verified=True,
    )

    report = validate_capture_h5(path, taxonomy())

    assert not report.ready
    assert "prod video actual span FPS is below 27" in report.issues


def test_verified_calibration_is_frozen_and_values_si_use_target_axes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "calibrated.h5"
    settings = ImuSettings(
        calibration_profile_id="device-v1",
        calibration_verified=True,
        accel_counts_per_g=4096.0,
        gyro_counts_per_dps=32.8,
        accel_bias_counts=(30.0, 48.0, -40.0),
        gyro_bias_counts=(-17.0, 0.0, 10.0),
        axis_signs=(1, -1, 1),
        calibration_method="fixture",
        calibration_evidence_sha256="a" * 64,
    )
    writer = CaptureH5Writer(
        path,
        RecordingStartRequest(collection_id="pilot", participant_id="xfan0282"),
        "calibrated",
        1_000_000_000,
        settings,
        taxonomy(),
    )
    writer.append_notification(
        pack_test_frame((30, -4048, 4056, -17, 328, 10), b"\x00\x00\x00\x00"),
        1_040_000_000,
    )

    assert bool(writer.handle.attrs["calibration_verified"])
    assert writer.handle["imu"].attrs["calibration_profile_id"] == "device-v1"
    assert writer.handle["imu"].attrs["calibration_evidence_sha256"] == "a" * 64
    assert writer.handle["imu"].attrs["byte_order"] == "big_endian_verified_current_device"
    assert (
        writer.handle["imu"].attrs["frame_layout_status"]
        == "six_axis_int16_verified_trailer_unknown"
    )
    values = writer.handle["imu/samples/values_si"][0]
    assert values[1] == pytest.approx(9.80665)
    assert values[2] == pytest.approx(9.80665)
    assert values[4] == pytest.approx(-np.deg2rad(10.0))
    writer.abort_close()


def test_test_tier_is_default_and_can_never_be_marked_training_eligible(
    tmp_path: Path,
) -> None:
    request = RecordingStartRequest(collection_id="pilot", participant_id="xfan0282")
    assert request.data_tier == DataTier.TEST

    with pytest.raises(ValueError, match="test 数据永久禁止"):
        CaptureH5Writer(
            tmp_path / "forbidden.h5",
            request,
            "forbidden",
            1_000_000_000,
            ImuSettings(),
            taxonomy(),
            training_eligible=True,
        )
    assert not (tmp_path / "forbidden.h5").exists()


def test_formal_recording_start_is_frozen_before_first_packet(tmp_path: Path) -> None:
    request = RecordingStartRequest(
        collection_id="pilot", participant_id="xfan0282", data_tier="test"
    )
    path = tmp_path / "start.partial.h5"
    writer = CaptureH5Writer(
        path,
        request,
        "start",
        1_000_000_000,
        ImuSettings(),
        taxonomy(),
    )

    writer.set_recording_start(2_000_000_000, "2026-08-25T15:00:00+00:00")

    assert writer.recording_start_monotonic_ns == 2_000_000_000
    assert int(writer.handle.attrs["recording_start_monotonic_ns"]) == 2_000_000_000
    assert writer.handle.attrs["started_at_utc"] == "2026-08-25T15:00:00+00:00"
    writer.abort_close()


def test_old_schema_without_data_tier_defaults_to_safe_test(
    tmp_path: Path,
) -> None:
    path = build_capture(tmp_path)
    with h5py.File(path, "r+") as handle:
        handle.attrs["capture_schema_version"] = "1.0.0"
        del handle.attrs["data_tier"]

    report = validate_capture_h5(path, taxonomy())
    assert report.ready, report.issues
    assert report.metrics["data_tier"] == "test"
    assert report.metrics["training_eligible"] == "false"


def test_invalid_annotation_does_not_replace_existing_revision(tmp_path: Path) -> None:
    path = build_capture(tmp_path)
    verify_host_sync(path)
    replace_annotations_atomic(path, finalized_annotation(), taxonomy())
    invalid = finalized_annotation().model_copy(
        update={
            "revision": 3,
            "segments": [
                finalized_annotation().segments[0].model_copy(
                    update={"activity_code": "walking"}
                )
            ],
        }
    )

    with pytest.raises(ValueError, match="not a fall taxonomy entry"):
        replace_annotations_atomic(path, invalid, taxonomy())

    assert read_annotations(path).revision == 2
    assert not path.with_suffix(".annotating.h5").exists()


def test_sync_update_uses_recording_relative_anchor_times(tmp_path: Path) -> None:
    path = build_capture(tmp_path)
    model = replace_sync_atomic(
        path,
        SyncDocument(
            anchors=[
                formal_anchor("start_tap", 80_000_000, 90_000_000),
                formal_anchor("end_tap", 200_000_000, 210_000_000),
            ]
        ),
        taxonomy(),
    )

    assert model.quality == "verified"
    assert model.estimated_offset_ns == 10_000_000
    assert model.applied_offset_ns == 0
    with h5py.File(path, "r") as handle:
        times = np.asarray(handle["imu/samples/recording_time_ns"])
        aligned = np.asarray(handle["imu/samples/aligned_video_time_ns"])
        assert times[0] < 200_000_000
        assert np.array_equal(times, aligned)
        assert str(handle["sync"].attrs["quality"]) == "verified"
        assert handle["sync/roles"].asstr()[:].tolist() == ["start_tap", "end_tap"]
    assert validate_capture_h5(path, taxonomy(), require_sync=True).ready


def test_conditional_fixed_offset_requires_confirmation_and_preserves_raw_time(
    tmp_path: Path,
) -> None:
    path = build_capture(tmp_path)
    anchors = [
        formal_anchor(
            "start_tap",
            20_000_000,
            170_000_000,
            video_interval_start_ns=136_666_667,
            imu_interval_start_ns=0,
        ),
        formal_anchor(
            "end_tap",
            80_000_000,
            250_000_000,
            video_interval_start_ns=216_666_667,
            imu_interval_start_ns=40_000_000,
        ),
    ]
    with h5py.File(path, "r") as handle:
        raw_time_before = np.asarray(handle["imu/samples/time_monotonic_ns"]).copy()

    pending = replace_sync_atomic(path, SyncDocument(anchors=anchors), taxonomy())
    assert pending.recommendation == "apply_fixed_offset"
    assert pending.quality == "awaiting_confirmation"
    assert pending.estimated_offset_ns == 160_000_000

    applied = replace_sync_atomic(
        path,
        SyncDocument(anchors=anchors, apply_fixed_offset=True),
        taxonomy(),
    )
    assert applied.quality == "verified"
    assert applied.applied_offset_ns == 160_000_000
    with h5py.File(path, "r") as handle:
        assert np.array_equal(
            raw_time_before, np.asarray(handle["imu/samples/time_monotonic_ns"])
        )
        recording = np.asarray(handle["imu/samples/recording_time_ns"])
        aligned = np.asarray(handle["imu/samples/aligned_video_time_ns"])
        assert np.array_equal(aligned, recording + 160_000_000)


def test_characterization_h5_and_report_are_never_training_eligible(
    tmp_path: Path,
) -> None:
    path = tmp_path / "characterization.h5"
    writer = CaptureH5Writer(
        path,
        RecordingStartRequest(
            collection_id="_diagnostics",
            participant_id="xfan0282",
            protocol_id="imu_characterization_v1",
        ),
        "characterization-1",
        1_000_000_000,
        ImuSettings(),
        taxonomy(),
        recording_kind="imu_characterization",
        training_eligible=False,
        video_status="not_requested",
    )
    for index, receive_time in enumerate(
        (2_000_000_000, 3_000_000_000, 4_000_000_000)
    ):
        payload = b"".join(
            pack_test_frame(
                (index + axis for axis in range(6)), bytes([index, 1, 2, 3])
            )
            for _ in range(25)
        )
        writer.append_notification(payload, receive_time)
    writer.append_experiment_stage(
        "interface_opposite_face_up", 0, 4_000_000_000, "candidate"
    )
    writer.append_experiment_stage(
        "interface_face_up", 0, 4_000_000_000, "candidate"
    )
    writer.reconstruct_times()
    writer.write_sync([])
    writer.finish()

    assert validate_capture_h5(path, taxonomy(), require_video=False).ready
    report = analyze_characterization(path)
    assert report["training_eligible"] is False
    assert report["packet_metrics"]["payload_length_histogram"] == {"400": 3}
    assert set(report["stage_metrics"]) == {
        "interface_opposite_face_up",
        "interface_face_up",
    }
    report_path = write_characterization_report(path)
    assert report_path.is_file()
    with h5py.File(path, "r") as handle:
        assert not bool(handle.attrs["training_eligible"])
        assert str(handle.attrs["recording_kind"]) == "imu_characterization"


def test_opposite_pose_pair_produces_conservative_accel_candidate(
    tmp_path: Path,
) -> None:
    def write_pose(path: Path, values: tuple[int, ...], stage: str) -> None:
        writer = CaptureH5Writer(
            path,
            RecordingStartRequest(
                collection_id="_diagnostics",
                participant_id="xfan0282",
                protocol_id="imu_characterization_v1",
            ),
            path.stem,
            1_000_000_000,
            ImuSettings(),
            taxonomy(),
            recording_kind="imu_characterization",
            training_eligible=False,
            video_status="not_requested",
        )
        payload = b"".join(
            pack_test_frame(values, bytes([0, 1, 2, index])) for index in range(25)
        )
        for receive_time in (2_000_000_000, 3_000_000_000, 4_000_000_000):
            writer.append_notification(payload, receive_time)
        writer.append_experiment_stage(stage, 0, 4_000_000_000, "candidate")
        writer.reconstruct_times()
        writer.write_sync([])
        writer.finish()

    positive = tmp_path / "positive.h5"
    negative = tmp_path / "negative.h5"
    write_pose(positive, (100, -50, 4050, 0, 0, 0), "button_face_up")
    write_pose(negative, (105, -45, -4140, 0, 0, 0), "button_face_down")
    report = compare_accel_pose_pair(
        positive, negative, "button_face_up", "button_face_down", "z"
    )

    assert report["dominant_raw_column_candidate"] == "az"
    assert report["counts_per_g_candidate"] == pytest.approx(4095.0)
    assert report["bias_counts_candidate"] == pytest.approx(-45.0)
    assert report["status"] == "strong_candidate"
    assert report["calibration_verified"] is False


def test_interrupted_characterization_recovery_preserves_partial(
    tmp_path: Path,
) -> None:
    partial = tmp_path / "interrupted.partial.h5"
    settings = ImuSettings()
    writer = CaptureH5Writer(
        partial,
        RecordingStartRequest(
            collection_id="_diagnostics",
            participant_id="xfan0282",
            protocol_id="imu_characterization_v1",
        ),
        "interrupted",
        1_000_000_000,
        settings,
        taxonomy(),
        recording_kind="imu_characterization",
        training_eligible=False,
        video_status="not_requested",
    )
    payload = b"".join(
        pack_test_frame((1, 2, 3, 4, 5, 6), bytes([0, 1, 2, index]))
        for index in range(25)
    )
    for receive_time in (2_000_000_000, 3_000_000_000, 4_000_000_000):
        writer.append_notification(payload, receive_time)
    writer.append_experiment_stage(
        "pendant_end_up_exploratory", 0, 4_000_000_000, "exploratory"
    )
    writer.abort_close()

    result = recover_interrupted_characterization(partial, settings, taxonomy())
    recovered = Path(result["recovered_h5"])
    assert partial.is_file()
    assert recovered.is_file()
    assert result["source_partial_preserved"] is True
    assert validate_capture_h5(recovered, taxonomy(), require_video=False).ready
    with h5py.File(recovered, "r") as handle:
        assert bool(handle.attrs["interrupted_capture"])
        assert int(handle["imu"].attrs["callback_drops"]) == -1

    corrected = correct_characterization_stage(
        recovered,
        "pendant_end_up_exploratory",
        "button_face_up",
        "candidate",
        "测试中的用户元数据更正",
    )
    assert corrected["original_file_sha256"] != corrected["corrected_file_sha256"]
    with h5py.File(recovered, "r") as handle:
        assert handle["experiment/stages/stage_code"].asstr()[0] == "button_face_up"
        corrections = json.loads(handle.attrs["metadata_corrections"])
        assert corrections[0]["old_stage_code"] == "pendant_end_up_exploratory"
