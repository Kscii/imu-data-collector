import numpy as np
import pytest

from imu_data_collector.models import SyncAnchor, SyncDocument
from imu_data_collector.sync import assess_conditional_fixed_offset, fit_sync_model


def _formal_sync(
    start_offset_ns: int,
    end_offset_ns: int,
    *,
    apply: bool = False,
) -> SyncDocument:
    return SyncDocument(
        anchors=[
            SyncAnchor(
                role="start_tap",
                imu_time_ns=1_000_000_000,
                video_time_ns=1_000_000_000 + start_offset_ns,
                source_video_frame=30,
                source_imu_sample=25,
                video_interval_start_ns=966_666_667 + start_offset_ns,
                imu_interval_start_ns=960_000_000,
                reviewer_id="xfan0282",
            ),
            SyncAnchor(
                role="end_tap",
                imu_time_ns=601_000_000_000,
                video_time_ns=601_000_000_000 + end_offset_ns,
                source_video_frame=18_030,
                source_imu_sample=15_025,
                video_interval_start_ns=600_966_666_667 + end_offset_ns,
                imu_interval_start_ns=600_960_000_000,
                reviewer_id="xfan0282",
            ),
        ],
        apply_fixed_offset=apply,
    )


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


def test_conditional_sync_keeps_host_time_below_trigger() -> None:
    result = assess_conditional_fixed_offset(_formal_sync(50_000_000, 70_000_000))

    assert result.scale == 1.0
    assert result.estimated_offset_ns == 60_000_000
    assert result.applied_offset_ns == 0
    assert result.recommendation == "keep_host_time"
    assert result.quality == "verified"


def test_conditional_sync_requires_confirmation_before_applying_offset() -> None:
    pending = assess_conditional_fixed_offset(_formal_sync(150_000_000, 170_000_000))
    applied = assess_conditional_fixed_offset(
        _formal_sync(150_000_000, 170_000_000, apply=True)
    )

    assert pending.recommendation == "apply_fixed_offset"
    assert pending.quality == "awaiting_confirmation"
    assert pending.applied_offset_ns == 0
    assert applied.applied_offset_ns == 160_000_000
    assert applied.decision == "fixed_offset"
    assert applied.quality == "verified"


def test_conditional_sync_rejects_inconsistent_anchor_pair() -> None:
    result = assess_conditional_fixed_offset(_formal_sync(20_000_000, 150_000_001))

    assert result.recommendation == "reselect_anchors"
    assert result.quality == "needs_review"
    with pytest.raises(ValueError, match="不满足条件式固定偏移"):
        assess_conditional_fixed_offset(
            _formal_sync(20_000_000, 150_000_001, apply=True)
        )


def test_formal_anchor_requires_auditable_source_fields() -> None:
    with pytest.raises(ValueError, match="正式同步锚点缺少来源字段"):
        SyncAnchor(
            role="start_tap",
            imu_time_ns=1_000_000_000,
            video_time_ns=1_010_000_000,
        )
