from imu_data_collector.cli import _estimate_batched_sample_rate


def test_batched_sample_rate_includes_one_packet_interval_of_coverage() -> None:
    packet_times = [1_000_000_000, 2_000_000_000, 3_000_000_000]

    coverage_seconds, rate_hz = _estimate_batched_sample_rate(75, packet_times)

    assert coverage_seconds == 3.0
    assert rate_hz == 25.0


def test_batched_sample_rate_requires_two_packets() -> None:
    assert _estimate_batched_sample_rate(25, [1_000_000_000]) == (None, None)
