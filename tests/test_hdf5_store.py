import json
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
    EventKind,
    RecordingStartRequest,
    SyncAnchor,
)
from imu_data_collector.validation import validate_capture_h5


def taxonomy() -> dict:
    return {
        "taxonomy_id": "fall_binary_v1",
        "version": "1.0.0",
        "fall": [{"code": "forward_fall", "display_name_zh": "向前跌倒"}],
        "non_fall": [{"code": "walking", "display_name_zh": "行走"}],
    }


def build_capture(tmp_path: Path) -> Path:
    start_ns = 1_000_000_000
    path = tmp_path / "capture.h5"
    video = tmp_path / "capture.mkv"
    video.write_bytes("用于测试的伪 MKV 数据".encode())
    writer = CaptureH5Writer(
        path,
        RecordingStartRequest(collection_id="pilot", participant_id="xfan0282"),
        "recording-1",
        start_ns,
        ImuSettings(),
        taxonomy(),
    )
    for index, receive_time in enumerate((1_100_000_000, 1_166_666_666, 1_233_333_332)):
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
    assert rate == pytest.approx(30.0, rel=0.02)
    writer.write_video_frames(
        pts_monotonic_ns=start_ns
        + np.asarray([50_000_000, 83_333_333, 116_666_666, 149_999_999]),
        duration_ns=np.full(4, 33_333_333, dtype=np.int64),
        key_frame=np.asarray([True, False, False, False]),
        video_path=video,
        codec="h264",
        width=1920,
        height=1080,
        requested_fps=30.0,
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
                time_ns=100_000_000,
                annotator_id="xfan0282",
            ),
            AnnotationEvent(
                segment_id="fall-1",
                kind=EventKind.IMPACT,
                time_ns=180_000_000,
                annotator_id="xfan0282",
            ),
        ],
    )


def test_capture_round_trip_and_atomic_annotation_update(tmp_path: Path) -> None:
    path = build_capture(tmp_path)

    initial = validate_capture_h5(path, taxonomy())
    assert initial.ready, initial.issues
    with h5py.File(path, "r") as handle:
        assert np.isnan(handle["imu/samples/values_si"][:]).all()
        assert handle["sync"].attrs["anchor_clock_domain"] == "recording_relative_ns"

    document = finalized_annotation()
    replace_annotations_atomic(path, document, taxonomy())

    stored = read_annotations(path)
    assert stored == document
    assert validate_capture_h5(path, taxonomy()).ready


def test_invalid_annotation_does_not_replace_existing_revision(tmp_path: Path) -> None:
    path = build_capture(tmp_path)
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
        [
            SyncAnchor(imu_time_ns=80_000_000, video_time_ns=90_000_000),
            SyncAnchor(imu_time_ns=200_000_000, video_time_ns=210_000_000),
        ],
        taxonomy(),
    )

    assert model.quality == "verified"
    with h5py.File(path, "r") as handle:
        times = np.asarray(handle["imu/samples/recording_time_ns"])
        assert times[0] < 200_000_000
        assert str(handle["sync"].attrs["quality"]) == "verified"
        assert handle["sync/labels"].asstr()[:].tolist() == ["tap", "tap"]
    assert validate_capture_h5(path, taxonomy(), require_sync=True).ready


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
