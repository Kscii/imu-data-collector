"""Read-only catalog and lifecycle state for benchmark ONNX publications."""

from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from imu_data_collector.artifact_contract import (
    require_compatible_version,
    validate_experiment_marker_v1,
    validate_model_marker_v1,
)
from imu_data_collector.storage import ObjectConflictError, ObjectInfo, ObjectStore

EXPERIMENT_SCHEMA = "imu_experiment_catalog_v1"
EXPERIMENT_CONTRACT_VERSION = "1.0.0"
LEGACY_EXPERIMENT_SCHEMA = "imu_experiment_catalog_v0"
LEGACY_EXPERIMENT_CONTRACT_VERSION = "0.1.0"
MODEL_SCHEMA = "imu_model_release_v1"
MODEL_CONTRACT_VERSION = "1.0.0"
LEGACY_MODEL_SCHEMA = "imu_model_release_v0"
LEGACY_MODEL_CONTRACT_VERSION = "0.1.0"
STATE_SCHEMA = "imu_model_catalog_state_v0"
EXPERIMENT_ROOT = "benchmark-model-catalog/experiments"
MODEL_ROOT = "benchmark-model-catalog/models"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
PublicationKind = Literal["experiment", "model"]


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
        self._loaded_at: float | None = None
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
        publication_id = marker.get("publication_id")
        schema = marker.get("schema_version")
        if (
            not isinstance(publication_id, str)
            or not _ID.fullmatch(publication_id)
            or marker.get("evidence_level") not in {"formal_cv", "engineering"}
        ):
            raise ValueError("实验目录 metadata 无效")
        if schema == EXPERIMENT_SCHEMA:
            require_compatible_version(
                marker.get("contract_version"),
                EXPERIMENT_CONTRACT_VERSION,
                name="实验目录合同版本",
            )
            validate_experiment_marker_v1(marker)
            contract_status = "v1"
        elif (
            schema == LEGACY_EXPERIMENT_SCHEMA
            and marker.get("contract_version") == LEGACY_EXPERIMENT_CONTRACT_VERSION
        ):
            contract_status = "legacy_pre_v1"
        else:
            raise ValueError("实验目录 schema 或合同版本不受支持")
        prefix = f"{EXPERIMENT_ROOT}/{publication_id}"
        if marker_key != f"{prefix}/metadata.json":
            raise ValueError("实验目录 metadata 路径无效")
        state, state_generation = self._state(
            f"{prefix}/state.json", "experiment", publication_id
        )
        methods = marker.get("methods")
        artifacts = marker.get("artifacts")
        if not isinstance(methods, list) or not methods:
            raise ValueError("实验目录缺少方法聚合指标")
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError("实验目录缺少 ONNX 模型元数据")
        files: dict[str, dict[str, Any]] = {}
        for artifact in artifacts:
            if not isinstance(artifact, dict) or not isinstance(
                artifact.get("artifact_id"), str
            ):
                raise ValueError("实验 ONNX 元数据无效")
            artifact_id = artifact["artifact_id"]
            descriptor = artifact.get("onnx")
            if not isinstance(descriptor, dict):
                raise ValueError("实验 ONNX 文件描述符无效")
            object_key = _safe_key(descriptor.get("object_key"))
            if object_key != f"{prefix}/onnx/{artifact_id}.onnx":
                raise ValueError("实验 ONNX 文件越出发布目录")
            decision = artifact.get("decision")
            threshold = decision.get("score_threshold") if isinstance(decision, dict) else None
            policies = decision.get("trigger_policies") if isinstance(decision, dict) else None
            if (
                not isinstance(threshold, dict)
                or threshold.get("selection_split") != "validation"
                or threshold.get("comparison") != ">="
                or decision.get("anchor") != "window_end"
                or not isinstance(policies, list)
                or not policies
            ):
                raise ValueError("实验 ONNX 缺少完整判定规则")
            policy_fields = {
                "policy_id",
                "required_positive_windows",
                "lookback_windows",
                "consecutive",
                "cooldown_seconds",
                "reference_policy",
                "validation_pareto",
            }
            if any(
                not isinstance(policy, dict) or not policy_fields.issubset(policy)
                for policy in policies
            ):
                raise ValueError("实验 ONNX 触发策略不完整")
            files[f"onnx-{artifact_id}"] = self._verify_file(
                descriptor, object_key=object_key
            )[0]
        result_evidence = marker.get("result_evidence")
        if not isinstance(result_evidence, dict):
            raise ValueError("实验目录缺少 result 证据引用")
        for name, filename in (("manifest", "manifest.json"), ("bundle", "run.tar.gz")):
            descriptor = result_evidence.get(name)
            if not isinstance(descriptor, dict):
                raise ValueError("实验目录 result 证据描述符无效")
            object_key = _safe_key(descriptor.get("object_key"))
            if (
                descriptor.get("filename") != filename
                or not object_key.startswith("benchmark-results/")
            ):
                raise ValueError("实验目录 result 证据对象键无效")
            verified = self._verify_file(descriptor, object_key=object_key)[0]
            if name == "bundle":
                files["result-bundle"] = verified
        return {
            "kind": "experiment",
            "publication_id": publication_id,
            "evidence_level": marker["evidence_level"],
            "marker": marker,
            "marker_key": marker_key,
            "marker_generation": marker_generation,
            "state": state,
            "state_generation": state_generation,
            "files": files,
            "contract_status": contract_status,
        }

    def _model(self, marker_key: str) -> dict[str, Any]:
        marker, marker_generation = self.store.read_json(marker_key)
        release_id = marker.get("release_id")
        schema = marker.get("schema_version")
        if (
            not isinstance(release_id, str)
            or not _ID.fullmatch(release_id)
        ):
            raise ValueError("模型发布 metadata 无效")
        if schema == MODEL_SCHEMA:
            require_compatible_version(
                marker.get("contract_version"),
                MODEL_CONTRACT_VERSION,
                name="模型发布合同版本",
            )
            validate_model_marker_v1(marker)
            contract_status = "v1"
        elif (
            schema == LEGACY_MODEL_SCHEMA
            and marker.get("contract_version") == LEGACY_MODEL_CONTRACT_VERSION
        ):
            contract_status = "legacy_pre_v1"
        else:
            raise ValueError("模型发布 schema 或合同版本不受支持")
        prefix = f"{MODEL_ROOT}/{release_id}"
        if marker_key != f"{prefix}/metadata.json":
            raise ValueError("模型发布 metadata 路径无效")
        state, state_generation = self._state(
            f"{prefix}/state.json", "model", release_id
        )
        descriptor = marker.get("model")
        if not isinstance(descriptor, dict):
            raise ValueError("模型发布缺少 model.onnx 描述符")
        object_key = _safe_key(descriptor.get("object_key"))
        if object_key != f"{prefix}/model.onnx":
            raise ValueError("模型文件越出发布目录")
        decision = marker.get("decision")
        threshold = decision.get("score_threshold") if isinstance(decision, dict) else None
        trigger = decision.get("trigger_policy") if isinstance(decision, dict) else None
        if (
            not isinstance(threshold, dict)
            or threshold.get("comparison") != ">="
            or not isinstance(trigger, dict)
            or (
                contract_status == "v1"
                and decision.get("status") != "provisional_validation_derived"
            )
            or (
                contract_status == "legacy_pre_v1"
                and decision.get("anchor") != "window_end"
            )
        ):
            raise ValueError("模型发布缺少固定阈值或触发策略")
        return {
            "kind": "model",
            "publication_id": release_id,
            "marker": marker,
            "marker_key": marker_key,
            "marker_generation": marker_generation,
            "state": state,
            "state_generation": state_generation,
            "files": {
                "model": self._verify_file(descriptor, object_key=object_key)[0]
            },
            "contract_status": contract_status,
        }

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if (
                self._loaded_at is not None
                and not force
                and now - self._loaded_at < self.cache_ttl_s
            ):
                return self.summary(refresh=False)
            entries: dict[tuple[PublicationKind, str], dict[str, Any]] = {}
            errors: list[dict[str, str]] = []
            marker_keys = [
                info.key
                for root in (EXPERIMENT_ROOT, MODEL_ROOT)
                for info in self.store.list(f"{root}/")
                if info.key.endswith("/metadata.json")
            ]
            for key in sorted(set(marker_keys)):
                try:
                    entry = (
                        self._model(key)
                        if key.startswith(f"{MODEL_ROOT}/")
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
        if (
            self._loaded_at is None
            or time.monotonic() - self._loaded_at >= self.cache_ttl_s
        ):
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
            "source": marker.get("source"),
            "schema_version": marker.get("schema_version"),
            "contract_version": marker.get("contract_version"),
            "contract_status": entry["contract_status"],
        }
        if entry["kind"] == "experiment":
            data = marker.get("data") or {}
            return {
                **base,
                "run_id": marker.get("run_id"),
                "experiment_id": marker.get("experiment_id"),
                "evidence_level": entry["evidence_level"],
                "scheduled_jobs": marker.get("scheduled_jobs"),
                "method_count": len(marker["methods"]),
                "artifact_count": len(marker["artifacts"]),
                "base_snapshot_id": data.get("base_snapshot_id"),
                "metric_split": "test",
                "selection_eligible": False,
            }
        return {
            **base,
            "release_id": entry["publication_id"],
            "model_code": marker.get("model_code"),
            "name": marker.get("name"),
            "release_stage": marker.get("release_stage"),
            "model_sha256": (marker.get("model") or {}).get("sha256"),
        }

    def summary(
        self, *, refresh: bool = True, include_deprecated: bool = False
    ) -> dict[str, Any]:
        if refresh:
            self._ensure()
        entries = [
            self._summary_entry(entry)
            for entry in self._entries.values()
            if include_deprecated or entry["state"]["status"] != "deprecated"
        ]
        entries.sort(
            key=lambda item: (
                str(item.get("created_at_utc") or ""),
                item["publication_id"],
            ),
            reverse=True,
        )
        return {
            "schema_version": "imu_model_catalog_api_v1",
            "cache_ttl_s": self.cache_ttl_s,
            "loaded": self._loaded_at is not None,
            "experiments": [item for item in entries if item["kind"] == "experiment"],
            "models": [item for item in entries if item["kind"] == "model"],
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
            "interpretation": {
                "metric_split": (
                    "test" if entry["kind"] == "experiment" else None
                ),
                "selection_eligible": (
                    False if entry["kind"] == "experiment" else None
                ),
                "contract_status": entry["contract_status"],
            },
            "files": [
                {"file_id": file_id, **descriptor}
                for file_id, descriptor in entry["files"].items()
            ],
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
        return self.store.read_bytes(entry["marker_key"]), "metadata.json"

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
