"""Build immutable, snapshot-bound customer delivery archives."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

CLIENT_DELIVERY_SCHEMA_VERSION = "cw12eu_client_delivery_v2"
CLIENT_DELIVERY_CONTRACT_VERSION = "2.0.0"


def safe_archive_component(value: str, *, name: str) -> str:
    """Reject values that could escape or ambiguously address an archive path."""

    if not re.fullmatch(r"[A-Za-z0-9._+-]+", value) or value in {".", ".."}:
        raise ValueError(f"{name} 不能安全写入客户交付 ZIP")
    return value


def public_taxonomy(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable taxonomy fields that are safe to give to a client."""

    return {
        "schema_version": "cw12eu_activity_taxonomy_v1",
        "taxonomy_id": str(payload["taxonomy_id"]),
        "version": str(payload["version"]),
        "fall": [
            {
                "code": str(item["code"]),
                "name": str(item["name"]),
                "active": bool(item.get("active", True)),
            }
            for item in payload["fall"]
        ],
        "non_fall": [
            {
                "code": str(item["code"]),
                "name": str(item["name"]),
                "active": bool(item.get("active", True)),
            }
            for item in payload["non_fall"]
        ],
    }


def canonical_json(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def delivery_readme(snapshot_id: str) -> bytes:
    return f"""# CW12EU-T client delivery

This immutable package is bound to training snapshot `{snapshot_id}`.

## Contents

- `dataset/cw12eu.h5`: the merged 25 Hz, six-axis SI training dataset (schema 3.1.0).
- `recordings/<sequence_index>/video.mp4`: the original review video for that recording.
- `recordings/<sequence_index>/view.json`: the frozen mapping from HDF5 samples and
  annotations to video media time.
- `taxonomies/<taxonomy_id>/<version>.json`: the frozen names and binary classes
  for annotation codes used by the recordings.
- `manifest.json`: package identity, provenance and file inventory.
- `DATASET_CARD.md`: units, scope, limitations and privacy notes.
- `SHA256SUMS`: SHA-256 checksums for package members.

The HDF5 file intentionally does not embed video. Keep the package together so the
snapshot identity, checksums and sample-to-video mappings remain auditable.

Open the complete ZIP or the standalone HDF5 file at
https://viewer.imu.kscii.tech. The viewer processes selected files locally in
the browser and does not upload the dataset.
""".encode()


def delivery_dataset_card(package_manifest: dict[str, Any]) -> bytes:
    recordings = package_manifest["recordings"]
    total_samples = sum(
        int(item["merged_sample_stop"]) - int(item["merged_sample_start"])
        for item in recordings
    )
    duration_seconds = total_samples / float(package_manifest["sampling_rate_hz"])
    return f"""# CW12EU-T dataset card

## Scope

- Snapshot: `{package_manifest['snapshot_id']}`
- Recordings: {len(recordings)}
- Samples: {total_samples}
- Duration: {duration_seconds:.3f} seconds
- Sampling rate: {package_manifest['sampling_rate_hz']:.1f} Hz
- HDF5 schema: {package_manifest['hdf5_schema_version']}

## Sensor data

`dataset/cw12eu.h5` contains `float32` SI values in this fixed order:
`acceleration_x_mps2`, `acceleration_y_mps2`, `acceleration_z_mps2`,
`angular_velocity_x_radps`, `angular_velocity_y_radps`,
`angular_velocity_z_radps`. The coordinate frame is sensor-local and gravity is
retained.

## Labels and synchronization

Activity intervals and point events are stored in `/annotations`. Stable codes
are resolved through the frozen taxonomy files in this package. Each
`view.json` maps recording-relative 25 Hz samples to the original video media
timeline; video frames and IMU samples are not assumed to have the same rate.

## Limitations and data use

This snapshot is research data and is not evidence that a fall-detection model
is safe or effective. Videos may identify participants. Distribution, access,
retention and deletion remain subject to the project agreement supplied
separately; the MIT license of the viewer does not license this dataset.
""".encode()


def build_delivery_archive(
    destination: Path,
    *,
    package_manifest: dict[str, Any],
    dataset_path: Path,
    recordings: list[dict[str, Any]],
    taxonomies: list[dict[str, Any]],
    read_object_chunks: Callable[[str, int], bytes],
) -> tuple[str, int]:
    """Create a deterministic ZIP64 archive without recompressing HDF5 or video."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    temporary.unlink(missing_ok=True)
    sums: list[tuple[str, str]] = []

    def info(name: str) -> zipfile.ZipInfo:
        entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        entry.compress_type = zipfile.ZIP_STORED
        entry.external_attr = 0o644 << 16
        return entry

    def write_bytes(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
        archive.writestr(info(name), content)
        sums.append((hashlib.sha256(content).hexdigest(), name))

    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
            dataset_name = "dataset/cw12eu.h5"
            dataset_digest = hashlib.sha256()
            with (
                dataset_path.open("rb") as source,
                archive.open(info(dataset_name), "w", force_zip64=True) as target,
            ):
                while chunk := source.read(4 * 1024 * 1024):
                    dataset_digest.update(chunk)
                    target.write(chunk)
            sums.append((dataset_digest.hexdigest(), dataset_name))

            for recording in recordings:
                recording_id = str(recording["recording_id"])
                prefix = str(recording["archive_prefix"])
                video = recording["video"]
                video_name = f"{prefix}/video.mp4"
                digest = hashlib.sha256()
                with archive.open(info(video_name), "w", force_zip64=True) as target:
                    cursor = 0
                    total = int(video["size_bytes"])
                    while cursor < total:
                        chunk = read_object_chunks(str(video["object_key"]), cursor)
                        if not chunk:
                            raise ValueError(f"视频对象提前结束：{recording_id}")
                        digest.update(chunk)
                        target.write(chunk)
                        cursor += len(chunk)
                if cursor != total or digest.hexdigest() != video["sha256"]:
                    raise ValueError(f"视频对象大小或 SHA-256 不匹配：{recording_id}")
                sums.append((digest.hexdigest(), video_name))
                write_bytes(archive, f"{prefix}/view.json", canonical_json(recording["view"]))

            for taxonomy in taxonomies:
                write_bytes(archive, str(taxonomy["path"]), canonical_json(taxonomy["payload"]))

            write_bytes(archive, "README.md", delivery_readme(str(package_manifest["snapshot_id"])))
            write_bytes(archive, "DATASET_CARD.md", delivery_dataset_card(package_manifest))
            manifest_bytes = canonical_json(package_manifest)
            archive.writestr(info("manifest.json"), manifest_bytes)
            sums.append((hashlib.sha256(manifest_bytes).hexdigest(), "manifest.json"))
            checksum_bytes = "".join(
                f"{digest}  {name}\n" for digest, name in sorted(sums, key=lambda row: row[1])
            ).encode()
            archive.writestr(info("SHA256SUMS"), checksum_bytes)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    digest = hashlib.sha256()
    with destination.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest(), destination.stat().st_size
