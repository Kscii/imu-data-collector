import numpy as np
import pytest

from imu_data_collector.cw12eu import (
    NotificationKind,
    calibrate_counts,
    classify_notification,
    pack_test_frame,
    parse_notification,
    reconstruct_sample_times,
)


def test_notification_classifier_separates_samples_auxiliary_and_unknown() -> None:
    imu = pack_test_frame((1, 2, 3, 4, 5, 6), b"\x00\x00\x00\x01")
    auxiliary_variants = (
        bytes.fromhex("aa1a0200f0f0f0f00146"),
        bytes.fromhex("aa1a0200f0f0f0f00046"),
    )

    assert classify_notification(imu) == NotificationKind.IMU_SAMPLES
    for payload in auxiliary_variants:
        assert classify_notification(payload) == NotificationKind.AUXILIARY_STATUS
    assert classify_notification(b"\x00" * 10) == NotificationKind.UNKNOWN_INVALID
    assert classify_notification(b"\x00" * 15) == NotificationKind.UNKNOWN_INVALID


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


def test_calibration_applies_raw_bias_axis_direction_and_si_units() -> None:
    raw = np.asarray([[130, 2048, 4056, 15, 328, -646]], dtype=np.int16)

    values = calibrate_counts(
        raw,
        4096.0,
        32.8,
        accel_bias_counts=(30.0, 48.0, -40.0),
        gyro_bias_counts=(-17.0, 0.0, 10.0),
        raw_axis_order=(0, 1, 2),
        axis_signs=(1, -1, 1),
    )

    assert values[0, 0] == pytest.approx(100 / 4096 * 9.80665)
    assert values[0, 1] == pytest.approx(-2000 / 4096 * 9.80665)
    assert values[0, 2] == pytest.approx(4096 / 4096 * 9.80665)
    assert values[0, 3] == pytest.approx(np.deg2rad(32 / 32.8))
    assert values[0, 4] == pytest.approx(np.deg2rad(-328 / 32.8))
    assert values[0, 5] == pytest.approx(np.deg2rad(-656 / 32.8))


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
