import numpy as np
import pytest

from imu_data_collector.sync import fit_sync_model


def test_no_anchor_keeps_host_clock_mapping_explicitly_unverified() -> None:
    model = fit_sync_model(np.asarray([]), np.asarray([]))

    assert model.quality == "host_only"
    assert model.scale == 1.0
    assert model.offset_ns == 0.0


def test_two_anchors_estimate_offset_and_clock_drift() -> None:
    imu = np.asarray([1_000_000_000, 21_000_000_000], dtype=np.int64)
    video = np.rint(imu * 1.0001 + 8_000_000).astype(np.int64)

    model = fit_sync_model(imu, video)

    assert model.quality == "verified"
    assert model.scale == pytest.approx(1.0001)
    assert model.offset_ns == pytest.approx(8_000_000, abs=10)
    np.testing.assert_allclose(model.imu_to_video(imu), video, atol=1)


def test_implausible_clock_drift_is_not_marked_verified() -> None:
    imu = np.asarray([0, 10_000_000_000], dtype=np.int64)
    video = np.asarray([0, 11_000_000_000], dtype=np.int64)

    model = fit_sync_model(imu, video)

    assert model.quality == "poor"
