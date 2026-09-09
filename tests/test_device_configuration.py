import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from imu_data_collector import broker_client
from imu_data_collector.broker_client import refresh_device_configuration_via_broker
from imu_data_collector.config import Settings
from imu_data_collector.device_configuration import (
    MIGRATION_CONFIRMATION,
    ConfigurationContentV2,
    ConfigurationReviewV2,
    ConfigurationSnapshotSubmission,
    DeviceConfigurationStore,
    LocalConfigurationManager,
    apply_migration_lock,
    bootstrap_snapshot_from_registry,
    content_sha256,
    migration_preview,
)
from imu_data_collector.storage import LocalFilesystemStore, ObjectConflictError

REGISTRY_PATH = Path("configs/imu-devices.yaml").resolve()


def submission(*, suffix: str = "") -> ConfigurationSnapshotSubmission:
    bootstrap = bootstrap_snapshot_from_registry(REGISTRY_PATH)
    content = bootstrap.content
    if suffix:
        devices = [item.model_copy(deep=True) for item in content.devices]
        devices[-1].display_name = f"{devices[-1].display_name} {suffix}"
        content = ConfigurationContentV2(devices=devices)
    return ConfigurationSnapshotSubmission(
        name=f"test fleet {suffix}".strip(),
        description="unit-test snapshot",
        client_build="pytest",
        content=content,
    )


def test_v1_registry_migrates_to_whole_fleet_v2_with_per_sn_si() -> None:
    snapshot = bootstrap_snapshot_from_registry(REGISTRY_PATH)

    assert snapshot.snapshot_id.startswith("local-cfg-")
    assert snapshot.content_sha256 == content_sha256(snapshot.content)
    assert [item.sensor_sn for item in snapshot.content.devices] == [
        "IMU-0001-R01",
        "IMU-0002-R01",
    ]
    verified, commissioning = snapshot.content.devices
    assert verified.si_profile.verified
    assert verified.si_profile.accel_counts_per_g == 4096.0
    assert verified.allowed_data_tiers == ["test", "prod"]
    assert not commissioning.si_profile.verified
    assert commissioning.si_profile.accel_counts_per_g == 16384.0
    assert commissioning.allowed_data_tiers == ["test"]


def test_snapshot_review_current_and_approval_at_capture_are_independent(
    tmp_path: Path,
) -> None:
    store = LocalFilesystemStore(tmp_path / "objects")
    configurations = DeviceConfigurationStore(store)
    submitted_at = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
    approved_at = submitted_at + timedelta(minutes=5)
    revoked_at = approved_at + timedelta(minutes=10)

    snapshot, review = configurations.submit(
        submission(), actor="member", now=submitted_at
    )
    assert review.state == "candidate"
    assert configurations.approval_at(
        snapshot.snapshot_id, (submitted_at + timedelta(minutes=1)).isoformat()
    )["state_at_capture"] == "candidate"

    approved = configurations.transition(
        snapshot.snapshot_id,
        "approved",
        actor="admin",
        expected_revision=review.revision,
        now=approved_at,
    )
    current = configurations.set_current(
        snapshot.snapshot_id,
        actor="admin",
        expected_revision=0,
        now=approved_at,
    )
    assert approved.state == "approved"
    assert current.revision == 1
    assert configurations.approval_at(
        snapshot.snapshot_id, (approved_at + timedelta(seconds=1)).isoformat()
    )["approved_at_capture"]

    replacement, replacement_review = configurations.submit(
        submission(suffix="replacement"),
        actor="member",
        now=approved_at + timedelta(minutes=1),
    )
    replacement_review = configurations.transition(
        replacement.snapshot_id,
        "approved",
        actor="admin",
        expected_revision=replacement_review.revision,
        now=approved_at + timedelta(minutes=2),
    )
    configurations.set_current(
        replacement.snapshot_id,
        actor="admin",
        expected_revision=current.revision,
        now=approved_at + timedelta(minutes=3),
    )
    revoked = configurations.transition(
        snapshot.snapshot_id,
        "revoked",
        actor="admin",
        expected_revision=approved.revision,
        now=revoked_at,
    )
    assert replacement_review.state == "approved"
    assert revoked.state == "revoked"
    assert configurations.approval_at(
        snapshot.snapshot_id, (revoked_at + timedelta(seconds=1)).isoformat()
    )["state_at_capture"] == "revoked"


def test_duplicate_content_is_rejected_even_when_metadata_changes(tmp_path: Path) -> None:
    configurations = DeviceConfigurationStore(LocalFilesystemStore(tmp_path / "objects"))
    configurations.submit(submission(), actor="first")
    duplicate = submission().model_copy(update={"name": "renamed duplicate"})

    with pytest.raises(ObjectConflictError, match="相同配置内容已发布"):
        configurations.submit(duplicate, actor="second")


def test_hash_tampering_is_detected(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path / "objects")
    configurations = DeviceConfigurationStore(store)
    snapshot, _review = configurations.submit(submission(), actor="member")
    key = configurations.snapshot_key(snapshot.snapshot_id)
    payload, generation = store.read_json(key)
    payload["name"] = "tampered"
    store.write_json(key, payload, if_generation_match=generation)

    with pytest.raises(ValueError, match="Snapshot SHA-256"):
        configurations.get_snapshot(snapshot.snapshot_id)


def test_si_profile_id_is_normalized_from_sensor_and_coefficients() -> None:
    content = submission().content.model_copy(deep=True)
    previous = content.devices[0].si_profile.profile_id
    content.devices[0].si_profile.accel_counts_per_g = 8192.0

    normalized = ConfigurationContentV2.model_validate(content.model_dump(mode="json"))

    assert normalized.devices[0].si_profile.profile_id != previous


def test_identity_allocator_considers_devices_already_in_snapshots(tmp_path: Path) -> None:
    configurations = DeviceConfigurationStore(LocalFilesystemStore(tmp_path / "objects"))
    configurations.submit(submission(), actor="member")

    asset = configurations.reserve_asset(actor="member")
    revision = configurations.reserve_revision("IMU-0002", actor="member")

    assert asset["sensor_sn"] == "IMU-0003-R01"
    assert revision["sensor_sn"] == "IMU-0002-R02"
    assert revision["supersedes_sn"] == "IMU-0002-R01"


def test_local_selection_persists_and_revoked_snapshot_cannot_be_selected(
    tmp_path: Path,
) -> None:
    manager = LocalConfigurationManager(
        tmp_path / "configuration",
        tmp_path / "cache",
        REGISTRY_PATH,
    )
    local = manager.save_local(submission(suffix="local"))
    manager.select(local.snapshot_id)

    reopened = LocalConfigurationManager(
        tmp_path / "configuration",
        tmp_path / "cache",
        REGISTRY_PATH,
    )
    assert reopened.selected_snapshot_id() == local.snapshot_id
    assert reopened.status()["manually_pinned"]
    assert not reopened.resolve_imu("IMU-0001-R01").prod_capture_enabled

    remote_store = LocalFilesystemStore(tmp_path / "remote")
    remote = DeviceConfigurationStore(remote_store)
    snapshot, review = remote.submit(submission(suffix="revoked"), actor="member")
    approved = remote.transition(
        snapshot.snapshot_id,
        "approved",
        actor="admin",
        expected_revision=review.revision,
    )
    revoked = remote.transition(
        snapshot.snapshot_id,
        "revoked",
        actor="admin",
        expected_revision=approved.revision,
    )
    reopened.cache_remote(snapshot, revoked)

    with pytest.raises(ValueError, match="revoked"):
        reopened.select(snapshot.snapshot_id)


def test_local_cache_rejects_unknown_schema_without_replacing_lkg(tmp_path: Path) -> None:
    manager = LocalConfigurationManager(
        tmp_path / "configuration",
        tmp_path / "cache",
        REGISTRY_PATH,
    )
    before = manager.selected_snapshot_id()
    invalid_path = manager.cache_root / "cfg-invalid.json"
    invalid_path.write_text(json.dumps({"object_schema": "future-v99"}), encoding="utf-8")

    reopened = LocalConfigurationManager(
        tmp_path / "configuration",
        tmp_path / "cache",
        REGISTRY_PATH,
    )
    assert reopened.selected_snapshot_id() == before


def test_unknown_protocol_disables_only_that_device(tmp_path: Path) -> None:
    manager = LocalConfigurationManager(
        tmp_path / "configuration",
        tmp_path / "cache",
        REGISTRY_PATH,
    )
    draft = submission(suffix="future protocol")
    draft.content.devices[1].protocol_id = "future_protocol_v99"
    snapshot = manager.save_local(draft)
    manager.select(snapshot.snapshot_id)

    assert manager.resolve_imu("IMU-0001-R01").protocol == "cw12eu_v1"
    with pytest.raises(ValueError, match="unsupported IMU protocol"):
        manager.resolve_imu("IMU-0002-R01")


def test_review_model_rejects_local_snapshot_ids() -> None:
    with pytest.raises(ValueError):
        ConfigurationReviewV2.model_validate(
            {
                "snapshot_id": "local-cfg-" + "0" * 24,
                "updated_by": "member",
                "updated_at_utc": datetime.now(UTC),
            }
        )


def test_bootstrap_lock_requires_matching_plan_and_source_files(tmp_path: Path) -> None:
    registry_path = tmp_path / "imu-devices.yaml"
    evidence_path = tmp_path / "calibration-evidence.yaml"
    shutil.copy2(REGISTRY_PATH, registry_path)
    shutil.copy2(REGISTRY_PATH.parent / evidence_path.name, evidence_path)
    plan = migration_preview(registry_path)

    with pytest.raises(ValueError, match="计划已变化"):
        apply_migration_lock(
            registry_path,
            output=None,
            plan_token="wrong",
            confirmation=MIGRATION_CONFIRMATION,
        )

    result = apply_migration_lock(
        registry_path,
        output=None,
        plan_token=plan["plan_token"],
        confirmation=MIGRATION_CONFIRMATION,
    )
    assert result["cloud_objects_written"] == 0
    assert bootstrap_snapshot_from_registry(registry_path).snapshot_id == result["snapshot_id"]

    evidence_path.write_text("changed: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="源文件不匹配"):
        bootstrap_snapshot_from_registry(registry_path)


async def test_broker_refresh_caches_current_without_overriding_manual_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = DeviceConfigurationStore(LocalFilesystemStore(tmp_path / "remote"))
    snapshot, review = remote.submit(submission(suffix="team"), actor="member")
    review = remote.transition(
        snapshot.snapshot_id,
        "approved",
        actor="admin",
        expected_revision=review.revision,
    )
    remote.set_current(
        snapshot.snapshot_id,
        actor="admin",
        expected_revision=0,
    )
    manager = LocalConfigurationManager(
        tmp_path / "configuration",
        tmp_path / "cache",
        REGISTRY_PATH,
    )
    pinned = manager.save_local(submission(suffix="pinned"))
    manager.select(pinned.snapshot_id)
    settings = Settings()
    settings.cloud.broker_url = "https://broker.example.test"
    advertised_current: list[str | None] = [snapshot.snapshot_id]

    class FakeAuth:
        @staticmethod
        def id_token() -> str:
            return "test-token"

    def fake_get(url: str, token: str, *, timeout: int = 30) -> dict:
        assert token == "test-token"
        assert timeout == 30
        if url.endswith("/v2/device-config/snapshots"):
            return {
                "current_snapshot_id": advertised_current[0],
                "snapshots": [{"snapshot_id": snapshot.snapshot_id}],
            }
        assert url.endswith(f"/v2/device-config/snapshots/{snapshot.snapshot_id}")
        return {
            "snapshot": snapshot.model_dump(mode="json"),
            "review": review.model_dump(mode="json"),
        }

    async def inline_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(broker_client, "_broker_get", fake_get)
    monkeypatch.setattr(broker_client.asyncio, "to_thread", inline_thread)

    result = await refresh_device_configuration_via_broker(
        settings,
        FakeAuth(),  # type: ignore[arg-type]
        manager,
    )

    assert result["cached_snapshots"] == 1
    assert result["current_snapshot_id"] == snapshot.snapshot_id
    assert result["selected_snapshot_id"] == pinned.snapshot_id
    assert result["manually_pinned"]
    assert result["update_available"]
    assert manager.reset_current().snapshot_id == snapshot.snapshot_id

    manager.select(pinned.snapshot_id)
    advertised_current[0] = None
    cleared = await refresh_device_configuration_via_broker(
        settings,
        FakeAuth(),  # type: ignore[arg-type]
        manager,
    )
    assert cleared["current_snapshot_id"] is None
    assert cleared["selected_snapshot_id"] == pinned.snapshot_id
    assert not cleared["update_available"]
