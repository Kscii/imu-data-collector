from __future__ import annotations

import json
import tarfile
from pathlib import Path

import h5py
import numpy as np
import pytest

from imu_data_collector.annotation_service import AnnotationService
from imu_data_collector.artifacts import (
    _annotation_rows,
    create_capture_package,
    create_training_snapshot_archive,
    export_aligned,
    merge_training_exports,
)
from imu_data_collector.config import ImuSettings
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    ActivitySegment,
    AnnotationDocument,
    AnnotationEvent,
    BinaryLabel,
    EventKind,
    ReviewDocument,
    ReviewWorkflow,
    ReviewWorkflowState,
    SourceArtifact,
    SyncAnchor,
    SyncDocument,
)
from imu_data_collector.review import (
    ReviewConflictError,
    load_review,
    mutate_review,
)
from imu_data_collector.validation import validate_annotations


def taxonomy() -> dict:
    return {
        "taxonomy_id": "fall_binary_v1",
        "version": "1.0.0",
        "fall": [{"code": "forward_fall"}],
        "non_fall": [{"code": "walking"}],
    }


def write_source_pair(tmp_path: Path, *, data_tier: str = "prod") -> tuple[Path, Path]:
    h5_path = tmp_path / "recording-1.h5"
    mkv_path = tmp_path / "recording-1.mkv"
    mkv_path.write_bytes(b"synthetic-mkv")
    with h5py.File(h5_path, "w") as handle:
        handle.attrs.update(
            {
                "recording_id": "recording-1",
                "collection_id": "pilot",
                "participant_id": "xfan0282",
                "body_location": "chest",
                "data_tier": data_tier,
                "duration_ns": 2_000_000_000,
                "recording_start_monotonic_ns": 10_000_000_000,
            }
        )
        imu = handle.create_group("imu")
        imu.attrs["observed_rate_hz"] = 25.0
        samples = imu.create_group("samples")
        times = np.arange(50, dtype=np.int64) * 40_000_000
        samples.create_dataset("recording_time_ns", data=times)
        samples.create_dataset("time_monotonic_ns", data=10_000_000_000 + times)
        samples.create_dataset(
            "raw_counts",
            data=np.column_stack(
                [
                    np.arange(50),
                    np.arange(50) + 1,
                    np.full(50, 4090),
                    np.arange(50) + 3,
                    np.arange(50) + 4,
                    np.arange(50) + 5,
                ]
            ).astype(np.int16),
        )
        frames = handle.create_group("video").create_group("frames")
        frames.create_dataset(
            "recording_time_ns", data=np.arange(60, dtype=np.int64) * 33_333_333
        )
    return h5_path, mkv_path


def artifact(path: Path, role: str) -> SourceArtifact:
    return SourceArtifact(
        role=role,
        filename=path.name,
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
    )


def completed_review(h5_path: Path, mkv_path: Path) -> ReviewDocument:
    return ReviewDocument(
        recording_id="recording-1",
        sources=[artifact(h5_path, "capture_h5"), artifact(mkv_path, "video_mkv")],
        sync=SyncDocument(
            anchors=[
                SyncAnchor(
                    imu_time_ns=200_000_000,
                    video_time_ns=200_000_000,
                    label="start",
                    role="start_tap",
                    source_video_frame=6,
                    source_imu_sample=5,
                    video_interval_start_ns=166_666_665,
                    imu_interval_start_ns=160_000_000,
                    reviewer_id="xfan0282",
                ),
                SyncAnchor(
                    imu_time_ns=1_600_000_000,
                    video_time_ns=1_600_000_000,
                    label="end",
                    role="end_tap",
                    source_video_frame=48,
                    source_imu_sample=40,
                    video_interval_start_ns=1_566_666_651,
                    imu_interval_start_ns=1_560_000_000,
                    reviewer_id="xfan0282",
                ),
            ]
        ),
        annotations=AnnotationDocument(
            taxonomy_id="fall_binary_v1",
            taxonomy_version="1.0.0",
            finalized=True,
            segments=[
                ActivitySegment(
                    segment_id="seg_001",
                    start_ns=0,
                    end_ns=2_000_000_000,
                    binary_label=BinaryLabel.NON_FALL,
                    activity_code="walking",
                    annotator_id="xfan0282",
                )
            ],
        ),
        workflow=ReviewWorkflow(
            state=ReviewWorkflowState.COMPLETED,
            annotator_id="xfan0282",
            last_editor_id="xfan0282",
        ),
    )


def test_review_sidecar_is_external_and_optimistically_locked(tmp_path: Path) -> None:
    h5_path, mkv_path = write_source_pair(tmp_path)
    original_h5_hash = sha256_file(h5_path)

    initial = load_review(h5_path, mkv_path, taxonomy())
    updated = mutate_review(
        h5_path,
        mkv_path,
        taxonomy(),
        initial.revision,
        lambda current: current.model_copy(
            update={
                "workflow": current.workflow.model_copy(
                    update={
                        "state": ReviewWorkflowState.IN_PROGRESS,
                        "annotator_id": "xfan0282",
                    }
                )
            }
        ),
    )

    assert updated.revision == 1
    assert (tmp_path / "review.json").is_file()
    assert sha256_file(h5_path) == original_h5_hash
    with pytest.raises(ReviewConflictError):
        mutate_review(
            h5_path,
            mkv_path,
            taxonomy(),
            initial.revision,
            lambda current: current,
        )


def test_capture_package_excludes_mutable_review_sidecar(tmp_path: Path) -> None:
    h5_path, mkv_path = write_source_pair(tmp_path)
    review = completed_review(h5_path, mkv_path)
    output = create_capture_package(
        review, h5_path, mkv_path, tmp_path / "recording-1.capture.tar"
    )

    with tarfile.open(output, "r:") as archive:
        assert archive.getnames() == ["manifest.json", "capture.h5", "video.mkv"]


def test_aligned25_export_uses_three_root_datasets_and_exact_grid(tmp_path: Path) -> None:
    h5_path, mkv_path = write_source_pair(tmp_path)
    output = export_aligned(
        completed_review(h5_path, mkv_path),
        h5_path,
        mkv_path,
        tmp_path / "aligned25.h5",
        ImuSettings(accel_counts_per_g=4090.0, gyro_counts_per_dps=16.4),
        taxonomy(),
    )

    with h5py.File(output, "r") as handle:
        assert set(handle.keys()) == {"samples", "sequences", "annotations"}
        assert handle.attrs["imu_schema_version"] == "3.1.0"
        assert handle.attrs["sampling_rate_hz"] == 25.0
        assert handle.attrs["evaluation_role"] == "training_only"
        assert handle["samples"].shape == (50, 6)
        assert handle["samples"].dtype == np.dtype("float32")
        assert handle["sequences"][0]["supervision_kind"].decode() == "temporal"
        annotation = handle["annotations"][0]
        assert annotation["kind"].decode() == "activity"
        assert (annotation["start_sample"], annotation["stop_sample"]) == (0, 50)


def test_fall_onset_is_derived_from_segment_start_and_impact_is_preserved(
    tmp_path: Path,
) -> None:
    h5_path, _mkv_path = write_source_pair(tmp_path)
    document = AnnotationDocument(
        taxonomy_id="fall_binary_v1",
        taxonomy_version="1.0.0",
        segments=[
            ActivitySegment(
                segment_id="seg_001",
                start_ns=400_000_000,
                end_ns=1_600_000_000,
                binary_label=BinaryLabel.FALL,
                activity_code="forward_fall",
                annotator_id="xfan0282",
            )
        ],
        events=[
            AnnotationEvent(
                segment_id="seg_001",
                kind=EventKind.ONSET,
                time_ns=600_000_000,
                annotator_id="xfan0282",
            ),
            AnnotationEvent(
                segment_id="seg_001",
                kind=EventKind.IMPACT,
                time_ns=1_200_000_000,
                annotator_id="xfan0282",
            ),
        ],
    )

    enriched = AnnotationService._canonicalize_and_enrich_events(h5_path, document)

    onset = next(item for item in enriched.events if item.kind == EventKind.ONSET)
    impact = next(item for item in enriched.events if item.kind == EventKind.IMPACT)
    assert onset.time_ns == 400_000_000
    assert onset.source_video_frame is not None
    assert onset.source_imu_sample is not None
    assert impact.time_ns == 1_200_000_000


def test_each_fall_segment_requires_its_own_impact_when_finalized() -> None:
    segments = [
        ActivitySegment(
            segment_id=f"seg_{index:03d}",
            start_ns=start,
            end_ns=end,
            binary_label=BinaryLabel.FALL,
            activity_code="forward_fall",
            annotator_id="xfan0282",
        )
        for index, (start, end) in enumerate(
            ((0, 1_000_000_000), (1_000_000_000, 2_000_000_000)),
            start=1,
        )
    ]
    events = [
        AnnotationEvent(
            segment_id=segment.segment_id,
            kind=kind,
            time_ns=(
                segment.start_ns
                if kind == EventKind.ONSET
                else segment.start_ns + 500_000_000
            ),
            annotator_id="xfan0282",
        )
        for segment in segments
        for kind in (EventKind.ONSET, EventKind.IMPACT)
    ]
    document = AnnotationDocument(
        taxonomy_id="fall_binary_v1",
        taxonomy_version="1.0.0",
        finalized=True,
        segments=segments,
        events=events,
    )

    assert validate_annotations(document, taxonomy(), 2_000_000_000) == []
    missing_second_impact = document.model_copy(
        update={
            "events": [
                event
                for event in document.events
                if not (
                    event.segment_id == "seg_002" and event.kind == EventKind.IMPACT
                )
            ]
        }
    )
    assert any(
        "seg_002 requires onset and impact" in issue
        for issue in validate_annotations(
            missing_second_impact,
            taxonomy(),
            2_000_000_000,
        )
    )


def test_aligned_grid_keeps_derived_onset_equal_to_fall_activity_start(
    tmp_path: Path,
) -> None:
    annotations = AnnotationDocument(
        taxonomy_id="fall_binary_v1",
        taxonomy_version="1.0.0",
        segments=[
            ActivitySegment(
                segment_id="seg_000",
                start_ns=0,
                end_ns=410_000_000,
                binary_label=BinaryLabel.NON_FALL,
                activity_code="walking",
                annotator_id="xfan0282",
            ),
            ActivitySegment(
                segment_id="seg_001",
                start_ns=410_000_000,
                end_ns=1_500_000_000,
                binary_label=BinaryLabel.FALL,
                activity_code="forward_fall",
                annotator_id="xfan0282",
            ),
            ActivitySegment(
                segment_id="seg_002",
                start_ns=1_500_000_000,
                end_ns=4_000_000_000,
                binary_label=BinaryLabel.NON_FALL,
                activity_code="walking",
                annotator_id="xfan0282",
            ),
        ],
        events=[
            AnnotationEvent(
                segment_id="seg_001",
                kind=EventKind.ONSET,
                time_ns=410_000_000,
                annotator_id="xfan0282",
            ),
            AnnotationEvent(
                segment_id="seg_001",
                kind=EventKind.IMPACT,
                time_ns=1_000_000_000,
                annotator_id="xfan0282",
            ),
        ],
    )
    h5_path, mkv_path = write_source_pair(tmp_path)
    review = completed_review(h5_path, mkv_path).model_copy(
        update={"annotations": annotations}
    )

    rows = _annotation_rows(review, grid_origin_ns=0, sample_count=100)
    activity = next(
        row
        for row in rows
        if row["kind"] == "activity" and row["code"] == "forward_fall"
    )
    onset = next(row for row in rows if row["kind"] == "onset")

    assert int(activity["start_sample"]) == 11
    assert int(onset["start_sample"]) == int(activity["start_sample"])


def test_aligned25_export_is_blocked_without_verified_calibration(tmp_path: Path) -> None:
    h5_path, mkv_path = write_source_pair(tmp_path)
    with pytest.raises(ValueError, match="校准"):
        export_aligned(
            completed_review(h5_path, mkv_path),
            h5_path,
            mkv_path,
            tmp_path / "aligned25.h5",
            ImuSettings(),
            taxonomy(),
        )


def test_training_snapshot_contains_manifest_and_per_recording_h5(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned25.h5"
    aligned.write_bytes(b"aligned")

    output = create_training_snapshot_archive(
        [("xfan0282", "recording-1", aligned)],
        tmp_path / "cw12eu_snapshot_test.tar",
    )

    with tarfile.open(output, "r:") as archive:
        assert archive.getnames() == [
            "manifest.json",
            "recordings/xfan0282/recording-1/aligned25.h5",
        ]
        manifest_stream = archive.extractfile("manifest.json")
        assert manifest_stream is not None
        manifest = json.loads(manifest_stream.read())
        assert manifest["schema_version"] == "2.0.0"
        assert manifest["dataset_id"] == "cw12eu"
        assert manifest["hdf5_schema_version"] == "3.1.0"
        assert manifest["sampling_rate_hz"] == 25
        assert manifest["files"] == [
            {
                "path": "recordings/xfan0282/recording-1/aligned25.h5",
                "size_bytes": len(b"aligned"),
                "sha256": sha256_file(aligned),
            }
        ]


def test_merge_training_exports_creates_benchmark_shard(tmp_path: Path) -> None:
    h5_path, mkv_path = write_source_pair(tmp_path)
    aligned = export_aligned(
        completed_review(h5_path, mkv_path),
        h5_path,
        mkv_path,
        tmp_path / "aligned25.h5",
        ImuSettings(accel_counts_per_g=4090.0, gyro_counts_per_dps=16.4),
        taxonomy(),
    )
    merged = merge_training_exports(
        [("xfan0282", "recording-1", aligned)], tmp_path / "cw12eu.h5"
    )
    with h5py.File(merged, "r") as handle:
        assert handle.attrs["dataset_id"] == "cw12eu"
        assert handle.attrs["imu_schema_version"] == "3.1.0"
        assert handle.attrs["evaluation_role"] == "training_only"
        assert handle.attrs["sampling_rate_hz"] == 25.0
        assert handle.attrs["sequence_count"] == 1
        assert handle.attrs["sample_count"] == 50
