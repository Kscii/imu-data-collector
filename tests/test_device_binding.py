from pathlib import Path

from imu_data_collector.device_binding import DeviceBindingStore


def test_imu_binding_is_loaded_only_for_matching_device_contract(tmp_path: Path) -> None:
    store = DeviceBindingStore(tmp_path / "bindings.json")
    saved = store.save_imu(
        device_name="CW12EU-T",
        local_device_id="EBA9B11B-72B4-46D7-900A-1C01DEADBEEF",
        notify_uuid="00002ae1-0000-1000-8000-00805f9b34fb",
    )

    assert store.load_imu(
        expected_name="CW12EU-T",
        notify_uuid="00002AE1-0000-1000-8000-00805F9B34FB",
    ) == saved
    assert store.load_imu(
        expected_name="OTHER",
        notify_uuid="00002ae1-0000-1000-8000-00805f9b34fb",
    ) is None


def test_imu_binding_can_be_forgotten_and_damaged_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "bindings.json"
    store = DeviceBindingStore(path)
    path.write_text("not-json", encoding="utf-8")
    assert store.status(expected_name="CW12EU-T", notify_uuid="2ae1")["state"] == "unbound"

    store.save_imu(
        device_name="CW12EU-T",
        local_device_id="local-id",
        notify_uuid="2ae1",
    )
    store.forget_imu()

    assert not path.exists()
