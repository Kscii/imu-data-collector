"""CW12EU-T 通知解析与样本时间戳重建。"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from enum import IntEnum

import numpy as np

from imu_data_collector.constants import CW12EU_FRAME_BYTES, STANDARD_GRAVITY_MPS2


class NotificationKind(IntEnum):
    """写入 H5 的稳定通知类型码。"""

    IMU_SAMPLES = 1
    AUXILIARY_STATUS = 2
    UNKNOWN_INVALID = 255


# 已在多次真实录制中观察到的 10 字节辅助状态通知。最后两个字节的语义
# 尚未由供应商协议或独立实验确认，因此只识别包族，不解释电量等字段。
CW12EU_AUXILIARY_PREFIX = bytes.fromhex("aa1a0200f0f0f0f0")


def classify_notification(
    payload: bytes,
    frame_size: int = CW12EU_FRAME_BYTES,
    protocol: str = "cw12eu_v1",
) -> NotificationKind:
    """先区分样本包、已知辅助包和未知无效包。"""

    if protocol == "acce_gyro_abf0_v1":
        if frame_size != 22 or len(payload) != frame_size:
            return NotificationKind.UNKNOWN_INVALID
        frame = np.frombuffer(payload, dtype=np.uint8)
        if np.array_equal(frame[-2:], np.asarray([0x0D, 0x0A], dtype=np.uint8)):
            return NotificationKind.IMU_SAMPLES
        return NotificationKind.UNKNOWN_INVALID
    if protocol != "cw12eu_v1":
        return NotificationKind.UNKNOWN_INVALID
    if len(payload) == 10 and payload.startswith(CW12EU_AUXILIARY_PREFIX):
        return NotificationKind.AUXILIARY_STATUS
    if payload and frame_size == CW12EU_FRAME_BYTES and len(payload) % frame_size == 0:
        return NotificationKind.IMU_SAMPLES
    return NotificationKind.UNKNOWN_INVALID


@dataclass(frozen=True, slots=True)
class ParsedNotification:
    raw_counts: np.ndarray
    trailer: np.ndarray | None = None
    device_time_ms: np.ndarray | None = None

    @property
    def sample_count(self) -> int:
        return len(self.raw_counts)


def parse_notification(
    payload: bytes,
    frame_size: int = CW12EU_FRAME_BYTES,
    protocol: str = "cw12eu_v1",
) -> ParsedNotification:
    if not payload:
        raise ValueError("empty IMU notification")
    if protocol == "acce_gyro_abf0_v1":
        if frame_size != 22:
            raise ValueError(f"unsupported frame size for {protocol}: {frame_size}")
        if len(payload) != frame_size:
            raise ValueError(
                f"{protocol} requires exactly one {frame_size}-byte frame per notification"
            )
        frames = np.frombuffer(payload, dtype=np.uint8).reshape(1, frame_size)
        if not np.all(frames[:, -2:] == np.asarray([0x0D, 0x0A], dtype=np.uint8)):
            raise ValueError("acce&gyro notification is missing the CRLF frame suffix")
        raw = np.frombuffer(
            frames[:, :12].copy().tobytes(), dtype="<i2"
        ).astype(np.int16)
        device_time_ms = np.frombuffer(
            frames[:, 12:20].copy().tobytes(), dtype="<u8"
        ).astype(np.uint64)
        return ParsedNotification(raw.reshape(-1, 6), device_time_ms=device_time_ms)
    if protocol != "cw12eu_v1":
        raise ValueError(f"unsupported IMU protocol: {protocol}")
    if frame_size != CW12EU_FRAME_BYTES:
        raise ValueError(f"unsupported frame size: {frame_size}")
    if len(payload) % frame_size:
        raise ValueError(
            f"notification length {len(payload)} is not a multiple of {frame_size}"
        )
    frames = np.frombuffer(payload, dtype=np.uint8).reshape(-1, frame_size)
    raw = np.frombuffer(frames[:, :12].copy().tobytes(), dtype=">i2").astype(np.int16)
    return ParsedNotification(raw.reshape(-1, 6), frames[:, 12:16].copy())


def reconstruct_device_clock_times(
    device_time_ms: np.ndarray,
    sample_packet_indices: np.ndarray,
    packet_receive_ns: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float, float, list[dict[str, float | int]]]:
    """Map device-local milliseconds to host monotonic time per clock epoch.

    A non-increasing device tick starts a new epoch. Test captures retain all epochs;
    production validation rejects resets. The affine slope is constrained to a
    conservative ±1% clock-drift envelope and falls back to one when the fit is not
    credible.
    """

    ticks = np.asarray(device_time_ms, dtype=np.uint64)
    packet_indices = np.asarray(sample_packet_indices, dtype=np.int64)
    receive = np.asarray(packet_receive_ns, dtype=np.int64)
    if ticks.ndim != 1 or packet_indices.shape != ticks.shape:
        raise ValueError("device ticks and packet indices must be equal one-dimensional arrays")
    if len(ticks) == 0:
        return (
            np.empty(0, dtype=np.int64),
            np.empty(0, dtype=np.uint32),
            0.0,
            0.0,
            [],
        )
    if np.any(packet_indices < 0) or np.any(packet_indices >= len(receive)):
        raise ValueError("sample packet index is outside packet receive timestamps")

    epochs = np.zeros(len(ticks), dtype=np.uint32)
    if len(ticks) > 1:
        resets = ticks[1:] <= ticks[:-1]
        epochs[1:] = np.cumsum(resets, dtype=np.uint32)
    mapped = np.empty(len(ticks), dtype=np.int64)
    mappings: list[dict[str, float | int]] = []
    squared_residuals: list[np.ndarray] = []
    for epoch in range(int(epochs[-1]) + 1):
        indices = np.flatnonzero(epochs == epoch)
        epoch_ticks = ticks[indices].astype(np.float64)
        x = (epoch_ticks - epoch_ticks[0]) * 1e6
        y = receive[packet_indices[indices]].astype(np.float64)
        if len(indices) >= 3 and x[-1] > x[0]:
            slope, intercept = np.polyfit(x, y, 1)
            if not math.isfinite(slope) or not 0.99 <= slope <= 1.01:
                slope = 1.0
                intercept = float(np.median(y - x))
        else:
            slope = 1.0
            intercept = float(np.median(y - x))
        fitted = intercept + slope * x
        mapped[indices] = np.rint(fitted).astype(np.int64)
        residual = y - fitted
        squared_residuals.append(residual * residual)
        mappings.append(
            {
                "epoch": epoch,
                "first_device_time_ms": int(ticks[indices[0]]),
                "last_device_time_ms": int(ticks[indices[-1]]),
                "scale": float(slope),
                "offset_ns": int(round(intercept)),
                "sample_count": len(indices),
            }
        )
    if len(mapped) > 1 and np.any(np.diff(mapped) <= 0):
        raise ValueError("mapped device timestamps are not strictly increasing")
    span_ms = int(ticks[-1]) - int(ticks[0]) if epochs[-1] == 0 else 0
    rate = (len(ticks) - 1) * 1000.0 / span_ms if span_ms > 0 else 0.0
    residuals = np.concatenate(squared_residuals)
    rms = float(math.sqrt(float(np.mean(residuals)))) if len(residuals) else 0.0
    return mapped, epochs, rate, rms, mappings


def calibrate_counts(
    raw_counts: np.ndarray,
    accel_counts_per_g: float | None,
    gyro_counts_per_dps: float | None,
    *,
    accel_bias_counts: tuple[float, float, float] = (0.0, 0.0, 0.0),
    gyro_bias_counts: tuple[float, float, float] = (0.0, 0.0, 0.0),
    raw_axis_order: tuple[int, int, int] = (0, 1, 2),
    axis_signs: tuple[int, int, int] = (1, 1, 1),
) -> np.ndarray:
    """把设备原始计数转换到项目坐标系和 SI 单位。

    偏置定义在设备原始轴空间；先减偏置，再按目标 X/Y/Z 的来源轴重排并
    应用方向符号。原始 ``raw_counts`` 不会被修改。
    """

    raw = np.asarray(raw_counts)
    if raw.ndim != 2 or raw.shape[1] != 6:
        raise ValueError("raw_counts 必须是 N x 6 数组")
    if sorted(raw_axis_order) != [0, 1, 2]:
        raise ValueError("raw_axis_order 必须是 0、1、2 的排列")
    if any(sign not in (-1, 1) for sign in axis_signs):
        raise ValueError("axis_signs 只能包含 -1 或 1")
    values = np.full(raw_counts.shape, np.nan, dtype=np.float32)
    order = np.asarray(raw_axis_order, dtype=np.intp)
    signs = np.asarray(axis_signs, dtype=np.float64)
    if accel_counts_per_g and math.isfinite(accel_counts_per_g) and accel_counts_per_g > 0:
        corrected = raw[:, :3].astype(np.float64) - np.asarray(
            accel_bias_counts, dtype=np.float64
        )
        values[:, :3] = (
            corrected[:, order]
            * signs
            / accel_counts_per_g
            * STANDARD_GRAVITY_MPS2
        )
    if gyro_counts_per_dps and math.isfinite(gyro_counts_per_dps) and gyro_counts_per_dps > 0:
        corrected = raw[:, 3:].astype(np.float64) - np.asarray(
            gyro_bias_counts, dtype=np.float64
        )
        values[:, 3:] = np.deg2rad(
            corrected[:, order] * signs / gyro_counts_per_dps
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
