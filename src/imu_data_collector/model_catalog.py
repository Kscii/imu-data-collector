"""Read-only catalog and lifecycle state for benchmark ONNX publications."""

from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from imu_data_collector.storage import ObjectConflictError, ObjectInfo, ObjectStore

EXPERIMENT_SCHEMA = "imu_benchmark_result_manifest_v2"
PACKAGE_SCHEMA = "imu_model_package_publication_v1"
STATE_SCHEMA = "imu_model_publication_state_v1"
EXPERIMENT_ROOTS = {
    "formal_cv": "benchmark-results/temporal-core",
    "engineering": "benchmark-results/engineering",
}
PACKAGE_ROOT = "benchmark-models/packages"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
PublicationKind = Literal["experiment", "package"]


def _safe_key(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("模型文件对象键无效")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("模型文件对象键无效")
    return value


def _artifact_identity(payload: dict[str, Any]) -> tuple[int, str]:
    size = payload.get("size_bytes")
    digest = payload.get("sha256")
    if (
        not isinstance(size, int)
        or size <= 0
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise ValueError("模型文件身份无效")
    return size, digest


class ModelCatalog:
    def __init__(self, store: ObjectStore, *, cache_ttl_s: float = 60.0) -> None:
        self.store = store
        self.cache_ttl_s = cache_ttl_s
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._entries: dict[tuple[PublicationKind, str], dict[str, Any]] = {}
        self._errors: list[dict[str, str]] = []

    def _verify_file(
        self, payload: dict[str, Any], *, object_key: str
    ) -> tuple[dict[str, Any], ObjectInfo]:
        size, digest = _artifact_identity(payload)
        info = self.store.stat(_safe_key(object_key))
        if info is None:
            raise ValueError(f"模型文件不存在：{object_key}")
        if info.size_bytes != size or info.metadata.get("sha256") != digest:
            raise ValueError(f"模型文件身份与发布标记不一致：{object_key}")
        descriptor = {
            **payload,
            "object_key": object_key,
            "size_bytes": size,
            "sha256": digest,
            "content_type": payload.get("content_type") or info.content_type,
        }
        return descriptor, info

    def _state(
        self, key: str, kind: PublicationKind, publication_id: str
    ) -> tuple[dict[str, Any], int]:
        state, generation = self.store.read_json(key)
        if (
            state.get("schema_version") != STATE_SCHEMA
            or state.get("kind") != kind
            or state.get("publication_id") != publication_id
            or state.get("status") not in {"available", "deprecated"}
            or not isinstance(state.get("history"), list)
        ):
            raise ValueError("模型发布状态无效")
        return state, generation

    def _experiment(self, marker_key: str) -> dict[str, Any]:
        marker, marker_generation = self.store.read_json(marker_key)
        run_id = marker.get("run_id")
        evidence = marker.get("evidence_level")
        if (
            marker.get("schema_version") != EXPERIMENT_SCHEMA
            or not isinstance(run_id, str)
            or not _ID.fullmatch(run_id)
            or evidence not in EXPERIMENT_ROOTS
        ):
            raise ValueError("实验发布 manifest 无效")
        prefix = f"{EXPERIMENT_ROOTS[evidence]}/{run_id}"
        if marker_key != f"{prefix}/manifest.json":
            raise ValueError("实验发布 manifest 路径与证据级别不一致")
        state, state_generation = self._state(
            f"{prefix}/state.json", "experiment", run_id
        )
        files: dict[str, dict[str, Any]] = {}
        bundle = marker.get("bundle")
        if not isinstance(bundle, dict) or bundle.get("filename") != "run.tar.gz":
            raise ValueError("实验 bundle 无效")
        files["bundle"] = self._verify_file(
            {**bundle, "file_id": "bundle", "filename": "run.tar.gz"},
            object_key=f"{prefix}/run.tar.gz",
        )[0]
        for item in marker.get("quick_files") or []:
            if not isinstance(item, dict):
                raise ValueError("实验快捷文件无效")
            filename = item.get("path")
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise ValueError("实验快捷文件名无效")
            file_id = f"quick-{filename.replace('.', '-')}"
            files[file_id] = self._verify_file(
                {**item, "file_id": file_id, "filename": filename},
                object_key=f"{prefix}/files/{filename}",
            )[0]
        direct_files = marker.get("direct_files")
        if not isinstance(direct_files, list) or not direct_files:
            raise ValueError("实验缺少 ONNX 直接下载文件")
        for item in direct_files:
            if not isinstance(item, dict) or not isinstance(item.get("file_id"), str):
                raise ValueError("实验直接下载文件无效")
            file_id = item["file_id"]
            if file_id in files:
                raise ValueError("实验文件 ID 重复")
            object_key = _safe_key(item.get("object_key"))
            if not object_key.startswith(f"{prefix}/models/"):
                raise ValueError("实验直接下载文件越出发布目录")
            files[file_id] = self._verify_file(item, object_key=object_key)[0]
        methods = marker.get("methods")
        artifacts = marker.get("artifacts")
        if not isinstance(methods, list) or not methods:
            raise ValueError("实验缺少方法聚合指标")
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError("实验缺少 ONNX 模型元数据")
        return {
            "kind": "experiment",
            "publication_id": run_id,
            "evidence_level": evidence,
            "marker": marker,
            "marker_key": marker_key,
            "marker_generation": marker_generation,
            "state": state,
            "state_generation": state_generation,
            "files": files,
        }

    def _package(self, marker_key: str) -> dict[str, Any]:
        marker, marker_generation = self.store.read_json(marker_key)
        package_id = marker.get("package_id")
        if (
            marker.get("schema_version") != PACKAGE_SCHEMA
            or not isinstance(package_id, str)
            or not _ID.fullmatch(package_id)
        ):
            raise ValueError("模型包 publication 无效")
        prefix = f"{PACKAGE_ROOT}/{package_id}"
        if marker_key != f"{prefix}/publication.json":
            raise ValueError("模型包 publication 路径无效")
        state, state_generation = self._state(
            f"{prefix}/state.json", "package", package_id
        )
        files: dict[str, dict[str, Any]] = {}
        bundle = marker.get("bundle")
        if not isinstance(bundle, dict) or bundle.get("filename") != "package.tar.gz":
            raise ValueError("模型包 bundle 无效")
        files["bundle"] = self._verify_file(
            {**bundle, "file_id": "bundle", "filename": "package.tar.gz"},
            object_key=f"{prefix}/package.tar.gz",
        )[0]
        published_files = marker.get("files")
        if not isinstance(published_files, list) or not published_files:
            raise ValueError("模型包缺少文件")
        for item in published_files:
            if not isinstance(item, dict) or not isinstance(item.get("file_id"), str):
                raise ValueError("模型包文件无效")
            file_id = item["file_id"]
            if file_id in files:
                raise ValueError("模型包文件 ID 重复")
            object_key = _safe_key(item.get("object_key"))
            if not object_key.startswith(f"{prefix}/files/"):
                raise ValueError("模型包文件越出发布目录")
            files[file_id] = self._verify_file(item, object_key=object_key)[0]
        return {
            "kind": "package",
            "publication_id": package_id,
            "marker": marker,
            "marker_key": marker_key,
            "marker_generation": marker_generation,
            "state": state,
            "state_generation": state_generation,
            "files": files,
        }

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if not force and now - self._loaded_at < self.cache_ttl_s:
                return self.summary(refresh=False)
            entries: dict[tuple[PublicationKind, str], dict[str, Any]] = {}
            errors: list[dict[str, str]] = []
            marker_keys = [
                info.key
                for root in (*EXPERIMENT_ROOTS.values(), PACKAGE_ROOT)
                for info in self.store.list(f"{root}/")
                if info.key.endswith(("/manifest.json", "/publication.json"))
            ]
            for key in sorted(set(marker_keys)):
                try:
                    entry = (
                        self._package(key)
                        if key.startswith(f"{PACKAGE_ROOT}/")
                        else self._experiment(key)
                    )
                    identity = (entry["kind"], entry["publication_id"])
                    if identity in entries:
                        raise ValueError("模型发布 ID 重复")
                    entries[identity] = entry
                except (FileNotFoundError, KeyError, TypeError, ValueError) as error:
                    errors.append({"object_key": key, "detail": str(error)})
            self._entries = entries
            self._errors = errors
            self._loaded_at = now
            return self.summary(refresh=False)

    def _ensure(self) -> None:
        if time.monotonic() - self._loaded_at >= self.cache_ttl_s:
            self.refresh()

    @staticmethod
    def _summary_entry(entry: dict[str, Any]) -> dict[str, Any]:
        marker = entry["marker"]
        base = {
            "kind": entry["kind"],
            "publication_id": entry["publication_id"],
            "status": entry["state"]["status"],
            "state_generation": entry["state_generation"],
            "updated_at_utc": entry["state"].get("updated_at_utc"),
            "created_at_utc": marker.get("created_at_utc"),
        }
        if entry["kind"] == "experiment":
            return {
                **base,
                "run_id": entry["publication_id"],
                "experiment_id": marker.get("experiment_id"),
                "evidence_level": entry["evidence_level"],
                "scheduled_jobs": marker.get("scheduled_jobs"),
                "method_count": len(marker["methods"]),
                "artifact_count": len(marker["artifacts"]),
                "source": marker.get("source"),
                "base_snapshot_id": marker.get("base_snapshot_id"),
                "data_quality_status": marker.get("data_quality_status"),
            }
        package_manifest = marker.get("manifest") or {}
        return {
            **base,
            "package_id": entry["publication_id"],
            "model_code": package_manifest.get("model_code"),
            "display_name": package_manifest.get("display_name"),
            "logical_digest": marker.get("logical_digest"),
            "source": package_manifest.get("source"),
        }

    def summary(self, *, refresh: bool = True) -> dict[str, Any]:
        if refresh:
            self._ensure()
        entries = [self._summary_entry(entry) for entry in self._entries.values()]
        entries.sort(
            key=lambda item: (str(item.get("created_at_utc") or ""), item["publication_id"]),
            reverse=True,
        )
        return {
            "schema_version": "imu_model_catalog_api_v1",
            "cache_ttl_s": self.cache_ttl_s,
            "loaded": bool(self._loaded_at),
            "experiments": [item for item in entries if item["kind"] == "experiment"],
            "packages": [item for item in entries if item["kind"] == "package"],
            "invalid_publications": list(self._errors),
        }

    def detail(self, kind: PublicationKind, publication_id: str) -> dict[str, Any]:
        self._ensure()
        try:
            entry = self._entries[(kind, publication_id)]
        except KeyError as error:
            raise KeyError(publication_id) from error
        return {
            **self._summary_entry(entry),
            "marker": entry["marker"],
            "state": entry["state"],
            "files": list(entry["files"].values()),
        }

    def file(
        self, kind: PublicationKind, publication_id: str, file_id: str
    ) -> tuple[dict[str, Any], ObjectInfo]:
        self._ensure()
        try:
            entry = self._entries[(kind, publication_id)]
            descriptor = entry["files"][file_id]
        except KeyError as error:
            raise KeyError(file_id) from error
        info = self.store.stat(descriptor["object_key"])
        if info is None:
            raise KeyError(file_id)
        return descriptor, info

    def marker(self, kind: PublicationKind, publication_id: str) -> tuple[bytes, str]:
        self._ensure()
        try:
            entry = self._entries[(kind, publication_id)]
        except KeyError as error:
            raise KeyError(publication_id) from error
        filename = "manifest.json" if kind == "experiment" else "publication.json"
        return self.store.read_bytes(entry["marker_key"]), filename

    def deprecate(
        self,
        kind: PublicationKind,
        publication_id: str,
        *,
        actor: str,
        expected_generation: int,
    ) -> dict[str, Any]:
        self._ensure()
        try:
            entry = self._entries[(kind, publication_id)]
        except KeyError as error:
            raise KeyError(publication_id) from error
        timestamp = datetime.now(UTC).isoformat()
        state = dict(entry["state"])
        history = list(state.get("history") or [])
        history.append(
            {
                "action": "deprecate",
                "from": state["status"],
                "to": "deprecated",
                "actor": actor,
                "at_utc": timestamp,
            }
        )
        state.update(
            {
                "status": "deprecated",
                "updated_at_utc": timestamp,
                "updated_by": actor,
                "history": history,
            }
        )
        state_key = str(PurePosixPath(entry["marker_key"]).with_name("state.json"))
        try:
            info = self.store.write_json(
                state_key,
                state,
                if_generation_match=expected_generation,
            )
        except ObjectConflictError:
            raise
        entry["state"] = state
        entry["state_generation"] = info.generation
        return {**state, "generation": info.generation}
