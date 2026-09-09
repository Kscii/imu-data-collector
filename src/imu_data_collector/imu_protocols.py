"""Supported, code-reviewed BLE protocol contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ImuProtocolSpec:
    protocol_id: str
    advertised_service_uuid: str | None
    notify_uuid: str
    frame_size_bytes: int
    force_le_bearer: bool
    byte_order: str
    device_clock: str | None


PROTOCOLS = {
    "cw12eu_v1": ImuProtocolSpec(
        protocol_id="cw12eu_v1",
        advertised_service_uuid=None,
        notify_uuid="00002ae1-0000-1000-8000-00805f9b34fb",
        frame_size_bytes=16,
        force_le_bearer=True,
        byte_order="big_endian",
        device_clock=None,
    ),
    "acce_gyro_abf0_v1": ImuProtocolSpec(
        protocol_id="acce_gyro_abf0_v1",
        advertised_service_uuid="0000abf0-0000-1000-8000-00805f9b34fb",
        notify_uuid="0000abf2-0000-1000-8000-00805f9b34fb",
        frame_size_bytes=22,
        force_le_bearer=False,
        byte_order="little_endian",
        device_clock="uint64_little_endian_milliseconds",
    ),
}


def protocol_spec(protocol_id: str) -> ImuProtocolSpec:
    try:
        return PROTOCOLS[protocol_id]
    except KeyError as error:
        raise ValueError(f"unsupported IMU protocol: {protocol_id}") from error
