import json
import shutil
from pathlib import Path

import pytest
import yaml

from imu_data_collector.device_registry import (
    DeviceCandidateStore,
    DeviceDraftStore,
    DeviceRegistryDocument,
    ImuDeviceProfile,
    activate_registry_snapshot,
    available_device_profiles,
    load_device_registry,
    publish_device_registry,
    refresh_device_registry_cache,
    registry_cache_metadata_path,
    resolve_available_imu_settings,
    resolve_imu_settings,
    validate_registry_cache,
)
from imu_data_collector.storage import LocalFilesystemStore

REGISTRY_PATH = Path("configs/imu-devices.yaml").resolve()


def test_tracked_registry_resolves_each_sn_to_its_own_contract() -> None:
    registry = load_device_registry(REGISTRY_PATH)
    old = resolve_imu_settings(REGISTRY_PATH, "IMU-0001-R01")
    new = resolve_imu_settings(REGISTRY_PATH, "IMU-0002-R01")

    assert registry.next_sensor_sn() == "IMU-0003-R01"
    assert old.protocol == "cw12eu_v1"
    assert old.calibration_verified
    assert old.accel_counts_per_g == 4096.0
    assert old.prod_capture_enabled
    assert new.protocol == "acce_gyro_abf0_v1"
    assert new.expected_rate_hz == 50.0
    assert not new.calibration_verified
    assert new.accel_counts_per_g is None
    assert new.candidate_accel_counts_per_g == 16384.0
    assert not new.prod_capture_enabled


def test_local_draft_is_selectable_but_cannot_claim_production_authority(
    tmp_path: Path,
) -> None:
    store = DeviceDraftStore(tmp_path / "drafts.json")
    profile = ImuDeviceProfile.model_validate(
        {
            "sensor_sn": "IMU-0003-R01",
            "hardware_asset_id": "IMU-0003",
            "revision": 1,
            "lifecycle": "commissioning",
            "display_name": "test candidate",
            "identity": {
                "advertised_name": "acce&gyro_TEST",
                "public_address": "AA:BB:CC:DD:EE:FF",
                "address_type": "public",
                "advertised_service_uuid": "0000abf0-0000-1000-8000-00805f9b34fb",
            },
            "firmware": {"version": "unknown", "evidence_status": "unreadable"},
            "protocol_id": "acce_gyro_abf0_v1",
            "expected_rate_hz": 50.0,
            "expected_rate_status": "operator_candidate",
            "prod_capture_enabled": False,
        }
    )

    store.save(profile)
    profiles = available_device_profiles(REGISTRY_PATH, store)
    settings, resolved, source = resolve_available_imu_settings(
        REGISTRY_PATH, "IMU-0003-R01", store
    )

    assert [item.sensor_sn for item, _source in profiles][-1] == "IMU-0003-R01"
    assert resolved == profile
    assert source == "local_draft"
    assert settings.protocol == "acce_gyro_abf0_v1"
    assert not settings.prod_capture_enabled
    assert "sensor_sn: IMU-0003-R01" in store.export_yaml("IMU-0003-R01")


def test_registry_rejects_duplicate_active_public_address() -> None:
    payload = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    duplicate = dict(payload["devices"][1])
    duplicate["sensor_sn"] = "IMU-0003-R01"
    duplicate["hardware_asset_id"] = "IMU-0003"
    payload["devices"].append(duplicate)

    with pytest.raises(ValueError, match="同时属于非 retired"):
        DeviceRegistryDocument.model_validate(payload)


def test_firmware_revision_requires_an_unbroken_same_asset_supersedes_chain() -> None:
    payload = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))["devices"][1]
    payload["sensor_sn"] = "IMU-0002-R02"
    payload["revision"] = 2
    payload["lifecycle"] = "commissioning"
    payload["prod_capture_enabled"] = False

    with pytest.raises(ValueError, match="必须 supersede IMU-0002-R01"):
        ImuDeviceProfile.model_validate(payload)

    payload["supersedes_sn"] = "IMU-0002-R01"
    assert ImuDeviceProfile.model_validate(payload).revision == 2


def test_retired_profile_remains_historical_authority_but_is_not_selectable(
    tmp_path: Path,
) -> None:
    payload = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    payload["devices"][1]["lifecycle"] = "retired"
    registry_path = tmp_path / "imu-devices.yaml"
    registry_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    historical = resolve_imu_settings(registry_path, "IMU-0002-R01")
    assert historical.sensor_sn == "IMU-0002-R01"

    with pytest.raises(KeyError):
        resolve_available_imu_settings(registry_path, "IMU-0002-R01")


def test_local_draft_cannot_reference_calibration_file(tmp_path: Path) -> None:
    payload = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))["devices"][1]
    payload["sensor_sn"] = "IMU-0003-R01"
    payload["hardware_asset_id"] = "IMU-0003"
    payload["calibration_evidence_path"] = "arbitrary.yaml"
    profile = ImuDeviceProfile.model_validate(payload)

    with pytest.raises(ValueError, match="不能声明正式校准证据"):
        DeviceDraftStore(tmp_path / "drafts.json").save(profile)


def test_registry_distribution_uses_verified_immutable_snapshot_and_lkg_cache(
    tmp_path: Path,
) -> None:
    store = LocalFilesystemStore(tmp_path / "objects")

    first = publish_device_registry(store, REGISTRY_PATH)
    second = publish_device_registry(store, REGISTRY_PATH)
    cache_path = tmp_path / "cache" / "imu-devices.json"
    cached = refresh_device_registry_cache(store, cache_path)

    assert first["snapshot_sha256"] == second["snapshot_sha256"]
    assert first["registry_revision"] == 1
    assert first["snapshot_object_key"].endswith("/registry.json")
    assert cached.by_sn("IMU-0002-R01").expected_rate_hz == 50.0
    assert load_device_registry(cache_path) == cached
    assert json.loads(
        registry_cache_metadata_path(cache_path).read_text(encoding="utf-8")
    )["snapshot_sha256"] == first["snapshot_sha256"]
    assert validate_registry_cache(cache_path) == cached
    assert (cache_path.parent / "calibration-evidence.yaml").is_file()
    source_settings = resolve_imu_settings(REGISTRY_PATH, "IMU-0001-R01")
    cached_settings = resolve_imu_settings(cache_path, "IMU-0001-R01")
    assert cached_settings.calibration_verified
    assert cached_settings.device_profile_sha256 == source_settings.device_profile_sha256


def test_registry_cache_refuses_revision_rollback(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path / "objects")
    first = publish_device_registry(store, REGISTRY_PATH)
    first_snapshot, _ = store.read_json(first["snapshot_object_key"])
    cache_path = tmp_path / "cache" / "imu-devices.json"
    refresh_device_registry_cache(store, cache_path)

    revision_two = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    revision_two["registry_revision"] = 2
    revision_two["devices"][1]["expected_rate_status"] = "reviewed_config_revision_2"
    registry_path = tmp_path / "revision-two" / "imu-devices.yaml"
    registry_path.parent.mkdir()
    registry_path.write_text(yaml.safe_dump(revision_two), encoding="utf-8")
    shutil.copy(
        REGISTRY_PATH.parent / "calibration-evidence.yaml",
        registry_path.parent / "calibration-evidence.yaml",
    )
    publish_device_registry(store, registry_path)
    assert refresh_device_registry_cache(store, cache_path).registry_revision == 2

    with pytest.raises(ValueError, match="拒绝发布比 current 更旧"):
        publish_device_registry(store, REGISTRY_PATH)
    with pytest.raises(ValueError, match="拒绝回滚"):
        activate_registry_snapshot(cache_path, first, first_snapshot)


def test_registry_publish_requires_new_revision_for_changed_content(
    tmp_path: Path,
) -> None:
    store = LocalFilesystemStore(tmp_path / "objects")
    publish_device_registry(store, REGISTRY_PATH)
    changed = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    changed["devices"][1]["expected_rate_status"] = "changed_without_revision"
    registry_path = tmp_path / "changed" / "imu-devices.yaml"
    registry_path.parent.mkdir()
    registry_path.write_text(yaml.safe_dump(changed), encoding="utf-8")
    shutil.copy(
        REGISTRY_PATH.parent / "calibration-evidence.yaml",
        registry_path.parent / "calibration-evidence.yaml",
    )

    with pytest.raises(ValueError, match="相同设备注册表 revision"):
        publish_device_registry(store, registry_path)


def test_local_candidate_override_is_diagnostic_and_exportable(tmp_path: Path) -> None:
    candidates = DeviceCandidateStore(tmp_path / "candidates.json")
    candidate = candidates.save(
        "IMU-0002-R01",
        {
            "accel_counts_per_g": 16384.0,
            "gyro_counts_per_dps": 32.8,
            "accel_bias_counts": [1, 2, 3],
            "gyro_bias_counts": [4, 5, 6],
            "raw_axis_order": [2, 1, 0],
            "axis_signs": [1, -1, 1],
            "evidence_status": "local_test_only",
        },
    )
    settings, _profile, source = resolve_available_imu_settings(
        REGISTRY_PATH,
        "IMU-0002-R01",
        DeviceDraftStore(tmp_path / "drafts.json"),
        candidates,
    )

    assert candidate.accel_counts_per_g == 16384.0
    assert source == "tracked"
    assert settings.candidate_conversion_source == "local_override"
    assert settings.candidate_accel_bias_counts == (1.0, 2.0, 3.0)
    assert settings.accel_counts_per_g is None
    assert not settings.calibration_verified
    assert "production_authority: false" in candidates.export_yaml("IMU-0002-R01")


def test_registry_cache_rejects_tampered_calibration_evidence(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path / "objects")
    publish_device_registry(store, REGISTRY_PATH)
    cache_path = tmp_path / "cache" / "imu-devices.json"
    refresh_device_registry_cache(store, cache_path)
    (cache_path.parent / "calibration-evidence.yaml").write_text(
        "tampered: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="校准证据哈希不一致"):
        validate_registry_cache(cache_path)
