"""从 IMU-only HDF5 生成保守、可追溯的设备表征报告。"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from imu_data_collector.config import ImuSettings
from imu_data_collector.hdf5_store import CaptureH5Writer, sha256_file

AXES = ("ax", "ay", "az", "gx", "gy", "gz")
POSE_PAIRS = {
    "x": ("interface_opposite_face_up", "interface_face_up"),
    "z": ("button_face_up", "button_face_down"),
    "y_exploratory": (
        "pendant_end_up_exploratory",
        "pendant_end_down_exploratory",
    ),
}


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _axis_stats(values: np.ndarray) -> dict[str, dict[str, float]]:
    if not len(values):
        return {}
    medians = np.median(values, axis=0)
    mad = np.median(np.abs(values - medians), axis=0)
    means = np.mean(values, axis=0)
    std = np.std(values, axis=0)
    return {
        axis: {
            "mean_counts": float(means[index]),
            "median_counts": float(medians[index]),
            "mad_counts": float(mad[index]),
            "std_counts": float(std[index]),
            "min_counts": float(np.min(values[:, index])),
            "p01_counts": float(np.percentile(values[:, index], 1)),
            "p99_counts": float(np.percentile(values[:, index], 99)),
            "max_counts": float(np.max(values[:, index])),
        }
        for index, axis in enumerate(AXES)
    }


def analyze_characterization(path: Path) -> dict[str, Any]:
    """分析包形态、接收间隔、各物理阶段和保守的比例候选。"""

    with h5py.File(path, "r") as handle:
        offsets = np.asarray(handle["imu/packets/payload_offsets"], dtype=np.int64)
        receive = np.asarray(handle["imu/packets/receive_time_ns"], dtype=np.int64)
        packet_sample_counts = np.asarray(
            handle["imu/packets/sample_count"], dtype=np.int64
        )
        sample_times = np.asarray(
            handle["imu/samples/recording_time_ns"], dtype=np.int64
        )
        raw = np.asarray(handle["imu/samples/raw_counts"], dtype=np.int16)
        trailer = np.asarray(handle["imu/samples/trailer"], dtype=np.uint8)
        stages = handle["experiment/stages"]
        stage_rows = [
            {
                "stage_code": _text(stages["stage_code"][index]),
                "start_ns": int(stages["start_ns"][index]),
                "end_ns": int(stages["end_ns"][index]),
                "reliability": _text(stages["reliability"][index]),
                "notes": _text(stages["notes"][index]),
            }
            for index in range(len(stages["stage_code"]))
        ]
        observed_rate = float(handle["imu"].attrs.get("observed_rate_hz", 0.0))
        parse_errors = int(handle.attrs.get("parse_error_count", 0))
        callback_drops = int(handle["imu"].attrs.get("callback_drops", -1))
        callback_drops_status = str(
            handle["imu"].attrs.get(
                "callback_drops_status",
                "measured" if callback_drops >= 0 else "unknown",
            )
        )

    packet_lengths = np.diff(offsets)
    packet_intervals = np.diff(receive) / 1e9
    median_packet_interval = (
        float(np.median(packet_intervals)) if len(packet_intervals) else 0.0
    )
    gap_threshold = max(1.5, median_packet_interval * 1.5)
    stage_metrics: dict[str, Any] = {}
    stage_medians: dict[str, np.ndarray] = {}
    for row in stage_rows:
        mask = (sample_times >= row["start_ns"]) & (sample_times < row["end_ns"])
        values = raw[mask]
        window_stats: list[dict[str, Any]] = []
        window_ns = 60_000_000_000
        for window_start in range(row["start_ns"], row["end_ns"], window_ns):
            window_end = min(row["end_ns"], window_start + window_ns)
            window_mask = (sample_times >= window_start) & (sample_times < window_end)
            window_values = raw[window_mask]
            if not len(window_values):
                continue
            medians = np.median(window_values, axis=0)
            window_stats.append(
                {
                    "start_ns": window_start,
                    "end_ns": window_end,
                    "sample_count": int(len(window_values)),
                    "median_counts": {
                        axis: float(medians[index])
                        for index, axis in enumerate(AXES)
                    },
                }
            )
        median_span = {
            axis: float(
                max(item["median_counts"][axis] for item in window_stats)
                - min(item["median_counts"][axis] for item in window_stats)
            )
            for axis in AXES
        } if window_stats else {}
        stage_metrics[row["stage_code"]] = {
            **row,
            "sample_count": int(len(values)),
            "axes": _axis_stats(values.astype(np.float64)),
            "window_seconds": 60,
            "window_stats": window_stats,
            "window_median_span_counts": median_span,
        }
        if len(values):
            stage_medians[row["stage_code"]] = np.median(values, axis=0)

    candidates: dict[str, Any] = {}
    for axis, (positive, negative) in POSE_PAIRS.items():
        if positive not in stage_medians or negative not in stage_medians:
            continue
        delta = stage_medians[positive][:3] - stage_medians[negative][:3]
        index = int(np.argmax(np.abs(delta)))
        upper = float(stage_medians[positive][index])
        lower = float(stage_medians[negative][index])
        candidates[axis] = {
            "dominant_raw_column_candidate": AXES[index],
            "counts_per_g_candidate": abs(upper - lower) / 2.0,
            "bias_counts_candidate": (upper + lower) / 2.0,
            "sign_from_named_pose": 1 if upper > lower else -1,
            "status": "exploratory_unverified" if "exploratory" in axis else "candidate",
            "source_stages": [positive, negative],
        }

    window_rates: list[dict[str, float | int]] = []
    window_packets = 60
    for start in range(0, len(receive), window_packets):
        stop = min(len(receive), start + window_packets)
        window_receive = receive[start:stop]
        if len(window_receive) < 2:
            continue
        window_interval = float(np.median(np.diff(window_receive)))
        coverage_ns = int(window_receive[-1] - window_receive[0] + window_interval)
        rate = (
            float(packet_sample_counts[start:stop].sum()) / (coverage_ns / 1e9)
            if coverage_ns > 0
            else 0.0
        )
        window_rates.append(
            {
                "packet_start": start,
                "packet_stop": stop,
                "rate_hz": rate,
            }
        )
    rate_drift_ppm = (
        (float(window_rates[-1]["rate_hz"]) / float(window_rates[0]["rate_hz"]) - 1)
        * 1e6
        if len(window_rates) >= 2 and float(window_rates[0]["rate_hz"]) > 0
        else None
    )
    window_rate_values = [float(item["rate_hz"]) for item in window_rates]

    trailer_metrics = {
        f"byte_{index}": {
            "min": int(trailer[:, index].min()) if len(trailer) else 0,
            "max": int(trailer[:, index].max()) if len(trailer) else 0,
            "unique_count": int(len(np.unique(trailer[:, index]))) if len(trailer) else 0,
            "change_count": int(np.count_nonzero(np.diff(trailer[:, index])))
            if len(trailer) > 1
            else 0,
        }
        for index in range(4)
    }
    interval_ms = np.diff(sample_times) / 1e6
    report: dict[str, Any] = {
        "report_schema": "cw12eu_characterization_report_v1",
        "说明": "本报告只给出候选结论；未经独立尺度和方向验证，不得写入训练 SI 数据。",
        "source_h5": str(path),
        "source_h5_sha256": sha256_file(path),
        "training_eligible": False,
        "calibration_status": "partial_unverified",
        "packet_metrics": {
            "packet_count": int(len(packet_lengths)),
            "sample_count": int(len(raw)),
            "payload_length_histogram": {
                str(int(value)): int(count)
                for value, count in zip(
                    *np.unique(packet_lengths, return_counts=True), strict=True
                )
            },
            "receive_interval_median_s": median_packet_interval,
            "receive_interval_p95_s": float(np.percentile(packet_intervals, 95))
            if len(packet_intervals)
            else 0.0,
            "receive_interval_max_s": float(packet_intervals.max())
            if len(packet_intervals)
            else 0.0,
            "gap_threshold_s": gap_threshold,
            "gap_count": int(np.count_nonzero(packet_intervals > gap_threshold)),
            "parse_error_count": parse_errors,
            "callback_drop_count": callback_drops,
            "callback_drop_count_status": callback_drops_status,
        },
        "timing_metrics": {
            "observed_rate_hz": observed_rate,
            "sample_interval_median_ms": float(np.median(interval_ms))
            if len(interval_ms)
            else 0.0,
            "sample_interval_p95_ms": float(np.percentile(interval_ms, 95))
            if len(interval_ms)
            else 0.0,
            "rate_window_packets": window_packets,
            "window_rates": window_rates,
            "first_to_last_window_drift_ppm": rate_drift_ppm,
            "window_rate_min_hz": min(window_rate_values)
            if window_rate_values
            else None,
            "window_rate_max_hz": max(window_rate_values)
            if window_rate_values
            else None,
            "window_rate_span_ppm": (
                (max(window_rate_values) - min(window_rate_values))
                / float(np.median(window_rate_values))
                * 1e6
                if window_rate_values
                else None
            ),
        },
        "stage_metrics": stage_metrics,
        "accel_calibration_candidates": candidates,
        "trailer_metrics": trailer_metrics,
        "verified_conclusions": [],
        "unverified_items": [
            "六轴物理顺序与正负号",
            "完整三轴 counts/g",
            "三轴 counts/(deg/s)",
            "4 字节尾部语义",
        ],
    }
    return report


def write_characterization_report(path: Path) -> Path:
    report = analyze_characterization(path)
    output = path.with_suffix(".characterization.json")
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output


def _read_stage_values(path: Path, stage_code: str) -> tuple[np.ndarray, dict[str, Any]]:
    with h5py.File(path, "r") as handle:
        stages = handle["experiment/stages"]
        codes = [_text(value) for value in stages["stage_code"]]
        matches = [index for index, code in enumerate(codes) if code == stage_code]
        if len(matches) != 1:
            raise ValueError(
                f"{path} 中阶段 {stage_code!r} 的数量必须恰好为 1，实际为 {len(matches)}"
            )
        index = matches[0]
        start_ns = int(stages["start_ns"][index])
        end_ns = int(stages["end_ns"][index])
        reliability = _text(stages["reliability"][index])
        sample_times = np.asarray(
            handle["imu/samples/recording_time_ns"], dtype=np.int64
        )
        raw = np.asarray(handle["imu/samples/raw_counts"], dtype=np.int16)
    values = raw[(sample_times >= start_ns) & (sample_times < end_ns)]
    if not len(values):
        raise ValueError(f"{path} 的阶段 {stage_code!r} 没有样本")
    return values.astype(np.float64), {
        "path": str(path),
        "sha256": sha256_file(path),
        "stage_code": stage_code,
        "reliability": reliability,
        "sample_count": int(len(values)),
        "axes": _axis_stats(values.astype(np.float64)),
    }


def compare_accel_pose_pair(
    positive_path: Path,
    negative_path: Path,
    positive_stage: str,
    negative_stage: str,
    physical_axis: str,
) -> dict[str, Any]:
    """比较相反静态姿态，输出原始列映射和 counts/g 的保守候选。"""

    if physical_axis not in {"x", "y", "z"}:
        raise ValueError("physical_axis 必须是 x、y 或 z")
    positive, positive_source = _read_stage_values(positive_path, positive_stage)
    negative, negative_source = _read_stage_values(negative_path, negative_stage)
    positive_median = np.median(positive, axis=0)
    negative_median = np.median(negative, axis=0)
    delta = positive_median[:3] - negative_median[:3]
    dominant_index = int(np.argmax(np.abs(delta)))
    dominant = float(abs(delta[dominant_index]))
    orthogonal = sorted(
        (float(abs(value)) for index, value in enumerate(delta) if index != dominant_index),
        reverse=True,
    )
    dominance_ratio = dominant / orthogonal[0] if orthogonal and orthogonal[0] else None
    counts_per_g = dominant / 2.0
    bias = float(
        (positive_median[dominant_index] + negative_median[dominant_index]) / 2.0
    )
    strength = (
        "strong"
        if dominance_ratio is not None and dominance_ratio >= 10 and counts_per_g > 0
        else "weak"
    )
    exploratory = "exploratory" in {
        positive_source["reliability"],
        negative_source["reliability"],
    }
    status = f"{strength}_exploratory_candidate" if exploratory else f"{strength}_candidate"
    return {
        "report_schema": "cw12eu_accel_pose_pair_v1",
        "说明": "这是相反静态姿态产生的比例和映射候选，仍需其他轴与独立参考验证。",
        "training_eligible": False,
        "calibration_verified": False,
        "physical_axis_candidate": physical_axis,
        "positive_source": positive_source,
        "negative_source": negative_source,
        "positive_median_counts": {
            axis: float(positive_median[index]) for index, axis in enumerate(AXES)
        },
        "negative_median_counts": {
            axis: float(negative_median[index]) for index, axis in enumerate(AXES)
        },
        "accel_delta_counts": {
            axis: float(delta[index]) for index, axis in enumerate(AXES[:3])
        },
        "dominant_raw_column_candidate": AXES[dominant_index],
        "dominance_ratio_to_next_column": dominance_ratio,
        "sign_from_named_pose": 1 if delta[dominant_index] > 0 else -1,
        "counts_per_g_candidate": counts_per_g,
        "bias_counts_candidate": bias,
        "status": status,
    }


def write_accel_pose_pair_report(
    positive_path: Path,
    negative_path: Path,
    positive_stage: str,
    negative_stage: str,
    physical_axis: str,
    output: Path | None = None,
) -> Path:
    report = compare_accel_pose_pair(
        positive_path,
        negative_path,
        positive_stage,
        negative_stage,
        physical_axis,
    )
    report_path = output or negative_path.with_suffix(
        f".{physical_axis}-accel-pair.json"
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report_path


def recover_interrupted_characterization(
    path: Path,
    imu_settings: ImuSettings,
    taxonomy: dict[str, Any],
) -> dict[str, Any]:
    """从中断的 partial 副本收尾诊断 H5，同时保留原 partial 作为证据。"""

    if not path.name.endswith(".partial.h5"):
        raise ValueError("只允许恢复名称以 .partial.h5 结尾的诊断文件")
    with h5py.File(path, "r") as source:
        if str(source.attrs.get("recording_kind", "")) != "imu_characterization":
            raise ValueError("只允许恢复 recording_kind=imu_characterization 的文件")
        recording_start = int(source.attrs["recording_start_monotonic_ns"])
        invalid_packets = int(
            np.count_nonzero(~np.asarray(source["imu/packets/parse_valid"], dtype=np.bool_))
        )
    final_path = path.with_name(path.name.replace(".partial.h5", ".h5"))
    recovering = path.with_name(path.name.replace(".partial.h5", ".recovering.h5"))
    if final_path.exists() or recovering.exists():
        raise FileExistsError("恢复目标已存在，拒绝覆盖")
    shutil.copy2(path, recovering)
    try:
        with h5py.File(recovering, "r+") as handle:
            writer = object.__new__(CaptureH5Writer)
            writer.handle = handle
            writer.path = recovering
            writer.imu_settings = imu_settings
            writer.recording_start_monotonic_ns = recording_start
            writer.parse_errors = ["中断恢复时发现无效通知"] * invalid_packets
            rate, residual = writer.reconstruct_times()
            handle["imu"].attrs.update(
                {
                    "callback_drops": -1,
                    "callback_drops_status": "unknown_due_to_process_interrupt",
                    "recovered_observed_rate_hz": rate,
                    "recovered_fit_residual_rms_ns": residual,
                }
            )
            handle.attrs.update(
                {
                    "interrupted_capture": True,
                    "interrupted_reason": "user_requested_early_stop",
                    "recovered_at_utc": datetime.now(UTC).isoformat(),
                    "original_partial_sha256": sha256_file(path),
                }
            )
            writer.write_sync([])
            writer.finish()
        from imu_data_collector.validation import validate_capture_h5

        validation = validate_capture_h5(
            recovering, taxonomy, require_video=False, require_sync=False
        )
        if not validation.ready:
            raise ValueError("; ".join(validation.issues))
        recovering.replace(final_path)
        report_path = write_characterization_report(final_path)
        return {
            "source_partial": str(path),
            "recovered_h5": str(final_path),
            "report_path": str(report_path),
            "source_partial_preserved": True,
            "training_eligible": False,
            "validation_issues": list(validation.issues),
        }
    finally:
        recovering.unlink(missing_ok=True)


def correct_characterization_stage(
    path: Path,
    old_stage: str,
    new_stage: str,
    reliability: str,
    reason: str,
) -> dict[str, Any]:
    """原子更正阶段元数据，并把原值与原文件哈希写入审计记录。"""

    if not path.name.endswith(".h5") or path.name.endswith(".partial.h5"):
        raise ValueError("只允许更正已收尾的表征 H5")
    original_sha256 = sha256_file(path)
    correcting = path.with_name(path.name.replace(".h5", ".correcting.h5"))
    if correcting.exists():
        raise FileExistsError("更正临时文件已存在，拒绝覆盖")
    shutil.copy2(path, correcting)
    corrected_at = datetime.now(UTC).isoformat()
    try:
        with h5py.File(correcting, "r+") as handle:
            if str(handle.attrs.get("recording_kind", "")) != "imu_characterization":
                raise ValueError("只允许更正 IMU 表征文件")
            stages = handle["experiment/stages"]
            codes = [_text(value) for value in stages["stage_code"]]
            matches = [index for index, code in enumerate(codes) if code == old_stage]
            if len(matches) != 1:
                raise ValueError(
                    f"阶段 {old_stage!r} 的数量必须恰好为 1，实际为 {len(matches)}"
                )
            index = matches[0]
            old_reliability = _text(stages["reliability"][index])
            old_notes = _text(stages["notes"][index])
            correction = {
                "corrected_at_utc": corrected_at,
                "reason": reason,
                "original_file_sha256": original_sha256,
                "old_stage_code": old_stage,
                "new_stage_code": new_stage,
                "old_reliability": old_reliability,
                "new_reliability": reliability,
                "old_notes": old_notes,
            }
            existing = json.loads(str(handle.attrs.get("metadata_corrections", "[]")))
            existing.append(correction)
            stages["stage_code"][index] = new_stage
            stages["reliability"][index] = reliability
            stages["notes"][index] = (
                f"{old_notes}【元数据更正：{reason}；{corrected_at}】"
            )
            handle.attrs["metadata_corrections"] = json.dumps(
                existing, ensure_ascii=False
            )
            handle.flush()
        from imu_data_collector.validation import validate_capture_h5

        validation = validate_capture_h5(
            correcting, require_video=False, require_sync=False
        )
        if not validation.ready:
            raise ValueError("; ".join(validation.issues))
        correcting.replace(path)
        report_path = write_characterization_report(path)
        return {
            "h5_path": str(path),
            "report_path": str(report_path),
            "old_stage_code": old_stage,
            "new_stage_code": new_stage,
            "original_file_sha256": original_sha256,
            "corrected_file_sha256": sha256_file(path),
            "validation_issues": list(validation.issues),
        }
    finally:
        correcting.unlink(missing_ok=True)
