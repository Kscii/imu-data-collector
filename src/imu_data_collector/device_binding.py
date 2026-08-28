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
    schema_version: str
    device_name: str
    local_device_id: str
    notify_uuid: str
    verified_at_utc: str


class DeviceBindingStore:
    """原子读写本机 IMU 绑定；损坏文件按未绑定处理。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or user_data_dir() / "device-bindings.json"

    def load_imu(self, *, expected_name: str, notify_uuid: str) -> ImuBinding | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            item = payload.get("imu") or {}
            binding = ImuBinding(**item)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if (
            binding.schema_version != "1.0.0"
            or binding.device_name != expected_name
            or binding.notify_uuid.lower() != notify_uuid.lower()
        ):
            return None
        return binding

    def save_imu(
        self, *, device_name: str, local_device_id: str, notify_uuid: str
    ) -> ImuBinding:
        binding = ImuBinding(
            schema_version="1.0.0",
            device_name=device_name,
            local_device_id=local_device_id,
            notify_uuid=notify_uuid,
            verified_at_utc=datetime.now(UTC).isoformat(),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.partial")
        temporary.write_text(
            json.dumps({"imu": asdict(binding)}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(self.path)
        return binding

    def forget_imu(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def status(self, *, expected_name: str, notify_uuid: str) -> dict[str, str | None]:
        binding = self.load_imu(
            expected_name=expected_name,
            notify_uuid=notify_uuid,
        )
        return {
            "state": "bound" if binding else "unbound",
            "device_name": expected_name,
            "local_device_id": binding.local_device_id if binding else None,
            "verified_at_utc": binding.verified_at_utc if binding else None,
        }
