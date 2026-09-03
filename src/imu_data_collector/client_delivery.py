"""Build immutable, snapshot-bound customer delivery archives."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

CLIENT_DELIVERY_SCHEMA_VERSION = "cw12eu_client_delivery_v1"


def canonical_json(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def delivery_readme(snapshot_id: str) -> bytes:
    return f"""# CW12EU-T client delivery

This immutable package is bound to training snapshot `{snapshot_id}`.

## Contents

- `dataset/cw12eu.h5`: the merged 25 Hz, six-axis SI training dataset (schema 3.1.0).
- `recordings/<recording_id>/video.mp4`: the original review video for that recording.
- `recordings/<recording_id>/view.json`: the frozen mapping from HDF5 samples and
  annotations to video media time.
- `manifest.json`: package identity, provenance and file inventory.
- `SHA256SUMS`: SHA-256 checksums for package members.

The HDF5 file intentionally does not embed video. Keep the package together so the
snapshot identity, checksums and sample-to-video mappings remain auditable.
""".encode()


def build_delivery_archive(
    destination: Path,
    *,
    package_manifest: dict[str, Any],
    dataset_path: Path,
    recordings: list[dict[str, Any]],
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
                prefix = f"recordings/{recording_id}"
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

            write_bytes(archive, "README.md", delivery_readme(str(package_manifest["snapshot_id"])))
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
