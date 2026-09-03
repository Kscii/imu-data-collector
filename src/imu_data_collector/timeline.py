"""Helpers for faithful overview and bounded detail timelines."""

from __future__ import annotations

import numpy as np


def peak_preserving_indices(values: np.ndarray, max_points: int) -> np.ndarray:
    """Return ordered indices that retain per-axis extrema in each time bucket."""

    rows = len(values)
    if rows <= max_points:
        return np.arange(rows, dtype=np.int64)
    if max_points < 2:
        raise ValueError("max_points must be at least 2")
    axis_count = values.shape[1] if values.ndim == 2 else 1
    points_per_bucket = max(2, axis_count * 2)
    bucket_count = max(1, (max_points - 2) // points_per_bucket)
    edges = np.linspace(0, rows, bucket_count + 1, dtype=np.int64)
    selected = {0, rows - 1}
    matrix = values if values.ndim == 2 else values[:, None]
    for start, stop in zip(edges[:-1], edges[1:], strict=True):
        if stop <= start:
            continue
        bucket = matrix[start:stop]
        for axis in range(axis_count):
            column = bucket[:, axis]
            finite = np.flatnonzero(np.isfinite(column))
            if not len(finite):
                continue
            finite_values = column[finite]
            selected.add(start + int(finite[int(np.argmin(finite_values))]))
            selected.add(start + int(finite[int(np.argmax(finite_values))]))
    ordered = np.asarray(sorted(selected), dtype=np.int64)
    if len(ordered) <= max_points:
        return ordered
    # Extrema from neighboring buckets can only exceed the budget slightly.
    keep = np.linspace(0, len(ordered) - 1, max_points, dtype=np.int64)
    return ordered[keep]


def timeline_payload(
    times_ns: np.ndarray,
    values: np.ndarray,
    *,
    max_points: int | None,
    unit: str,
) -> dict[str, object]:
    indices = (
        peak_preserving_indices(values, max_points)
        if max_points is not None
        else np.arange(len(times_ns), dtype=np.int64)
    )
    return {
        "time_s": (times_ns[indices] / 1e9).tolist(),
        "values": np.asarray(values[indices], dtype=np.float32).tolist(),
        "unit": unit,
        "source_point_count": len(times_ns),
        "display_point_count": len(indices),
        "downsample_kind": "per_axis_min_max" if len(indices) < len(times_ns) else "none",
    }
