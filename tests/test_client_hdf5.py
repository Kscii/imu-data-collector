from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import h5py
import numpy as np
import pytest

from imu_data_collector.client_hdf5 import (
    CLIENT_ARTIFACT_PROFILE,
    CLIENT_HDF5_CONTRACT_VERSION,
    CORE_DATASET_SCHEMA_VERSION,
    FEATURE_COLUMNS,
    FEATURE_UNITS,
    _logical_digest,
    build_client_hdf5,
    validate_client_hdf5,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _training_h5(path: Path, *, annotation_code: str = "walking") -> None:
    text = h5py.string_dtype(encoding="utf-8")
    sequence_dtype = np.dtype(
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
    annotation_dtype = np.dtype(
        [
            ("sequence_index", "<i4"),
            ("kind", text),
            ("start_sample", "<i8"),
            ("stop_sample", "<i8"),
            ("code", text),
        ]
    )
    samples = np.arange(150, dtype=np.float32).reshape(25, 6) / 100
    sequences = np.asarray(
        [
            (
                0,
                25,
                "cw12eu:20260904T000000.000000Z",
                "cw12eu:subject-001",
                "cw12eu:20260904T000000.000000Z",
                "chest",
                "mixed",
                False,
                "temporal",
                25.0,
            )
        ],
        dtype=sequence_dtype,
    )
    annotations = np.asarray(
        [(0, "activity", 0, 25, annotation_code)],
        dtype=annotation_dtype,
    )
    with h5py.File(path, "w", libver="latest") as handle:
        handle.attrs["imu_schema_version"] = CORE_DATASET_SCHEMA_VERSION
        handle.attrs["artifact_profile"] = "training_dataset"
        handle.attrs["dataset_id"] = "cw12eu"
        handle.attrs["sampling_rate_hz"] = 25.0
        handle.attrs["evaluation_role"] = "training_only"
        handle.attrs["axis_frame"] = "sensor_local"
        handle.attrs["feature_columns"] = json.dumps(FEATURE_COLUMNS)
        handle.attrs["hdf5_compatibility"] = "1.14"
        handle.attrs["sequence_count"] = 1
        handle.attrs["sample_count"] = len(samples)
        handle.attrs["annotation_count"] = len(annotations)
        handle.attrs["logical_content_sha256"] = _logical_digest(
            samples, sequences, annotations
        )
        sample_dataset = handle.create_dataset("samples", data=samples, dtype="<f4")
        sample_dataset.attrs["columns"] = json.dumps(FEATURE_COLUMNS)
        sample_dataset.attrs["units"] = json.dumps(FEATURE_UNITS)
        handle.create_dataset("sequences", data=sequences)
        handle.create_dataset("annotations", data=annotations)


def _inputs(
    tmp_path: Path, *, annotation_code: str = "walking"
) -> tuple[Path, bytes, list[dict[str, object]], list[dict[str, object]]]:
    dataset = tmp_path / "cw12eu.h5"
    _training_h5(dataset, annotation_code=annotation_code)
    video = b"\x00\x00\x00\x18ftypisom" + bytes(range(256)) * 8
    view = {
        "recording_id": "20260904T000000.000000Z",
        "sequence_index": 0,
        "sample_count": 25,
        "merged_sample_start": 0,
        "merged_sample_stop": 25,
        "sample_zero_recording_time_ns": 0,
        "sample_zero_video_media_time_ns": 80_000_000,
        "taxonomy_id": "fall_binary_v1",
        "taxonomy_version": "1.2.0+r2",
        "video_frames": {
            "recording_time_ns": [0, 480_000_000, 960_000_000],
            "media_time_ns": [80_000_000, 560_000_000, 1_040_000_000],
        },
    }
    recordings: list[dict[str, object]] = [
        {
            "sequence_index": 0,
            "recording_id": "20260904T000000.000000Z",
            "video": {
                "object_key": "frozen/video.mp4",
                "size_bytes": len(video),
                "sha256": _sha256(video),
                "content_type": "video/mp4",
            },
            "view": view,
        }
    ]
    taxonomies: list[dict[str, object]] = [
        {
            "payload": {
                "taxonomy_id": "fall_binary_v1",
                "version": "1.2.0+r2",
                "fall": [
                    {
                        "code": "forward_fall",
                        "name": "Forward fall",
                        "active": True,
                    }
                ],
                "non_fall": [
                    {"code": "walking", "name": "Walking", "active": True}
                ],
            }
        }
    ]
    return dataset, video, recordings, taxonomies


def _build(
    tmp_path: Path,
    *,
    annotation_code: str = "walking",
    progress: Callable[[str, int, int], None] | None = None,
) -> tuple[Path, bytes]:
    dataset, video, recordings, taxonomies = _inputs(
        tmp_path, annotation_code=annotation_code
    )
    destination = tmp_path / "client.h5"

    def read_chunk(key: str, cursor: int) -> bytes:
        assert key == "frozen/video.mp4"
        return video[cursor : cursor + 17]

    report = build_client_hdf5(
        destination,
        dataset_path=dataset,
        snapshot_id="snapshot-0123456789abcdef01234567",
        snapshot_content_fingerprint="f" * 64,
        snapshot_created_at_utc="2026-09-04T00:00:00+00:00",
        recordings=recordings,
        taxonomies=taxonomies,
        read_object_chunks=read_chunk,
        progress=progress,
    )
    assert report.sha256 == _sha256(destination.read_bytes())
    return destination, video


def test_build_reports_copy_and_single_pass_validation_progress(tmp_path: Path) -> None:
    updates: list[tuple[str, int, int]] = []
    destination, video = _build(tmp_path, progress=lambda *item: updates.append(item))

    assert any(stage == "copying_videos" for stage, _current, _total in updates)
    assert updates[-1] == ("validating", destination.stat().st_size, destination.stat().st_size)
    assert validate_client_hdf5(destination)["sha256"] == _sha256(destination.read_bytes())
    assert sum(current for stage, current, _total in updates if stage == "copying_videos") >= len(
        video
    )


def test_build_client_hdf5_keeps_strict_core_and_embeds_native_video(
    tmp_path: Path,
) -> None:
    destination, video = _build(tmp_path)
    report = validate_client_hdf5(destination)

    assert report["recording_count"] == 1
    with h5py.File(destination, "r") as handle:
        assert handle.attrs["imu_schema_version"] == CORE_DATASET_SCHEMA_VERSION
        assert handle.attrs["artifact_profile"] == CLIENT_ARTIFACT_PROFILE
        assert (
            handle.attrs["client_delivery_contract_version"]
            == CLIENT_HDF5_CONTRACT_VERSION
        )
        assert set(handle) == {
            "samples",
            "sequences",
            "annotations",
            "media",
            "labels",
        }
        row = handle["media/index"][0]
        offset = int(row["file_offset"])
        length = int(row["byte_length"])
        assert bytes(handle["media/videos/0"][:]) == video
        assert int(row["media_duration_ns"]) == 1_040_000_000
        assert int(row["sample_zero_media_time_ns"]) == 80_000_000
        assert handle["media/videos/0"].chunks is None
        assert handle["media/videos/0"].compression is None
        assert np.array_equal(
            handle["media/timing/0"][:],
            np.asarray(
                [
                    [0, 80_000_000],
                    [480_000_000, 560_000_000],
                    [960_000_000, 1_040_000_000],
                ],
                dtype=np.int64,
            ),
        )
        assert len(handle["labels/catalog"]) == 2
    assert destination.read_bytes()[offset : offset + length] == video


def test_build_rejects_unknown_activity_code(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="taxonomy"):
        _build(tmp_path, annotation_code="unknown")


def test_validator_rejects_corrupt_physical_video_bytes(tmp_path: Path) -> None:
    destination, _video = _build(tmp_path)
    with h5py.File(destination, "r") as handle:
        offset = int(handle["media/index"][0]["file_offset"])
    with destination.open("r+b") as raw:
        raw.seek(offset + 12)
        original = raw.read(1)
        raw.seek(offset + 12)
        raw.write(bytes([original[0] ^ 0xFF]))

    with pytest.raises(ValueError, match="SHA-256"):
        validate_client_hdf5(destination)


def test_validator_rejects_non_monotonic_timing(tmp_path: Path) -> None:
    destination, _video = _build(tmp_path)
    with h5py.File(destination, "r+") as handle:
        handle["media/timing/0"][1, 0] = 0

    with pytest.raises(ValueError, match="timing"):
        validate_client_hdf5(destination)


def test_builder_rejects_duplicate_sequence_mapping(tmp_path: Path) -> None:
    dataset, video, recordings, taxonomies = _inputs(tmp_path)
    recordings.append(dict(recordings[0]))

    with pytest.raises(ValueError, match="sequence"):
        build_client_hdf5(
            tmp_path / "duplicate.h5",
            dataset_path=dataset,
            snapshot_id="snapshot-0123456789abcdef01234567",
            snapshot_content_fingerprint="f" * 64,
            snapshot_created_at_utc="2026-09-04T00:00:00+00:00",
            recordings=recordings,
            taxonomies=taxonomies,
            read_object_chunks=lambda _key, cursor: video[cursor : cursor + 128],
        )
