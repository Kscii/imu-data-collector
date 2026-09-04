"""Build and validate the experimental single-file CW12EU-T client container."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO

import h5py
import numpy as np

CLIENT_HDF5_SCHEMA_VERSION = "cw12eu_client_hdf5_v1"
CLIENT_HDF5_CONTRACT_VERSION = "1.0.0"
SOURCE_DELIVERY_SCHEMA_VERSION = "cw12eu_client_delivery_v2"
SOURCE_DELIVERY_CONTRACT_VERSION = "2.0.0"
CORE_DATASET_SCHEMA_VERSION = "3.1.0"
COPY_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ClientHdf5Report:
    output_path: str
    size_bytes: int
    sha256: str
    source_zip_size_bytes: int
    source_member_size_bytes: int
    overhead_ratio: float
    recording_count: int
    video_bytes: int
    dataset_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256_stream(handle: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := handle.read(COPY_BYTES):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _sha256_path(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_stream(handle)[0]


def _descriptor(manifest: dict[str, Any], path: str) -> dict[str, Any]:
    matches = [item for item in manifest["files"] if item.get("path") == path]
    if len(matches) != 1:
        raise ValueError(f"交付清单中的文件描述符不唯一：{path}")
    descriptor = matches[0]
    if not isinstance(descriptor.get("size_bytes"), int) or not isinstance(
        descriptor.get("sha256"), str
    ):
        raise ValueError(f"交付清单中的文件描述符无效：{path}")
    return descriptor


def _copy_zip_member(
    archive: zipfile.ZipFile,
    path: str,
    target: BinaryIO,
) -> tuple[str, int]:
    info = archive.getinfo(path)
    if info.compress_type != zipfile.ZIP_STORED:
        raise ValueError(f"客户交付成员必须使用 ZIP_STORED：{path}")
    with archive.open(info) as source:
        digest = hashlib.sha256()
        size = 0
        while chunk := source.read(COPY_BYTES):
            target.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _string_dtype():
    return h5py.string_dtype(encoding="utf-8")


def _video_index_dtype() -> np.dtype:
    text = _string_dtype()
    return np.dtype(
        [
            ("sequence_index", "<i8"),
            ("recording_id", text),
            ("dataset_path", text),
            ("content_type", text),
            ("container", text),
            ("byte_length", "<u8"),
            ("file_offset", "<u8"),
            ("sha256", "S64"),
            ("media_duration_ns", "<i8"),
            ("sample_zero_video_media_time_ns", "<i8"),
        ]
    )


def _label_catalog_dtype() -> np.dtype:
    text = _string_dtype()
    return np.dtype(
        [
            ("taxonomy_id", text),
            ("taxonomy_version", text),
            ("code", text),
            ("name", text),
            ("is_fall", "?"),
            ("active", "?"),
        ]
    )


def _sequence_taxonomy_dtype() -> np.dtype:
    text = _string_dtype()
    return np.dtype(
        [
            ("sequence_index", "<i8"),
            ("taxonomy_id", text),
            ("taxonomy_version", text),
        ]
    )


def _validate_source_manifest(manifest: dict[str, Any]) -> None:
    expected = {
        "schema_version": SOURCE_DELIVERY_SCHEMA_VERSION,
        "contract_version": SOURCE_DELIVERY_CONTRACT_VERSION,
        "hdf5_schema_version": CORE_DATASET_SCHEMA_VERSION,
        "sampling_rate_hz": 25.0,
        "coordinate_frame": "sensor_local",
        "gravity_retained": True,
    }
    for name, value in expected.items():
        if manifest.get(name) != value:
            raise ValueError(f"客户 ZIP 的 {name} 不受支持：{manifest.get(name)!r}")
    if not isinstance(manifest.get("recordings"), list) or not manifest["recordings"]:
        raise ValueError("客户 ZIP 没有录制")
    if not isinstance(manifest.get("taxonomies"), list) or not manifest["taxonomies"]:
        raise ValueError("客户 ZIP 没有冻结标签目录")


def build_client_hdf5(source_zip: Path, destination: Path) -> ClientHdf5Report:
    """Convert one immutable delivery-v2 ZIP to a local experimental HDF5 file."""

    source_zip = source_zip.resolve()
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
    partial.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(source_zip) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            _validate_source_manifest(manifest)
            dataset_descriptor = _descriptor(manifest, "dataset/cw12eu.h5")
            with tempfile.TemporaryDirectory(
                prefix="cw12eu-client-hdf5-", dir=destination.parent
            ) as temporary_directory:
                dataset_path = Path(temporary_directory) / "cw12eu.h5"
                with dataset_path.open("wb") as target:
                    digest, size = _copy_zip_member(
                        archive, "dataset/cw12eu.h5", target
                    )
                if digest != dataset_descriptor["sha256"] or size != dataset_descriptor[
                    "size_bytes"
                ]:
                    raise ValueError("客户 ZIP 中的训练 H5 与清单不一致")

                video_rows: list[tuple[Any, ...]] = []
                label_rows: list[tuple[Any, ...]] = []
                sequence_taxonomy_rows: list[tuple[Any, ...]] = []
                taxonomy_by_path: dict[str, dict[str, Any]] = {}
                for taxonomy_item in manifest["taxonomies"]:
                    path = str(taxonomy_item["path"])
                    descriptor = _descriptor(manifest, path)
                    payload = archive.read(path)
                    if (
                        len(payload) != descriptor["size_bytes"]
                        or hashlib.sha256(payload).hexdigest() != descriptor["sha256"]
                    ):
                        raise ValueError(f"冻结标签文件与清单不一致：{path}")
                    taxonomy = json.loads(payload)
                    taxonomy_by_path[path] = taxonomy
                    for is_fall, group in ((True, "fall"), (False, "non_fall")):
                        for item in taxonomy[group]:
                            label_rows.append(
                                (
                                    str(taxonomy["taxonomy_id"]),
                                    str(taxonomy["version"]),
                                    str(item["code"]),
                                    str(item["name"]),
                                    is_fall,
                                    bool(item.get("active", True)),
                                )
                            )

                with h5py.File(dataset_path, "r") as dataset, h5py.File(
                    partial, "w", libver="latest"
                ) as output:
                    if str(dataset.attrs.get("imu_schema_version")) != CORE_DATASET_SCHEMA_VERSION:
                        raise ValueError("内嵌训练数据不是 HDF5 3.1.0")
                    if set(dataset.keys()) != {"samples", "sequences", "annotations"}:
                        raise ValueError("内嵌训练数据根结构不是严格 HDF5 3.1.0")
                    for name, value in dataset.attrs.items():
                        if name != "imu_schema_version":
                            output.attrs[name] = value
                    output.attrs["schema_version"] = CLIENT_HDF5_SCHEMA_VERSION
                    output.attrs["contract_version"] = CLIENT_HDF5_CONTRACT_VERSION
                    output.attrs["embedded_dataset_schema_version"] = (
                        CORE_DATASET_SCHEMA_VERSION
                    )
                    output.attrs["snapshot_id"] = str(manifest["snapshot_id"])
                    output.attrs["snapshot_content_fingerprint"] = str(
                        manifest["snapshot_content_fingerprint"]
                    )
                    output.attrs["snapshot_created_at_utc"] = str(
                        manifest["snapshot_created_at_utc"]
                    )
                    output.attrs["video_contains_identifiable_participants"] = True
                    for name in ("samples", "sequences", "annotations"):
                        dataset.copy(name, output, name=name)

                    media = output.create_group("media")
                    media.attrs["video_container"] = "mp4"
                    videos = media.create_group("videos")
                    timing = media.create_group("timing")
                    for recording in sorted(
                        manifest["recordings"], key=lambda item: item["sequence_index"]
                    ):
                        sequence_index = int(recording["sequence_index"])
                        name = f"{sequence_index:04d}"
                        video_path = str(recording["video_path"])
                        view_path = str(recording["view_path"])
                        taxonomy_path = str(recording["taxonomy_path"])
                        video_descriptor = _descriptor(manifest, video_path)
                        view_descriptor = _descriptor(manifest, view_path)
                        view_bytes = archive.read(view_path)
                        if (
                            len(view_bytes) != view_descriptor["size_bytes"]
                            or hashlib.sha256(view_bytes).hexdigest()
                            != view_descriptor["sha256"]
                        ):
                            raise ValueError(f"视频时间映射与清单不一致：{view_path}")
                        view = json.loads(view_bytes)
                        if int(view["sequence_index"]) != sequence_index:
                            raise ValueError(f"sequence 与视频时间映射不一致：{view_path}")
                        video_size = int(video_descriptor["size_bytes"])
                        video_dataset = videos.create_dataset(
                            name,
                            shape=(video_size,),
                            dtype=np.uint8,
                            chunks=None,
                            compression=None,
                        )
                        digest = hashlib.sha256()
                        cursor = 0
                        with archive.open(video_path) as source:
                            while chunk := source.read(COPY_BYTES):
                                stop = cursor + len(chunk)
                                video_dataset[cursor:stop] = np.frombuffer(
                                    chunk, dtype=np.uint8
                                )
                                digest.update(chunk)
                                cursor = stop
                        if cursor != video_size or digest.hexdigest() != video_descriptor[
                            "sha256"
                        ]:
                            raise ValueError(f"视频与清单不一致：{video_path}")
                        output.flush()
                        file_offset = video_dataset.id.get_offset()
                        if file_offset is None or file_offset < 0:
                            raise ValueError(f"视频没有连续物理偏移：{video_path}")
                        frames = view.get("video_frames") or {}
                        recording_times = np.asarray(
                            frames.get("recording_time_ns", []), dtype=np.int64
                        )
                        media_times = np.asarray(
                            frames.get("media_time_ns", []), dtype=np.int64
                        )
                        if (
                            recording_times.ndim != 1
                            or media_times.ndim != 1
                            or recording_times.shape != media_times.shape
                            or not len(recording_times)
                        ):
                            raise ValueError(f"视频帧时间映射无效：{view_path}")
                        mapping = np.column_stack((recording_times, media_times))
                        mapping_dataset = timing.create_dataset(name, data=mapping)
                        mapping_dataset.attrs["columns"] = np.asarray(
                            ["recording_time_ns", "media_time_ns"],
                            dtype=_string_dtype(),
                        )
                        taxonomy = taxonomy_by_path[taxonomy_path]
                        sequence_taxonomy_rows.append(
                            (
                                sequence_index,
                                str(taxonomy["taxonomy_id"]),
                                str(taxonomy["version"]),
                            )
                        )
                        video_rows.append(
                            (
                                sequence_index,
                                str(recording["recording_id"]),
                                f"/media/videos/{name}",
                                "video/mp4",
                                "mp4",
                                video_size,
                                int(file_offset),
                                str(video_descriptor["sha256"]).encode("ascii"),
                                int(media_times[-1] - media_times[0]),
                                int(view["sample_zero_video_media_time_ns"]),
                            )
                        )
                    media.create_dataset(
                        "index", data=np.asarray(video_rows, dtype=_video_index_dtype())
                    )
                    labels = output.create_group("labels")
                    labels.create_dataset(
                        "catalog",
                        data=np.asarray(label_rows, dtype=_label_catalog_dtype()),
                    )
                    labels.create_dataset(
                        "sequence_versions",
                        data=np.asarray(
                            sequence_taxonomy_rows, dtype=_sequence_taxonomy_dtype()
                        ),
                    )

        partial.replace(destination)
        validate_client_hdf5(destination)
    finally:
        partial.unlink(missing_ok=True)

    video_bytes = sum(
        int(_descriptor(manifest, str(item["video_path"]))["size_bytes"])
        for item in manifest["recordings"]
    )
    member_bytes = int(dataset_descriptor["size_bytes"]) + video_bytes
    return ClientHdf5Report(
        output_path=str(destination),
        size_bytes=destination.stat().st_size,
        sha256=_sha256_path(destination),
        source_zip_size_bytes=source_zip.stat().st_size,
        source_member_size_bytes=member_bytes,
        overhead_ratio=(destination.stat().st_size - member_bytes) / member_bytes,
        recording_count=len(manifest["recordings"]),
        video_bytes=video_bytes,
        dataset_bytes=int(dataset_descriptor["size_bytes"]),
    )


def validate_client_hdf5(path: Path) -> None:
    """Validate structure and every embedded video's frozen physical range."""

    with h5py.File(path, "r") as handle:
        if handle.attrs.get("schema_version") != CLIENT_HDF5_SCHEMA_VERSION:
            raise ValueError("客户 H5 schema_version 无效")
        if handle.attrs.get("contract_version") != CLIENT_HDF5_CONTRACT_VERSION:
            raise ValueError("客户 H5 contract_version 无效")
        if set(handle.keys()) != {
            "samples",
            "sequences",
            "annotations",
            "media",
            "labels",
        }:
            raise ValueError("客户 H5 根结构无效")
        index = handle["media/index"][:]
        video_ranges = [
            (
                int(row["file_offset"]),
                int(row["byte_length"]),
                bytes(row["sha256"]).decode("ascii"),
            )
            for row in index
        ]
    with path.open("rb") as source:
        for offset, size, expected_digest in video_ranges:
            source.seek(offset)
            remaining = size
            digest = hashlib.sha256()
            while remaining:
                chunk = source.read(min(COPY_BYTES, remaining))
                if not chunk:
                    raise ValueError("客户 H5 中的视频物理范围提前结束")
                digest.update(chunk)
                remaining -= len(chunk)
            if digest.hexdigest() != expected_digest:
                raise ValueError("客户 H5 中的视频物理范围 SHA-256 不匹配")
