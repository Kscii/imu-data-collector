"""Synerglobal history identities, time semantics and conservative numeric projections."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Any

PREFIX = "external-device-data/synerglobal"
BASE_URL = "https://api.synerglobal.com.au/api/open/device-history"
DEVICE_TYPES = {"radar-watch": "RADAR_WATCH", "mattress": "MATTRESS_DEVICE"}


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Time must include a timezone")
    return result.astimezone(UTC)


def utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now() -> str:
    return utc(datetime.now(UTC))


def json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def device_id(kind: str, history_no: str) -> str:
    return digest(json_bytes([kind, history_no]))[:32]


def record_id(device: str, kind: str, source_id: int) -> str:
    return digest(json_bytes([device, kind, str(source_id)]))


def numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def metrics(record: dict, device_kind: str) -> dict[str, float | None]:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return {}
    if device_kind == "mattress":
        values = payload.get("params")
        values = values if isinstance(values, dict) else payload
        fields = ("HeartRate", "RespiratoryRate", "People_flag", "D", "E", "moving", "RSSI")
        result = {field: numeric(values.get(field)) for field in fields}
        for source, target, modulus in (("People_flag", "in_bed", 2), ("D", "sleep_stage", 8)):
            value = result[source]
            result[target] = (
                value % modulus
                if (value is not None and value >= 0 and value.is_integer())
                else None
            )
        if record.get("messageType") == "REALTIME":
            result["Amp_value"] = numeric(values.get("Amp_value"))
        return result
    fields = {
        "UHR": ("HR",),
        "USPO": ("HR", "SPO"),
        "UHT": ("HT", "AT"),
        "UBP": ("SBP", "DBP"),
        "UBRR": ("BRR",),
        "LK": ("BP", "ST", "CST", "KCAL"),
    }.get(record.get("operation"), ())
    result = {field: numeric(payload.get(field)) for field in fields}
    if record.get("operation") == "LK":
        for source, target, divisor in (
            ("CO", "CO", 1),
            ("DTC", "distance_km", 100),
            ("STTIME", "STTIME", 1),
            ("KCAL", "energy_kcal", 10),
        ):
            value = numeric(payload.get(source))
            result[target] = value / divisor if value is not None else None
    return result


def is_synthetic(record: dict) -> bool:
    payload = record.get("payload")
    return isinstance(payload, dict) and payload.get("_synthetic") is True


def validate_history(response: dict, device: dict, start: str, end: str) -> list[dict]:
    """Reject partial, wrong-identity or malformed responses before publishing coverage."""
    data = response.get("data")
    if response.get("code") != 200 or not isinstance(data, dict):
        raise ValueError("Invalid upstream success envelope")
    records = data.get("records")
    if not isinstance(records, list) or type(data.get("total")) is not int:
        raise ValueError("Invalid upstream record array")
    if len(records) != data["total"] or data.get("timeField") != "receivedAt":
        raise ValueError("Incomplete upstream response")
    if (
        data.get("deviceType") != DEVICE_TYPES[device["kind"]]
        or data.get("deviceNo") != device["device_no"]
        or timestamp(data["startTime"]) != timestamp(start)
        or timestamp(data["endTime"]) != timestamp(end)
    ):
        raise ValueError("Upstream query identity or time range mismatch")
    mattress = device["kind"] == "mattress"
    if mattress and data.get("productKey") != device["product_key"]:
        raise ValueError("Upstream product identity mismatch")
    previous = None
    seen = set()
    for record in records:
        if not isinstance(record, dict) or type(record.get("id")) is not int:
            raise ValueError("Invalid upstream record identity")
        received = timestamp(record["receivedAt"])
        message = record.get("messageType" if mattress else "operation")
        if not isinstance(message, str) or not message:
            raise ValueError("Invalid upstream record type")
        if not timestamp(start) <= received < timestamp(end):
            raise ValueError("Upstream record outside requested range")
        identity = (
            (record.get("productKey"), record.get("deviceName"))
            if mattress
            else record.get("deviceNo")
        )
        expected = (device["product_key"], device["device_no"]) if mattress else device["device_no"]
        if identity != expected:
            raise ValueError("Upstream record belongs to another device")
        unique = (message if mattress else "", record["id"])
        ordering = (received, message, record["id"]) if mattress else (received, record["id"])
        if unique in seen or (previous is not None and ordering < previous):
            raise ValueError("Duplicate or unsorted upstream records")
        seen.add(unique)
        previous = ordering
    return records


def merged_intervals(intervals: list[tuple[str, str]]) -> list[list[str]]:
    result: list[list[str]] = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result
