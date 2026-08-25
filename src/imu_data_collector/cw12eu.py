"""CW12EU-T 通知解析与样本时间戳重建。"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

import numpy as np

from imu_data_collector.constants import CW12EU_FRAME_BYTES, STANDARD_GRAVITY_MPS2


@dataclass(frozen=True, slots=True)
class ParsedNotification:
    raw_counts: np.ndarray
    trailer: np.ndarray

    @property
    def sample_count(self) -> int:
        return len(self.raw_counts)


def parse_notification(payload: bytes, frame_size: int = CW12EU_FRAME_BYTES) -> ParsedNotification:
    if not payload:
        raise ValueError("empty CW12EU-T notification")
    if frame_size != 16:
        raise ValueError(f"unsupported frame size: {frame_size}")
    if len(payload) % frame_size:
        raise ValueError(
            f"notification length {len(payload)} is not a multiple of {frame_size}"
        )
    frames = np.frombuffer(payload, dtype=np.uint8).reshape(-1, frame_size)
    raw = np.frombuffer(frames[:, :12].copy().tobytes(), dtype=">i2").astype(np.int16)
    return ParsedNotification(raw.reshape(-1, 6), frames[:, 12:16].copy())


def calibrate_counts(
    raw_counts: np.ndarray,
    accel_counts_per_g: float | None,
    gyro_counts_per_dps: float | None,
) -> np.ndarray:
    values = np.full(raw_counts.shape, np.nan, dtype=np.float32)
    if accel_counts_per_g and math.isfinite(accel_counts_per_g) and accel_counts_per_g > 0:
        values[:, :3] = (
            raw_counts[:, :3].astype(np.float64)
            / accel_counts_per_g
            * STANDARD_GRAVITY_MPS2
        )
    if gyro_counts_per_dps and math.isfinite(gyro_counts_per_dps) and gyro_counts_per_dps > 0:
        values[:, 3:] = np.deg2rad(
            raw_counts[:, 3:].astype(np.float64) / gyro_counts_per_dps
        )
    return values


def reconstruct_sample_times(
    packet_receive_ns: np.ndarray,
    packet_sample_counts: np.ndarray,
    fallback_rate_hz: float,
) -> tuple[np.ndarray, float, float]:
    """拟合各包末尾的到达时间，并为每个样本返回一个估算时间。

    BLE 传输延迟会保留在截距中；物理同步锚点在另一阶段应用，因此不会被
    这个估算器掩盖。
    """

    receive = np.asarray(packet_receive_ns, dtype=np.int64)
    counts = np.asarray(packet_sample_counts, dtype=np.int64)
    if len(receive) != len(counts) or np.any(counts <= 0):
        raise ValueError("packet timestamp/count arrays must have equal positive length")
    if len(receive) == 0:
        return np.empty(0, dtype=np.int64), float(fallback_rate_hz), 0.0

    ends = np.cumsum(counts, dtype=np.int64) - 1
    if len(receive) >= 3 and receive[-1] > receive[0] and ends[-1] > ends[0]:
        x = ends.astype(np.float64)
        y = receive.astype(np.float64)
        slope, intercept = np.polyfit(x, y, 1)
        fitted_rate = 1e9 / slope if slope > 0 else float(fallback_rate_hz)
        if not math.isfinite(fitted_rate) or fitted_rate < 1 or fitted_rate > 1000:
            fitted_rate = float(fallback_rate_hz)
            slope = 1e9 / fitted_rate
            intercept = float(receive[-1]) - float(ends[-1]) * slope
        residual = float(np.sqrt(np.mean((y - (intercept + slope * x)) ** 2)))
    else:
        fitted_rate = float(fallback_rate_hz)
        slope = 1e9 / fitted_rate
        intercept = float(receive[-1]) - float(ends[-1]) * slope
        residual = 0.0

    sample_indices = np.arange(int(counts.sum()), dtype=np.float64)
    times = np.rint(intercept + slope * sample_indices).astype(np.int64)
    return times, float(fitted_rate), residual


def pack_test_frame(values: tuple[int, int, int, int, int, int], trailer: bytes) -> bytes:
    """供测试使用的编码辅助函数，确保协议示例只有一个标准实现。"""

    if len(trailer) != 4:
        raise ValueError("trailer must contain exactly four bytes")
    return struct.pack(">6h", *values) + trailer
