"""Build and strongly validate the frozen single-HDF5 client delivery."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO

import h5py
import numpy as np

from imu_data_collector.logical_content import logical_content_sha256

CORE_DATASET_SCHEMA_VERSION = "3.2.0"
TRAINING_ARTIFACT_PROFILE = "training_dataset"
CLIENT_ARTIFACT_PROFILE = "client_delivery"
CLIENT_HDF5_CONTRACT_VERSION = "1.0.0"
CLIENT_DELIVERY_MANIFEST_SCHEMA = "cw12eu_client_hdf5_delivery_v1"
COPY_BYTES = 8 * 1024 * 1024
FEATURE_COLUMNS = (
    "acceleration_x_mps2",
    "acceleration_y_mps2",
    "acceleration_z_mps2",
    "angular_velocity_x_rad_s",
    "angular_velocity_y_rad_s",
    "angular_velocity_z_rad_s",
)
FEATURE_UNITS = ("m/s^2", "m/s^2", "m/s^2", "rad/s", "rad/s", "rad/s")
RESERVED_EXCLUDE_CODES = frozenset({"sync_tap", "other"})


@dataclass(frozen=True, slots=True)
class ClientHdf5Report:
    output_path: str
    size_bytes: int
    sha256: str
    recording_count: int
    video_bytes: int
    dataset_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def public_taxonomy(payload: dict[str, Any]) -> dict[str, Any]:
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


def _text(value: object) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _sha256_stream(handle: BinaryIO, *, limit: int | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while limit is None or size < limit:
        read_size = COPY_BYTES if limit is None else min(COPY_BYTES, limit - size)
        chunk = handle.read(read_size)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def sha256_path(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_stream(handle)[0]


def _string_dtype() -> np.dtype:
    return h5py.string_dtype(encoding="utf-8")


def _video_index_dtype() -> np.dtype:
    text = _string_dtype()
    return np.dtype(
        [
            ("sequence_index", "<i4"),
            ("byte_length", "<i8"),
            ("file_offset", "<i8"),
            ("sha256", text),
            ("content_type", text),
            ("container", text),
            ("media_duration_ns", "<i8"),
            ("sample_zero_recording_time_ns", "<i8"),
            ("sample_zero_media_time_ns", "<i8"),
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
            ("sequence_index", "<i4"),
            ("taxonomy_id", text),
            ("taxonomy_version", text),
        ]
    )


def _is_utf8(dtype: np.dtype) -> bool:
    info = h5py.check_string_dtype(dtype)
    return info is not None and info.encoding == "utf-8"


def _check_compound_dtype(
    dataset: h5py.Dataset,
    expected: tuple[tuple[str, np.dtype | None], ...],
    *,
    label: str,
) -> None:
    if dataset.dtype.names != tuple(name for name, _dtype in expected):
        raise ValueError(f"{label} 字段或顺序无效")
    for name, expected_dtype in expected:
        actual = dataset.dtype.fields[name][0]
        valid = _is_utf8(actual) if expected_dtype is None else actual == expected_dtype
        if not valid:
            raise ValueError(f"{label}.{name} dtype 无效")


def _logical_digest(
    values: np.ndarray, sequences: np.ndarray, annotations: np.ndarray
) -> str:
    return logical_content_sha256(
        values,
        sequences,
        annotations,
        dataset_id="cw12eu",
        sampling_rate_hz=25.0,
    )


def _validate_core(
    handle: h5py.File,
    *,
    expected_profile: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    for name, expected in (
        ("imu_schema_version", CORE_DATASET_SCHEMA_VERSION),
        ("artifact_profile", expected_profile),
        ("dataset_id", "cw12eu"),
        ("axis_frame", "sensor_local"),
        ("hdf5_compatibility", "1.14"),
    ):
        if _text(handle.attrs.get(name, "")) != expected:
            raise ValueError(f"客户 H5 的 {name} 无效")
    if float(handle.attrs.get("sampling_rate_hz", 0.0)) != 25.0:
        raise ValueError("客户 H5 必须为 25 Hz")
    if _text(handle.attrs.get("evaluation_role", "")) != "training_only":
        raise ValueError("客户 H5 必须来自 training_only 快照")
    if tuple(json.loads(_text(handle.attrs.get("feature_columns", "[]")))) != FEATURE_COLUMNS:
        raise ValueError("客户 H5 的六轴列顺序无效")

    samples = handle["samples"]
    sequences = handle["sequences"]
    annotations = handle["annotations"]
    if samples.dtype != np.dtype("float32") or samples.ndim != 2 or samples.shape[1] != 6:
        raise ValueError("/samples 必须为 float32 [N, 6]")
    values = np.asarray(samples, dtype=np.float32)
    if not np.isfinite(values).all():
        raise ValueError("/samples 包含非有限值")
    if tuple(json.loads(_text(samples.attrs.get("columns", "[]")))) != FEATURE_COLUMNS:
        raise ValueError("/samples columns 无效")
    if tuple(json.loads(_text(samples.attrs.get("units", "[]")))) != FEATURE_UNITS:
        raise ValueError("/samples units 无效")
    _check_compound_dtype(
        sequences,
        (
            ("sample_start", np.dtype("int64")),
            ("sample_stop", np.dtype("int64")),
            ("source_file", None),
            ("participant_id", None),
            ("recording_id", None),
            ("body_location", None),
            ("activity_code", None),
            ("is_fall", np.dtype("bool")),
            ("supervision_kind", None),
            ("source_sampling_rate_hz", np.dtype("float64")),
        ),
        label="/sequences",
    )
    _check_compound_dtype(
        annotations,
        (
            ("sequence_index", np.dtype("int32")),
            ("kind", None),
            ("start_sample", np.dtype("int64")),
            ("stop_sample", np.dtype("int64")),
            ("code", None),
        ),
        label="/annotations",
    )
    sequence_rows = np.asarray(sequences)
    annotation_rows = np.asarray(annotations)
    if not len(sequence_rows):
        raise ValueError("客户 H5 至少需要一个 sequence")
    starts = np.asarray(sequence_rows["sample_start"], dtype=np.int64)
    stops = np.asarray(sequence_rows["sample_stop"], dtype=np.int64)
    if (
        starts[0] != 0
        or not np.array_equal(starts[1:], stops[:-1])
        or stops[-1] != len(values)
        or np.any(stops - starts < 2)
    ):
        raise ValueError("/sequences 未无缝覆盖 /samples")
    identities: set[tuple[str, str]] = set()
    for row in sequence_rows:
        identity = (_text(row["recording_id"]), _text(row["body_location"]))
        if identity in identities:
            raise ValueError("/sequences 存在重复录制和佩戴位置")
        identities.add(identity)
        if _text(row["supervision_kind"]) not in {"recording", "temporal"}:
            raise ValueError("/sequences supervision_kind 无效")
        rate = float(row["source_sampling_rate_hz"])
        if not np.isfinite(rate) or rate <= 0:
            raise ValueError("/sequences source_sampling_rate_hz 无效")
        for field in (
            "source_file",
            "participant_id",
            "recording_id",
            "body_location",
            "activity_code",
        ):
            if not _text(row[field]):
                raise ValueError(f"/sequences.{field} 不能为空")

    kind_order = {"activity": 0, "onset": 1, "impact": 2, "exclude": 3}
    previous: tuple[int, int, int, int, str] | None = None
    by_sequence: dict[int, list[tuple[str, int, int, str]]] = {
        index: [] for index in range(len(sequence_rows))
    }
    for row in annotation_rows:
        sequence_index = int(row["sequence_index"])
        kind = _text(row["kind"])
        start = int(row["start_sample"])
        stop = int(row["stop_sample"])
        code = _text(row["code"])
        if sequence_index not in by_sequence or kind not in kind_order or not code:
            raise ValueError("/annotations 包含无效身份、kind 或 code")
        length = int(stops[sequence_index] - starts[sequence_index])
        if start < 0 or start >= length:
            raise ValueError("/annotations start_sample 越界")
        if kind in {"onset", "impact"}:
            if stop != start:
                raise ValueError("onset/impact 必须满足 start_sample == stop_sample")
        elif not start < stop <= length:
            raise ValueError("activity/exclude 区间无效")
        if kind == "exclude" and code not in RESERVED_EXCLUDE_CODES:
            raise ValueError("exclude code 不属于 3.2 保留集合")
        key = (sequence_index, start, kind_order[kind], stop, code)
        if previous is not None and key < previous:
            raise ValueError("/annotations 未按确定顺序排列")
        previous = key
        by_sequence[sequence_index].append((kind, start, stop, code))

    for sequence_index, sequence in enumerate(sequence_rows):
        rows = by_sequence[sequence_index]
        if _text(sequence["supervision_kind"]) != "temporal":
            continue
        length = int(stops[sequence_index] - starts[sequence_index])
        intervals = sorted(
            (start, stop)
            for kind, start, stop, _code in rows
            if kind in {"activity", "exclude"}
        )
        cursor = 0
        for start, stop in intervals:
            if start != cursor:
                raise ValueError("temporal activity/exclude 未完整且无重叠覆盖 sequence")
            cursor = stop
        if cursor != length:
            raise ValueError("temporal activity/exclude 未完整覆盖 sequence")
        activities = [row for row in rows if row[0] == "activity"]
        onsets = [row for row in rows if row[0] == "onset"]
        impacts = [row for row in rows if row[0] == "impact"]
        used_impacts: set[int] = set()
        for _kind, onset, _stop, code in onsets:
            matches = [
                row
                for row in activities
                if row[1] == onset and row[2] > onset and row[3] == code
            ]
            if len(matches) != 1:
                raise ValueError("每个 onset 必须匹配一个同 code 跌倒区间")
            activity = matches[0]
            candidates = [
                (index, row)
                for index, row in enumerate(impacts)
                if index not in used_impacts
                and row[3] == code
                and onset < row[1] < activity[2]
            ]
            if len(candidates) != 1:
                raise ValueError("每个跌倒区间必须包含一个同 code impact")
            used_impacts.add(candidates[0][0])
        if len(used_impacts) != len(impacts) or bool(sequence["is_fall"]) != bool(onsets):
            raise ValueError("temporal 跌倒事件与 is_fall 不一致")

    for name, expected in (
        ("sequence_count", len(sequence_rows)),
        ("sample_count", len(values)),
        ("annotation_count", len(annotation_rows)),
    ):
        if int(handle.attrs.get(name, -1)) != expected:
            raise ValueError(f"根属性 {name} 与实际数据不一致")
    logical = _text(handle.attrs.get("logical_content_sha256", ""))
    if logical != _logical_digest(values, sequence_rows, annotation_rows):
        raise ValueError("logical_content_sha256 与三张核心表不一致")
    return values, sequence_rows, annotation_rows


def _timing_mapping(view: dict[str, Any], sample_count: int) -> np.ndarray:
    frames = view.get("video_frames") or {}
    recording_times = np.asarray(frames.get("recording_time_ns", []), dtype=np.int64)
    media_times = np.asarray(frames.get("media_time_ns", []), dtype=np.int64)
    if (
        recording_times.ndim != 1
        or media_times.ndim != 1
        or recording_times.shape != media_times.shape
        or len(recording_times) < 2
        or np.any(np.diff(recording_times) <= 0)
        or np.any(np.diff(media_times) <= 0)
    ):
        raise ValueError("冻结视频 timing 必须包含两列严格递增的帧时间")
    sequence_start = int(view["sample_zero_recording_time_ns"])
    sequence_stop = sequence_start + (sample_count - 1) * 40_000_000
    if sequence_start < recording_times[0] or sequence_stop > recording_times[-1]:
        raise ValueError("视频 timing 没有覆盖完整 25 Hz sequence")
    inner = recording_times[
        (recording_times > sequence_start) & (recording_times < sequence_stop)
    ]
    clipped_recording = np.unique(
        np.concatenate(
            (np.asarray([sequence_start]), inner, np.asarray([sequence_stop]))
        )
    )
    clipped_media = np.rint(
        np.interp(clipped_recording, recording_times, media_times)
    ).astype(np.int64)
    if np.any(np.diff(clipped_media) <= 0):
        raise ValueError("裁剪后的视频 timing 不是严格递增")
    expected_zero = int(view["sample_zero_video_media_time_ns"])
    if abs(int(clipped_media[0]) - expected_zero) > 1:
        raise ValueError("sample zero 的 recording/media anchor 不一致")
    return np.column_stack((clipped_recording, clipped_media)).astype(np.int64)


def build_client_hdf5(
    destination: Path,
    *,
    dataset_path: Path,
    snapshot_id: str,
    snapshot_content_fingerprint: str,
    snapshot_created_at_utc: str,
    recordings: list[dict[str, Any]],
    taxonomies: list[dict[str, Any]],
    read_object_chunks: Callable[[str, int], bytes],
) -> ClientHdf5Report:
    """Build a client delivery directly from immutable snapshot inputs."""

    destination = destination.resolve()
    dataset_path = dataset_path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".partial",
        dir=destination.parent,
        delete=False,
    )
    partial = Path(temporary_handle.name)
    temporary_handle.close()
    partial.unlink(missing_ok=True)
    video_bytes = 0
    try:
        taxonomy_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
        label_rows: list[tuple[Any, ...]] = []
        for item in taxonomies:
            taxonomy = public_taxonomy(item["payload"])
            identity = (str(taxonomy["taxonomy_id"]), str(taxonomy["version"]))
            if identity in taxonomy_by_identity:
                raise ValueError("客户交付包含重复 taxonomy 版本")
            taxonomy_by_identity[identity] = taxonomy
            seen_codes: set[str] = set()
            for is_fall, group in ((True, "fall"), (False, "non_fall")):
                for entry in taxonomy[group]:
                    code = str(entry["code"])
                    if not code or code in seen_codes:
                        raise ValueError("taxonomy 版本内的 code 必须非空且唯一")
                    seen_codes.add(code)
                    label_rows.append(
                        (
                            identity[0],
                            identity[1],
                            code,
                            str(entry["name"]),
                            is_fall,
                            bool(entry["active"]),
                        )
                    )

        with h5py.File(dataset_path, "r") as source:
            if set(source.keys()) != {"samples", "sequences", "annotations"}:
                raise ValueError("客户交付来源必须是严格 training_dataset")
            _values, source_sequences, _annotations = _validate_core(
                source, expected_profile=TRAINING_ARTIFACT_PROFILE
            )
            if len(recordings) != len(source_sequences):
                raise ValueError("每个 sequence 必须恰好对应一条冻结录制")
            with h5py.File(partial, "w", libver="latest") as output:
                for name, value in source.attrs.items():
                    output.attrs[name] = value
                output.attrs["artifact_profile"] = CLIENT_ARTIFACT_PROFILE
                output.attrs["client_delivery_contract_version"] = (
                    CLIENT_HDF5_CONTRACT_VERSION
                )
                output.attrs["snapshot_id"] = snapshot_id
                output.attrs["snapshot_content_fingerprint"] = (
                    snapshot_content_fingerprint
                )
                output.attrs["snapshot_created_at_utc"] = snapshot_created_at_utc
                output.attrs["video_contains_identifiable_participants"] = True
                for name in ("samples", "sequences", "annotations"):
                    source.copy(name, output, name=name)

                media = output.create_group("media", track_order=False)
                videos = media.create_group("videos", track_order=False)
                timing_group = media.create_group("timing", track_order=False)
                video_rows: list[tuple[Any, ...]] = []
                sequence_taxonomy_rows: list[tuple[Any, ...]] = []
                seen_sequences: set[int] = set()
                for recording in sorted(
                    recordings, key=lambda item: int(item["sequence_index"])
                ):
                    sequence_index = int(recording["sequence_index"])
                    if sequence_index in seen_sequences or not 0 <= sequence_index < len(
                        source_sequences
                    ):
                        raise ValueError("客户交付 sequence_index 重复或越界")
                    seen_sequences.add(sequence_index)
                    sequence = source_sequences[sequence_index]
                    expected_recording_id = f"cw12eu:{recording['recording_id']}"
                    if _text(sequence["recording_id"]) != expected_recording_id:
                        raise ValueError("冻结录制与 /sequences recording_id 不一致")
                    sample_count = int(sequence["sample_stop"] - sequence["sample_start"])
                    view = recording["view"]
                    if (
                        int(view["sequence_index"]) != sequence_index
                        or int(view["sample_count"]) != sample_count
                        or int(view["merged_sample_start"])
                        != int(sequence["sample_start"])
                        or int(view["merged_sample_stop"]) != int(sequence["sample_stop"])
                    ):
                        raise ValueError("冻结 view 与核心 sequence 范围不一致")
                    taxonomy_identity = (
                        str(view["taxonomy_id"]),
                        str(view["taxonomy_version"]),
                    )
                    if taxonomy_identity not in taxonomy_by_identity:
                        raise ValueError("冻结 view 引用了缺失的 taxonomy 版本")
                    sequence_taxonomy_rows.append((sequence_index, *taxonomy_identity))

                    mapping = _timing_mapping(view, sample_count)
                    name = str(sequence_index)
                    timing_dataset = timing_group.create_dataset(
                        name, data=mapping, dtype="<i8", track_times=False
                    )
                    timing_dataset.attrs["columns"] = json.dumps(
                        ["recording_time_ns", "media_time_ns"]
                    )
                    video = recording["video"]
                    size = int(video["size_bytes"])
                    expected_sha256 = str(video["sha256"])
                    if size <= 0 or len(expected_sha256) != 64:
                        raise ValueError("冻结视频描述符无效")
                    video_dataset = videos.create_dataset(
                        name,
                        shape=(size,),
                        dtype=np.uint8,
                        chunks=None,
                        compression=None,
                        track_times=False,
                    )
                    digest = hashlib.sha256()
                    cursor = 0
                    while cursor < size:
                        chunk = read_object_chunks(str(video["object_key"]), cursor)
                        if not chunk or cursor + len(chunk) > size:
                            raise ValueError("冻结视频对象提前结束或超出声明大小")
                        video_dataset[cursor : cursor + len(chunk)] = np.frombuffer(
                            chunk, dtype=np.uint8
                        )
                        digest.update(chunk)
                        cursor += len(chunk)
                    if digest.hexdigest() != expected_sha256:
                        raise ValueError("冻结视频对象 SHA-256 不一致")
                    output.flush()
                    offset = video_dataset.id.get_offset()
                    if offset is None or int(offset) < 0:
                        raise ValueError("视频 dataset 不是连续物理存储")
                    media_times = np.asarray(
                        view["video_frames"]["media_time_ns"], dtype=np.int64
                    )
                    media_duration_ns = int(media_times[-1])
                    if media_duration_ns < int(mapping[-1, 1]):
                        raise ValueError("视频媒体范围短于同步 timing")
                    video_rows.append(
                        (
                            sequence_index,
                            size,
                            int(offset),
                            expected_sha256,
                            str(video.get("content_type") or "video/mp4"),
                            "mp4",
                            media_duration_ns,
                            int(view["sample_zero_recording_time_ns"]),
                            int(view["sample_zero_video_media_time_ns"]),
                        )
                    )
                    video_bytes += size
                if seen_sequences != set(range(len(source_sequences))):
                    raise ValueError("客户交付没有一一覆盖全部 sequence")
                media.create_dataset(
                    "index",
                    data=np.asarray(video_rows, dtype=_video_index_dtype()),
                    track_times=False,
                )
                labels = output.create_group("labels", track_order=False)
                labels.create_dataset(
                    "catalog",
                    data=np.asarray(label_rows, dtype=_label_catalog_dtype()),
                    track_times=False,
                )
                labels.create_dataset(
                    "sequence_versions",
                    data=np.asarray(
                        sequence_taxonomy_rows, dtype=_sequence_taxonomy_dtype()
                    ),
                    track_times=False,
                )
                output.flush()
        validate_client_hdf5(partial)
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)

    return ClientHdf5Report(
        output_path=str(destination),
        size_bytes=destination.stat().st_size,
        sha256=sha256_path(destination),
        recording_count=len(recordings),
        video_bytes=video_bytes,
        dataset_bytes=dataset_path.stat().st_size,
    )


def validate_client_hdf5(path: Path) -> dict[str, Any]:
    """Validate all logical tables and each embedded MP4 physical byte range."""

    file_size = path.stat().st_size
    with h5py.File(path, "r") as handle:
        if set(handle.keys()) != {
            "samples",
            "sequences",
            "annotations",
            "media",
            "labels",
        }:
            raise ValueError("client_delivery 根结构无效")
        _values, sequences, annotations = _validate_core(
            handle, expected_profile=CLIENT_ARTIFACT_PROFILE
        )
        if _text(handle.attrs.get("client_delivery_contract_version", "")) != (
            CLIENT_HDF5_CONTRACT_VERSION
        ):
            raise ValueError("client delivery contract version 无效")
        if set(handle["media"].keys()) != {"index", "videos", "timing"}:
            raise ValueError("/media 结构无效")
        if set(handle["labels"].keys()) != {"catalog", "sequence_versions"}:
            raise ValueError("/labels 结构无效")
        index = handle["media/index"]
        _check_compound_dtype(
            index,
            (
                ("sequence_index", np.dtype("int32")),
                ("byte_length", np.dtype("int64")),
                ("file_offset", np.dtype("int64")),
                ("sha256", None),
                ("content_type", None),
                ("container", None),
                ("media_duration_ns", np.dtype("int64")),
                ("sample_zero_recording_time_ns", np.dtype("int64")),
                ("sample_zero_media_time_ns", np.dtype("int64")),
            ),
            label="/media/index",
        )
        index_rows = np.asarray(index)
        if len(index_rows) != len(sequences) or [
            int(row["sequence_index"]) for row in index_rows
        ] != list(range(len(sequences))):
            raise ValueError("/media/index 必须按 sequence 一一覆盖")
        expected_names = {str(index) for index in range(len(sequences))}
        if set(handle["media/videos"].keys()) != expected_names or set(
            handle["media/timing"].keys()
        ) != expected_names:
            raise ValueError("/media video/timing 路径与 sequence 不一致")

        physical_ranges: list[tuple[int, int, str]] = []
        for row in index_rows:
            sequence_index = int(row["sequence_index"])
            name = str(sequence_index)
            video = handle[f"media/videos/{name}"]
            size = int(row["byte_length"])
            offset = int(row["file_offset"])
            digest = _text(row["sha256"])
            if (
                video.dtype != np.dtype("uint8")
                or video.ndim != 1
                or len(video) != size
                or video.chunks is not None
                or video.compression is not None
                or video.id.get_offset() != offset
                or size <= 0
                or offset < 0
                or offset + size > file_size
                or len(digest) != 64
                or _text(row["content_type"]) != "video/mp4"
                or _text(row["container"]) != "mp4"
            ):
                raise ValueError("连续 MP4 dataset 或 index 描述符无效")
            physical_ranges.append((offset, size, digest))
            timing = handle[f"media/timing/{name}"]
            mapping = np.asarray(timing)
            sequence_length = int(
                sequences[sequence_index]["sample_stop"]
                - sequences[sequence_index]["sample_start"]
            )
            sequence_start = int(row["sample_zero_recording_time_ns"])
            sequence_stop = sequence_start + (sequence_length - 1) * 40_000_000
            if (
                timing.dtype != np.dtype("int64")
                or mapping.ndim != 2
                or mapping.shape[1] != 2
                or len(mapping) < 2
                or np.any(np.diff(mapping[:, 0]) <= 0)
                or np.any(np.diff(mapping[:, 1]) <= 0)
                or int(mapping[0, 0]) != sequence_start
                or int(mapping[-1, 0]) != sequence_stop
                or int(mapping[0, 1]) != int(row["sample_zero_media_time_ns"])
                or int(mapping[0, 1]) < 0
                or int(mapping[-1, 1]) > int(row["media_duration_ns"])
            ):
                raise ValueError("/media/timing 与 sequence 或媒体范围不一致")
        ordered_ranges = sorted(
            (offset, offset + size) for offset, size, _digest in physical_ranges
        )
        if any(
            stop > next_start
            for (_start, stop), (next_start, _next_stop) in zip(
                ordered_ranges, ordered_ranges[1:], strict=False
            )
        ):
            raise ValueError("视频物理范围互相重叠")

        catalog = handle["labels/catalog"]
        sequence_versions = handle["labels/sequence_versions"]
        _check_compound_dtype(
            catalog,
            (
                ("taxonomy_id", None),
                ("taxonomy_version", None),
                ("code", None),
                ("name", None),
                ("is_fall", np.dtype("bool")),
                ("active", np.dtype("bool")),
            ),
            label="/labels/catalog",
        )
        _check_compound_dtype(
            sequence_versions,
            (
                ("sequence_index", np.dtype("int32")),
                ("taxonomy_id", None),
                ("taxonomy_version", None),
            ),
            label="/labels/sequence_versions",
        )
        catalog_rows = np.asarray(catalog)
        version_rows = np.asarray(sequence_versions)
        if len(version_rows) != len(sequences) or [
            int(row["sequence_index"]) for row in version_rows
        ] != list(range(len(sequences))):
            raise ValueError("每个 sequence 必须恰好对应一个冻结 taxonomy 版本")
        catalog_codes: dict[tuple[str, str], set[str]] = {}
        seen_catalog_rows: set[tuple[str, str, str]] = set()
        for row in catalog_rows:
            identity = (_text(row["taxonomy_id"]), _text(row["taxonomy_version"]))
            key = (*identity, _text(row["code"]))
            if not all(key) or key in seen_catalog_rows or not _text(row["name"]):
                raise ValueError("/labels/catalog 身份、code 或 name 无效")
            seen_catalog_rows.add(key)
            catalog_codes.setdefault(identity, set()).add(key[2])
        version_by_sequence = {
            int(row["sequence_index"]): (
                _text(row["taxonomy_id"]),
                _text(row["taxonomy_version"]),
            )
            for row in version_rows
        }
        for row in annotations:
            if _text(row["kind"]) == "exclude":
                continue
            identity = version_by_sequence[int(row["sequence_index"])]
            if _text(row["code"]) not in catalog_codes.get(identity, set()):
                raise ValueError("annotation code 无法在该 sequence 的 taxonomy 中解析")

    with path.open("rb") as source:
        for offset, size, expected_digest in physical_ranges:
            source.seek(offset)
            observed_digest, observed_size = _sha256_stream(source, limit=size)
            if observed_size != size or observed_digest != expected_digest:
                raise ValueError("客户 H5 中的视频物理范围 SHA-256 不匹配")
    return {
        "hdf5_schema_version": CORE_DATASET_SCHEMA_VERSION,
        "artifact_profile": CLIENT_ARTIFACT_PROFILE,
        "contract_version": CLIENT_HDF5_CONTRACT_VERSION,
        "recording_count": len(sequences),
        "annotation_count": len(annotations),
        "size_bytes": file_size,
    }
