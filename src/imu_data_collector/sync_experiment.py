"""同步实验观察、主机时间比较和 CW12EU-T 尾部候选分析。"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    SyncExperimentDocument,
    SyncExperimentSource,
    SyncObservation,
)

SYNC_EXPERIMENT_SCHEMA_VERSION = "1.0.0"
ONE_VIDEO_FRAME_NS = 1e9 / 30.0
PEAK_RECOMMENDATION_ALGORITHM = "tap_onset_v2"


def sync_experiment_path(data_root: Path, experiment_id: str) -> Path:
    """返回受约束的实验 JSON 路径，避免实验编号逃逸数据根目录。"""

    validated = SyncExperimentDocument(experiment_id=experiment_id)
    directory = data_root / "_diagnostics" / validated.experiment_id
    return directory / f"{validated.experiment_id}.sync-experiment.json"


def load_sync_experiment(data_root: Path, experiment_id: str) -> SyncExperimentDocument:
    path = sync_experiment_path(data_root, experiment_id)
    if not path.is_file():
        return SyncExperimentDocument(experiment_id=experiment_id)
    with path.open("r", encoding="utf-8") as handle:
        document = SyncExperimentDocument.model_validate(json.load(handle))
    if document.experiment_id != experiment_id:
        raise ValueError("同步实验文件中的 experiment_id 与路径不一致")
    return document


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _recording_times(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with h5py.File(path, "r") as handle:
        if "video/frames/recording_time_ns" not in handle:
            raise ValueError(f"录制缺少视频逐帧时间：{path}")
        video_time = np.asarray(
            handle["video/frames/recording_time_ns"], dtype=np.int64
        )
        sample_time = np.asarray(
            handle["imu/samples/time_monotonic_ns"], dtype=np.int64
        )
        recording_start = int(handle.attrs["recording_start_monotonic_ns"])
    return video_time, sample_time - recording_start


def _source_for_paths(
    recording_id: str,
    h5_path: Path,
    mkv_path: Path,
    existing: SyncExperimentSource | None,
) -> SyncExperimentSource:
    if not h5_path.is_file() or not mkv_path.is_file():
        raise ValueError(f"同步实验录制文件不完整：{recording_id}")
    h5_size = h5_path.stat().st_size
    mkv_size = mkv_path.stat().st_size
    if (
        existing is not None
        and Path(existing.h5_path) == h5_path
        and Path(existing.mkv_path) == mkv_path
        and existing.h5_size_bytes == h5_size
        and existing.mkv_size_bytes == mkv_size
    ):
        return existing
    return SyncExperimentSource(
        recording_id=recording_id,
        h5_path=str(h5_path),
        mkv_path=str(mkv_path),
        h5_size_bytes=h5_size,
        mkv_size_bytes=mkv_size,
        h5_sha256=sha256_file(h5_path),
        mkv_sha256=sha256_file(mkv_path),
    )


def save_sync_experiment(
    data_root: Path,
    document: SyncExperimentDocument,
    recording_paths: dict[str, tuple[Path, Path]],
) -> SyncExperimentDocument:
    """核对观察索引、补齐精确时间和来源哈希后原子保存实验文件。"""

    existing_document = load_sync_experiment(data_root, document.experiment_id)
    observation_ids = [item.observation_id for item in document.observations]
    if len(set(observation_ids)) != len(observation_ids):
        raise ValueError("同步实验 observation_id 必须唯一")

    by_recording: dict[str, list[SyncObservation]] = defaultdict(list)
    for observation in document.observations:
        if observation.recording_id not in recording_paths:
            raise ValueError(f"找不到同步实验录制：{observation.recording_id}")
        by_recording[observation.recording_id].append(observation)

    enriched: list[SyncObservation] = []
    for recording_id, observations in by_recording.items():
        h5_path, _mkv_path = recording_paths[recording_id]
        video_time, imu_time = _recording_times(h5_path)
        for observation in observations:
            if observation.video_frame_index >= len(video_time):
                raise ValueError(
                    f"{observation.observation_id} 的视频帧超出范围"
                )
            if observation.imu_sample_index >= len(imu_time):
                raise ValueError(
                    f"{observation.observation_id} 的 IMU 样本超出范围"
                )
            enriched.append(
                observation.model_copy(
                    update={
                        "video_time_ns": int(video_time[observation.video_frame_index]),
                        "imu_time_ns": int(imu_time[observation.imu_sample_index]),
                    }
                )
            )
    enriched.sort(key=lambda item: (item.recording_id, item.video_time_ns))

    previous_sources = {item.recording_id: item for item in existing_document.sources}
    sources = [
        _source_for_paths(
            recording_id,
            recording_paths[recording_id][0],
            recording_paths[recording_id][1],
            previous_sources.get(recording_id),
        )
        for recording_id in sorted(by_recording)
    ]
    saved = document.model_copy(
        update={
            "schema_version": SYNC_EXPERIMENT_SCHEMA_VERSION,
            "revision": existing_document.revision + 1,
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "observations": enriched,
            "sources": sources,
        }
    )
    _atomic_write_json(
        sync_experiment_path(data_root, saved.experiment_id),
        saved.model_dump(mode="json"),
    )
    return saved


def read_frame_times(path: Path) -> dict[str, Any]:
    with h5py.File(path, "r") as handle:
        frames = handle["video/frames"]
        times = np.asarray(frames["recording_time_ns"], dtype=np.int64)
        # 1.2.0 起，交付 MKV 从零开始；旧版 MKV 保留绝对单调时钟 PTS。
        media_times = np.asarray(
            frames["media_time_ns"]
            if "media_time_ns" in frames
            else frames["pts_monotonic_ns"]
            if "pts_monotonic_ns" in frames
            else frames["recording_time_ns"],
            dtype=np.int64,
        )
        durations = np.asarray(frames["duration_ns"], dtype=np.int64)
        key_frames = np.asarray(frames["key_frame"], dtype=np.bool_)
    return {
        "frame_count": len(times),
        "time_ns": times.tolist(),
        "media_time_ns": media_times.tolist(),
        "duration_ns": durations.tolist(),
        "key_frame": key_frames.tolist(),
    }


def _peak_candidates(raw_counts: np.ndarray, absolute_indices: np.ndarray) -> list[int]:
    if len(raw_counts) < 3:
        return []
    accel = raw_counts[:, :3].astype(np.float64)
    score = np.linalg.norm(np.diff(accel, axis=0, prepend=accel[[0]]), axis=1)
    local = np.flatnonzero(
        (score[1:-1] > score[:-2])
        & (score[1:-1] >= score[2:])
        & (score[1:-1] > 0)
    ) + 1
    ranked = sorted(local.tolist(), key=lambda index: float(score[index]), reverse=True)
    selected: list[int] = []
    for index in ranked:
        if all(abs(index - previous) >= 2 for previous in selected):
            selected.append(index)
        if len(selected) == 10:
            break
    return [int(absolute_indices[index]) for index in sorted(selected)]


def _candidate_peak_details(
    raw_counts: np.ndarray,
    absolute_indices: np.ndarray,
    imu_time_ns: np.ndarray,
    video_time_ns: int,
    expected_video_minus_imu_ns: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """为轻拍候选计算显著性和时间先验分数，只自动推荐而不自动保存。"""

    accel = raw_counts[:, :3].astype(np.float64)
    strengths = np.linalg.norm(
        np.diff(accel, axis=0, prepend=accel[[0]]), axis=1
    )
    median = float(np.median(strengths))
    mad = float(np.median(np.abs(strengths - median)))
    robust_scale = max(1.4826 * mad, 1.0)
    robust_z_by_local_index = np.maximum(0.0, (strengths - median) / robust_scale)
    peak_indices = _peak_candidates(raw_counts, absolute_indices)
    if not peak_indices:
        return [], {
            "algorithm": PEAK_RECOMMENDATION_ALGORITHM,
            "sample_index": None,
            "confidence": "low",
            "reason": "窗口内没有可辨认的轻拍响应",
            "expected_video_minus_imu_ns": expected_video_minus_imu_ns,
            "score_margin_ratio": None,
        }

    # 正式同步的共同语义是“视频首接触帧 ↔ IMU 首个明显响应”，不是最大峰。
    # 每个局部峰向前回溯至同一突变簇的首个显著样本；峰值仍保留给人工复核。
    candidate_basis: dict[int, str] = {
        sample_index: "local_peak" for sample_index in peak_indices
    }
    for peak_sample_index in peak_indices:
        peak_local_index = int(
            np.searchsorted(absolute_indices, peak_sample_index)
        )
        peak_strength = float(strengths[peak_local_index])
        onset_threshold = max(median + 4.0 * robust_scale, peak_strength * 0.05)
        onset_local_index = peak_local_index
        for preceding in range(peak_local_index - 1, max(-1, peak_local_index - 7), -1):
            if float(strengths[preceding]) < onset_threshold:
                break
            onset_local_index = preceding
        onset_sample_index = int(absolute_indices[onset_local_index])
        candidate_basis[onset_sample_index] = "event_onset"

    projected_sample_index: int | None = None
    if expected_video_minus_imu_ns is not None:
        expected_imu_time_ns = video_time_ns - expected_video_minus_imu_ns
        projected_local_index = int(
            np.argmin(np.abs(imu_time_ns - expected_imu_time_ns))
        )
        nearby_left = max(0, projected_local_index - 3)
        nearby_right = min(len(raw_counts), projected_local_index + 4)
        # 时间模型只能在附近确有冲击事件时补充代表样本，不能凭先验在平静区造峰。
        if float(np.max(robust_z_by_local_index[nearby_left:nearby_right])) >= 4.0:
            projected_sample_index = int(absolute_indices[projected_local_index])
            candidate_basis.setdefault(projected_sample_index, "timing_projection")
    candidate_indices = list(candidate_basis)
    strength_rank = {
        index: rank
        for rank, index in enumerate(
            sorted(
                candidate_indices,
                key=lambda sample_index: float(
                    strengths[int(np.searchsorted(absolute_indices, sample_index))]
                ),
                reverse=True,
            ),
            start=1,
        )
    }
    time_scale_ns = 80_000_000 if expected_video_minus_imu_ns is not None else 250_000_000
    details: list[dict[str, Any]] = []
    for sample_index in candidate_indices:
        local_index = int(np.searchsorted(absolute_indices, sample_index))
        candidate_time = int(imu_time_ns[local_index])
        offset_ns = int(video_time_ns - candidate_time)
        residual_ns = (
            offset_ns - expected_video_minus_imu_ns
            if expected_video_minus_imu_ns is not None
            else offset_ns
        )
        strength = float(strengths[local_index])
        robust_z = float(robust_z_by_local_index[local_index])
        event_left = max(0, local_index - 3)
        event_right = min(len(raw_counts), local_index + 4)
        event_robust_z = float(
            np.max(robust_z_by_local_index[event_left:event_right])
        )
        timing_weight = math.exp(-0.5 * (residual_ns / time_scale_ns) ** 2)
        recommendation_score = math.log1p(event_robust_z) * timing_weight
        details.append(
            {
                "sample_index": int(sample_index),
                "time_ns": candidate_time,
                "time_s": candidate_time / 1e9,
                "video_minus_imu_ms": offset_ns / 1e6,
                "expected_offset_residual_ms": residual_ns / 1e6,
                "accel_delta_score": strength,
                "robust_z": robust_z,
                "event_robust_z": event_robust_z,
                "strength_rank": strength_rank[sample_index],
                "recommendation_score": recommendation_score,
                "selection_basis": candidate_basis[sample_index],
            }
        )

    # 只在“首个响应”候选之间自动排名；最大峰和时间投影点仅供核对。
    # 如果极短冲击使首响应与峰值落在同一样本，该样本也会标为 event_onset。
    onset_details = [
        item for item in details if item["selection_basis"] == "event_onset"
    ]
    ranked = sorted(
        onset_details or details,
        key=lambda item: (item["recommendation_score"], item["accel_delta_score"]),
        reverse=True,
    )
    for rank, item in enumerate(ranked, start=1):
        item["recommendation_rank"] = rank
    chosen = ranked[0]
    second_score = float(ranked[1]["recommendation_score"]) if len(ranked) > 1 else 0.0
    margin_ratio = (
        float(chosen["recommendation_score"]) / second_score
        if second_score > 0
        else None
    )
    residual_limit_high = 120.0 if expected_video_minus_imu_ns is not None else 250.0
    residual_limit_medium = 200.0 if expected_video_minus_imu_ns is not None else 400.0
    residual_ms = abs(float(chosen["expected_offset_residual_ms"]))
    robust_z = float(chosen["event_robust_z"])
    unambiguous_high = margin_ratio is None or margin_ratio >= 1.5
    unambiguous_medium = margin_ratio is None or margin_ratio >= 1.2
    if robust_z >= 8 and unambiguous_high and residual_ms <= residual_limit_high:
        confidence = "high"
        reason = "首个响应显著、候选区分清楚且符合当前时间模型"
    elif robust_z >= 4 and unambiguous_medium and residual_ms <= residual_limit_medium:
        confidence = "medium"
        reason = "存在可用首响应，但显著性、候选间隔或时间位置需要人工复核"
    else:
        confidence = "low"
        reason = "峰值偏弱、候选接近或时间位置偏离预期，必须人工选择"
    details.sort(key=lambda item: item["time_ns"])
    return details, {
        "algorithm": PEAK_RECOMMENDATION_ALGORITHM,
        "sample_index": int(chosen["sample_index"]),
        "confidence": confidence,
        "reason": reason,
        "expected_video_minus_imu_ns": expected_video_minus_imu_ns,
        "score_margin_ratio": margin_ratio,
    }


def read_sync_window(
    path: Path,
    frame_index: int,
    radius_seconds: float,
    expected_video_minus_imu_ns: int | None = None,
) -> dict[str, Any]:
    if not 0.1 <= radius_seconds <= 5.0:
        raise ValueError("同步窗口半径必须在 0.1 到 5 秒之间")
    with h5py.File(path, "r") as handle:
        video_time = np.asarray(
            handle["video/frames/recording_time_ns"], dtype=np.int64
        )
        if not 0 <= frame_index < len(video_time):
            raise ValueError("视频帧号超出范围")
        recording_start = int(handle.attrs["recording_start_monotonic_ns"])
        imu_time = (
            np.asarray(handle["imu/samples/time_monotonic_ns"], dtype=np.int64)
            - recording_start
        )
        raw_counts = np.asarray(handle["imu/samples/raw_counts"], dtype=np.int16)
        trailer = np.asarray(handle["imu/samples/trailer"], dtype=np.uint8)
        packet_index = np.asarray(handle["imu/samples/packet_index"], dtype=np.int64)
        sample_in_packet = np.asarray(
            handle["imu/samples/sample_in_packet"], dtype=np.uint16
        )
    center = int(video_time[frame_index])
    radius_ns = int(round(radius_seconds * 1e9))
    left = int(np.searchsorted(imu_time, center - radius_ns, side="left"))
    right = int(np.searchsorted(imu_time, center + radius_ns, side="right"))
    indices = np.arange(left, right, dtype=np.int64)
    values = raw_counts[left:right]
    candidate_peaks, recommendation = _candidate_peak_details(
        values,
        indices,
        imu_time[left:right],
        center,
        expected_video_minus_imu_ns,
    )
    return {
        "video_frame_index": frame_index,
        "video_time_ns": center,
        "radius_seconds": radius_seconds,
        "sample_index": indices.tolist(),
        "time_ns": imu_time[left:right].tolist(),
        "time_s": (imu_time[left:right] / 1e9).tolist(),
        "raw_counts": values.tolist(),
        "trailer": trailer[left:right].tolist(),
        "packet_index": packet_index[left:right].tolist(),
        "sample_in_packet": sample_in_packet[left:right].tolist(),
        "candidate_sample_index": [item["sample_index"] for item in candidate_peaks],
        "candidate_peaks": candidate_peaks,
        "recommendation": recommendation,
    }


def _error_metrics(errors_ns: list[float]) -> dict[str, float | int | None]:
    if not errors_ns:
        return {
            "count": 0,
            "median_signed_ms": None,
            "median_absolute_ms": None,
            "p95_absolute_ms": None,
            "maximum_absolute_ms": None,
            "rms_ms": None,
        }
    errors = np.asarray(errors_ns, dtype=np.float64)
    absolute = np.abs(errors)
    return {
        "count": len(errors),
        "median_signed_ms": float(np.median(errors) / 1e6),
        "median_absolute_ms": float(np.median(absolute) / 1e6),
        "p95_absolute_ms": float(np.percentile(absolute, 95) / 1e6),
        "maximum_absolute_ms": float(np.max(absolute) / 1e6),
        "rms_ms": float(np.sqrt(np.mean(errors**2)) / 1e6),
    }


def _passes_reference(metrics: dict[str, float | int | None]) -> bool:
    median = metrics["median_absolute_ms"]
    p95 = metrics["p95_absolute_ms"]
    return bool(
        metrics["count"]
        and median is not None
        and p95 is not None
        and median <= ONE_VIDEO_FRAME_NS / 1e6
        and p95 <= 2 * ONE_VIDEO_FRAME_NS / 1e6
    )


def _fit_affine(imu: np.ndarray, video: np.ndarray) -> tuple[float, float]:
    centered = imu - imu[0]
    coefficients = np.polyfit(centered, video, 1)
    scale = float(coefficients[0])
    offset = float(coefficients[1] - scale * imu[0])
    return scale, offset


def _method_report(observations: list[SyncObservation]) -> dict[str, Any]:
    grouped: dict[str, list[SyncObservation]] = defaultdict(list)
    for observation in observations:
        grouped[observation.recording_id].append(observation)
    delta_by_recording = {
        recording_id: np.asarray(
            [item.video_time_ns - item.imu_time_ns for item in items],
            dtype=np.float64,
        )
        for recording_id, items in grouped.items()
    }
    all_deltas = [float(item.video_time_ns - item.imu_time_ns) for item in observations]
    host = _error_metrics(all_deltas)

    global_errors: list[float] = []
    for recording_id, items in grouped.items():
        training = np.concatenate(
            [values for key, values in delta_by_recording.items() if key != recording_id]
        ) if len(grouped) > 1 else np.empty(0)
        if not len(training):
            continue
        offset = float(np.median(training))
        global_errors.extend(
            float(item.video_time_ns - (item.imu_time_ns + offset)) for item in items
        )

    per_recording_errors: list[float] = []
    affine_errors: list[float] = []
    affine_fits: dict[str, dict[str, float | int | bool]] = {}
    for recording_id, items in grouped.items():
        deltas = delta_by_recording[recording_id]
        if len(items) >= 2:
            for index, item in enumerate(items):
                other = np.delete(deltas, index)
                offset = float(np.median(other))
                per_recording_errors.append(
                    float(item.video_time_ns - (item.imu_time_ns + offset))
                )
        imu_all = np.asarray([item.imu_time_ns for item in items], dtype=np.float64)
        video_all = np.asarray([item.video_time_ns for item in items], dtype=np.float64)
        if len(items) >= 2 and float(np.ptp(imu_all)) > 0:
            scale, offset = _fit_affine(imu_all, video_all)
            predicted = scale * imu_all + offset
            residual = video_all - predicted
            affine_fits[recording_id] = {
                "anchor_count": len(items),
                "scale": scale,
                "offset_ms": offset / 1e6,
                "residual_rms_ms": float(np.sqrt(np.mean(residual**2)) / 1e6),
                "drift_over_span_ms": float((scale - 1.0) * np.ptp(imu_all) / 1e6),
                "scale_plausible": 0.995 <= scale <= 1.005,
            }
        if len(items) >= 3:
            for index, item in enumerate(items):
                keep = np.arange(len(items)) != index
                scale, offset = _fit_affine(imu_all[keep], video_all[keep])
                affine_errors.append(
                    float(item.video_time_ns - (scale * item.imu_time_ns + offset))
                )

    methods = {
        "host_only": _error_metrics(all_deltas),
        "global_fixed_offset_leave_one_recording_out": _error_metrics(global_errors),
        "per_recording_offset_leave_one_anchor_out": _error_metrics(
            per_recording_errors
        ),
        "per_recording_affine_leave_one_anchor_out": _error_metrics(affine_errors),
    }
    for metrics in methods.values():
        metrics["meets_30fps_reference"] = _passes_reference(metrics)
    recording_offsets = {
        recording_id: {
            "anchor_count": len(values),
            "median_offset_ms": float(np.median(values) / 1e6),
            "minimum_offset_ms": float(np.min(values) / 1e6),
            "maximum_offset_ms": float(np.max(values) / 1e6),
        }
        for recording_id, values in delta_by_recording.items()
    }
    return {
        "reference": {
            "video_fps": 30,
            "one_frame_ms": ONE_VIDEO_FRAME_NS / 1e6,
            "median_absolute_limit_ms": ONE_VIDEO_FRAME_NS / 1e6,
            "p95_absolute_limit_ms": 2 * ONE_VIDEO_FRAME_NS / 1e6,
        },
        "methods": methods,
        "recording_offsets": recording_offsets,
        "affine_fits": affine_fits,
        "host_only_raw_metrics": host,
    }


def _top_deltas(values: np.ndarray) -> list[dict[str, int]]:
    if len(values) < 2:
        return []
    counts = Counter(int(item) for item in np.diff(values.astype(np.int64)))
    return [
        {"delta": delta, "count": count}
        for delta, count in counts.most_common(8)
    ]


def _integer_candidate(values: np.ndarray) -> dict[str, Any]:
    if len(values) < 2:
        return {
            "unique_count": len(np.unique(values)),
            "monotonic_fraction": None,
            "unit_step_fraction": None,
            "top_deltas": [],
        }
    delta = np.diff(values.astype(np.int64))
    return {
        "unique_count": len(np.unique(values)),
        "monotonic_fraction": float(np.mean(delta >= 0)),
        "unit_step_fraction": float(np.mean(delta == 1)),
        "top_deltas": _top_deltas(values),
    }


def _trailer_report(sources: list[SyncExperimentSource]) -> dict[str, Any]:
    recordings: dict[str, Any] = {}
    candidate_votes: Counter[str] = Counter()
    for source in sources:
        with h5py.File(source.h5_path, "r") as handle:
            trailer = np.asarray(handle["imu/samples/trailer"], dtype=np.uint8)
        if trailer.ndim != 2 or trailer.shape[1] != 4:
            recordings[source.recording_id] = {"error": "trailer 不是 N×4"}
            continue
        contiguous = np.ascontiguousarray(trailer)
        uint32_be = np.frombuffer(contiguous.tobytes(), dtype=">u4").astype(np.uint64)
        uint32_le = np.frombuffer(contiguous.tobytes(), dtype="<u4").astype(np.uint64)
        uint16_be = np.frombuffer(contiguous.tobytes(), dtype=">u2").reshape(-1, 2)
        uint16_le = np.frombuffer(contiguous.tobytes(), dtype="<u2").reshape(-1, 2)
        candidates = {
            "uint32_be": _integer_candidate(uint32_be),
            "uint32_le": _integer_candidate(uint32_le),
            "uint16_be_0": _integer_candidate(uint16_be[:, 0]),
            "uint16_be_1": _integer_candidate(uint16_be[:, 1]),
            "uint16_le_0": _integer_candidate(uint16_le[:, 0]),
            "uint16_le_1": _integer_candidate(uint16_le[:, 1]),
        }
        for name, candidate in candidates.items():
            monotonic = candidate["monotonic_fraction"]
            unit = candidate["unit_step_fraction"]
            if monotonic is not None and unit is not None and monotonic >= 0.99 and unit >= 0.95:
                candidate_votes[name] += 1
        recordings[source.recording_id] = {
            "sample_count": len(trailer),
            "byte_unique_counts": [
                int(len(np.unique(trailer[:, column]))) for column in range(4)
            ],
            "byte_change_counts": [
                int(np.count_nonzero(np.diff(trailer[:, column].astype(np.int64))))
                for column in range(4)
            ],
            "integer_candidates": candidates,
        }
    unanimous = [
        name for name, count in candidate_votes.items() if count == len(recordings) and count > 0
    ]
    return {
        "recordings": recordings,
        "counter_candidates_consistent_in_all_recordings": unanimous,
        "conclusion": (
            "仅列出统计候选；没有供应商依据或独立实验时，不确认任何 trailer 语义。"
        ),
    }


def analyze_sync_experiment(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        document = SyncExperimentDocument.model_validate(json.load(handle))
    source_checks = []
    for source in document.sources:
        h5_path = Path(source.h5_path)
        mkv_path = Path(source.mkv_path)
        source_checks.append(
            {
                "recording_id": source.recording_id,
                "h5_exists": h5_path.is_file(),
                "mkv_exists": mkv_path.is_file(),
                "h5_sha256_matches": h5_path.is_file()
                and sha256_file(h5_path) == source.h5_sha256,
                "mkv_sha256_matches": mkv_path.is_file()
                and sha256_file(mkv_path) == source.mkv_sha256,
            }
        )
    return {
        "schema_version": SYNC_EXPERIMENT_SCHEMA_VERSION,
        "experiment_id": document.experiment_id,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "observation_count": len(document.observations),
        "recording_count": len(document.sources),
        "source_checks": source_checks,
        "timing": _method_report(document.observations),
        "trailer": _trailer_report(document.sources),
    }


def _format_metric(value: float | int | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        return "—"
    return f"{value:.3f}"


def write_sync_experiment_report(path: Path) -> tuple[Path, Path]:
    report = analyze_sync_experiment(path)
    json_path = path.with_name(f"{report['experiment_id']}.sync-analysis.json")
    markdown_path = path.with_name(f"{report['experiment_id']}.sync-analysis.md")
    _atomic_write_json(json_path, report)

    lines = [
        f"# 同步实验报告：{report['experiment_id']}",
        "",
        f"- 生成时间：`{report['generated_at_utc']}`",
        f"- 录制数：{report['recording_count']}",
        f"- 人工观察数：{report['observation_count']}",
        "- 误差定义：`视频接触帧 PTS - 映射后的 IMU 峰时间`。",
        "- 参考尺度：30 FPS 一帧约 33.333 ms；该尺度不是毫秒级地面真值。",
        "",
        "## 方法比较",
        "",
        "| 方法 | 样本 | 中位绝对误差 ms | P95 ms | 最大 ms | RMS ms | 达到参考 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    display_names = {
        "host_only": "主机时间，不校正",
        "global_fixed_offset_leave_one_recording_out": "跨录制固定偏移（留一录制）",
        "per_recording_offset_leave_one_anchor_out": "每条固定偏移（留一锚点）",
        "per_recording_affine_leave_one_anchor_out": "每条仿射拟合（留一锚点）",
    }
    for name, metrics in report["timing"]["methods"].items():
        lines.append(
            "| "
            + " | ".join(
                (
                    display_names[name],
                    _format_metric(metrics["count"]),
                    _format_metric(metrics["median_absolute_ms"]),
                    _format_metric(metrics["p95_absolute_ms"]),
                    _format_metric(metrics["maximum_absolute_ms"]),
                    _format_metric(metrics["rms_ms"]),
                    "是" if metrics["meets_30fps_reference"] else "否",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 每条录制的主机偏移",
            "",
            "| 录制 | 锚点 | 中位偏移 ms | 最小 ms | 最大 ms |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for recording_id, metrics in report["timing"]["recording_offsets"].items():
        lines.append(
            f"| `{recording_id}` | {metrics['anchor_count']} | "
            f"{metrics['median_offset_ms']:.3f} | {metrics['minimum_offset_ms']:.3f} | "
            f"{metrics['maximum_offset_ms']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Trailer 结论边界",
            "",
            report["trailer"]["conclusion"],
            "",
            "跨全部录制一致的计数器候选："
            + (
                ", ".join(
                    f"`{item}`"
                    for item in report["trailer"][
                        "counter_candidates_consistent_in_all_recordings"
                    ]
                )
                or "无"
            ),
            "",
            "## 决策门禁",
            "",
            "本报告只排名候选方法，不自动写入正式同步模型。应结合逐帧复核后，再决定 "
            "host-only、固定偏移或每条录制锚点拟合。若所有方法均未达到参考范围，先继续调查 "
            "BLE 批量延迟与 trailer，不冻结正式时间字段。",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path
