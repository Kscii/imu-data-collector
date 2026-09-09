"""Immutable, whole-fleet device configuration snapshots.

The v2 contract deliberately keeps host/runtime configuration out of the shared
snapshot.  A snapshot contains only portable sensor identity, wire-protocol and
SI evidence needed to reproduce a capture.  Review state and the Current pointer
remain separate mutable control objects.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from imu_data_collector.device_registry import (
    DeviceIdentity,
    DeviceRegistryDocument,
    FirmwareIdentity,
    canonical_json_bytes,
    load_device_registry,
    sha256_bytes,
)
from imu_data_collector.imu_protocols import protocol_spec
from imu_data_collector.storage import ObjectConflictError, ObjectStore

CONFIGURATION_SCHEMA_VERSION = "2.0"
CONFIGURATION_SNAPSHOT_SCHEMA = "imu_device_configuration_snapshot_v2"
CONFIGURATION_REVIEW_SCHEMA = "imu_device_configuration_review_v2"
CONFIGURATION_CURRENT_SCHEMA = "imu_device_configuration_current_v2"
CONFIGURATION_EVENT_SCHEMA = "imu_device_configuration_event_v2"
CONFIGURATION_IDENTITY_SCHEMA = "imu_device_identity_reservation_v2"
CONFIGURATION_BOOTSTRAP_LOCK_SCHEMA = "imu_device_configuration_bootstrap_lock_v2"
CONFIGURATION_PREFIX = "device-config/v2"
MIGRATION_CONFIRMATION = "WRITE DEVICE CONFIGURATION V2 BOOTSTRAP LOCK"
CONFIGURATION_ID_RE = re.compile(r"^(?:local-)?cfg-[0-9a-f]{24}$")
SI_PROFILE_ID_RE = re.compile(r"^si-[0-9a-f]{24}$")
SENSOR_SN_RE = re.compile(r"^IMU-(?P<asset>[0-9]{4})-R(?P<revision>[0-9]{2})$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _atomic_json_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.partial")
    try:
        temporary.write_bytes(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            + b"\n"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ConfigurationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recording_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=80)
    summary_zh: str = Field(default="", max_length=1000)
    summary_en: str = Field(default="", max_length=1000)


class SiProfileV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(pattern=r"^si-[0-9a-f]{24}$")
    verified: bool = False
    accel_counts_per_g: float | None = Field(default=None, gt=0)
    gyro_counts_per_dps: float | None = Field(default=None, gt=0)
    accel_bias_counts: tuple[float, float, float] = (0.0, 0.0, 0.0)
    gyro_bias_counts: tuple[float, float, float] = (0.0, 0.0, 0.0)
    raw_axis_order: tuple[int, int, int] = (0, 1, 2)
    axis_signs: tuple[Literal[-1, 1], Literal[-1, 1], Literal[-1, 1]] = (1, 1, 1)
    method: str = Field(default="unverified", min_length=1, max_length=200)
    evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    coordinate_system: dict[str, str] = Field(default_factory=dict)
    evidence: list[ConfigurationEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_profile(self) -> SiProfileV2:
        if sorted(self.raw_axis_order) != [0, 1, 2]:
            raise ValueError("raw_axis_order 必须是 0、1、2 的排列")
        if self.verified and (
            self.accel_counts_per_g is None
            or self.gyro_counts_per_dps is None
            or self.evidence_sha256 is None
        ):
            raise ValueError("正式 SI Profile 必须具有两个尺度和证据 SHA-256")
        return self


class ConfigurationDeviceV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sensor_sn: str = Field(pattern=r"^IMU-[0-9]{4}-R[0-9]{2}$")
    hardware_asset_id: str = Field(pattern=r"^IMU-[0-9]{4}$")
    revision: int = Field(ge=1, le=99)
    supersedes_sn: str | None = Field(
        default=None,
        pattern=r"^IMU-[0-9]{4}-R[0-9]{2}$",
    )
    lifecycle: Literal["active", "retired"] = "active"
    display_name: str = Field(min_length=1, max_length=128)
    identity: DeviceIdentity
    firmware: FirmwareIdentity
    protocol_id: str = Field(min_length=1, max_length=128)
    expected_rate_hz: float = Field(gt=0, le=1000)
    expected_rate_status: str = Field(min_length=1, max_length=200)
    allowed_data_tiers: list[Literal["test", "prod"]] = Field(default_factory=lambda: ["test"])
    si_profile: SiProfileV2
    audit_document: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_device(self) -> ConfigurationDeviceV2:
        match = SENSOR_SN_RE.fullmatch(self.sensor_sn)
        assert match is not None
        if self.hardware_asset_id != f"IMU-{match.group('asset')}":
            raise ValueError("hardware_asset_id 必须与 sensor_sn 一致")
        if self.revision != int(match.group("revision")):
            raise ValueError("revision 必须与 sensor_sn 一致")
        expected_previous = (
            f"{self.hardware_asset_id}-R{self.revision - 1:02d}"
            if self.revision > 1
            else None
        )
        if self.supersedes_sn != expected_previous:
            raise ValueError("supersedes_sn 必须指向同一资产的上一 revision")
        if len(self.allowed_data_tiers) != len(set(self.allowed_data_tiers)):
            raise ValueError("allowed_data_tiers 不能重复")
        if "prod" in self.allowed_data_tiers and not self.si_profile.verified:
            raise ValueError("允许 prod 的设备必须具有正式 SI Profile")
        si_payload = self.si_profile.model_dump(mode="json", exclude={"profile_id"})
        # The identifier is derived metadata, not an operator-entered authority.
        # Normalize it before a submission is frozen; immutable snapshot hash
        # verification still rejects a stored object whose claimed bytes differ.
        self.si_profile.profile_id = si_profile_id(self.sensor_sn, si_payload)
        return self


class ConfigurationContentV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = CONFIGURATION_SCHEMA_VERSION
    devices: list[ConfigurationDeviceV2] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_content(self) -> ConfigurationContentV2:
        by_sn = {item.sensor_sn: item for item in self.devices}
        if len(by_sn) != len(self.devices):
            raise ValueError("配置包含重复 sensor_sn")
        active_addresses: dict[str, str] = {}
        for item in self.devices:
            if item.supersedes_sn and item.supersedes_sn not in by_sn:
                raise ValueError(f"{item.sensor_sn} 引用了不存在的 supersedes_sn")
            address = (item.identity.public_address or "").upper()
            if address and item.lifecycle == "active":
                if previous := active_addresses.get(address):
                    raise ValueError(
                        f"公共 BLE 地址 {address} 同时属于 {previous} 和 {item.sensor_sn}"
                    )
                active_addresses[address] = item.sensor_sn
        return self


class ConfigurationSnapshotSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    base_snapshot_id: str | None = Field(default=None, pattern=r"^(?:local-)?cfg-[0-9a-f]{24}$")
    client_build: str = Field(default="unknown", min_length=1, max_length=160)
    content: ConfigurationContentV2


class ConfigurationSnapshotV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_schema: Literal["imu_device_configuration_snapshot_v2"] = (
        CONFIGURATION_SNAPSHOT_SCHEMA
    )
    snapshot_id: str = Field(pattern=r"^(?:local-)?cfg-[0-9a-f]{24}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    base_snapshot_id: str | None = Field(default=None, pattern=r"^(?:local-)?cfg-[0-9a-f]{24}$")
    publisher: str = Field(min_length=1, max_length=80)
    published_at_utc: datetime
    client_build: str = Field(min_length=1, max_length=160)
    content: ConfigurationContentV2


class ConfigurationReviewV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_schema: Literal["imu_device_configuration_review_v2"] = (
        CONFIGURATION_REVIEW_SCHEMA
    )
    snapshot_id: str = Field(pattern=r"^cfg-[0-9a-f]{24}$")
    state: Literal["candidate", "approved", "revoked"] = "candidate"
    revision: int = Field(default=1, ge=1)
    updated_by: str = Field(min_length=1, max_length=80)
    updated_at_utc: datetime


class ConfigurationCurrentV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_schema: Literal["imu_device_configuration_current_v2"] = (
        CONFIGURATION_CURRENT_SCHEMA
    )
    snapshot_id: str = Field(pattern=r"^cfg-[0-9a-f]{24}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(default=1, ge=1)
    updated_by: str = Field(min_length=1, max_length=80)
    updated_at_utc: datetime


class ConfigurationReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class ConfigurationCurrentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(pattern=r"^cfg-[0-9a-f]{24}$")
    expected_revision: int = Field(ge=0)


class ConfigurationSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(pattern=r"^(?:local-)?cfg-[0-9a-f]{24}$")


class IdentityRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hardware_asset_id: str = Field(pattern=r"^IMU-[0-9]{4}$")


def content_sha256(content: ConfigurationContentV2) -> str:
    return sha256_bytes(canonical_json_bytes(content.model_dump(mode="json")))


def _snapshot_from_submission(
    submission: ConfigurationSnapshotSubmission,
    *,
    publisher: str,
    published_at_utc: datetime,
    local: bool,
) -> ConfigurationSnapshotV2:
    content_digest = content_sha256(submission.content)
    envelope = {
        "object_schema": CONFIGURATION_SNAPSHOT_SCHEMA,
        "content_sha256": content_digest,
        "name": submission.name.strip(),
        "description": submission.description.strip(),
        "base_snapshot_id": submission.base_snapshot_id,
        "publisher": publisher,
        "published_at_utc": published_at_utc.astimezone(UTC).isoformat().replace(
            "+00:00", "Z"
        ),
        "client_build": submission.client_build,
        "content": submission.content.model_dump(mode="json"),
    }
    digest = sha256_bytes(canonical_json_bytes(envelope))
    prefix = "local-cfg" if local else "cfg"
    return ConfigurationSnapshotV2(
        snapshot_id=f"{prefix}-{(content_digest if local else digest)[:24]}",
        snapshot_sha256=digest,
        **envelope,
    )


def si_profile_id(sensor_sn: str, payload: dict[str, Any]) -> str:
    material = {"sensor_sn": sensor_sn, "si": payload}
    return f"si-{sha256_bytes(canonical_json_bytes(material))[:24]}"


def _legacy_si_profile(
    sensor_sn: str,
    registry_path: Path,
    evidence_relative: str | None,
    candidate: Any,
) -> SiProfileV2:
    if evidence_relative:
        path = (registry_path.parent / evidence_relative).resolve()
        raw = path.read_bytes()
        payload = yaml.safe_load(raw) or {}
        calibration = payload.get("calibration") or {}
        si = {
            "verified": calibration.get("status") == "engineering_verified",
            "accel_counts_per_g": calibration.get("accel_counts_per_g"),
            "gyro_counts_per_dps": calibration.get("gyro_counts_per_dps"),
            "accel_bias_counts": calibration.get("accel_bias_counts_raw", [0, 0, 0]),
            "gyro_bias_counts": calibration.get("gyro_bias_counts_raw", [0, 0, 0]),
            "raw_axis_order": calibration.get("raw_axis_order", [0, 1, 2]),
            "axis_signs": calibration.get("axis_signs", [1, 1, 1]),
            "method": "six_face_static_and_multi_axis_360deg_engineering_v1",
            "evidence_sha256": sha256_bytes(raw),
            "coordinate_system": {
                str(key): str(value)
                for key, value in (payload.get("coordinate_system") or {}).items()
            },
            "evidence": [
                {
                    "recording_id": str(item.get("recording_id", "unknown")),
                    "kind": str(item.get("kind", "unknown")),
                    "summary_zh": str(item.get("observed_zh", "")),
                    "summary_en": str(item.get("observed_en", "")),
                }
                for item in payload.get("evidence", [])
            ],
        }
    else:
        si = {
            "verified": False,
            "accel_counts_per_g": getattr(candidate, "accel_counts_per_g", None),
            "gyro_counts_per_dps": getattr(candidate, "gyro_counts_per_dps", None),
            "accel_bias_counts": getattr(candidate, "accel_bias_counts", (0, 0, 0)),
            "gyro_bias_counts": getattr(candidate, "gyro_bias_counts", (0, 0, 0)),
            "raw_axis_order": getattr(candidate, "raw_axis_order", (0, 1, 2)),
            "axis_signs": getattr(candidate, "axis_signs", (1, 1, 1)),
            "method": (
                getattr(candidate, "evidence_status", "unverified")
                if candidate
                else "unverified"
            ),
            "evidence_sha256": None,
            "coordinate_system": {},
            "evidence": [],
        }
    return SiProfileV2(
        profile_id=si_profile_id(sensor_sn, si),
        **si,
    )


def migrate_registry_document(
    document: DeviceRegistryDocument,
    registry_path: Path,
) -> ConfigurationContentV2:
    devices = []
    for profile in document.devices:
        si = _legacy_si_profile(
            profile.sensor_sn,
            registry_path,
            profile.calibration_evidence_path,
            profile.candidate_conversion,
        )
        devices.append(
            ConfigurationDeviceV2(
                sensor_sn=profile.sensor_sn,
                hardware_asset_id=profile.hardware_asset_id,
                revision=profile.revision,
                supersedes_sn=profile.supersedes_sn,
                lifecycle="retired" if profile.lifecycle == "retired" else "active",
                display_name=profile.display_name,
                identity=profile.identity,
                firmware=profile.firmware,
                protocol_id=profile.protocol_id,
                expected_rate_hz=profile.expected_rate_hz,
                expected_rate_status=profile.expected_rate_status,
                allowed_data_tiers=(
                    ["test", "prod"] if profile.prod_capture_enabled else ["test"]
                ),
                si_profile=si,
                audit_document=profile.audit_document,
            )
        )
    return ConfigurationContentV2(devices=devices)


def _migrated_bootstrap_snapshot(registry_path: Path) -> ConfigurationSnapshotV2:
    document = load_device_registry(registry_path)
    submission = ConfigurationSnapshotSubmission(
        name=f"Bundled registry revision {document.registry_revision}",
        description="Automatically migrated v1 bootstrap; no cloud mutation performed.",
        client_build="v1-registry-migration",
        content=migrate_registry_document(document, registry_path),
    )
    return _snapshot_from_submission(
        submission,
        publisher="bootstrap",
        published_at_utc=datetime.fromtimestamp(registry_path.stat().st_mtime, tz=UTC),
        local=True,
    )


def _validate_snapshot_integrity(snapshot: ConfigurationSnapshotV2) -> None:
    expected_content = content_sha256(snapshot.content)
    if expected_content != snapshot.content_sha256:
        raise ValueError("配置 Snapshot content SHA-256 不匹配")
    envelope = snapshot.model_dump(mode="json", exclude={"snapshot_id", "snapshot_sha256"})
    if sha256_bytes(canonical_json_bytes(envelope)) != snapshot.snapshot_sha256:
        raise ValueError("配置 Snapshot SHA-256 不匹配")
    prefix_digest = (
        snapshot.content_sha256
        if snapshot.snapshot_id.startswith("local-cfg-")
        else snapshot.snapshot_sha256
    )
    prefix = "local-cfg" if snapshot.snapshot_id.startswith("local-cfg-") else "cfg"
    if snapshot.snapshot_id != f"{prefix}-{prefix_digest[:24]}":
        raise ValueError("配置 Snapshot ID 与 SHA-256 不匹配")


def bootstrap_lock_path(registry_path: Path) -> Path:
    return registry_path.with_name("device-config-bootstrap.lock.json")


def build_bootstrap_lock(registry_path: Path) -> dict[str, Any]:
    snapshot = _migrated_bootstrap_snapshot(registry_path)
    registry = load_device_registry(registry_path)
    inputs = {registry_path.name: sha256_bytes(registry_path.read_bytes())}
    for device in registry.devices:
        if not device.calibration_evidence_path:
            continue
        relative = Path(device.calibration_evidence_path)
        path = registry_path.parent / relative
        inputs[relative.as_posix()] = sha256_bytes(path.read_bytes())
    payload = {
        "object_schema": CONFIGURATION_BOOTSTRAP_LOCK_SCHEMA,
        "source_files": dict(sorted(inputs.items())),
        "snapshot": snapshot.model_dump(mode="json"),
    }
    payload["lock_sha256"] = sha256_bytes(canonical_json_bytes(payload))
    return payload


def validate_bootstrap_lock(
    registry_path: Path, payload: dict[str, Any]
) -> ConfigurationSnapshotV2:
    if payload.get("object_schema") != CONFIGURATION_BOOTSTRAP_LOCK_SCHEMA:
        raise ValueError("设备配置 bootstrap lock schema 不受支持")
    claimed = str(payload.get("lock_sha256", ""))
    unsigned = {key: value for key, value in payload.items() if key != "lock_sha256"}
    if claimed != sha256_bytes(canonical_json_bytes(unsigned)):
        raise ValueError("设备配置 bootstrap lock SHA-256 不匹配")
    source_files = payload.get("source_files")
    if not isinstance(source_files, dict):
        raise ValueError("设备配置 bootstrap lock 缺少 source_files")
    for relative, expected_sha256 in source_files.items():
        relative_path = Path(str(relative))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("设备配置 bootstrap lock 包含不安全的源文件路径")
        path = registry_path.parent / relative_path
        if not path.is_file() or sha256_bytes(path.read_bytes()) != expected_sha256:
            raise ValueError(f"设备配置 bootstrap lock 源文件不匹配：{relative}")
    snapshot = ConfigurationSnapshotV2.model_validate(payload.get("snapshot"))
    _validate_snapshot_integrity(snapshot)
    if not snapshot.snapshot_id.startswith("local-cfg-"):
        raise ValueError("bootstrap lock 必须包含 local-cfg Snapshot")
    return snapshot


def bootstrap_snapshot_from_registry(registry_path: Path) -> ConfigurationSnapshotV2:
    lock_path = bootstrap_lock_path(registry_path)
    if not lock_path.is_file():
        return _migrated_bootstrap_snapshot(registry_path)
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    return validate_bootstrap_lock(registry_path, payload)


class DeviceConfigurationStore:
    """Cloud/object-store lifecycle for immutable snapshots and mutable review state."""

    def __init__(self, store: ObjectStore) -> None:
        self.store = store

    @staticmethod
    def snapshot_key(snapshot_id: str) -> str:
        return f"{CONFIGURATION_PREFIX}/snapshots/{snapshot_id}.json"

    @staticmethod
    def review_key(snapshot_id: str) -> str:
        return f"{CONFIGURATION_PREFIX}/reviews/{snapshot_id}.json"

    @staticmethod
    def event_key(event_id: str) -> str:
        return f"{CONFIGURATION_PREFIX}/events/{event_id}.json"

    @staticmethod
    def identity_key(sensor_sn: str) -> str:
        return f"{CONFIGURATION_PREFIX}/identities/{sensor_sn}.json"

    def submit(
        self,
        submission: ConfigurationSnapshotSubmission,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> tuple[ConfigurationSnapshotV2, ConfigurationReviewV2]:
        snapshot = _snapshot_from_submission(
            submission,
            publisher=actor,
            published_at_utc=now or _utc_now(),
            local=False,
        )
        for existing, _review in self.list_snapshots():
            if existing.content_sha256 == snapshot.content_sha256:
                raise ObjectConflictError(
                    f"相同配置内容已发布：{existing.snapshot_id}"
                )
        self.store.write_json(
            self.snapshot_key(snapshot.snapshot_id),
            snapshot.model_dump(mode="json"),
            if_generation_match=0,
        )
        review = ConfigurationReviewV2(
            snapshot_id=snapshot.snapshot_id,
            updated_by=actor,
            updated_at_utc=now or _utc_now(),
        )
        self.store.write_json(
            self.review_key(snapshot.snapshot_id),
            review.model_dump(mode="json"),
            if_generation_match=0,
        )
        self._event(
            "submitted",
            snapshot.snapshot_id,
            actor,
            review.revision,
            now=now or review.updated_at_utc,
        )
        return snapshot, review

    def get_snapshot(self, snapshot_id: str) -> ConfigurationSnapshotV2:
        payload, _generation = self.store.read_json(self.snapshot_key(snapshot_id))
        snapshot = ConfigurationSnapshotV2.model_validate(payload)
        _validate_snapshot_integrity(snapshot)
        if snapshot.snapshot_id.startswith("local-cfg-"):
            raise ValueError("团队对象不能使用 local-cfg Snapshot ID")
        return snapshot

    def get_review(self, snapshot_id: str) -> tuple[ConfigurationReviewV2, int]:
        payload, generation = self.store.read_json(self.review_key(snapshot_id))
        return ConfigurationReviewV2.model_validate(payload), generation

    def current(self) -> tuple[ConfigurationCurrentV2, int]:
        payload, generation = self.store.read_json(f"{CONFIGURATION_PREFIX}/current.json")
        current = ConfigurationCurrentV2.model_validate(payload)
        snapshot = self.get_snapshot(current.snapshot_id)
        if snapshot.snapshot_sha256 != current.snapshot_sha256:
            raise ValueError("Current 指针与 Snapshot SHA-256 不一致")
        review, _ = self.get_review(current.snapshot_id)
        if review.state != "approved":
            raise ValueError("Current 必须指向 approved Snapshot")
        return current, generation

    def list_snapshots(
        self,
    ) -> list[tuple[ConfigurationSnapshotV2, ConfigurationReviewV2]]:
        values: list[tuple[ConfigurationSnapshotV2, ConfigurationReviewV2]] = []
        for info in self.store.list(f"{CONFIGURATION_PREFIX}/snapshots"):
            if not info.key.endswith(".json"):
                continue
            snapshot_id = Path(info.key).stem
            try:
                values.append((self.get_snapshot(snapshot_id), self.get_review(snapshot_id)[0]))
            except (FileNotFoundError, ValueError):
                continue
        values.sort(key=lambda item: item[0].published_at_utc, reverse=True)
        return values

    def transition(
        self,
        snapshot_id: str,
        target: Literal["approved", "revoked"],
        *,
        actor: str,
        expected_revision: int,
        now: datetime | None = None,
    ) -> ConfigurationReviewV2:
        self.get_snapshot(snapshot_id)
        review, generation = self.get_review(snapshot_id)
        if review.revision != expected_revision:
            raise ObjectConflictError("Snapshot 审批 revision 已更新")
        allowed = {("candidate", "approved"), ("approved", "revoked")}
        if (review.state, target) not in allowed:
            raise ValueError(f"不允许从 {review.state} 转换到 {target}")
        if target == "revoked":
            try:
                current, _ = self.current()
            except FileNotFoundError:
                current = None
            if current and current.snapshot_id == snapshot_id:
                raise ValueError("请先将 Current 切换到其他 approved Snapshot")
        updated = review.model_copy(
            update={
                "state": target,
                "revision": review.revision + 1,
                "updated_by": actor,
                "updated_at_utc": now or _utc_now(),
            }
        )
        self.store.write_json(
            self.review_key(snapshot_id),
            updated.model_dump(mode="json"),
            if_generation_match=generation,
        )
        self._event(
            target,
            snapshot_id,
            actor,
            updated.revision,
            now=now or updated.updated_at_utc,
        )
        return updated

    def set_current(
        self,
        snapshot_id: str,
        *,
        actor: str,
        expected_revision: int,
        now: datetime | None = None,
    ) -> ConfigurationCurrentV2:
        snapshot = self.get_snapshot(snapshot_id)
        review, _ = self.get_review(snapshot_id)
        if review.state != "approved":
            raise ValueError("只有 approved Snapshot 可以设为 Current")
        key = f"{CONFIGURATION_PREFIX}/current.json"
        try:
            current, generation = self.current()
        except FileNotFoundError:
            current, generation = None, 0
        revision = current.revision if current else 0
        if revision != expected_revision:
            raise ObjectConflictError("Current revision 已更新")
        updated = ConfigurationCurrentV2(
            snapshot_id=snapshot_id,
            snapshot_sha256=snapshot.snapshot_sha256,
            revision=revision + 1,
            updated_by=actor,
            updated_at_utc=now or _utc_now(),
        )
        self.store.write_json(
            key,
            updated.model_dump(mode="json"),
            if_generation_match=generation,
        )
        self._event(
            "set_current",
            snapshot_id,
            actor,
            updated.revision,
            now=now or updated.updated_at_utc,
        )
        return updated

    def reserve_asset(self, *, actor: str) -> dict[str, Any]:
        key = f"{CONFIGURATION_PREFIX}/identities/next.json"
        try:
            payload, generation = self.store.read_json(key)
            next_asset = int(payload["next_asset"])
        except FileNotFoundError:
            generation = 0
            allocated_assets = {
                int(match.group("asset"))
                for info in self.store.list(f"{CONFIGURATION_PREFIX}/identities")
                if (match := SENSOR_SN_RE.fullmatch(Path(info.key).stem))
            }
            for snapshot, _review in self.list_snapshots():
                allocated_assets.update(
                    int(device.hardware_asset_id[-4:])
                    for device in snapshot.content.devices
                )
            next_asset = max(allocated_assets, default=0) + 1
        sensor_sn = f"IMU-{next_asset:04d}-R01"
        reservation = {
            "schema": CONFIGURATION_IDENTITY_SCHEMA,
            "sensor_sn": sensor_sn,
            "hardware_asset_id": sensor_sn[:8],
            "revision": 1,
            "reserved_by": actor,
            "reserved_at_utc": _utc_now().isoformat(),
        }
        self.store.write_json(
            key,
            {"next_asset": next_asset + 1},
            if_generation_match=generation,
        )
        # Advance the allocator first. A crash may create a harmless gap, while
        # concurrent callers can never both claim the same identity.
        self.store.write_json(
            self.identity_key(sensor_sn), reservation, if_generation_match=0
        )
        self._event("reserve_identity", sensor_sn, actor, 1)
        return reservation

    def reserve_revision(self, hardware_asset_id: str, *, actor: str) -> dict[str, Any]:
        revisions = []
        for info in self.store.list(f"{CONFIGURATION_PREFIX}/identities"):
            match = SENSOR_SN_RE.fullmatch(Path(info.key).stem)
            if match and f"IMU-{match.group('asset')}" == hardware_asset_id:
                revisions.append(int(match.group("revision")))
        for snapshot, _review in self.list_snapshots():
            revisions.extend(
                device.revision
                for device in snapshot.content.devices
                if device.hardware_asset_id == hardware_asset_id
            )
        if not revisions:
            raise ValueError("找不到该物理资产的已保留 SN")
        revision = max(revisions) + 1
        if revision > 99:
            raise ValueError("该物理资产的 firmware revision 已达到上限")
        sensor_sn = f"{hardware_asset_id}-R{revision:02d}"
        reservation = {
            "schema": CONFIGURATION_IDENTITY_SCHEMA,
            "sensor_sn": sensor_sn,
            "hardware_asset_id": hardware_asset_id,
            "revision": revision,
            "supersedes_sn": f"{hardware_asset_id}-R{revision - 1:02d}",
            "reserved_by": actor,
            "reserved_at_utc": _utc_now().isoformat(),
        }
        self.store.write_json(
            self.identity_key(sensor_sn), reservation, if_generation_match=0
        )
        self._event("reserve_identity", sensor_sn, actor, revision)
        return reservation

    def events(self, subject_id: str | None = None) -> list[dict[str, Any]]:
        values = []
        for info in self.store.list(f"{CONFIGURATION_PREFIX}/events"):
            try:
                payload, _generation = self.store.read_json(info.key)
            except (FileNotFoundError, ValueError):
                continue
            if subject_id is None or payload.get("subject_id") == subject_id:
                values.append(payload)
        values.sort(key=lambda item: str(item.get("occurred_at_utc", "")))
        return values

    def approval_at(self, snapshot_id: str, captured_at_utc: str) -> dict[str, Any]:
        """Evaluate authority from append-only events at recording start time."""

        self.get_snapshot(snapshot_id)
        captured_at = datetime.fromisoformat(captured_at_utc.replace("Z", "+00:00"))
        if captured_at.tzinfo is None:
            raise ValueError("captured_at_utc 必须带时区")
        state = "candidate"
        relevant = []
        for event in self.events(snapshot_id):
            occurred = datetime.fromisoformat(
                str(event["occurred_at_utc"]).replace("Z", "+00:00")
            )
            if occurred <= captured_at:
                relevant.append(event)
                if event.get("action") == "approved":
                    state = "approved"
                elif event.get("action") == "revoked":
                    state = "revoked"
        return {
            "snapshot_id": snapshot_id,
            "captured_at_utc": captured_at.isoformat(),
            "state_at_capture": state,
            "approved_at_capture": state == "approved",
            "evaluated_events": len(relevant),
        }

    def _event(
        self,
        action: str,
        subject_id: str,
        actor: str,
        revision: int,
        *,
        now: datetime | None = None,
    ) -> None:
        now = now or _utc_now()
        event_id = f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex[:8]}"
        payload = {
            "schema": CONFIGURATION_EVENT_SCHEMA,
            "event_id": event_id,
            "action": action,
            "subject_id": subject_id,
            "actor": actor,
            "revision": revision,
            "occurred_at_utc": now.isoformat(),
        }
        self.store.write_json(self.event_key(event_id), payload, if_generation_match=0)


class LocalConfigurationManager:
    """Local workspace, immutable snapshots, cache and persistent selection."""

    def __init__(self, data_root: Path, cache_root: Path, registry_path: Path) -> None:
        self.data_root = data_root
        self.cache_root = cache_root
        self.workspace_path = data_root / "workspace.json"
        self.local_root = data_root / "local"
        self.selection_path = data_root / "selection.json"
        self.cache_index_path = cache_root / "index.json"
        self.bootstrap = bootstrap_snapshot_from_registry(registry_path)
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        if not self.workspace_path.is_file():
            _atomic_json_write(
                self.workspace_path,
                ConfigurationSnapshotSubmission(
                    name=self.bootstrap.name,
                    description=self.bootstrap.description,
                    base_snapshot_id=self.bootstrap.snapshot_id,
                    client_build="bootstrap-workspace",
                    content=self.bootstrap.content,
                ).model_dump(mode="json"),
            )

    def workspace(self) -> ConfigurationSnapshotSubmission:
        return ConfigurationSnapshotSubmission.model_validate_json(
            self.workspace_path.read_text(encoding="utf-8")
        )

    def save_workspace(
        self, submission: ConfigurationSnapshotSubmission
    ) -> ConfigurationSnapshotSubmission:
        _atomic_json_write(self.workspace_path, submission.model_dump(mode="json"))
        return submission

    def save_local(
        self, submission: ConfigurationSnapshotSubmission, *, actor: str = "local"
    ) -> ConfigurationSnapshotV2:
        snapshot = _snapshot_from_submission(
            submission,
            publisher=actor,
            published_at_utc=_utc_now(),
            local=True,
        )
        path = self.local_root / f"{snapshot.snapshot_id}.json"
        if path.exists():
            existing = ConfigurationSnapshotV2.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            _validate_snapshot_integrity(existing)
            if existing.content_sha256 == snapshot.content_sha256:
                return existing
            raise ObjectConflictError("本地 Snapshot ID 冲突")
        _atomic_json_write(path, snapshot.model_dump(mode="json"))
        return snapshot

    def cache_remote(
        self,
        snapshot: ConfigurationSnapshotV2,
        review: ConfigurationReviewV2,
        *,
        current: bool = False,
    ) -> None:
        _validate_snapshot_integrity(snapshot)
        if snapshot.snapshot_id.startswith("local-cfg-"):
            raise ValueError("团队缓存不能接收 local-cfg Snapshot")
        if review.snapshot_id != snapshot.snapshot_id:
            raise ValueError("配置 Snapshot 与 review ID 不一致")
        if current and review.state != "approved":
            raise ValueError("团队 Current 必须指向 approved Snapshot")
        _atomic_json_write(
            self.cache_root / f"{snapshot.snapshot_id}.json",
            snapshot.model_dump(mode="json"),
        )
        index = self._cache_index()
        index["snapshots"][snapshot.snapshot_id] = {
            "review": review.model_dump(mode="json"),
            "cached_at_utc": _utc_now().isoformat(),
        }
        if current:
            index["current_snapshot_id"] = snapshot.snapshot_id
            index["current_checked_at_utc"] = _utc_now().isoformat()
        _atomic_json_write(self.cache_index_path, index)

    def record_team_current(self, snapshot_id: str | None) -> None:
        """Record one fully refreshed team pointer without changing manual selection."""

        index = self._cache_index()
        if snapshot_id is not None:
            self.snapshot(snapshot_id)
            entry = index["snapshots"].get(snapshot_id)
            if not isinstance(entry, dict):
                raise ValueError("团队 Current Snapshot 尚未缓存")
            review = ConfigurationReviewV2.model_validate(entry.get("review"))
            if review.state != "approved":
                raise ValueError("团队 Current 必须指向 approved Snapshot")
        index["current_snapshot_id"] = snapshot_id
        index["current_checked_at_utc"] = _utc_now().isoformat()
        _atomic_json_write(self.cache_index_path, index)

    def _cache_index(self) -> dict[str, Any]:
        if not self.cache_index_path.is_file():
            return {"snapshots": {}, "current_snapshot_id": None}
        try:
            payload = json.loads(self.cache_index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"snapshots": {}, "current_snapshot_id": None}
        payload.setdefault("snapshots", {})
        payload.setdefault("current_snapshot_id", None)
        return payload

    def list_snapshots(self) -> list[dict[str, Any]]:
        selected = self.selected_snapshot_id()
        index = self._cache_index()
        values = [
            self._summary(
                self.bootstrap,
                state="approved",
                source="bootstrap",
                current=index.get("current_snapshot_id") == self.bootstrap.snapshot_id,
                selected=selected == self.bootstrap.snapshot_id,
            )
        ]
        for path in sorted(self.local_root.glob("local-cfg-*.json")):
            try:
                snapshot = ConfigurationSnapshotV2.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                _validate_snapshot_integrity(snapshot)
            except (OSError, ValueError):
                continue
            values.append(
                self._summary(
                    snapshot,
                    state="local",
                    source="local",
                    current=False,
                    selected=selected == snapshot.snapshot_id,
                )
            )
        for snapshot_id, entry in index["snapshots"].items():
            path = self.cache_root / f"{snapshot_id}.json"
            try:
                snapshot = ConfigurationSnapshotV2.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                _validate_snapshot_integrity(snapshot)
                review = ConfigurationReviewV2.model_validate(entry["review"])
            except (OSError, ValueError, KeyError):
                continue
            values.append(
                self._summary(
                    snapshot,
                    state=review.state,
                    source=review.state,
                    current=index.get("current_snapshot_id") == snapshot_id,
                    selected=selected == snapshot_id,
                    review_revision=review.revision,
                )
            )
        return sorted(
            values,
            key=lambda item: (
                not item["selected"],
                not item["current"],
                item["name"],
            ),
        )

    @staticmethod
    def _summary(
        snapshot: ConfigurationSnapshotV2,
        *,
        state: str,
        source: str,
        current: bool,
        selected: bool,
        review_revision: int | None = None,
    ) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_sha256": snapshot.snapshot_sha256,
            "content_sha256": snapshot.content_sha256,
            "name": snapshot.name,
            "description": snapshot.description,
            "publisher": snapshot.publisher,
            "published_at_utc": snapshot.published_at_utc.isoformat(),
            "device_count": len(snapshot.content.devices),
            "state": state,
            "source": source,
            "current": current,
            "selected": selected,
            "review_revision": review_revision,
        }

    def selected_snapshot_id(self) -> str:
        if self.selection_path.is_file():
            try:
                snapshot_id = str(
                    json.loads(self.selection_path.read_text(encoding="utf-8"))[
                        "snapshot_id"
                    ]
                )
                self.snapshot(snapshot_id)
                return snapshot_id
            except (OSError, ValueError, KeyError, FileNotFoundError):
                pass
        current = self._cache_index().get("current_snapshot_id")
        if isinstance(current, str):
            try:
                self.snapshot(current)
                return current
            except (OSError, ValueError, FileNotFoundError):
                pass
        return self.bootstrap.snapshot_id

    def select(self, snapshot_id: str) -> ConfigurationSnapshotV2:
        snapshot = self.snapshot(snapshot_id)
        state = self.state(snapshot_id)
        if state == "revoked":
            raise ValueError("revoked Snapshot 只能查看，不能用于新采集")
        _atomic_json_write(
            self.selection_path,
            {"snapshot_id": snapshot_id, "selected_at_utc": _utc_now().isoformat()},
        )
        return snapshot

    def reset_current(self) -> ConfigurationSnapshotV2:
        self.selection_path.unlink(missing_ok=True)
        return self.snapshot(self.selected_snapshot_id())

    def snapshot(self, snapshot_id: str) -> ConfigurationSnapshotV2:
        if snapshot_id == self.bootstrap.snapshot_id:
            return self.bootstrap
        for root in (self.local_root, self.cache_root):
            path = root / f"{snapshot_id}.json"
            if path.is_file():
                snapshot = ConfigurationSnapshotV2.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                _validate_snapshot_integrity(snapshot)
                return snapshot
        raise FileNotFoundError(snapshot_id)

    def selected_snapshot(self) -> ConfigurationSnapshotV2:
        return self.snapshot(self.selected_snapshot_id())

    def state(self, snapshot_id: str) -> str:
        if snapshot_id == self.bootstrap.snapshot_id:
            return "approved"
        if snapshot_id.startswith("local-cfg-"):
            return "local"
        entry = self._cache_index().get("snapshots", {}).get(snapshot_id, {})
        return str(entry.get("review", {}).get("state", "candidate"))

    def status(self) -> dict[str, Any]:
        selected = self.selected_snapshot()
        index = self._cache_index()
        current_id = index.get("current_snapshot_id")
        return {
            "schema_version": CONFIGURATION_SCHEMA_VERSION,
            "selected_snapshot_id": selected.snapshot_id,
            "selected_snapshot_sha256": selected.snapshot_sha256,
            "selected_content_sha256": selected.content_sha256,
            "selected_state": self.state(selected.snapshot_id),
            "selected_source": (
                "bootstrap"
                if selected.snapshot_id == self.bootstrap.snapshot_id
                else "local"
                if selected.snapshot_id.startswith("local-cfg-")
                else self.state(selected.snapshot_id)
            ),
            "current_snapshot_id": current_id,
            "current_checked_at_utc": index.get("current_checked_at_utc"),
            "update_available": bool(current_id and current_id != selected.snapshot_id),
            "manually_pinned": self.selection_path.is_file(),
            "workspace_path": str(self.workspace_path),
            "local_root": str(self.local_root),
            "cache_root": str(self.cache_root),
        }

    def resolve_imu(self, sensor_sn: str):
        from imu_data_collector.config import ImuSettings

        snapshot = self.selected_snapshot()
        device = next(
            (item for item in snapshot.content.devices if item.sensor_sn == sensor_sn),
            None,
        )
        if device is None:
            raise KeyError(sensor_sn)
        if device.lifecycle == "retired":
            raise ValueError("retired 设备不能用于新采集")
        state = self.state(snapshot.snapshot_id)
        if state == "revoked":
            raise ValueError("revoked Snapshot 不能用于新采集")
        spec = protocol_spec(device.protocol_id)
        si = device.si_profile
        authoritative = state == "approved" and si.verified
        settings = ImuSettings(
            sensor_sn=device.sensor_sn,
            device_profile_sha256=sha256_bytes(
                canonical_json_bytes(device.model_dump(mode="json"))
            ),
            name=device.identity.advertised_name,
            address=device.identity.public_address or "",
            notify_uuid=spec.notify_uuid,
            protocol=device.protocol_id,
            force_le_bearer=spec.force_le_bearer,
            prod_capture_enabled=(
                authoritative and "prod" in device.allowed_data_tiers
            ),
            expected_rate_hz=device.expected_rate_hz,
            expected_rate_status=device.expected_rate_status,
            frame_size_bytes=spec.frame_size_bytes,
            calibration_profile_id=si.profile_id if authoritative else "unverified",
            calibration_verified=authoritative,
            accel_counts_per_g=si.accel_counts_per_g if authoritative else None,
            gyro_counts_per_dps=si.gyro_counts_per_dps if authoritative else None,
            accel_bias_counts=si.accel_bias_counts,
            gyro_bias_counts=si.gyro_bias_counts,
            raw_axis_order=si.raw_axis_order,
            axis_signs=si.axis_signs,
            calibration_method=si.method,
            calibration_evidence_sha256=si.evidence_sha256,
            firmware_version=device.firmware.version,
            firmware_evidence_status=device.firmware.evidence_status,
            candidate_accel_counts_per_g=(
                None if authoritative else si.accel_counts_per_g
            ),
            candidate_gyro_counts_per_dps=(
                None if authoritative else si.gyro_counts_per_dps
            ),
            configuration_snapshot_id=snapshot.snapshot_id,
            configuration_snapshot_sha256=snapshot.snapshot_sha256,
            configuration_content_sha256=snapshot.content_sha256,
            configuration_source=self.status()["selected_source"],
            configuration_approval_state=state,
            configuration_checked_at_utc=index_checked_at(self._cache_index()),
            si_profile_id=si.profile_id,
        )
        if not authoritative:
            settings.candidate_accel_bias_counts = si.accel_bias_counts
            settings.candidate_gyro_bias_counts = si.gyro_bias_counts
            settings.candidate_raw_axis_order = si.raw_axis_order
            settings.candidate_axis_signs = si.axis_signs
            settings.candidate_conversion_source = state
            settings.candidate_conversion_sha256 = sha256_bytes(
                canonical_json_bytes(si.model_dump(mode="json"))
            )
        return settings


def index_checked_at(index: dict[str, Any]) -> str | None:
    value = index.get("current_checked_at_utc")
    return str(value) if value else None


def migration_preview(registry_path: Path) -> dict[str, Any]:
    lock = build_bootstrap_lock(registry_path)
    plan_token = sha256_bytes(canonical_json_bytes(lock))
    return {
        "dry_run": True,
        "source": str(registry_path),
        "output": str(bootstrap_lock_path(registry_path)),
        "plan_token": plan_token,
        "confirmation": MIGRATION_CONFIRMATION,
        "lock": lock,
        "warnings": [
            "bootstrap Snapshot is local and is not approved in cloud",
            "no Bucket objects or Current pointers were written",
        ],
    }


def apply_migration_lock(
    registry_path: Path,
    *,
    output: Path | None,
    plan_token: str,
    confirmation: str,
) -> dict[str, Any]:
    preview = migration_preview(registry_path)
    if plan_token != preview["plan_token"]:
        raise ValueError("迁移计划已变化，请重新执行 dry-run")
    if confirmation != MIGRATION_CONFIRMATION:
        raise ValueError(f"确认文本必须为 {MIGRATION_CONFIRMATION}")
    target = output or bootstrap_lock_path(registry_path)
    _atomic_json_write(target, preview["lock"])
    validated = validate_bootstrap_lock(registry_path, preview["lock"])
    return {
        "dry_run": False,
        "output": str(target),
        "snapshot_id": validated.snapshot_id,
        "snapshot_sha256": validated.snapshot_sha256,
        "content_sha256": validated.content_sha256,
        "device_count": len(validated.content.devices),
        "cloud_objects_written": 0,
    }
