import json
from pathlib import Path

from imu_data_collector.device_binding import DeviceBindingStore


def test_imu_binding_is_loaded_only_for_matching_device_contract(tmp_path: Path) -> None:
    store = DeviceBindingStore(tmp_path / "bindings.json")
    saved = store.save_imu(
        sensor_sn="IMU-0001-R01",
        device_name="CW12EU-T",
        local_device_id="EBA9B11B-72B4-46D7-900A-1C01DEADBEEF",
        notify_uuid="00002ae1-0000-1000-8000-00805f9b34fb",
    )

    assert store.load_imu(
        sensor_sn="IMU-0001-R01",
        expected_name="CW12EU-T",
        notify_uuid="00002AE1-0000-1000-8000-00805F9B34FB",
    ) == saved
    assert store.load_imu(
        sensor_sn="IMU-0001-R01",
        expected_name="OTHER",
        notify_uuid="00002ae1-0000-1000-8000-00805f9b34fb",
    ) is None


def test_imu_binding_can_be_forgotten_and_damaged_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "bindings.json"
    store = DeviceBindingStore(path)
    path.write_text("not-json", encoding="utf-8")
    assert store.status(
        sensor_sn="IMU-0001-R01", expected_name="CW12EU-T", notify_uuid="2ae1"
    )["state"] == "unbound"

    store.save_imu(
        sensor_sn="IMU-0001-R01",
        device_name="CW12EU-T",
        local_device_id="local-id",
        notify_uuid="2ae1",
    )
    store.forget_imu("IMU-0001-R01")

    assert store.status(
        sensor_sn="IMU-0001-R01", expected_name="CW12EU-T", notify_uuid="2ae1"
    )["state"] == "unbound"


def test_bindings_are_independent_per_sensor_sn(tmp_path: Path) -> None:
    store = DeviceBindingStore(tmp_path / "bindings.json")
    store.save_imu(
        sensor_sn="IMU-0001-R01",
        device_name="CW12EU-T",
        local_device_id="LOCAL-OLD",
        notify_uuid="2ae1",
    )
    store.save_imu(
        sensor_sn="IMU-0002-R01",
        device_name="acce&gyro_C18A0F78",
        local_device_id="LOCAL-NEW",
        notify_uuid="abf2",
    )

    store.forget_imu("IMU-0001-R01")

    assert store.load_imu(
        sensor_sn="IMU-0001-R01", expected_name="CW12EU-T", notify_uuid="2ae1"
    ) is None
    assert store.load_imu(
        sensor_sn="IMU-0002-R01",
        expected_name="acce&gyro_C18A0F78",
        notify_uuid="abf2",
    ).local_device_id == "LOCAL-NEW"


def test_forget_migrates_and_removes_legacy_global_binding(tmp_path: Path) -> None:
    path = tmp_path / "bindings.json"
    path.write_text(
        json.dumps(
            {
                "imu": {
                    "schema_version": "1.0.0",
                    "device_name": "CW12EU-T",
                    "local_device_id": "LEGACY-LOCAL-ID",
                    "notify_uuid": "2ae1",
                    "verified_at_utc": "2026-08-01T00:00:00+00:00",
                }
            }
        ),
        encoding="utf-8",
    )
    store = DeviceBindingStore(path)

    assert store.load_imu(
        sensor_sn="IMU-0001-R01", expected_name="CW12EU-T", notify_uuid="2ae1"
    ) is not None
    store.forget_imu("IMU-0001-R01")

    assert store.load_imu(
        sensor_sn="IMU-0001-R01", expected_name="CW12EU-T", notify_uuid="2ae1"
    ) is None
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == "2.0.0"
