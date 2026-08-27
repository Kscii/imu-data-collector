from typing import Any

import pytest

import imu_data_collector.ble as ble_module
from imu_data_collector.ble import CW12EUBleSource
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

    source = CW12EUBleSource(ImuSettings())
    await source.connect()

    assert captured["bearer_path"] == cached_path
    assert captured["device"].details["path"] == cached_path
    assert captured["connected"] is True
    assert source.connected
