"""通过 BlueZ/Bleak 订阅 CW12EU-T 通知特征并采集数据。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from dbus_fast import Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType, MessageType
from dbus_fast.message import Message

from imu_data_collector.config import ImuSettings


@dataclass(frozen=True, slots=True)
class NotificationPacket:
    payload: bytes
    receive_time_ns: int


class CW12EUBleSource:
    def __init__(self, settings: ImuSettings) -> None:
        self.settings = settings
        self.queue: asyncio.Queue[NotificationPacket] = asyncio.Queue(maxsize=256)
        self.client: BleakClient | None = None
        self.connected = False
        self.notifying = False
        self.dropped_callback_packets = 0
        self.last_packet_ns: int | None = None
        self.disconnect_reason: str | None = None
        self._stopping = False

    @staticmethod
    async def _find_cached_device_path(address: str) -> str | None:
        """从 BlueZ 对象树复用已知设备；已连接设备通常不会继续广播。"""

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            reply = await bus.call(
                Message(
                    destination="org.bluez",
                    path="/",
                    interface="org.freedesktop.DBus.ObjectManager",
                    member="GetManagedObjects",
                )
            )
            if reply.message_type == MessageType.ERROR:
                raise RuntimeError(
                    "无法读取 BlueZ 设备对象树："
                    f"{reply.error_name} {reply.body}"
                )
            wanted = address.upper()
            for path, interfaces in reply.body[0].items():
                properties = interfaces.get("org.bluez.Device1")
                if not properties:
                    continue
                candidate = properties.get("Address")
                if candidate is not None and str(candidate.value).upper() == wanted:
                    return str(path)
            return None
        finally:
            bus.disconnect()
            await bus.wait_for_disconnect()

    @staticmethod
    async def _connect_le_bearer(device_path: str) -> None:
        """设置 LE 为首选承载，并绕过双模设备的通用连接选择。"""

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            preference_reply = await bus.call(
                Message(
                    destination="org.bluez",
                    path=device_path,
                    interface="org.freedesktop.DBus.Properties",
                    member="Set",
                    signature="ssv",
                    body=[
                        "org.bluez.Device1",
                        "PreferredBearer",
                        Variant("s", "le"),
                    ],
                )
            )
            if preference_reply.message_type == MessageType.ERROR:
                raise RuntimeError(
                    "无法设置 BlueZ PreferredBearer=le；请确认 bluetoothd "
                    "已使用 --experimental 启动："
                    f"{preference_reply.error_name} {preference_reply.body}"
                )

            connect_reply = await bus.call(
                Message(
                    destination="org.bluez",
                    path=device_path,
                    interface="org.bluez.Bearer.LE1",
                    member="Connect",
                )
            )
            if (
                connect_reply.message_type == MessageType.ERROR
                and connect_reply.error_name != "org.bluez.Error.AlreadyConnected"
            ):
                raise RuntimeError(
                    "BlueZ LE 承载连接失败："
                    f"{connect_reply.error_name} {connect_reply.body}"
                )
        finally:
            bus.disconnect()
            await bus.wait_for_disconnect()

    async def connect(self, timeout: float = 15.0) -> None:
        device_path = await self._find_cached_device_path(self.settings.address)
        device: BLEDevice | None = None
        if device_path is not None:
            # 直接提供 BlueZ path 可跳过 Bleak 的隐式扫描，也能接管应用异常退出后仍由
            # BlueZ 保持的 LE 连接。该设备使用 public address，路径可稳定复用。
            device = BLEDevice(
                self.settings.address,
                self.settings.name,
                {"path": device_path},
            )

        found_event = asyncio.Event()

        def detection_callback(found: Any, advertisement: Any) -> None:
            nonlocal device
            if (
                found.address.upper() == self.settings.address.upper()
                or found.name == self.settings.name
                or advertisement.local_name == self.settings.name
            ):
                device = found
                found_event.set()

        if device is None:
            scanner = BleakScanner(
                detection_callback=detection_callback,
                bluez={"filters": {"Pattern": self.settings.name}},
            )
            await scanner.start()
            try:
                await asyncio.wait_for(found_event.wait(), timeout=timeout)
                if device is None:
                    raise RuntimeError("扫描回调没有返回 CW12EU-T 设备对象")
            finally:
                await scanner.stop()

        self.client = BleakClient(
            device,
            disconnected_callback=self._on_disconnect,
            timeout=timeout,
        )
        device_path = device.details.get("path")
        if not isinstance(device_path, str):
            raise RuntimeError("BlueZ 设备对象缺少 D-Bus 路径")
        await self._connect_le_bearer(device_path)
        await asyncio.sleep(0.1)
        await self.client.connect()
        self.connected = True
        self.disconnect_reason = None
        self._stopping = False

    async def start(self, timeout: float = 15.0) -> None:
        await self.connect(timeout)
        assert self.client is not None
        try:
            await self.client.start_notify(self.settings.notify_uuid, self._on_notification)
            self.notifying = True
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        client = self.client
        self._stopping = True
        self.connected = False
        was_notifying = self.notifying
        self.notifying = False
        if client is None:
            return
        try:
            if was_notifying and client.is_connected:
                await client.stop_notify(self.settings.notify_uuid)
        finally:
            if client.is_connected:
                await client.disconnect()
            self.client = None

    def _on_notification(self, _characteristic: Any, payload: bytearray) -> None:
        received = time.monotonic_ns()
        self.last_packet_ns = received
        packet = NotificationPacket(bytes(payload), received)
        try:
            self.queue.put_nowait(packet)
        except asyncio.QueueFull:
            self.dropped_callback_packets += 1

    def _on_disconnect(self, _client: BleakClient) -> None:
        self.connected = False
        self.notifying = False
        self.disconnect_reason = "local_stop" if self._stopping else "bluez_disconnected"

    @staticmethod
    async def discover(
        timeout: float = 5.0,
        settings: ImuSettings | None = None,
    ) -> list[dict[str, Any]]:
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        output: list[dict[str, Any]] = []
        for address, (device, advertisement) in devices.items():
            name = device.name or advertisement.local_name
            if settings and not (
                address.upper() == settings.address.upper() or name == settings.name
            ):
                continue
            output.append(
                {
                    "address": address,
                    "name": name,
                    "rssi": advertisement.rssi,
                    "service_uuids": advertisement.service_uuids,
                }
            )
        return sorted(output, key=lambda item: (item["name"] or "", item["address"]))
