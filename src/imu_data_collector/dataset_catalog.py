"""只读浏览 benchmark 公共与团队数据快照。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from imu_data_collector.storage import ObjectInfo, ObjectStore

CURRENT_SCHEMA = "imu_benchmark_current_v1"
MANIFEST_SCHEMA = "imu_benchmark_dataset_manifest_v1"
CONTRACT_VERSION = "imu_benchmark_contract_v2"
HDF5_SCHEMA_VERSION = "3.1.0"
SAMPLING_RATE_HZ = 25.0
DATASET_HANDOFF_VERSION = "0.3.0"
Kind = Literal["base", "team"]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ValidatedSnapshot:
    kind: Kind
    snapshot_id: str
    manifest_key: str
    manifest_bytes: bytes
    manifest: dict[str, Any]
    files: dict[str, tuple[dict[str, Any], ObjectInfo]]

    def public_dict(self, *, current: bool) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "snapshot_id": self.snapshot_id,
            "current": current,
            "created_at_utc": self.manifest.get("created_at_utc"),
            "contract_version": self.manifest["contract_version"],
            "handoff_contract_version": self.manifest.get(
                "handoff_contract_version"
            ),
            "manifest_sha256": hashlib.sha256(self.manifest_bytes).hexdigest(),
            "source": self.manifest.get("source"),
            "files": [entry for entry, _info in self.files.values()],
        }


class DatasetCatalog:
    """从对象存储验证并解析只读数据集目录。"""

    def __init__(self, store: ObjectStore) -> None:
        self.store = store

    @staticmethod
    def _prefix(kind: Kind) -> str:
        return "benchmark-datasets/base" if kind == "base" else "benchmark-datasets/team/cw12eu"

    @staticmethod
    def _expected_role(kind: Kind) -> str:
        return "cross_validation" if kind == "base" else "training_only"

    @staticmethod
    def _json(payload: bytes, *, source: str) -> dict[str, Any]:
        try:
            value = json.loads(payload)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{source} 不是有效 JSON") from error
        if not isinstance(value, dict):
            raise ValueError(f"{source} 必须是 JSON object")
        return value

    @staticmethod
    def _safe_identifier(value: Any, *, name: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"{name} 无效")
        return value

    @staticmethod
    def _sha256(value: Any, *, name: str) -> str:
        if not isinstance(value, str) or not _HEX_SHA256.fullmatch(value):
            raise ValueError(f"{name} 无效")
        return value

    def _validate_manifest(
        self,
        kind: Kind,
        manifest_key: str,
        manifest_bytes: bytes,
    ) -> ValidatedSnapshot:
        manifest = self._json(manifest_bytes, source=manifest_key)
        if manifest.get("schema_version") != MANIFEST_SCHEMA:
            raise ValueError("数据集 manifest schema 不受支持")
        if manifest.get("contract_version") != CONTRACT_VERSION:
            raise ValueError("数据集合同版本不受支持")
        if kind == "team" and manifest.get("handoff_contract_version") != DATASET_HANDOFF_VERSION:
            raise ValueError("团队数据 handoff 合同版本不受支持")
        if manifest.get("kind") != kind:
            raise ValueError("数据集 manifest kind 不一致")
        snapshot_id = self._safe_identifier(manifest.get("snapshot_id"), name="snapshot_id")
        expected_manifest_key = f"{self._prefix(kind)}/{snapshot_id}/manifest.json"
        if manifest_key != expected_manifest_key:
            raise ValueError("manifest 对象键与 snapshot_id 不一致")
        created = manifest.get("created_at_utc")
        if not isinstance(created, str) or not created:
            raise ValueError("manifest 缺少 created_at_utc")
        try:
            datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("manifest created_at_utc 无效") from error

        raw_files = manifest.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise ValueError("manifest 没有数据文件")
        required = {
            "dataset_id",
            "object_key",
            "filename",
            "size_bytes",
            "sha256",
            "logical_content_sha256",
            "hdf5_schema_version",
            "sampling_rate_hz",
            "evaluation_role",
            "sequences",
            "rows",
            "annotations",
        }
        expected_role = self._expected_role(kind)
        files: dict[str, tuple[dict[str, Any], ObjectInfo]] = {}
        filenames: set[str] = set()
        for raw_entry in raw_files:
            if not isinstance(raw_entry, dict) or not required.issubset(raw_entry):
                raise ValueError("manifest 包含不完整的数据文件条目")
            entry = dict(raw_entry)
            dataset_id = self._safe_identifier(entry.get("dataset_id"), name="dataset_id")
            filename = entry.get("filename")
            if (
                not isinstance(filename, str)
                or PurePosixPath(filename).name != filename
                or not filename.endswith(".h5")
            ):
                raise ValueError("manifest filename 不安全")
            if dataset_id in files or filename in filenames:
                raise ValueError("manifest 包含重复的数据集")
            filenames.add(filename)
            expected_key = f"{self._prefix(kind)}/{snapshot_id}/datasets/{filename}"
            if entry.get("object_key") != expected_key:
                raise ValueError("manifest 数据文件对象键不符合快照目录")
            if entry.get("hdf5_schema_version") != HDF5_SCHEMA_VERSION:
                raise ValueError("目录只接受 HDF5 schema 3.1.0")
            if float(entry.get("sampling_rate_hz", 0.0)) != SAMPLING_RATE_HZ:
                raise ValueError("目录只接受 25 Hz 数据")
            if entry.get("evaluation_role") != expected_role:
                raise ValueError("数据集 evaluation_role 不符合集合类型")
            digest = self._sha256(entry.get("sha256"), name="sha256")
            self._sha256(
                entry.get("logical_content_sha256"),
                name="logical_content_sha256",
            )
            size = entry.get("size_bytes")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                raise ValueError("manifest size_bytes 无效")
            for name in (
                "sequences",
                "rows",
                "annotations",
                "events",
                "segments",
                "fall_sequences",
                "participants",
            ):
                value = entry.get(name, 0)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError(f"manifest {name} 无效")
            info = self.store.stat(expected_key)
            if info is None or info.size_bytes != size:
                raise ValueError(f"数据文件缺失或大小不一致：{filename}")
            metadata_sha = info.metadata.get("sha256")
            if metadata_sha and metadata_sha != digest:
                raise ValueError(f"数据文件 SHA-256 metadata 不一致：{filename}")
            files[dataset_id] = (entry, info)
        return ValidatedSnapshot(
            kind=kind,
            snapshot_id=snapshot_id,
            manifest_key=manifest_key,
            manifest_bytes=manifest_bytes,
            manifest=manifest,
            files=files,
        )

    def _load_manifest(self, kind: Kind, manifest_key: str) -> ValidatedSnapshot:
        try:
            manifest_bytes = self.store.read_bytes(manifest_key)
        except FileNotFoundError as error:
            raise KeyError(manifest_key) from error
        return self._validate_manifest(kind, manifest_key, manifest_bytes)

    def _load_current(self, kind: Kind) -> ValidatedSnapshot:
        current_key = f"{self._prefix(kind)}/current.json"
        try:
            current_bytes = self.store.read_bytes(current_key)
        except FileNotFoundError as error:
            raise KeyError(current_key) from error
        current = self._json(current_bytes, source=current_key)
        required = {
            "schema_version",
            "kind",
            "snapshot_id",
            "manifest_object",
            "manifest_sha256",
            "updated_at_utc",
        }
        if kind == "team":
            required.add("handoff_contract_version")
        if set(current) != required or current.get("schema_version") != CURRENT_SCHEMA:
            raise ValueError("current pointer schema 无效")
        if current.get("kind") != kind:
            raise ValueError("current pointer kind 不一致")
        if kind == "team" and current.get("handoff_contract_version") != DATASET_HANDOFF_VERSION:
            raise ValueError("团队 current pointer handoff 合同版本不受支持")
        snapshot_id = self._safe_identifier(current.get("snapshot_id"), name="current snapshot_id")
        expected_manifest = f"{self._prefix(kind)}/{snapshot_id}/manifest.json"
        if current.get("manifest_object") != expected_manifest:
            raise ValueError("current pointer manifest_object 无效")
        expected_sha = self._sha256(current.get("manifest_sha256"), name="current manifest_sha256")
        snapshot = self._load_manifest(kind, expected_manifest)
        if hashlib.sha256(snapshot.manifest_bytes).hexdigest() != expected_sha:
            raise ValueError("current pointer 的 manifest SHA-256 不一致")
        if snapshot.snapshot_id != snapshot_id:
            raise ValueError("current pointer 与 manifest snapshot_id 不一致")
        return snapshot

    def collection(self, kind: Kind) -> dict[str, Any]:
        warnings: list[str] = []
        current: ValidatedSnapshot | None = None
        try:
            current = self._load_current(kind)
        except (KeyError, ValueError) as error:
            warnings.append(str(error.args[0] if isinstance(error, KeyError) else error))

        history: list[ValidatedSnapshot] = []
        prefix = f"{self._prefix(kind)}/"
        for info in self.store.list(prefix):
            if not info.key.endswith("/manifest.json"):
                continue
            try:
                snapshot = self._load_manifest(kind, info.key)
            except (KeyError, ValueError) as error:
                warnings.append(f"忽略异常历史快照 {info.key}：{error}")
                continue
            if current is None or snapshot.snapshot_id != current.snapshot_id:
                history.append(snapshot)
        history.sort(
            key=lambda item: str(item.manifest.get("created_at_utc", "")),
            reverse=True,
        )
        return {
            "kind": kind,
            "available": current is not None,
            "current": None if current is None else current.public_dict(current=True),
            "history": [item.public_dict(current=False) for item in history],
            "warnings": warnings,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": "imu_dataset_catalog_v1",
            "collections": [self.collection("base"), self.collection("team")],
        }

    def snapshot(self, kind: Kind, snapshot_id: str) -> ValidatedSnapshot:
        safe_id = self._safe_identifier(snapshot_id, name="snapshot_id")
        return self._load_manifest(kind, f"{self._prefix(kind)}/{safe_id}/manifest.json")

    def manifest_download(self, kind: Kind, snapshot_id: str) -> tuple[bytes, str]:
        snapshot = self.snapshot(kind, snapshot_id)
        return snapshot.manifest_bytes, f"{snapshot.snapshot_id}.manifest.json"

    def dataset_download(
        self, kind: Kind, snapshot_id: str, dataset_id: str
    ) -> tuple[dict[str, Any], ObjectInfo]:
        snapshot = self.snapshot(kind, snapshot_id)
        safe_dataset_id = self._safe_identifier(dataset_id, name="dataset_id")
        try:
            return snapshot.files[safe_dataset_id]
        except KeyError as error:
            raise KeyError(safe_dataset_id) from error
