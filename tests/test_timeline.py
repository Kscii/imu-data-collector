import numpy as np

from imu_data_collector.timeline import peak_preserving_indices, timeline_payload


def test_peak_preserving_indices_retains_short_axis_extrema() -> None:
    values = np.zeros((10_000, 6), dtype=np.float32)
    values[4_321, 2] = 99.0
    values[7_654, 4] = -77.0

    indices = peak_preserving_indices(values, 500)

    assert 4_321 in indices
    assert 7_654 in indices
    assert indices[0] == 0
    assert indices[-1] == len(values) - 1
    assert len(indices) <= 500


def test_timeline_payload_reports_downsampling_contract() -> None:
    times = np.arange(1_000, dtype=np.int64) * 40_000_000
    values = np.zeros((1_000, 6), dtype=np.float32)

    payload = timeline_payload(times, values, max_points=100, unit="SI")

    assert payload["source_point_count"] == 1_000
    assert payload["display_point_count"] <= 100
    assert payload["downsample_kind"] == "per_axis_min_max"
