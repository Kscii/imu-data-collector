from typing import Any

import pytest

import imu_data_collector.ble as ble_module
from imu_data_collector.ble import BleOperationError, CW12EUBleSource
from imu_data_collector.config import ImuSettings


@pytest.mark.asyncio
async def test_cached_bluez_device_path_bypasses_advertisement_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached_path = "/org/bluez/hci0/dev_83_FC_90_14_1E_A4"
    captured: dict[str, Any] = {}

    async def find_cached(_address: str) -> str:
        return cached_path

    async def connect_bearer(path: str) -> None:
        captured["bearer_path"] = path

    class ScannerMustNotStart:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise AssertionError("已有 BlueZ path 时不应重新扫描广播")

    class FakeClient:
        def __init__(self, device: Any, **kwargs: Any) -> None:
            captured["device"] = device
            captured["client_kwargs"] = kwargs

        async def connect(self) -> None:
            captured["connected"] = True

    monkeypatch.setattr(
        CW12EUBleSource, "_find_cached_device_path", staticmethod(find_cached)
    )
    monkeypatch.setattr(
        CW12EUBleSource, "_connect_le_bearer", staticmethod(connect_bearer)
    )
    monkeypatch.setattr(ble_module, "BleakScanner", ScannerMustNotStart)
    monkeypatch.setattr(ble_module, "BleakClient", FakeClient)
    monkeypatch.setattr(ble_module.sys, "platform", "linux")

    source = CW12EUBleSource(ImuSettings())
    await source.connect()

    assert captured["bearer_path"] == cached_path
    assert captured["device"].details["path"] == cached_path
    assert captured["connected"] is True
    assert source.connected


@pytest.mark.asyncio
async def test_windows_connect_uses_fixed_public_address_without_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class ScannerMustNotStart:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("Windows 固定样机应先按 public address 直连")

    class FakeClient:
        def __init__(self, device: Any, **kwargs: Any) -> None:
            captured["device"] = device
            captured["client_kwargs"] = kwargs
            self.is_connected = False

        async def connect(self) -> None:
            self.is_connected = True
            return None

    monkeypatch.setattr(ble_module.sys, "platform", "win32")
    monkeypatch.setattr(ble_module, "BleakScanner", ScannerMustNotStart)
    monkeypatch.setattr(ble_module, "BleakClient", FakeClient)

    source = CW12EUBleSource(ImuSettings())
    await source.connect()

    assert captured["device"].address == "83:FC:90:14:1E:A4"
    assert captured["device"].details is None
    assert captured["client_kwargs"]["winrt"] == {
        "address_type": "public",
        "use_cached_services": False,
    }
    assert source.device_identifier == "83:FC:90:14:1E:A4"
    assert source.backend_name == "bleak_winrt"


@pytest.mark.asyncio
async def test_scan_timeout_reports_target_not_advertising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyScanner:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(ble_module.sys, "platform", "darwin")
    monkeypatch.setattr(ble_module, "BleakScanner", EmptyScanner)

    source = CW12EUBleSource(ImuSettings())
    with pytest.raises(BleOperationError) as caught:
        await source.connect(timeout=0.001)

    assert caught.value.code == "imu_not_advertising"
    assert caught.value.phase == "scan"
    assert "手机" in caught.value.hint


@pytest.mark.asyncio
async def test_gatt_discovery_failure_has_stable_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingClient:
        def __init__(self, _device: Any, **_kwargs: Any) -> None:
            self.is_connected = False

        async def connect(self) -> None:
            raise RuntimeError("failed to discover services, device disconnected")

    monkeypatch.setattr(ble_module.sys, "platform", "win32")
    monkeypatch.setattr(ble_module, "BleakClient", FailingClient)

    source = CW12EUBleSource(ImuSettings())
    with pytest.raises(BleOperationError) as caught:
        await source.connect()

    assert caught.value.code == "imu_gatt_discovery_failed"
    assert caught.value.phase == "gatt"
