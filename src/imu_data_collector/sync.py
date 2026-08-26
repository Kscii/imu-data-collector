"""IMU 与视频单调时间轴之间的条件式固定偏移同步。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from imu_data_collector.models import SyncAnchor, SyncDocument

OFFSET_TRIGGER_NS = 100_000_000
ANCHOR_CONSISTENCY_NS = 100_000_000
NORMAL_TARGET_NS = 200_000_000
HARD_LIMIT_NS = 500_000_000


@dataclass(frozen=True, slots=True)
class SyncModel:
    scale: float
    offset_ns: float
    residual_rms_ns: float
    quality: str

    def imu_to_video(self, imu_time_ns: np.ndarray | int) -> np.ndarray:
        values = (
            np.asarray(imu_time_ns, dtype=np.float64) * self.scale + self.offset_ns
        )
        return np.rint(values).astype(np.int64)


@dataclass(frozen=True, slots=True)
class ConditionalSyncAssessment:
    """条件式固定偏移的计算结果；scale 永远为 1。"""

    policy: str
    scale: float
    estimated_offset_ns: int
    applied_offset_ns: int
    start_offset_ns: int
    end_offset_ns: int
    anchor_disagreement_ns: int
    residual_rms_ns: float
    residual_upper_bound_ns: int
    recommendation: str
    decision: str
    quality: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "scale": self.scale,
            "estimated_offset_ns": self.estimated_offset_ns,
            "applied_offset_ns": self.applied_offset_ns,
            "offset_ns": self.applied_offset_ns,
            "start_offset_ns": self.start_offset_ns,
            "end_offset_ns": self.end_offset_ns,
            "anchor_disagreement_ns": self.anchor_disagreement_ns,
            "residual_rms_ns": self.residual_rms_ns,
            "residual_upper_bound_ns": self.residual_upper_bound_ns,
            "recommendation": self.recommendation,
            "decision": self.decision,
            "quality": self.quality,
            "offset_trigger_ns": OFFSET_TRIGGER_NS,
            "anchor_consistency_ns": ANCHOR_CONSISTENCY_NS,
            "normal_target_ns": NORMAL_TARGET_NS,
            "hard_limit_ns": HARD_LIMIT_NS,
        }


def _ordered_sync_anchors(anchors: list[SyncAnchor]) -> tuple[SyncAnchor, SyncAnchor]:
    if len(anchors) != 2:
        raise ValueError("正式同步必须恰好包含开始和结束两个轻拍锚点")
    by_role = {anchor.role: anchor for anchor in anchors if anchor.role != "legacy"}
    if set(by_role) == {"start_tap", "end_tap"}:
        return by_role["start_tap"], by_role["end_tap"]
    if all(anchor.role == "legacy" for anchor in anchors):
        ordered = sorted(anchors, key=lambda item: item.video_time_ns)
        return ordered[0], ordered[1]
    raise ValueError("正式同步必须各包含一个 start_tap 和 end_tap")


def _offset_interval(anchor: SyncAnchor) -> tuple[int, int]:
    video_start = anchor.video_interval_start_ns
    imu_start = anchor.imu_interval_start_ns
    lower = int(
        (video_start if video_start is not None else anchor.video_time_ns)
        - anchor.imu_time_ns
    )
    upper = int(
        anchor.video_time_ns
        - (imu_start if imu_start is not None else anchor.imu_time_ns)
    )
    return min(lower, upper), max(lower, upper)


def assess_conditional_fixed_offset(document: SyncDocument) -> ConditionalSyncAssessment:
    start, end = _ordered_sync_anchors(document.anchors)
    start_offset = int(start.video_time_ns - start.imu_time_ns)
    end_offset = int(end.video_time_ns - end.imu_time_ns)
    estimated = int(round((start_offset + end_offset) / 2))
    disagreement = abs(end_offset - start_offset)

    if disagreement > ANCHOR_CONSISTENCY_NS:
        recommendation = "reselect_anchors"
    elif abs(estimated) >= OFFSET_TRIGGER_NS:
        recommendation = "apply_fixed_offset"
    else:
        recommendation = "keep_host_time"

    if document.apply_fixed_offset and recommendation != "apply_fixed_offset":
        raise ValueError("当前锚点不满足条件式固定偏移的自动应用规则")
    applied = estimated if document.apply_fixed_offset else 0
    residuals = np.asarray(
        [start_offset - applied, end_offset - applied], dtype=np.float64
    )
    residual_rms = float(np.sqrt(np.mean(np.square(residuals))))
    residual_intervals = [
        tuple(value - applied for value in _offset_interval(anchor))
        for anchor in (start, end)
    ]
    residual_upper = int(
        max(abs(bound) for interval in residual_intervals for bound in interval)
    )

    if residual_upper > HARD_LIMIT_NS:
        quality = "rejected"
    elif recommendation == "reselect_anchors":
        quality = "needs_review"
    elif recommendation == "apply_fixed_offset" and not document.apply_fixed_offset:
        quality = "awaiting_confirmation"
    elif residual_upper <= NORMAL_TARGET_NS:
        quality = "verified"
    else:
        quality = "needs_review"

    decision = "fixed_offset" if document.apply_fixed_offset else "host_only"
    return ConditionalSyncAssessment(
        policy=document.policy,
        scale=1.0,
        estimated_offset_ns=estimated,
        applied_offset_ns=applied,
        start_offset_ns=start_offset,
        end_offset_ns=end_offset,
        anchor_disagreement_ns=disagreement,
        residual_rms_ns=residual_rms,
        residual_upper_bound_ns=residual_upper,
        recommendation=recommendation,
        decision=decision,
        quality=quality,
    )


def fit_sync_model(imu_time_ns: np.ndarray, video_time_ns: np.ndarray) -> SyncModel:
    imu = np.asarray(imu_time_ns, dtype=np.float64)
    video = np.asarray(video_time_ns, dtype=np.float64)
    if imu.shape != video.shape or imu.ndim != 1:
        raise ValueError("sync anchor arrays must be one-dimensional and equal length")
    if len(imu) == 0:
        return SyncModel(1.0, 0.0, float("nan"), "host_only")
    if len(imu) == 1:
        return SyncModel(1.0, float(video[0] - imu[0]), 0.0, "single_anchor")
    centered = imu - imu[0]
    coefficients = np.polyfit(centered, video, 1)
    scale = float(coefficients[0])
    offset = float(coefficients[1] - scale * imu[0])
    predicted = scale * imu + offset
    residual = float(np.sqrt(np.mean((video - predicted) ** 2)))
    scale_is_plausible = 0.995 <= scale <= 1.005
    quality = "verified" if residual <= 40_000_000 and scale_is_plausible else "poor"
    return SyncModel(scale, offset, residual, quality)
