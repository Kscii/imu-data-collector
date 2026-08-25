"""IMU 与视频单调时间轴之间的仿射同步。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


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
