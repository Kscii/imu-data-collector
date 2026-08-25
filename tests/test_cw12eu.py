import numpy as np
import pytest

from imu_data_collector.cw12eu import (
    calibrate_counts,
    pack_test_frame,
    parse_notification,
    reconstruct_sample_times,
)


def test_notification_parser_preserves_all_candidate_fields() -> None:
    first = pack_test_frame((1, -2, 3, -4, 5, -6), b"\x10\x11\x12\x13")
    second = pack_test_frame((300, 301, 302, 303, 304, 305), b"\xaa\xbb\xcc\xdd")

    parsed = parse_notification(first + second)

    np.testing.assert_array_equal(
        parsed.raw_counts,
        np.asarray([[1, -2, 3, -4, 5, -6], [300, 301, 302, 303, 304, 305]]),
    )
    np.testing.assert_array_equal(
        parsed.trailer,
        np.asarray([[0x10, 0x11, 0x12, 0x13], [0xAA, 0xBB, 0xCC, 0xDD]]),
    )


def test_notification_parser_rejects_partial_frame() -> None:
    with pytest.raises(ValueError, match="not a multiple"):
        parse_notification(b"\x00" * 15)


def test_unverified_scales_produce_nan_instead_of_guessed_units() -> None:
    raw = np.asarray([[100, 200, 300, 400, 500, 600]], dtype=np.int16)

    values = calibrate_counts(raw, None, None)

    assert np.isnan(values).all()


def test_packet_end_fit_reconstructs_monotonic_sample_clock() -> None:
    packet_counts = np.asarray([2, 2, 2, 2], dtype=np.int64)
    expected_times = 1_000_000_000 + np.arange(8) * 33_333_333
    jitter = np.asarray([2_000_000, 3_000_000, 1_000_000, 2_000_000])
    receive = expected_times[[1, 3, 5, 7]] + jitter

    times, rate, residual = reconstruct_sample_times(receive, packet_counts, 30.0)

    assert len(times) == 8
    assert np.all(np.diff(times) > 0)
    assert rate == pytest.approx(30.0, rel=0.01)
    assert residual < 2_000_000
