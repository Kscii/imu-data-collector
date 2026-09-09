"""保存仅对当前电脑有效的设备标识。

macOS CoreBluetooth 不暴露 BLE MAC 地址，而是为每台电脑生成稳定 UUID。
该 UUID 只用于缩短后续连接路径；校准档案仍由配置中的物理设备标识约束。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from imu_data_collector.host import user_data_dir


@dataclass(frozen=True, slots=True)
class ImuBinding:
    sensor_sn: str
    device_name: str
    local_device_id: str
    notify_uuid: str
    verified_at_utc: str


class DeviceBindingStore:
    """原子读写本机 IMU 绑定；损坏文件按未绑定处理。"""

    schema_version = "2.0.0"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or user_data_dir() / "device-bindings.json"

    def _read(self) -> dict[str, object]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"schema_version": self.schema_version, "imu": {}}
        if payload.get("schema_version") == self.schema_version:
            return payload
        # v1 only stored one binding. Keep it readable long enough for the
        # configured SN to claim it, then the next save migrates to v2.
        legacy = payload.get("imu") or {}
        if isinstance(legacy, dict) and legacy.get("schema_version") == "1.0.0":
            return {"schema_version": "1.0.0", "legacy_imu": legacy, "imu": {}}
        return {"schema_version": self.schema_version, "imu": {}}

    def load_imu(
        self,
        *,
        sensor_sn: str,
        expected_name: str,
        notify_uuid: str,
    ) -> ImuBinding | None:
        payload = self._read()
        items = payload.get("imu") or {}
        item = items.get(sensor_sn) if isinstance(items, dict) else None
        if item is None and payload.get("schema_version") == "1.0.0":
            legacy = payload.get("legacy_imu")
            if isinstance(legacy, dict):
                item = {**legacy, "sensor_sn": sensor_sn}
                item.pop("schema_version", None)
        try:
            binding = ImuBinding(**item)
        except (ValueError, TypeError):
            return None
        if (
            binding.sensor_sn != sensor_sn
            or binding.device_name != expected_name
            or binding.notify_uuid.lower() != notify_uuid.lower()
        ):
            return None
        return binding

    def save_imu(
        self,
        *,
        sensor_sn: str,
        device_name: str,
        local_device_id: str,
        notify_uuid: str,
    ) -> ImuBinding:
        binding = ImuBinding(
            sensor_sn=sensor_sn,
            device_name=device_name,
            local_device_id=local_device_id,
            notify_uuid=notify_uuid,
            verified_at_utc=datetime.now(UTC).isoformat(),
        )
        payload = self._read()
        items = dict(payload.get("imu") or {})
        items[sensor_sn] = asdict(binding)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.partial")
        temporary.write_text(
            json.dumps(
                {"schema_version": self.schema_version, "imu": items},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(self.path)
        return binding

    def forget_imu(self, sensor_sn: str) -> None:
        payload = self._read()
        items = dict(payload.get("imu") or {})
        removed = items.pop(sensor_sn, None) is not None
        # A v1 file represented one global binding. Once the operator asks to
        # forget a specific SN, migrate to an empty v2 map so that legacy data
        # cannot be rediscovered as that SN on the next status read.
        if not removed and payload.get("schema_version") != "1.0.0":
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.partial")
        temporary.write_text(
            json.dumps(
                {"schema_version": self.schema_version, "imu": items},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(self.path)

    def status(
        self,
        *,
        sensor_sn: str,
        expected_name: str,
        notify_uuid: str,
    ) -> dict[str, str | None]:
        binding = self.load_imu(
            sensor_sn=sensor_sn,
            expected_name=expected_name,
            notify_uuid=notify_uuid,
        )
        return {
            "state": "bound" if binding else "unbound",
            "sensor_sn": sensor_sn,
            "device_name": expected_name,
            "local_device_id": binding.local_device_id if binding else None,
            "verified_at_utc": binding.verified_at_utc if binding else None,
        }
