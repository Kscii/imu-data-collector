"""加载具有明确且可检查默认值的项目配置。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class ImuSettings:
    name: str = "CW12EU-T"
    address: str = "83:FC:90:14:1E:A4"
    notify_uuid: str = "00002ae1-0000-1000-8000-00805f9b34fb"
    expected_rate_hz: float = 25.0
    expected_rate_status: str = "short_probe_observed_2026-08-25_pending_long_run"
    frame_size_bytes: int = 16
    calibration_profile_id: str = "unverified"
    calibration_verified: bool = False
    accel_counts_per_g: float | None = None
    gyro_counts_per_dps: float | None = None
    accel_bias_counts: tuple[float, float, float] = (0.0, 0.0, 0.0)
    gyro_bias_counts: tuple[float, float, float] = (0.0, 0.0, 0.0)
    raw_axis_order: tuple[int, int, int] = (0, 1, 2)
    axis_signs: tuple[int, int, int] = (1, 1, 1)
    calibration_method: str = "unverified"
    calibration_evidence_sha256: str | None = None


@dataclass(slots=True)
class VideoSettings:
    device: str | None = None
    camera_id: str | None = None
    width: int = 1920
    height: int = 1080
    requested_fps: int = 30
    input_format: str = "mjpeg"
    bitrate: str = "6M"
    maxrate: str = "8M"
    preview_width: int = 640
    preview_fps: int = 10
    vaapi_device: str | None = "/dev/dri/renderD128"
    manual_controls_enabled: bool = False
    auto_exposure: int = 1
    exposure_time_absolute: int = 200
    gain: int = 192
    exposure_dynamic_framerate: int = 0
    power_line_frequency: int = 1
    source_fps_window_seconds: float = 5.0
    prod_min_source_fps: float = 29.0
    prod_min_span_fps: float = 27.0


@dataclass(slots=True)
class UploadSettings:
    enabled: bool = False
    remote: str = "gdrive"
    remote_root: str | None = None


@dataclass(slots=True)
class StorageSettings:
    backend: str = "local"
    root: Path = Path("~/.local/share/imu-data-collector/objects")
    bucket: str | None = None
    project: str | None = None
    cache_root: Path = Path("~/.cache/imu-annotation")


@dataclass(slots=True)
class AuthSettings:
    mode: str = "local"
    iap_audience: str | None = None
    local_actor_id: str = "xfan0282"


@dataclass(slots=True)
class AnnotationSettings:
    server_host: str = "127.0.0.1"
    server_port: int = 8766
    catalog_path: Path = Path("~/.local/share/imu-annotation/catalog.sqlite3")
    catalog_refresh_interval_s: float = 10.0


@dataclass(slots=True)
class IdentitySettings:
    allowed_unikeys: tuple[str, ...] = (
        "rkim6933",
        "zche0826",
        "jzho8728",
        "jzha9115",
        "xfan0282",
        "yniu0950",
        "hche5673",
        "jmia0254",
        "xliu0452",
    )
    admins: tuple[str, ...] = ("xfan0282",)
    email_to_unikey: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Settings:
    data_root: Path = Path("~/IMUData")
    catalog_path: Path = Path("~/.local/share/imu-data-collector/catalog.sqlite3")
    activity_taxonomy_path: Path = Path("configs/activities.yaml")
    calibration_evidence_path: Path = Path("configs/calibration-evidence.yaml")
    server_host: str = "127.0.0.1"
    server_port: int = 8765
    minimum_free_gib: int = 20
    imu: ImuSettings = field(default_factory=ImuSettings)
    video: VideoSettings = field(default_factory=VideoSettings)
    upload: UploadSettings = field(default_factory=UploadSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    annotation: AnnotationSettings = field(default_factory=AnnotationSettings)
    identity: IdentitySettings = field(default_factory=IdentitySettings)

    def resolve_paths(self, project_root: Path) -> None:
        self.data_root = self.data_root.expanduser().resolve()
        self.catalog_path = self.catalog_path.expanduser().resolve()
        self.storage.root = self.storage.root.expanduser().resolve()
        self.storage.cache_root = self.storage.cache_root.expanduser().resolve()
        self.annotation.catalog_path = self.annotation.catalog_path.expanduser().resolve()
        for name in ("activity_taxonomy_path", "calibration_evidence_path"):
            path = getattr(self, name).expanduser()
            if not path.is_absolute():
                path = project_root / path
            setattr(self, name, path.resolve())
        if (
            self.imu.calibration_verified
            and self.imu.calibration_evidence_sha256 is None
            and self.calibration_evidence_path.is_file()
        ):
            self.imu.calibration_evidence_sha256 = hashlib.sha256(
                self.calibration_evidence_path.read_bytes()
            ).hexdigest()


def _construct_settings(payload: dict[str, Any]) -> Settings:
    values = dict(payload)
    imu_values = values.pop("imu", {})
    for tuple_key in (
        "accel_bias_counts",
        "gyro_bias_counts",
        "raw_axis_order",
        "axis_signs",
    ):
        if tuple_key in imu_values:
            imu_values[tuple_key] = tuple(imu_values[tuple_key])
    imu = ImuSettings(**imu_values)
    video = VideoSettings(**values.pop("video", {}))
    upload = UploadSettings(**values.pop("upload", {}))
    storage_values = values.pop("storage", {})
    if "root" in storage_values:
        storage_values["root"] = Path(storage_values["root"])
    if "cache_root" in storage_values:
        storage_values["cache_root"] = Path(storage_values["cache_root"])
    storage = StorageSettings(**storage_values)
    auth = AuthSettings(**values.pop("auth", {}))
    annotation_values = values.pop("annotation", {})
    if "catalog_path" in annotation_values:
        annotation_values["catalog_path"] = Path(annotation_values["catalog_path"])
    annotation = AnnotationSettings(**annotation_values)
    identity_values = values.pop("identity", {})
    for tuple_key in ("allowed_unikeys", "admins"):
        if tuple_key in identity_values:
            identity_values[tuple_key] = tuple(identity_values[tuple_key])
    if "email_to_unikey" in identity_values:
        identity_values["email_to_unikey"] = {
            str(email).strip().lower(): str(unikey)
            for email, unikey in identity_values["email_to_unikey"].items()
        }
    identity = IdentitySettings(**identity_values)
    for key in (
        "data_root",
        "catalog_path",
        "activity_taxonomy_path",
        "calibration_evidence_path",
    ):
        if key in values:
            values[key] = Path(values[key])
    return Settings(
        imu=imu,
        video=video,
        upload=upload,
        storage=storage,
        auth=auth,
        annotation=annotation,
        identity=identity,
        **values,
    )


def load_settings(config_path: Path | None = None) -> Settings:
    project_root = Path(__file__).resolve().parents[2]
    chosen = config_path or Path(
        os.environ.get("IMU_COLLECTOR_CONFIG", project_root / "configs/default.yaml")
    )
    with chosen.expanduser().open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    settings = _construct_settings(payload)
    if data_root := os.environ.get("IMU_COLLECTOR_DATA_ROOT"):
        settings.data_root = Path(data_root)
    settings.resolve_paths(project_root)
    return settings


def load_activity_taxonomy(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not payload.get("taxonomy_id") or not payload.get("version"):
        raise ValueError("Activity taxonomy requires taxonomy_id and version")
    codes: set[str] = set()
    for binary_label in ("fall", "non_fall"):
        items = payload.get(binary_label)
        if not isinstance(items, list) or not items:
            raise ValueError(f"Activity taxonomy requires non-empty {binary_label}")
        for item in items:
            code = item.get("code") if isinstance(item, dict) else None
            if not code or code in codes:
                raise ValueError(f"Invalid or duplicate activity code: {code!r}")
            for field_name in ("display_name_zh", "display_name_en"):
                if not isinstance(item.get(field_name), str) or not item[field_name].strip():
                    raise ValueError(f"Activity {code!r} requires {field_name}")
            codes.add(code)
    return payload


def load_calibration_evidence(path: Path) -> dict[str, Any]:
    """读取设备校准证据登记。"""

    if not path.is_file():
        raise FileNotFoundError(f"缺少校准证据清单：{path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if payload.get("schema_version") != "1.0.0" or not payload.get("profile_id"):
        raise ValueError("校准证据清单缺少有效 schema_version 或 profile_id")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("校准证据清单 evidence 必须是列表")
    recording_ids = [str(item.get("recording_id", "")) for item in evidence]
    if any(not item for item in recording_ids) or len(set(recording_ids)) != len(
        recording_ids
    ):
        raise ValueError("校准证据 recording_id 必须存在且唯一")
    bilingual_fields = (
        (payload.get("device", {}), ("scope_zh", "scope_en")),
        (
            payload.get("coordinate_system", {}),
            (
                "x_positive_zh",
                "x_positive_en",
                "y_positive_zh",
                "y_positive_en",
                "z_positive_zh",
                "z_positive_en",
            ),
        ),
        (payload.get("calibration", {}), ("conclusion_zh", "conclusion_en")),
    )
    for section, fields in bilingual_fields:
        if any(
            not isinstance(section.get(name), str) or not section[name].strip()
            for name in fields
        ):
            raise ValueError("校准证据清单缺少中英文说明字段")
    for item in evidence:
        if any(
            not isinstance(item.get(name), str) or not item[name].strip()
            for name in (
                "setup_zh",
                "setup_en",
                "expected_zh",
                "expected_en",
                "observed_zh",
                "observed_en",
            )
        ):
            raise ValueError("每条校准证据必须包含完整中英文语义")
    return payload
