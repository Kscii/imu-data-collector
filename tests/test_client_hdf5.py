from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import h5py
import numpy as np
import pytest

from imu_data_collector.client_hdf5 import (
    CLIENT_HDF5_SCHEMA_VERSION,
    build_client_hdf5,
    validate_client_hdf5,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _build_source_zip(tmp_path: Path, *, bad_video_hash: bool = False) -> tuple[Path, bytes]:
    dataset_path = tmp_path / "dataset.h5"
    with h5py.File(dataset_path, "w") as handle:
        handle.attrs["imu_schema_version"] = "3.1.0"
        handle.attrs["sampling_rate_hz"] = 25.0
        handle.attrs["axis_frame"] = "sensor_local"
        handle.create_dataset("samples", data=np.arange(150, dtype=np.float32).reshape(25, 6))
        interval_dtype = [("sample_start", "<i8"), ("sample_stop", "<i8")]
        handle.create_dataset(
            "sequences", data=np.asarray([(0, 25)], dtype=interval_dtype)
        )
        handle.create_dataset(
            "annotations", data=np.asarray([(0, 25)], dtype=interval_dtype)
        )
    dataset_bytes = dataset_path.read_bytes()
    video_bytes = bytes(range(256)) * 8
    taxonomy = {
        "schema_version": "cw12eu_activity_taxonomy_v1",
        "taxonomy_id": "fall_binary_v1",
        "version": "1.2.0+r2",
        "fall": [{"code": "forward_fall", "name": "Forward fall", "active": True}],
        "non_fall": [{"code": "walking", "name": "Walking", "active": True}],
    }
    view = {
        "recording_id": "20260904T000000.000000Z_subject-001",
        "sequence_index": 0,
        "sample_zero_video_media_time_ns": 80_000_000,
        "video_frames": {
            "recording_time_ns": [0, 40_000_000, 80_000_000],
            "media_time_ns": [80_000_000, 120_000_000, 160_000_000],
        },
    }
    taxonomy_bytes = (json.dumps(taxonomy, sort_keys=True) + "\n").encode()
    view_bytes = (json.dumps(view, sort_keys=True) + "\n").encode()
    files = {
        "dataset/cw12eu.h5": dataset_bytes,
        "recordings/0000/video.mp4": video_bytes,
        "recordings/0000/view.json": view_bytes,
        "taxonomies/fall_binary_v1/1.2.0+r2.json": taxonomy_bytes,
    }
    descriptors = [
        {
            "path": name,
            "size_bytes": len(payload),
            "sha256": (
                "0" * 64
                if bad_video_hash and name.endswith("video.mp4")
                else _sha256(payload)
            ),
        }
        for name, payload in files.items()
    ]
    manifest = {
        "schema_version": "cw12eu_client_delivery_v2",
        "contract_version": "2.0.0",
        "snapshot_id": "snapshot-test",
        "snapshot_content_fingerprint": "f" * 64,
        "snapshot_created_at_utc": "2026-09-04T00:00:00Z",
        "hdf5_schema_version": "3.1.0",
        "sampling_rate_hz": 25.0,
        "coordinate_frame": "sensor_local",
        "gravity_retained": True,
        "files": descriptors,
        "recordings": [
            {
                "sequence_index": 0,
                "recording_id": view["recording_id"],
                "video_path": "recordings/0000/video.mp4",
                "view_path": "recordings/0000/view.json",
                "taxonomy_path": "taxonomies/fall_binary_v1/1.2.0+r2.json",
            }
        ],
        "taxonomies": [
            {
                "path": "taxonomies/fall_binary_v1/1.2.0+r2.json",
                "taxonomy_id": "fall_binary_v1",
                "version": "1.2.0+r2",
            }
        ],
    }
    archive_path = tmp_path / "delivery.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
        archive.writestr("manifest.json", json.dumps(manifest))
    return archive_path, video_bytes


def test_build_client_hdf5_keeps_strict_core_and_embeds_native_video(tmp_path: Path) -> None:
    source_zip, video_bytes = _build_source_zip(tmp_path)
    destination = tmp_path / "client.h5"

    report = build_client_hdf5(source_zip, destination)
    validate_client_hdf5(destination)

    assert report.recording_count == 1
    assert report.video_bytes == len(video_bytes)
    assert report.size_bytes == destination.stat().st_size
    assert report.sha256 == _sha256(destination.read_bytes())
    with h5py.File(destination, "r") as handle:
        assert handle.attrs["schema_version"] == CLIENT_HDF5_SCHEMA_VERSION
        assert handle.attrs["embedded_dataset_schema_version"] == "3.1.0"
        assert "imu_schema_version" not in handle.attrs
        assert set(handle) == {"samples", "sequences", "annotations", "media", "labels"}
        assert "documents" not in handle
        assert "integrity" not in handle
        assert "delivery" not in handle
        row = handle["media/index"][0]
        offset = int(row["file_offset"])
        length = int(row["byte_length"])
        assert bytes(handle["media/videos/0000"][:]) == video_bytes
        assert int(row["media_duration_ns"]) == 80_000_000
        assert int(row["sample_zero_video_media_time_ns"]) == 80_000_000
        assert len(handle["labels/catalog"]) == 2
    assert destination.read_bytes()[offset : offset + length] == video_bytes


def test_build_client_hdf5_rejects_manifest_hash_mismatch(tmp_path: Path) -> None:
    source_zip, _video_bytes = _build_source_zip(tmp_path, bad_video_hash=True)

    with pytest.raises(ValueError, match="视频与清单不一致"):
        build_client_hdf5(source_zip, tmp_path / "client.h5")
