"""加载具有明确且可检查默认值的项目配置。"""

from __future__ import annotations

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
    accel_counts_per_g: float | None = None
    gyro_counts_per_dps: float | None = None


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
    review_policy: str = "single_user"


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
        taxonomy = self.activity_taxonomy_path.expanduser()
        if not taxonomy.is_absolute():
            taxonomy = project_root / taxonomy
        self.activity_taxonomy_path = taxonomy.resolve()


def _construct_settings(payload: dict[str, Any]) -> Settings:
    values = dict(payload)
    imu = ImuSettings(**values.pop("imu", {}))
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
    for key in ("data_root", "catalog_path", "activity_taxonomy_path"):
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
            codes.add(code)
    return payload
