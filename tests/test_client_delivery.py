from __future__ import annotations

import pytest

from imu_data_collector.annotation_service import AnnotationService
from imu_data_collector.client_delivery import public_taxonomy, safe_archive_component


def test_public_taxonomy_removes_internal_change_audit() -> None:
    public = public_taxonomy(
        {
            "taxonomy_id": "fall_binary_v1",
            "version": "1.1.0+r7",
            "revision": 7,
            "change": {
                "actor_unikey": "private-user",
                "changed_at_utc": "2026-09-04T00:00:00Z",
                "operation": "update",
            },
            "fall": [{"code": "forward_fall", "name": "Forward fall", "active": True}],
            "non_fall": [{"code": "walking", "name": "Walking", "active": False}],
        }
    )

    assert public == {
        "schema_version": "cw12eu_activity_taxonomy_v1",
        "taxonomy_id": "fall_binary_v1",
        "version": "1.1.0+r7",
        "fall": [{"code": "forward_fall", "name": "Forward fall", "active": True}],
        "non_fall": [{"code": "walking", "name": "Walking", "active": False}],
    }
    assert "private-user" not in str(public)


@pytest.mark.parametrize("value", ("../escape", "bad/path", "bad value", ".", "..", ""))
def test_unsafe_archive_component_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        safe_archive_component(value, name="test component")


@pytest.mark.parametrize("value", ("fall_binary_v1", "1.1.0+r7", "name-01"))
def test_safe_archive_component_is_preserved(value: str) -> None:
    assert safe_archive_component(value, name="test component") == value


def test_delivery_preflight_rejects_pre_identity_v3_snapshots() -> None:
    issue = AnnotationService._client_delivery_identity_issue(
        {
            "recordings": [
                {
                    "participant_id": "cw12eu:xfan0282",
                    "recording_id": "20260827T132356.924704Z_xfan0282",
                }
            ]
        }
    )

    assert issue is not None
    assert "身份 v3" in issue
