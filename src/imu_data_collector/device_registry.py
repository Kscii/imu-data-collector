"""Versioned logical sensor identities and per-SN runtime profiles."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from imu_data_collector.host import user_data_dir
from imu_data_collector.imu_protocols import protocol_spec
from imu_data_collector.models import CalibrationProfile
from imu_data_collector.storage import ObjectConflictError, ObjectStore

SENSOR_SN_RE = re.compile(r"^IMU-(?P<asset>[0-9]{4})-R(?P<revision>[0-9]{2})$")
REGISTRY_SCHEMA_VERSION = "1.0.0"
REGISTRY_SNAPSHOT_SCHEMA = "imu_device_registry_snapshot_v1"
REGISTRY_CURRENT_SCHEMA = "imu_device_registry_current_v1"
REGISTRY_PREFIX = "device-registry/v1"


def canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class DeviceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    advertised_name: str = Field(min_length=1, max_length=128)
    public_address: str | None = Field(default=None, max_length=64)
    address_type: Literal["public", "random", "unknown"] = "unknown"
    advertised_service_uuid: str | None = None
    gatt_fingerprint_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class FirmwareIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=128)
    evidence_status: str = Field(min_length=1, max_length=160)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CandidateConversion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accel_counts_per_g: float | None = Field(default=None, gt=0)
    gyro_counts_per_dps: float | None = Field(default=None, gt=0)
    accel_bias_counts: tuple[float, float, float] = (0.0, 0.0, 0.0)
    gyro_bias_counts: tuple[float, float, float] = (0.0, 0.0, 0.0)
    raw_axis_order: tuple[int, int, int] = (0, 1, 2)
    axis_signs: tuple[Literal[-1, 1], Literal[-1, 1], Literal[-1, 1]] = (1, 1, 1)
    evidence_status: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_axes(self) -> CandidateConversion:
        if sorted(self.raw_axis_order) != [0, 1, 2]:
            raise ValueError("raw_axis_order 必须是 0、1、2 的排列")
        return self


class ImuDeviceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sensor_sn: str
    hardware_asset_id: str = Field(pattern=r"^IMU-[0-9]{4}$")
    revision: int = Field(ge=1, le=99)
    lifecycle: Literal["commissioning", "verified", "retired"]
    supersedes_sn: str | None = None
    display_name: str = Field(min_length=1, max_length=128)
    identity: DeviceIdentity
    firmware: FirmwareIdentity
    protocol_id: Literal["cw12eu_v1", "acce_gyro_abf0_v1"]
    expected_rate_hz: float = Field(gt=0, le=1000)
    expected_rate_status: str = Field(min_length=1, max_length=200)
    calibration_evidence_path: str | None = None
    candidate_conversion: CandidateConversion | None = None
    prod_capture_enabled: bool = False
    audit_document: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> ImuDeviceProfile:
        match = SENSOR_SN_RE.fullmatch(self.sensor_sn)
        if match is None:
            raise ValueError("sensor_sn 必须使用 IMU-0000-R00 格式")
        if self.hardware_asset_id != f"IMU-{match.group('asset')}":
            raise ValueError("hardware_asset_id 必须与 sensor_sn 的资产编号一致")
        if self.revision != int(match.group("revision")):
            raise ValueError("revision 必须与 sensor_sn 的 Rxx 一致")
        expected_previous = (
            f"{self.hardware_asset_id}-R{self.revision - 1:02d}"
            if self.revision > 1
            else None
        )
        if self.supersedes_sn != expected_previous:
            if expected_previous is None:
                raise ValueError("R01 不能声明 supersedes_sn")
            raise ValueError(f"R{self.revision:02d} 必须 supersede {expected_previous}")
        if self.supersedes_sn is not None:
            previous = SENSOR_SN_RE.fullmatch(self.supersedes_sn)
            if previous is None:
                raise ValueError("supersedes_sn 格式无效")
            if previous.group("asset") != match.group("asset"):
                raise ValueError("supersedes_sn 必须属于同一物理资产")
        if self.lifecycle == "verified" and not self.calibration_evidence_path:
            raise ValueError("verified 设备必须引用校准证据")
        if self.calibration_evidence_path:
            evidence_path = Path(self.calibration_evidence_path)
            if evidence_path.is_absolute() or ".." in evidence_path.parts:
                raise ValueError("calibration_evidence_path 必须是注册表目录内的相对路径")
        if self.prod_capture_enabled and self.lifecycle != "verified":
            raise ValueError("只有 verified 设备可以启用 prod")
        return self


class DeviceRegistryDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = REGISTRY_SCHEMA_VERSION
    registry_revision: int = Field(ge=1)
    devices: list[ImuDeviceProfile]

    @model_validator(mode="after")
    def validate_registry(self) -> DeviceRegistryDocument:
        by_sn = {item.sensor_sn: item for item in self.devices}
        if len(by_sn) != len(self.devices):
            raise ValueError("设备注册表包含重复 sensor_sn")
        active_addresses: dict[str, str] = {}
        for item in self.devices:
            if item.supersedes_sn and item.supersedes_sn not in by_sn:
                raise ValueError(f"{item.sensor_sn} 引用了不存在的 supersedes_sn")
            address = (item.identity.public_address or "").upper()
            if address and item.lifecycle != "retired":
                previous = active_addresses.get(address)
                if previous:
                    raise ValueError(
                        f"公共地址 {address} 同时属于非 retired 设备 {previous} 和 {item.sensor_sn}"
                    )
                active_addresses[address] = item.sensor_sn
        return self

    def by_sn(self, sensor_sn: str) -> ImuDeviceProfile:
        for item in self.devices:
            if item.sensor_sn == sensor_sn:
                return item
        raise KeyError(sensor_sn)

    def next_sensor_sn(self) -> str:
        assets = [int(item.hardware_asset_id.removeprefix("IMU-")) for item in self.devices]
        return f"IMU-{max(assets, default=0) + 1:04d}-R01"


def load_device_registry(path: Path) -> DeviceRegistryDocument:
    if not path.is_file():
        raise FileNotFoundError(f"缺少 IMU 设备注册表：{path}")
    with path.open("r", encoding="utf-8") as handle:
        return DeviceRegistryDocument.model_validate(yaml.safe_load(handle) or {})


def device_profile_sha256(
    profile: ImuDeviceProfile,
    *,
    calibration_evidence_sha256: str | None = None,
) -> str:
    payload = profile.model_dump(mode="json")
    payload["calibration_evidence_sha256"] = calibration_evidence_sha256
    return sha256_bytes(canonical_json_bytes(payload))


def _calibration_from_evidence(path: Path) -> tuple[CalibrationProfile, str]:
    raw = path.read_bytes()
    payload = yaml.safe_load(raw) or {}
    calibration = payload.get("calibration") or {}
    verified = calibration.get("status") == "engineering_verified"
    return (
        CalibrationProfile(
            profile_id=str(payload.get("profile_id", "unverified")),
            verified=verified,
            accel_counts_per_g=calibration.get("accel_counts_per_g"),
            gyro_counts_per_dps=calibration.get("gyro_counts_per_dps"),
            accel_bias_counts=tuple(calibration.get("accel_bias_counts_raw", (0, 0, 0))),
            gyro_bias_counts=tuple(calibration.get("gyro_bias_counts_raw", (0, 0, 0))),
            raw_axis_order=tuple(calibration.get("raw_axis_order", (0, 1, 2))),
            axis_signs=tuple(calibration.get("axis_signs", (1, 1, 1))),
            method=str(
                calibration.get(
                    "method",
                    "six_face_static_and_multi_axis_360deg_engineering_v1"
                    if verified
                    else "unverified",
                )
            ),
            evidence_sha256=sha256_bytes(raw),
        ),
        sha256_bytes(raw),
    )


def imu_settings_from_profile(registry_path: Path, profile: ImuDeviceProfile):
    """Resolve one frozen profile into the runtime settings used by a session."""

    from imu_data_collector.config import ImuSettings

    calibration = CalibrationProfile()
    calibration_sha: str | None = None
    calibration_path: Path | None = None
    if profile.calibration_evidence_path:
        calibration_path = (registry_path.parent / profile.calibration_evidence_path).resolve()
        calibration, calibration_sha = _calibration_from_evidence(calibration_path)
    protocol = protocol_spec(profile.protocol_id)
    digest = device_profile_sha256(
        profile,
        calibration_evidence_sha256=calibration_sha,
    )
    settings = ImuSettings(
        sensor_sn=profile.sensor_sn,
        device_profile_sha256=digest,
        name=profile.identity.advertised_name,
        address=profile.identity.public_address or "",
        notify_uuid=protocol.notify_uuid,
        protocol=profile.protocol_id,
        force_le_bearer=protocol.force_le_bearer,
        prod_capture_enabled=profile.prod_capture_enabled,
        expected_rate_hz=profile.expected_rate_hz,
        expected_rate_status=profile.expected_rate_status,
        frame_size_bytes=protocol.frame_size_bytes,
        calibration_profile_id=calibration.profile_id,
        calibration_verified=calibration.verified,
        accel_counts_per_g=calibration.accel_counts_per_g,
        gyro_counts_per_dps=calibration.gyro_counts_per_dps,
        accel_bias_counts=calibration.accel_bias_counts,
        gyro_bias_counts=calibration.gyro_bias_counts,
        raw_axis_order=calibration.raw_axis_order,
        axis_signs=calibration.axis_signs,
        calibration_method=calibration.method,
        calibration_evidence_sha256=calibration.evidence_sha256,
        calibration_evidence_path=calibration_path,
        firmware_version=profile.firmware.version,
        firmware_evidence_status=profile.firmware.evidence_status,
        candidate_accel_counts_per_g=(
            profile.candidate_conversion.accel_counts_per_g
            if profile.candidate_conversion
            else None
        ),
        candidate_gyro_counts_per_dps=(
            profile.candidate_conversion.gyro_counts_per_dps
            if profile.candidate_conversion
            else None
        ),
    )
    if profile.candidate_conversion:
        apply_candidate_conversion(settings, profile.candidate_conversion, "registry")
    return settings


def apply_candidate_conversion(settings, candidate: CandidateConversion, source: str):
    """Attach a display-only conversion without granting calibration authority."""

    settings.candidate_accel_counts_per_g = candidate.accel_counts_per_g
    settings.candidate_gyro_counts_per_dps = candidate.gyro_counts_per_dps
    settings.candidate_accel_bias_counts = candidate.accel_bias_counts
    settings.candidate_gyro_bias_counts = candidate.gyro_bias_counts
    settings.candidate_raw_axis_order = candidate.raw_axis_order
    settings.candidate_axis_signs = candidate.axis_signs
    settings.candidate_conversion_sha256 = sha256_bytes(
        canonical_json_bytes(candidate.model_dump(mode="json"))
    )
    settings.candidate_conversion_source = source
    return settings


def resolve_imu_settings(registry_path: Path, sensor_sn: str):
    """Resolve an authoritative Git-tracked profile by SN."""

    registry = load_device_registry(registry_path)
    settings = imu_settings_from_profile(registry_path, registry.by_sn(sensor_sn))
    return attach_registry_identity(settings, registry_path, registry)


def attach_registry_identity(
    settings,
    registry_path: Path,
    document: DeviceRegistryDocument,
):
    settings.device_registry_revision = document.registry_revision
    metadata_path = registry_cache_metadata_path(registry_path)
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            metadata = {}
        digest = str(metadata.get("snapshot_sha256") or "")
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            settings.device_registry_snapshot_sha256 = digest
    return settings


def available_device_profiles(
    registry_path: Path,
    draft_store: DeviceDraftStore | None = None,
) -> list[tuple[ImuDeviceProfile, Literal["tracked", "local_draft"]]]:
    """Return authority-first profiles; drafts can never shadow tracked SNs."""

    tracked = load_device_registry(registry_path).devices
    output: list[tuple[ImuDeviceProfile, Literal["tracked", "local_draft"]]] = [
        (item, "tracked") for item in tracked
    ]
    occupied = {item.sensor_sn for item in tracked}
    for item in (draft_store or DeviceDraftStore()).list():
        if item.sensor_sn not in occupied:
            output.append((item, "local_draft"))
    return sorted(output, key=lambda item: item[0].sensor_sn)


def resolve_available_imu_settings(
    registry_path: Path,
    sensor_sn: str,
    draft_store: DeviceDraftStore | None = None,
    candidate_store: DeviceCandidateStore | None = None,
):
    document = load_device_registry(registry_path)
    for profile, source in available_device_profiles(registry_path, draft_store):
        if profile.sensor_sn == sensor_sn:
            if profile.lifecycle == "retired":
                raise KeyError(sensor_sn)
            settings = imu_settings_from_profile(registry_path, profile)
            override = candidate_store.get(sensor_sn) if candidate_store else None
            if override is not None:
                apply_candidate_conversion(settings, override, "local_override")
            return attach_registry_identity(settings, registry_path, document), profile, source
    raise KeyError(sensor_sn)


def registry_snapshot_payload(
    document: DeviceRegistryDocument,
    registry_path: Path,
) -> tuple[dict[str, Any], str]:
    registry_payload = document.model_dump(mode="json")
    evidence_files: dict[str, dict[str, str]] = {}
    for profile in document.devices:
        relative = profile.calibration_evidence_path
        if not relative or relative in evidence_files:
            continue
        raw = (registry_path.parent / relative).read_bytes()
        evidence_files[relative] = {
            "sha256": sha256_bytes(raw),
            "base64": base64.b64encode(raw).decode("ascii"),
        }
    bundle = {
        "registry": registry_payload,
        "calibration_evidence_files": evidence_files,
    }
    digest = sha256_bytes(canonical_json_bytes(bundle))
    return (
        {
            "schema_version": REGISTRY_SNAPSHOT_SCHEMA,
            "snapshot_sha256": digest,
            "registry_revision": document.registry_revision,
            "registry_document_sha256": sha256_bytes(
                canonical_json_bytes(registry_payload)
            ),
            **bundle,
        },
        digest,
    )


def publish_device_registry(
    store: ObjectStore,
    registry_path: Path,
) -> dict[str, Any]:
    """Publish an immutable snapshot, verify it, then CAS the current pointer."""

    document = load_device_registry(registry_path)
    snapshot, digest = registry_snapshot_payload(document, registry_path)
    snapshot_key = f"{REGISTRY_PREFIX}/snapshots/{digest}/registry.json"
    current_key = f"{REGISTRY_PREFIX}/current.json"
    current = store.stat(current_key)
    existing_pointer: dict[str, Any] | None = None
    if current is not None:
        existing_pointer, generation = store.read_json(current_key)
        existing_revision = existing_pointer.get("registry_revision")
        if not isinstance(existing_revision, int):
            raise ValueError("现有设备注册表 current 指针缺少有效 revision")
        if document.registry_revision < existing_revision:
            raise ValueError("拒绝发布比 current 更旧的设备注册表 revision")
        if (
            document.registry_revision == existing_revision
            and existing_pointer.get("snapshot_sha256") != digest
        ):
            raise ValueError("相同设备注册表 revision 不允许发布不同内容")
    else:
        generation = 0
    current_snapshot = store.stat(snapshot_key)
    if current_snapshot is None:
        store.write_json(snapshot_key, snapshot, if_generation_match=0)
    else:
        existing, _generation = store.read_json(snapshot_key)
        if existing != snapshot:
            raise ObjectConflictError("设备注册表快照键已存在但内容不一致")
    verified, _snapshot_generation = store.read_json(snapshot_key)
    registry_document_sha256 = sha256_bytes(
        canonical_json_bytes(verified.get("registry"))
    )
    if (
        verified.get("schema_version") != REGISTRY_SNAPSHOT_SCHEMA
        or verified.get("snapshot_sha256") != digest
        or verified.get("registry_document_sha256") != registry_document_sha256
        or sha256_bytes(
            canonical_json_bytes(
                {
                    "registry": verified.get("registry"),
                    "calibration_evidence_files": verified.get(
                        "calibration_evidence_files", {}
                    ),
                }
            )
        )
        != digest
    ):
        raise ValueError("设备注册表快照回读验证失败")
    DeviceRegistryDocument.model_validate(verified["registry"])
    pointer = {
        "schema_version": REGISTRY_CURRENT_SCHEMA,
        "snapshot_sha256": digest,
        "registry_revision": document.registry_revision,
        "registry_document_sha256": registry_document_sha256,
        "snapshot_object_key": snapshot_key,
        "published_at_utc": datetime.now(UTC).isoformat(),
    }
    if existing_pointer is not None:
        if (
            existing_pointer.get("snapshot_sha256") == digest
            and existing_pointer.get("snapshot_object_key") == snapshot_key
        ):
            return {**existing_pointer, "generation": generation}
    written = store.write_json(
        current_key,
        pointer,
        if_generation_match=generation,
    )
    return {**pointer, "generation": written.generation}


def validate_registry_snapshot(
    pointer: dict[str, Any],
    snapshot: dict[str, Any],
) -> DeviceRegistryDocument:
    """Validate one current pointer and immutable registry bundle as one unit."""

    if pointer.get("schema_version") != REGISTRY_CURRENT_SCHEMA:
        raise ValueError("远端设备注册表 current 指针 schema 无效")
    digest = str(pointer.get("snapshot_sha256", ""))
    snapshot_key = str(pointer.get("snapshot_object_key", ""))
    expected_key = f"{REGISTRY_PREFIX}/snapshots/{digest}/registry.json"
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or snapshot_key != expected_key:
        raise ValueError("远端设备注册表 current 指针身份无效")
    registry_payload = snapshot.get("registry")
    registry_document_sha256 = sha256_bytes(canonical_json_bytes(registry_payload))
    if (
        snapshot.get("schema_version") != REGISTRY_SNAPSHOT_SCHEMA
        or snapshot.get("snapshot_sha256") != digest
        or snapshot.get("registry_revision") != pointer.get("registry_revision")
        or snapshot.get("registry_document_sha256") != registry_document_sha256
        or pointer.get("registry_document_sha256") != registry_document_sha256
        or sha256_bytes(
            canonical_json_bytes(
                {
                    "registry": registry_payload,
                    "calibration_evidence_files": snapshot.get(
                        "calibration_evidence_files", {}
                    ),
                }
            )
        )
        != digest
    ):
        raise ValueError("远端设备注册表快照哈希验证失败")
    document = DeviceRegistryDocument.model_validate(registry_payload)
    if document.registry_revision != pointer.get("registry_revision"):
        raise ValueError("远端设备注册表 revision 不一致")
    evidence_files = snapshot.get("calibration_evidence_files", {})
    if not isinstance(evidence_files, dict):
        raise ValueError("远端设备注册表校准证据包格式无效")
    referenced = {
        item.calibration_evidence_path
        for item in document.devices
        if item.calibration_evidence_path
    }
    if not referenced.issubset(evidence_files):
        raise ValueError("远端设备注册表缺少引用的校准证据")
    return document


def registry_cache_metadata_path(cache_path: Path) -> Path:
    return cache_path.with_name(f"{cache_path.name}.meta.json")


def validate_registry_cache(cache_path: Path) -> DeviceRegistryDocument:
    """Revalidate the complete local LKG bundle before selecting it at startup."""

    metadata_path = registry_cache_metadata_path(cache_path)
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("本机设备注册表 LKG 元数据缺失或损坏") from error
    if not isinstance(metadata, dict):
        raise ValueError("本机设备注册表 LKG 元数据格式无效")
    if metadata.get("schema_version") != REGISTRY_CURRENT_SCHEMA:
        raise ValueError("本机设备注册表 LKG schema 无效")
    digest = str(metadata.get("snapshot_sha256") or "")
    expected_key = f"{REGISTRY_PREFIX}/snapshots/{digest}/registry.json"
    if (
        not re.fullmatch(r"[0-9a-f]{64}", digest)
        or metadata.get("snapshot_object_key") != expected_key
    ):
        raise ValueError("本机设备注册表 LKG 快照身份无效")
    document = load_device_registry(cache_path)
    document_sha256 = sha256_bytes(
        canonical_json_bytes(document.model_dump(mode="json"))
    )
    if (
        metadata.get("registry_revision") != document.registry_revision
        or metadata.get("registry_document_sha256") != document_sha256
    ):
        raise ValueError("本机设备注册表 LKG 文档哈希或 revision 不一致")
    evidence_sha256 = metadata.get("calibration_evidence_sha256")
    if not isinstance(evidence_sha256, dict):
        raise ValueError("本机设备注册表 LKG 缺少校准证据哈希")
    referenced = {
        item.calibration_evidence_path
        for item in document.devices
        if item.calibration_evidence_path
    }
    if not referenced.issubset(evidence_sha256):
        raise ValueError("本机设备注册表 LKG 缺少引用的校准证据")
    for relative in referenced:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("本机设备注册表 LKG 校准证据路径越界")
        evidence_path = cache_path.parent / relative_path
        try:
            actual = sha256_bytes(evidence_path.read_bytes())
        except OSError as error:
            raise ValueError("本机设备注册表 LKG 校准证据缺失") from error
        if actual != evidence_sha256.get(relative):
            raise ValueError("本机设备注册表 LKG 校准证据哈希不一致")
    return document


def activate_registry_snapshot(
    cache_path: Path,
    pointer: dict[str, Any],
    snapshot: dict[str, Any],
) -> DeviceRegistryDocument:
    """Atomically activate a verified bundle while refusing revision rollback."""

    document = validate_registry_snapshot(pointer, snapshot)
    if cache_path.is_file():
        current = load_device_registry(cache_path)
        if document.registry_revision < current.registry_revision:
            raise ValueError("拒绝回滚到更旧的设备注册表 revision")
        if document.registry_revision == current.registry_revision and document != current:
            raise ValueError("相同设备注册表 revision 对应了不同内容")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    registry_payload = snapshot["registry"]
    evidence_files = snapshot.get("calibration_evidence_files", {})
    evidence_sha256: dict[str, str] = {}
    for relative, descriptor in evidence_files.items():
        if not isinstance(relative, str) or not isinstance(descriptor, dict):
            raise ValueError("远端设备注册表校准证据条目无效")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("远端设备注册表校准证据路径越界")
        try:
            raw = base64.b64decode(str(descriptor.get("base64", "")), validate=True)
        except ValueError as error:
            raise ValueError("远端设备注册表校准证据编码无效") from error
        if sha256_bytes(raw) != descriptor.get("sha256"):
            raise ValueError("远端设备注册表校准证据哈希无效")
        evidence_sha256[relative] = sha256_bytes(raw)
        evidence_path = cache_path.parent / relative_path
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        if evidence_path.is_file():
            if sha256_bytes(evidence_path.read_bytes()) != evidence_sha256[relative]:
                raise ValueError(
                    "校准证据路径已存在不同内容；正式证据路径必须不可变"
                )
            continue
        evidence_temporary = evidence_path.with_name(
            f".{evidence_path.name}.{os.getpid()}.partial"
        )
        evidence_temporary.write_bytes(raw)
        if os.name != "nt":
            evidence_temporary.chmod(0o600)
        evidence_temporary.replace(evidence_path)
    temporary = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.partial")
    temporary.write_text(
        json.dumps(registry_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(cache_path)
    metadata_path = registry_cache_metadata_path(cache_path)
    metadata_temporary = metadata_path.with_name(
        f".{metadata_path.name}.{os.getpid()}.partial"
    )
    metadata_temporary.write_text(
        json.dumps(
            {
                **pointer,
                "calibration_evidence_sha256": evidence_sha256,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        metadata_temporary.chmod(0o600)
    metadata_temporary.replace(metadata_path)
    return document


def refresh_device_registry_cache(
    store: ObjectStore,
    cache_path: Path,
) -> DeviceRegistryDocument:
    """Fetch, verify, and atomically replace the last-known-good registry cache."""

    pointer, _generation = store.read_json(f"{REGISTRY_PREFIX}/current.json")
    digest = str(pointer.get("snapshot_sha256", ""))
    snapshot_key = str(pointer.get("snapshot_object_key", ""))
    expected_key = f"{REGISTRY_PREFIX}/snapshots/{digest}/registry.json"
    if snapshot_key != expected_key:
        raise ValueError("远端设备注册表 current 指针身份无效")
    snapshot, _snapshot_generation = store.read_json(snapshot_key)
    return activate_registry_snapshot(cache_path, pointer, snapshot)


class DeviceCandidateStore:
    """Per-user SI hypotheses; never grant production calibration authority."""

    schema_version = "1.0.0"

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            return {"schema_version": self.schema_version, "candidates": {}}
        if payload.get("schema_version") != self.schema_version:
            return {"schema_version": self.schema_version, "candidates": {}}
        return payload

    def get(self, sensor_sn: str) -> CandidateConversion | None:
        value = self._read().get("candidates", {}).get(sensor_sn)
        if not isinstance(value, dict):
            return None
        try:
            return CandidateConversion.model_validate(value)
        except ValueError:
            return None

    def save(
        self,
        sensor_sn: str,
        candidate: CandidateConversion | dict[str, Any],
    ) -> CandidateConversion:
        if SENSOR_SN_RE.fullmatch(sensor_sn) is None:
            raise ValueError("SN 格式无效")
        candidate = CandidateConversion.model_validate(candidate)
        payload = self._read()
        candidates = dict(payload.get("candidates", {}))
        candidates[sensor_sn] = candidate.model_dump(mode="json")
        target = {
            "schema_version": self.schema_version,
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "candidates": candidates,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.partial")
        temporary.write_text(
            json.dumps(target, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(self.path)
        return candidate

    def delete(self, sensor_sn: str) -> bool:
        payload = self._read()
        candidates = dict(payload.get("candidates", {}))
        removed = candidates.pop(sensor_sn, None) is not None
        if removed:
            target = {
                "schema_version": self.schema_version,
                "updated_at_utc": datetime.now(UTC).isoformat(),
                "candidates": candidates,
            }
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.partial")
            temporary.write_text(
                json.dumps(target, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if os.name != "nt":
                temporary.chmod(0o600)
            temporary.replace(self.path)
        return removed

    def export_yaml(self, sensor_sn: str) -> str:
        candidate = self.get(sensor_sn)
        if candidate is None:
            raise KeyError(sensor_sn)
        return yaml.safe_dump(
            {
                "schema_version": "imu_candidate_conversion_v1",
                "sensor_sn": sensor_sn,
                "production_authority": False,
                "candidate_conversion": candidate.model_dump(mode="json"),
            },
            allow_unicode=True,
            sort_keys=False,
        )


class DeviceDraftStore:
    """Local commissioning drafts; never an authority for production capture."""

    schema_version = "1.0.0"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or user_data_dir() / "imu-device-drafts.json"

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": self.schema_version, "devices": {}}
        except (OSError, ValueError, json.JSONDecodeError):
            return {"schema_version": self.schema_version, "devices": {}}
        if payload.get("schema_version") != self.schema_version:
            return {"schema_version": self.schema_version, "devices": {}}
        return payload

    def list(self) -> list[ImuDeviceProfile]:
        payload = self._read()
        output: list[ImuDeviceProfile] = []
        for item in payload.get("devices", {}).values():
            try:
                output.append(ImuDeviceProfile.model_validate(item))
            except ValueError:
                continue
        return sorted(output, key=lambda item: item.sensor_sn)

    def save(self, profile: ImuDeviceProfile) -> ImuDeviceProfile:
        if profile.lifecycle != "commissioning" or profile.prod_capture_enabled:
            raise ValueError("本机草稿只能是禁止 prod 的 commissioning 设备")
        if profile.calibration_evidence_path is not None:
            raise ValueError("本机草稿不能声明正式校准证据")
        payload = self._read()
        devices = dict(payload.get("devices", {}))
        devices[profile.sensor_sn] = profile.model_dump(mode="json")
        target = {
            "schema_version": self.schema_version,
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "devices": devices,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.partial")
        temporary.write_text(
            json.dumps(target, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(self.path)
        return profile

    def delete(self, sensor_sn: str) -> bool:
        payload = self._read()
        devices = dict(payload.get("devices", {}))
        if devices.pop(sensor_sn, None) is None:
            return False
        target = {**payload, "devices": devices, "updated_at_utc": datetime.now(UTC).isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.partial")
        temporary.write_text(
            json.dumps(target, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(self.path)
        return True

    def export_yaml(self, sensor_sn: str) -> str:
        profile = next((item for item in self.list() if item.sensor_sn == sensor_sn), None)
        if profile is None:
            raise KeyError(sensor_sn)
        return yaml.safe_dump(
            profile.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
        )
