from pathlib import Path

import h5py
import numpy as np
import pytest

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
    assert validate_capture_h5(path, taxonomy(), require_sync=True).ready
