"""通过 Bleak 的系统后端订阅 CW12EU-T 通知特征并采集数据。"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from imu_data_collector.config import ImuSettings
from imu_data_collector.host import platform_id

if sys.platform.startswith("linux"):
    from dbus_fast import Variant
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import BusType, MessageType
    from dbus_fast.message import Message


@dataclass(frozen=True, slots=True)
class NotificationPacket:
    payload: bytes
    receive_time_ns: int


class BleOperationError(RuntimeError):
    """带稳定阶段码的 BLE 失败，供 API、日志和 WebUI 使用。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str,
        hint: str,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.hint = hint
        self.retryable = retryable

    def as_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "component": "ble",
            "phase": self.phase,
            "message": str(self),
            "hint": self.hint,
            "retryable": self.retryable,
        }


def _scanner_error(error: Exception) -> BleOperationError:
    message = str(error).strip() or type(error).__name__
    lowered = message.lower()
    if any(
        marker in lowered
        for marker in (
            "bluetooth device is turned off",
            "no bluetooth",
            "adapter not found",
            "radio is off",
            "access denied",
        )
    ):
        return BleOperationError(
            "ble_adapter_unavailable",
            f"系统蓝牙适配器不可用：{message}",
            phase="scan",
            hint="请开启 Windows 蓝牙并确认系统已允许桌面应用访问蓝牙",
        )
    return BleOperationError(
        "ble_scan_failed",
        f"BLE 扫描失败：{message}",
        phase="scan",
        hint="请检查 Windows 蓝牙服务和适配器状态后重试",
    )


def _connect_error(error: Exception) -> BleOperationError:
    message = str(error).strip() or type(error).__name__
    lowered = message.lower()
    if any(marker in lowered for marker in ("access denied", "auth", "pair")):
        return BleOperationError(
            "imu_pairing_required",
            f"Windows 拒绝访问 IMU：{message}",
            phase="connect",
            hint="请先在 Windows 蓝牙设置中完成一次系统配对，然后返回数采平台重试",
        )
    if isinstance(error, TimeoutError):
        return BleOperationError(
            "imu_gatt_timeout",
            f"已按固定地址访问 IMU，但 GATT 服务发现超时：{message}",
            phase="gatt",
            hint="确认 IMU 未被手机或其他电脑连接；重新上电后再试",
        )
    if "service" in lowered and any(
        marker in lowered for marker in ("discover", "enumerat", "resolve")
    ):
        return BleOperationError(
            "imu_gatt_discovery_failed",
            f"已找到 IMU，但 GATT 服务发现失败：{message}",
            phase="gatt",
            hint="确认 IMU 未被手机或其他电脑连接；重新上电后再试",
        )
    return BleOperationError(
        "imu_connect_failed",
        f"已找到 IMU，但连接失败：{message}",
        phase="connect",
        hint="确认 IMU 未被手机或其他电脑连接；重新上电后再试",
    )


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
        self.device_identifier: str | None = None
        self.backend_name = {
            "linux": "bleak_bluez",
            "windows": "bleak_winrt",
            "macos": "bleak_corebluetooth",
        }.get(platform_id(), "bleak_unknown")
        self._stopping = False

    @staticmethod
    async def _find_cached_device_path(address: str) -> str | None:
        """从 BlueZ 对象树复用已知设备；已连接设备通常不会继续广播。"""

        if not sys.platform.startswith("linux"):
            return None

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

        if not sys.platform.startswith("linux"):
            return

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
        is_linux = sys.platform.startswith("linux")
        is_windows = sys.platform.startswith("win")
        device_path = (
            await self._find_cached_device_path(self.settings.address)
            if is_linux
            else None
        )
        device: BLEDevice | None = None
        if device_path is not None:
            # 直接提供 BlueZ path 可跳过 Bleak 的隐式扫描，也能接管应用异常退出后仍由
            # BlueZ 保持的 LE 连接。该设备使用 public address，路径可稳定复用。
            device = BLEDevice(
                self.settings.address,
                self.settings.name,
                {"path": device_path},
            )
        elif is_windows:
            # 当前 CW12EU-T 使用 public address。实机验证表明 WinRT 扫描看不到其
            # 广播，但按地址创建 BluetoothLEDevice 后可以访问 GATT；同时必须关闭
            # Windows 的服务缓存，否则 get_gatt_services_async 可能持续超时。
            device = BLEDevice(
                self.settings.address,
                self.settings.name,
                None,
            )

        found_event = asyncio.Event()

        def detection_callback(found: Any, advertisement: Any) -> None:
            nonlocal device
            if (
                found.address.upper() == self.settings.address.upper()
                or (
                    self.settings.local_device_id
                    and found.address == self.settings.local_device_id
                )
                or found.name == self.settings.name
                or advertisement.local_name == self.settings.name
            ):
                device = found
                found_event.set()

        if device is None:
            scanner_options: dict[str, Any] = {
                "detection_callback": detection_callback,
            }
            if is_linux:
                scanner_options["bluez"] = {
                    "filters": {"Pattern": self.settings.name}
                }
            scanner = BleakScanner(**scanner_options)
            try:
                await scanner.start()
            except Exception as error:
                raise _scanner_error(error) from error
            try:
                try:
                    await asyncio.wait_for(found_event.wait(), timeout=timeout)
                except TimeoutError as error:
                    raise BleOperationError(
                        "imu_not_advertising",
                        f"在 {timeout:.0f} 秒扫描期内未发现 {self.settings.name}",
                        phase="scan",
                        hint=(
                            "关闭手机自动重连和其他电脑上的 IMU 会话，"
                            "将 IMU 重新上电并进入匹配状态后重试"
                        ),
                    ) from error
                if device is None:
                    raise RuntimeError("扫描回调没有返回 CW12EU-T 设备对象")
            finally:
                await scanner.stop()

        client_options: dict[str, Any] = {}
        if is_windows:
            client_options["winrt"] = {
                "address_type": "public",
                "use_cached_services": False,
            }
        self.client = BleakClient(
            device,
            disconnected_callback=self._on_disconnect,
            timeout=timeout,
            **client_options,
        )
        self.device_identifier = str(device.address)
        if is_linux:
            device_path = device.details.get("path")
            if not isinstance(device_path, str):
                raise RuntimeError("BlueZ 设备对象缺少 D-Bus 路径")
            await self._connect_le_bearer(device_path)
            await asyncio.sleep(0.1)
        try:
            await self.client.connect()
        except Exception as error:
            client = self.client
            self.client = None
            if client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            raise _connect_error(error) from error
        self.connected = True
        self.disconnect_reason = None
        self._stopping = False

    async def start(self, timeout: float = 15.0) -> None:
        await self.connect(timeout)
        assert self.client is not None
        try:
            await self.client.start_notify(self.settings.notify_uuid, self._on_notification)
            self.notifying = True
        except Exception as error:
            await self.stop()
            raise BleOperationError(
                "imu_notify_failed",
                f"已连接 IMU，但无法订阅通知特征 {self.settings.notify_uuid}："
                f"{str(error).strip() or type(error).__name__}",
                phase="notify",
                hint="确认当前样机固件仍提供 0x2AE1 通知特征，然后重新连接",
            ) from error

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
        self.disconnect_reason = (
            "local_stop" if self._stopping else f"{self.backend_name}_disconnected"
        )

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
                address.upper() == settings.address.upper()
                or (settings.local_device_id and address == settings.local_device_id)
                or name == settings.name
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

    @staticmethod
    async def discover_with_diagnostics(
        timeout: float = 5.0,
        settings: ImuSettings | None = None,
    ) -> dict[str, Any]:
        """主动扫描固定目标，并返回足以区分适配器与广播问题的摘要。"""

        started = time.monotonic()
        try:
            devices = await CW12EUBleSource.discover(timeout=timeout, settings=settings)
        except BleOperationError:
            raise
        except Exception as error:
            raise _scanner_error(error) from error
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return {
            "requested": True,
            "adapter_state": "available",
            "target_name": settings.name if settings else None,
            "target_address": settings.address if settings else None,
            "target_found": bool(devices),
            "elapsed_ms": elapsed_ms,
            "devices": devices,
            "error": None,
        }
