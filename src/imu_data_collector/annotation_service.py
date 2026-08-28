"""不依赖任何采集硬件的独立标注业务服务。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import h5py
import numpy as np
from pydantic import ValidationError

from imu_data_collector.annotation_catalog import AnnotationCatalog
from imu_data_collector.annotation_review import AnnotationReviewStore
from imu_data_collector.artifacts import (
    TRAINING_SCHEMA_VERSION,
    create_training_snapshot_archive,
    export_aligned,
    merge_training_exports,
)
from imu_data_collector.build_info import ANNOTATION_API_BUILD_ID
from imu_data_collector.config import (
    ImuSettings,
    Settings,
    load_activity_taxonomy,
    load_calibration_evidence,
)
from imu_data_collector.constants import ANNOTATION_ACCEPTED_CAPTURE_SCHEMA_VERSIONS
from imu_data_collector.file_lock import exclusive_file_lock
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    ActivityTaxonomyCreateRequest,
    ActivityTaxonomyDefinition,
    ActivityTaxonomyEntry,
    ActivityTaxonomyUpdateRequest,
    AnnotationCapabilities,
    AnnotationDocument,
    AnnotationEvent,
    AnnotationReviewWorkflowRequest,
    BinaryLabel,
    CalibrationProfile,
    CaptureManifestV2,
    EventKind,
    IndexReceipt,
    IndexRefreshIssue,
    IndexRefreshResult,
    ReviewDocument,
    ReviewWorkflowState,
    SyncDocument,
    TrainingExportReference,
)
from imu_data_collector.review import ReviewConflictError, workflow_with_timestamp
from imu_data_collector.storage import ObjectConflictError, ObjectStore
from imu_data_collector.sync import assess_conditional_fixed_offset
from imu_data_collector.sync_experiment import read_frame_times, read_sync_window
from imu_data_collector.taxonomy_store import ActivityTaxonomyStore
from imu_data_collector.validation import validate_annotations

logger = logging.getLogger(__name__)

ACCEPTED_MANIFEST_SCHEMA_VERSIONS = ("2.1.0",)


class ManifestIndexError(ValueError):
    """可稳定呈现给采集端和管理员的索引失败。"""

    def __init__(self, code: str, stage: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage


class AnnotationService:
    def __init__(self, settings: Settings, store: ObjectStore) -> None:
        self.settings = settings
        self.store = store
        seed_taxonomy = load_activity_taxonomy(settings.activity_taxonomy_path)
        self.taxonomies = ActivityTaxonomyStore(store, seed_taxonomy)
        self.taxonomy = self.taxonomies.current()[0].model_dump(mode="json")
        self.calibration_evidence = load_calibration_evidence(
            settings.calibration_evidence_path
        )
        self.calibration_recording_ids = {
            str(item["recording_id"])
            for item in self.calibration_evidence.get("evidence", [])
        }
        self.catalog = AnnotationCatalog(settings.annotation.catalog_path)
        self.cache_root = settings.storage.cache_root
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.reviews = AnnotationReviewStore(store, self.taxonomy)
        self._release_delete_lock = threading.RLock()
        self._catalog_refresh_lock = threading.Lock()

    def _publish_taxonomy(self, definition: ActivityTaxonomyDefinition) -> dict[str, Any]:
        payload = definition.model_dump(mode="json")
        self.taxonomy = payload
        self.reviews.taxonomy = payload
        return payload

    def taxonomy_definition(self, version: str | None = None) -> dict[str, Any]:
        """返回当前或历史活动分类表；历史快照从不被当前配置重写。"""

        try:
            definition = (
                self.taxonomies.version(version)
                if version is not None
                else self.taxonomies.current()[0]
            )
        except FileNotFoundError as error:
            raise KeyError(version) from error
        if version is None and definition.version != self.taxonomy["version"]:
            self._publish_taxonomy(definition)
        return definition.model_dump(mode="json")

    def _taxonomy_usage(self) -> dict[str, int]:
        usage = {
            item["code"]: 0
            for label in ("fall", "non_fall")
            for item in self.taxonomy[label]
        }
        for manifest in self.catalog.list():
            try:
                payload, _generation = self.store.read_json(
                    self.reviews.key(manifest.recording_id)
                )
                review = ReviewDocument.model_validate(payload)
            except (FileNotFoundError, ValidationError, ValueError):
                continue
            for segment in review.annotations.segments:
                usage[segment.activity_code] = usage.get(segment.activity_code, 0) + 1
        return usage

    def taxonomy_admin_summary(self) -> dict[str, Any]:
        definition = self.taxonomy_definition()
        usage = self._taxonomy_usage()
        return {
            **definition,
            "fall": [
                {**item, "usage_count": usage.get(item["code"], 0)}
                for item in definition["fall"]
            ],
            "non_fall": [
                {**item, "usage_count": usage.get(item["code"], 0)}
                for item in definition["non_fall"]
            ],
        }

    def create_taxonomy_activity(
        self, request: ActivityTaxonomyCreateRequest
    ) -> dict[str, Any]:
        def update(current: ActivityTaxonomyDefinition) -> ActivityTaxonomyDefinition:
            if any(
                item.code == request.code
                for item in (*current.fall, *current.non_fall)
            ):
                raise ValueError(f"活动标签 code 已存在：{request.code}")
            entry = ActivityTaxonomyEntry(
                code=request.code,
                display_name_zh=request.display_name_zh.strip(),
                display_name_en=request.display_name_en.strip(),
            )
            field = request.binary_label.value
            return current.model_copy(
                update={field: [*getattr(current, field), entry]}
            )

        return self._publish_taxonomy(
            self.taxonomies.mutate(request.expected_version, update)
        )

    def update_taxonomy_activity(
        self, code: str, request: ActivityTaxonomyUpdateRequest
    ) -> dict[str, Any]:
        def update(current: ActivityTaxonomyDefinition) -> ActivityTaxonomyDefinition:
            found = False
            changes = {
                name: value.strip() if isinstance(value, str) else value
                for name, value in {
                    "display_name_zh": request.display_name_zh,
                    "display_name_en": request.display_name_en,
                    "active": request.active,
                }.items()
                if value is not None
            }
            updated: dict[str, list[ActivityTaxonomyEntry]] = {}
            for label in ("fall", "non_fall"):
                entries = []
                for item in getattr(current, label):
                    if item.code == code:
                        found = True
                        item = ActivityTaxonomyEntry.model_validate(
                            item.model_dump(mode="json") | changes
                        )
                    entries.append(item)
                updated[label] = entries
            if not found:
                raise KeyError(code)
            if any(not any(item.active for item in entries) for entries in updated.values()):
                raise ValueError("每个标签类型至少保留一个启用活动")
            return current.model_copy(update=updated)

        return self._publish_taxonomy(
            self.taxonomies.mutate(request.expected_version, update)
        )

    def delete_taxonomy_activity(self, code: str, expected_version: str) -> dict[str, Any]:
        usage = self._taxonomy_usage().get(code, 0)
        if usage:
            raise ValueError(f"活动标签已被 {usage} 个区间使用，只能停用")

        def update(current: ActivityTaxonomyDefinition) -> ActivityTaxonomyDefinition:
            found = False
            updated: dict[str, list[ActivityTaxonomyEntry]] = {}
            for label in ("fall", "non_fall"):
                entries = [item for item in getattr(current, label) if item.code != code]
                if len(entries) != len(getattr(current, label)):
                    found = True
                if not entries:
                    raise ValueError("每个标签类型至少保留一个活动")
                updated[label] = entries
            if not found:
                raise KeyError(code)
            return current.model_copy(update=updated)

        return self._publish_taxonomy(
            self.taxonomies.mutate(expected_version, update)
        )

    @contextmanager
    def _cache_lock(self, digest: str) -> Iterator[None]:
        """跨线程、跨 worker 串行化同一不可变对象的首次下载。"""

        lock_path = self.cache_root / "locks" / f"{digest}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(lock_path):
            yield

    def publish_capabilities(self) -> AnnotationCapabilities:
        """公布采集端上传前必须核对的生产能力合同。"""

        capabilities = AnnotationCapabilities(
            accepted_manifest_schema_versions=list(ACCEPTED_MANIFEST_SCHEMA_VERSIONS),
            accepted_capture_h5_schema_versions=list(
                ANNOTATION_ACCEPTED_CAPTURE_SCHEMA_VERSIONS
            ),
            annotation_build_id=ANNOTATION_API_BUILD_ID,
            generated_at_utc=datetime.now(UTC).isoformat(),
        )
        key = "contracts/annotation-capabilities.json"
        try:
            _payload, generation = self.store.read_json(key)
        except FileNotFoundError:
            generation = 0
        self.store.write_json(
            key,
            capabilities.model_dump(mode="json"),
            if_generation_match=generation,
        )
        return capabilities

    @staticmethod
    def _receipt_key(recording_id: str) -> str:
        return f"index-receipts/{recording_id}.json"

    def _receipt_matches(self, recording_id: str, manifest_generation: int) -> bool:
        try:
            payload, _generation = self.store.read_json(
                self._receipt_key(recording_id)
            )
            receipt = IndexReceipt.model_validate(payload)
        except (FileNotFoundError, ValidationError, ValueError):
            return False
        return (
            receipt.manifest_generation == manifest_generation
            and receipt.status == "indexed"
        )

    def _write_receipt(
        self,
        recording_id: str,
        manifest_generation: int,
        *,
        status: str,
        code: str,
        message: str,
    ) -> IndexReceipt:
        receipt = IndexReceipt(
            recording_id=recording_id,
            manifest_generation=manifest_generation,
            status=status,
            annotation_build_id=ANNOTATION_API_BUILD_ID,
            processed_at_utc=datetime.now(UTC).isoformat(),
            code=code,
            message=message,
        )
        key = self._receipt_key(recording_id)
        try:
            _payload, generation = self.store.read_json(key)
        except FileNotFoundError:
            generation = 0
        self.store.write_json(
            key,
            receipt.model_dump(mode="json"),
            if_generation_match=generation,
        )
        return receipt

    def refresh(self) -> dict[str, Any]:
        """扫描 Bucket，并只校验新增或 generation 已变化的 manifest。"""

        with self._catalog_refresh_lock:
            result = IndexRefreshResult()
            for info in self.store.list("captures/"):
                if not info.key.endswith("/manifest.json"):
                    continue
                recording_id = info.key.removeprefix("captures/").removesuffix(
                    "/manifest.json"
                )
                if not recording_id or "/" in recording_id:
                    result.skipped += 1
                    result.issues.append(
                        IndexRefreshIssue(
                            recording_id=recording_id or "unknown",
                            manifest_key=info.key,
                            stage="discovery",
                            code="manifest_invalid",
                            message="manifest 对象键中的 recording_id 无效",
                        )
                    )
                    continue
                if (
                    self.catalog.manifest_generation(recording_id) == info.generation
                    and self._receipt_matches(recording_id, info.generation)
                ):
                    result.unchanged += 1
                    continue
                try:
                    payload, generation = self.store.read_json(info.key)
                    schema_version = str(payload.get("schema_version", ""))
                    if schema_version not in ACCEPTED_MANIFEST_SCHEMA_VERSIONS:
                        raise ManifestIndexError(
                            "unsupported_schema",
                            "manifest",
                            f"不支持 manifest schema {schema_version or 'missing'}",
                        )
                    try:
                        manifest = CaptureManifestV2.model_validate(payload)
                    except ValidationError as error:
                        raise ManifestIndexError(
                            "manifest_invalid", "manifest", str(error)
                        ) from error
                    if manifest.recording_id != recording_id:
                        raise ManifestIndexError(
                            "manifest_invalid",
                            "manifest",
                            "manifest recording_id 与对象键不一致",
                        )
                    if (
                        manifest.source_h5_schema_version
                        not in ANNOTATION_ACCEPTED_CAPTURE_SCHEMA_VERSIONS
                    ):
                        raise ManifestIndexError(
                            "unsupported_h5_schema",
                            "manifest",
                            "不支持 capture H5 schema "
                            f"{manifest.source_h5_schema_version}",
                        )
                    self._verify_manifest_objects(manifest)
                    self.catalog.upsert(manifest, generation)
                    self._write_receipt(
                        recording_id,
                        generation,
                        status="indexed",
                        code="indexed",
                        message="标注端已校验并建立索引",
                    )
                    result.imported += 1
                except FileNotFoundError as error:
                    failure = ManifestIndexError(
                        "artifact_missing", "artifact", str(error)
                    )
                    self._record_refresh_failure(result, recording_id, info, failure)
                except ManifestIndexError as error:
                    self._record_refresh_failure(result, recording_id, info, error)
                except (ValidationError, ValueError) as error:
                    failure = ManifestIndexError(
                        "manifest_invalid", "manifest", str(error)
                    )
                    self._record_refresh_failure(result, recording_id, info, failure)
            return result.model_dump(mode="json")

    def _record_refresh_failure(
        self,
        result: IndexRefreshResult,
        recording_id: str,
        info: Any,
        error: ManifestIndexError,
    ) -> None:
        result.skipped += 1
        result.issues.append(
            IndexRefreshIssue(
                recording_id=recording_id,
                manifest_key=info.key,
                stage=error.stage,
                code=error.code,
                message=str(error),
            )
        )
        try:
            self._write_receipt(
                recording_id,
                info.generation,
                status="rejected",
                code=error.code,
                message=str(error),
            )
        except (ObjectConflictError, OSError, ValueError):
            logger.exception("写入索引拒绝回执失败：%s", recording_id)

    def _verify_manifest_objects(self, manifest: CaptureManifestV2) -> None:
        for artifact in manifest.artifacts:
            info = self.store.stat(artifact.object_key)
            if info is None:
                raise ManifestIndexError(
                    "artifact_missing", "artifact", f"manifest 缺少制品：{artifact.role}"
                )
            if info.size_bytes != artifact.size_bytes:
                raise ManifestIndexError(
                    "artifact_size_mismatch",
                    "artifact",
                    f"制品大小不匹配：{artifact.role}",
                )
            stored_sha = info.metadata.get("sha256")
            if not stored_sha:
                raise ManifestIndexError(
                    "artifact_sha_missing",
                    "artifact",
                    f"制品缺少 SHA-256 metadata：{artifact.role}",
                )
            if stored_sha != artifact.sha256:
                raise ManifestIndexError(
                    "artifact_sha_mismatch",
                    "artifact",
                    f"制品 SHA-256 不匹配：{artifact.role}",
                )

    def list_recordings(self) -> list[CaptureManifestV2]:
        return self.catalog.list()

    def recording_summary(self, manifest: CaptureManifestV2) -> dict[str, Any]:
        """把不可变 manifest 投影成前端共用的轻量录制摘要。"""

        return {
            "recording_id": manifest.recording_id,
            "collection_id": manifest.collection_id,
            "participant_id": manifest.participant_id,
            "data_tier": manifest.data_tier.value,
            "state": "published",
            "started_at_utc": manifest.captured_at_utc,
            "duration_ns": manifest.duration_ns,
            "issues": [],
            "upload_state": "published",
            "purpose": (
                "calibration_evidence"
                if manifest.recording_id in self.calibration_recording_ids
                else "annotation"
            ),
        }

    def calibration_evidence_summary(self) -> dict[str, Any]:
        """返回只读证据登记，并标识对应不可变制品当前是否可用。"""

        profile_id = str(self.calibration_evidence["profile_id"])
        return {
            **self.calibration_evidence,
            "evidence": [
                {
                    **item,
                    "available": self.store.stat(
                        self._calibration_manifest_key(
                            profile_id, str(item["recording_id"])
                        )
                    )
                    is not None,
                }
                for item in self.calibration_evidence.get("evidence", [])
            ],
        }

    @staticmethod
    def _calibration_manifest_key(profile_id: str, recording_id: str) -> str:
        return f"calibration-evidence/{profile_id}/{recording_id}/manifest.json"

    def calibration_evidence_artifact(
        self, recording_id: str, role: str
    ) -> tuple[dict[str, Any], Any]:
        """返回独立证据档案中的不可变制品。"""

        if recording_id not in self.calibration_recording_ids:
            raise KeyError(recording_id)
        profile_id = str(self.calibration_evidence["profile_id"])
        try:
            payload, _generation = self.store.read_json(
                self._calibration_manifest_key(profile_id, recording_id)
            )
        except FileNotFoundError as error:
            raise KeyError(recording_id) from error
        artifact = next(
            (item for item in payload.get("artifacts", []) if item.get("role") == role),
            None,
        )
        if not isinstance(artifact, dict):
            raise ValueError(f"校准证据缺少制品：{role}")
        info = self.store.stat(str(artifact["object_key"]))
        if info is None:
            raise ValueError(f"校准证据制品不存在：{role}")
        if (
            info.size_bytes != int(artifact["size_bytes"])
            or info.metadata.get("sha256") != artifact.get("sha256")
        ):
            raise ValueError(f"校准证据制品校验失败：{role}")
        return artifact, info

    def archive_calibration_evidence(
        self,
        *,
        apply: bool = False,
        delete_source: bool = False,
    ) -> dict[str, Any]:
        """把证据白名单从普通录制区迁移到独立只读档案。"""

        profile_id = str(self.calibration_evidence["profile_id"])
        results: list[dict[str, Any]] = []
        for item in self.calibration_evidence.get("evidence", []):
            recording_id = str(item["recording_id"])
            source_manifest_key = f"captures/{recording_id}/manifest.json"
            archive_manifest_key = self._calibration_manifest_key(
                profile_id, recording_id
            )
            existing = self.store.stat(archive_manifest_key)
            if existing is not None:
                for required_role in (
                    "capture_h5",
                    "video_mkv",
                    "preview_mp4",
                    "capture_manifest",
                ):
                    self.calibration_evidence_artifact(recording_id, required_role)
                results.append(
                    {
                        "recording_id": recording_id,
                        "status": "already_archived",
                        "archive_manifest_key": archive_manifest_key,
                    }
                )
                continue
            try:
                source_payload, source_generation = self.store.read_json(
                    source_manifest_key
                )
            except FileNotFoundError:
                results.append(
                    {"recording_id": recording_id, "status": "source_missing"}
                )
                continue
            source_artifacts = source_payload.get("artifacts", [])
            if not isinstance(source_artifacts, list) or not source_artifacts:
                raise ValueError(f"{recording_id} 的源 manifest 没有制品清单")
            if not apply:
                results.append(
                    {
                        "recording_id": recording_id,
                        "status": "ready",
                        "artifact_count": len(source_artifacts) + 1,
                        "archive_manifest_key": archive_manifest_key,
                    }
                )
                continue

            directory = self.cache_root / "calibration-migration" / recording_id
            directory.mkdir(parents=True, exist_ok=True)
            archived: list[dict[str, Any]] = []
            for artifact in source_artifacts:
                role = str(artifact["role"])
                source_key = str(artifact["object_key"])
                sha256 = str(artifact["sha256"])
                size_bytes = int(artifact["size_bytes"])
                filename = str(artifact["filename"])
                source_info = self.store.stat(source_key)
                if source_info is None:
                    raise ValueError(f"{recording_id} 缺少源制品：{role}")
                if (
                    source_info.size_bytes != size_bytes
                    or source_info.metadata.get("sha256") != sha256
                ):
                    raise ValueError(f"{recording_id} 源制品校验失败：{role}")
                local_path = directory / filename
                self.store.download_file(source_key, local_path)
                if local_path.stat().st_size != size_bytes or sha256_file(local_path) != sha256:
                    raise ValueError(f"{recording_id} 下载后校验失败：{role}")
                destination_key = (
                    f"calibration-evidence/{profile_id}/{recording_id}/{filename}"
                )
                try:
                    destination = self.store.put_file(
                        local_path,
                        destination_key,
                        content_type=str(
                            artifact.get("content_type")
                            or source_info.content_type
                            or "application/octet-stream"
                        ),
                        metadata={
                            "sha256": sha256,
                            "recording_id": recording_id,
                            "calibration_profile_id": profile_id,
                            "role": role,
                        },
                    )
                except ObjectConflictError as error:
                    destination = self.store.stat(destination_key)
                    if (
                        destination is None
                        or destination.size_bytes != size_bytes
                        or destination.metadata.get("sha256") != sha256
                    ):
                        raise ValueError(
                            f"{recording_id} 目标制品冲突：{role}"
                        ) from error
                archived.append(
                    {
                        "role": role,
                        "filename": filename,
                        "object_key": destination_key,
                        "size_bytes": destination.size_bytes,
                        "sha256": sha256,
                        "content_type": destination.content_type,
                    }
                )

            source_manifest_bytes = self.store.read_bytes(source_manifest_key)
            source_manifest_sha256 = hashlib.sha256(source_manifest_bytes).hexdigest()
            source_manifest_path = directory / "source-manifest.json"
            source_manifest_path.write_bytes(source_manifest_bytes)
            source_manifest_archive_key = (
                f"calibration-evidence/{profile_id}/{recording_id}/"
                "source-manifest.json"
            )
            try:
                source_manifest_info = self.store.put_file(
                    source_manifest_path,
                    source_manifest_archive_key,
                    content_type="application/json",
                    metadata={
                        "sha256": source_manifest_sha256,
                        "recording_id": recording_id,
                        "calibration_profile_id": profile_id,
                        "role": "capture_manifest",
                    },
                )
            except ObjectConflictError as error:
                source_manifest_info = self.store.stat(source_manifest_archive_key)
                if (
                    source_manifest_info is None
                    or source_manifest_info.size_bytes != len(source_manifest_bytes)
                    or source_manifest_info.metadata.get("sha256")
                    != source_manifest_sha256
                ):
                    raise ValueError(
                        f"{recording_id} 目标制品冲突：capture_manifest"
                    ) from error
            archived.append(
                {
                    "role": "capture_manifest",
                    "filename": "source-manifest.json",
                    "object_key": source_manifest_archive_key,
                    "size_bytes": source_manifest_info.size_bytes,
                    "sha256": source_manifest_sha256,
                    "content_type": source_manifest_info.content_type,
                }
            )
            archive_payload = {
                "schema_version": "1.0.0",
                "profile_id": profile_id,
                "recording_id": recording_id,
                "archived_at_utc": datetime.now(UTC).isoformat(),
                "source_manifest_object_key": source_manifest_key,
                "source_manifest_generation": source_generation,
                "source_manifest_sha256": source_manifest_sha256,
                "artifacts": archived,
            }
            self.store.write_json(
                archive_manifest_key,
                archive_payload,
                if_generation_match=0,
            )
            for required_role in (
                "capture_h5",
                "video_mkv",
                "preview_mp4",
                "capture_manifest",
            ):
                self.calibration_evidence_artifact(recording_id, required_role)

            deleted_objects = 0
            if delete_source:
                prefixes = (
                    f"captures/{recording_id}/",
                    f"reviews/{recording_id}/",
                    f"exports/{recording_id}/",
                )
                objects = {
                    candidate.key: candidate
                    for prefix in prefixes
                    for candidate in self.store.list(prefix)
                }
                receipt = self.store.stat(self._receipt_key(recording_id))
                if receipt is not None:
                    objects[receipt.key] = receipt
                for candidate in objects.values():
                    if self.store.delete(
                        candidate.key, if_generation_match=candidate.generation
                    ):
                        deleted_objects += 1
                self.catalog.delete(recording_id)
            results.append(
                {
                    "recording_id": recording_id,
                    "status": "archived",
                    "artifact_count": len(archived),
                    "deleted_source_objects": deleted_objects,
                    "archive_manifest_key": archive_manifest_key,
                }
            )
        return {
            "apply": apply,
            "delete_source": delete_source,
            "profile_id": profile_id,
            "recordings": results,
        }
    def required_manifest(self, recording_id: str) -> CaptureManifestV2:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", recording_id):
            raise ValueError("recording_id 格式无效")
        manifest = self.catalog.get(recording_id)
        if manifest is None:
            raise KeyError(recording_id)
        return manifest

    @staticmethod
    def _artifact(manifest: CaptureManifestV2, role: str):
        return next(item for item in manifest.artifacts if item.role == role)

    def cached_h5(self, manifest: CaptureManifestV2) -> Path:
        artifact = self._artifact(manifest, "capture_h5")
        directory = self.cache_root / "objects" / artifact.sha256
        path = directory / "capture.h5"
        marker = directory / "verified.json"
        expected = {
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "object_key": artifact.object_key,
        }
        with self._cache_lock(artifact.sha256):
            if path.is_file() and path.stat().st_size == artifact.size_bytes:
                try:
                    if json.loads(marker.read_text(encoding="utf-8")) == expected:
                        return path
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    pass
                if sha256_file(path) == artifact.sha256:
                    self._write_cache_marker(marker, expected)
                    return path
            path.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
            self.store.download_file(artifact.object_key, path)
            if path.stat().st_size != artifact.size_bytes or sha256_file(path) != artifact.sha256:
                path.unlink(missing_ok=True)
                raise ValueError("下载后的 H5 大小或 SHA-256 不匹配")
            self._write_cache_marker(marker, expected)
            return path

    @staticmethod
    def _write_cache_marker(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def review(self, recording_id: str) -> ReviewDocument:
        manifest = self.required_manifest(recording_id)
        review, _generation = self.reviews.load(manifest)
        return review

    def frame_times(self, recording_id: str) -> dict[str, Any]:
        return read_frame_times(self.cached_h5(self.required_manifest(recording_id)))

    def sync_window(
        self,
        recording_id: str,
        frame_index: int,
        radius_seconds: float,
        expected_video_minus_imu_ns: int | None,
    ) -> dict[str, Any]:
        return read_sync_window(
            self.cached_h5(self.required_manifest(recording_id)),
            frame_index,
            radius_seconds,
            expected_video_minus_imu_ns,
        )

    def timeline(self, recording_id: str, max_points: int) -> dict[str, Any]:
        manifest = self.required_manifest(recording_id)
        review, _generation = self.reviews.load(manifest)
        offset_ns = 0
        if len(review.sync.anchors) == 2:
            offset_ns = assess_conditional_fixed_offset(review.sync).applied_offset_ns
        with h5py.File(self.cached_h5(manifest), "r") as handle:
            times_dataset = handle["imu/samples/recording_time_ns"]
            step = max(1, int(np.ceil(len(times_dataset) / max_points)))
            times = np.asarray(times_dataset[::step], dtype=np.int64) + offset_ns
            calibrated = manifest.calibration.verified
            values_dataset = handle[
                "imu/samples/values_si" if calibrated else "imu/samples/raw_counts"
            ]
            values = np.asarray(values_dataset[::step], dtype=np.float32)
        return {
            "time_s": (times / 1e9).tolist(),
            "values": values.tolist(),
            "unit": "SI" if calibrated else "raw_counts",
            "downsample_step": step,
        }

    def annotations(self, recording_id: str) -> AnnotationDocument:
        return self.review(recording_id).annotations

    def save_annotations(
        self,
        recording_id: str,
        document: AnnotationDocument,
        actor_id: str,
        expected_revision: int,
    ) -> AnnotationDocument:
        self._require_allowed_actor(actor_id)
        manifest = self.required_manifest(recording_id)
        current, _generation = self.reviews.load(manifest)
        if current.revision != expected_revision:
            raise ReviewConflictError("review.json 已更新，请刷新后重试")
        self._require_current_annotator(current, actor_id)
        current_taxonomy = self.taxonomy_definition()
        active_codes = {
            item["code"]
            for label in ("fall", "non_fall")
            for item in current_taxonomy[label]
            if item.get("active", True)
        }
        previous_codes = {
            (item.segment_id, item.activity_code)
            for item in current.annotations.segments
        }
        newly_selected_inactive = sorted(
            {
                item.activity_code
                for item in document.segments
                if item.activity_code not in active_codes
                and (item.segment_id, item.activity_code) not in previous_codes
            }
        )
        if newly_selected_inactive:
            raise ValueError(
                "停用标签不能用于新标注：" + "、".join(newly_selected_inactive)
            )
        h5_path = self.cached_h5(manifest)
        document = document.model_copy(
            update={
                "taxonomy_id": current_taxonomy["taxonomy_id"],
                "taxonomy_version": current_taxonomy["version"],
                "segments": [
                    item.model_copy(update={"annotator_id": actor_id})
                    for item in document.segments
                ],
                "events": [
                    item.model_copy(update={"annotator_id": actor_id})
                    for item in document.events
                ],
                "exclusions": [
                    item.model_copy(update={"annotator_id": actor_id})
                    for item in document.exclusions
                ],
            }
        )
        enriched = self._canonicalize_and_enrich_events(h5_path, document)
        annotators = {
            item.annotator_id
            for item in (*enriched.segments, *enriched.events, *enriched.exclusions)
        }
        if len(annotators) > 1:
            raise ValueError("一个 review.json 修订只能由一名标注者保存")
        issues = validate_annotations(enriched, current_taxonomy, manifest.duration_ns)
        if issues:
            raise ValueError("；".join(issues))
        if enriched.revision != current.annotations.revision + 1:
            raise ReviewConflictError(
                f"标注已更新：下一 revision 应为 {current.annotations.revision + 1}"
            )

        def update(review: ReviewDocument) -> ReviewDocument:
            if review.workflow.state == ReviewWorkflowState.COMPLETED:
                raise ValueError("已完成的标注必须先重开")
            self._require_current_annotator(review, actor_id)
            return review.model_copy(
                update={
                    "annotations": enriched,
                    "workflow": workflow_with_timestamp(
                        review.workflow, last_editor_id=actor_id
                    ),
                }
            )

        return self.reviews.mutate(manifest, expected_revision, update).annotations

    @staticmethod
    def _canonicalize_and_enrich_events(
        path: Path, document: AnnotationDocument
    ) -> AnnotationDocument:
        with h5py.File(path, "r") as handle:
            video = np.asarray(
                handle["video/frames/recording_time_ns"], dtype=np.int64
            )
            imu = np.asarray(
                handle["imu/samples/recording_time_ns"], dtype=np.int64
            )

        def nearest(values: np.ndarray, target: int) -> int | None:
            if not len(values):
                return None
            index = int(np.searchsorted(values, target))
            candidates = [item for item in (index - 1, index) if 0 <= item < len(values)]
            return min(candidates, key=lambda item: abs(int(values[item]) - target))

        # onset 是跌倒区间起点的派生事实，前端不再单独标记。丢弃传入的
        # onset 并依据每个 fall segment 重建，避免旧客户端制造矛盾状态。
        canonical_events = [
            event for event in document.events if event.kind != EventKind.ONSET
        ]
        canonical_events.extend(
            AnnotationEvent(
                segment_id=segment.segment_id,
                kind=EventKind.ONSET,
                time_ns=segment.start_ns,
                annotator_id=segment.annotator_id,
            )
            for segment in document.segments
            if segment.binary_label == BinaryLabel.FALL
        )
        canonical_events.sort(
            key=lambda event: (event.time_ns, event.segment_id, event.kind.value)
        )
        events = [
            AnnotationEvent(
                **event.model_dump(exclude={"source_video_frame", "source_imu_sample"}),
                source_video_frame=nearest(video, event.time_ns),
                source_imu_sample=nearest(imu, event.time_ns),
            )
            for event in canonical_events
        ]
        return document.model_copy(update={"events": events})

    def save_sync(
        self,
        recording_id: str,
        document: SyncDocument,
        actor_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require_allowed_actor(actor_id)
        manifest = self.required_manifest(recording_id)
        current, _generation = self.reviews.load(manifest)
        if current.revision != expected_revision:
            raise ReviewConflictError("review.json 已更新，请刷新后重试")
        self._require_current_annotator(current, actor_id)
        document = document.model_copy(
            update={
                "reviewer_id": actor_id,
                "anchors": [
                    item.model_copy(update={"reviewer_id": actor_id})
                    for item in document.anchors
                ],
            }
        )
        h5_path = self.cached_h5(manifest)
        self._validate_sync_sources(h5_path, document)
        assessment = assess_conditional_fixed_offset(document)
        def update(review: ReviewDocument) -> ReviewDocument:
            if review.workflow.state == ReviewWorkflowState.COMPLETED:
                raise ValueError("已完成的同步必须先重开")
            self._require_current_annotator(review, actor_id)
            return review.model_copy(
                update={
                    "sync": document,
                    "workflow": workflow_with_timestamp(
                        review.workflow, last_editor_id=actor_id
                    ),
                }
            )

        self.reviews.mutate(manifest, expected_revision, update)
        return self._sync_display(h5_path, assessment.as_dict(), document)

    def sync(self, recording_id: str) -> dict[str, Any]:
        manifest = self.required_manifest(recording_id)
        document, _generation = self.reviews.load(manifest)
        if len(document.sync.anchors) == 2:
            result = assess_conditional_fixed_offset(document.sync).as_dict()
        else:
            result = {
                "policy": document.sync.policy,
                "quality": "missing",
                "estimated_offset_ns": 0,
                "applied_offset_ns": 0,
                "anchor_disagreement_ns": 0,
                "residual_rms_ns": float("nan"),
                "recommendation": "none",
                "decision": "host_only",
            }
        return self._sync_display(
            self.cached_h5(manifest), result, document.sync
        )

    @staticmethod
    def _validate_sync_sources(path: Path, document: SyncDocument) -> None:
        with h5py.File(path, "r") as handle:
            start_ns = int(handle.attrs["recording_start_monotonic_ns"])
            video = np.asarray(
                handle["video/frames/recording_time_ns"], dtype=np.int64
            )
            imu = (
                np.asarray(handle["imu/samples/time_monotonic_ns"], dtype=np.int64)
                - start_ns
            )
        for anchor in document.anchors:
            if anchor.source_video_frame is not None:
                index = anchor.source_video_frame
                if index >= len(video) or int(video[index]) != anchor.video_time_ns:
                    raise ValueError("同步锚点的视频来源不一致")
                if anchor.video_interval_start_ns != int(video[max(0, index - 1)]):
                    raise ValueError("同步锚点的视频离散区间不一致")
            if anchor.source_imu_sample is not None:
                index = anchor.source_imu_sample
                if index >= len(imu) or int(imu[index]) != anchor.imu_time_ns:
                    raise ValueError("同步锚点的 IMU 来源不一致")
                if anchor.imu_interval_start_ns != int(imu[max(0, index - 1)]):
                    raise ValueError("同步锚点的 IMU 离散区间不一致")

    @staticmethod
    def _sync_display(
        path: Path, result: dict[str, Any], document: SyncDocument
    ) -> dict[str, Any]:
        with h5py.File(path, "r") as handle:
            frames = np.asarray(
                handle["video/frames/recording_time_ns"], dtype=np.int64
            )
            delta = np.diff(frames)
            median = float(np.median(delta[delta > 0])) if np.any(delta > 0) else 0.0
            rate = float(handle["imu"].attrs.get("observed_rate_hz", 0.0))
        estimated = int(result.get("estimated_offset_ns", 0))
        applied = int(result.get("applied_offset_ns", 0))
        return {
            **result,
            "anchors": [item.model_dump(mode="json") for item in document.anchors],
            "estimated_offset_seconds": estimated / 1e9,
            "applied_offset_seconds": applied / 1e9,
            "estimated_offset_video_frames": estimated / median if median else None,
            "applied_offset_video_frames": applied / median if median else None,
            "estimated_offset_imu_samples": estimated / 1e9 * rate if rate else None,
            "applied_offset_imu_samples": applied / 1e9 * rate if rate else None,
            "actual_median_fps": 1e9 / median if median else None,
            "observed_imu_rate_hz": rate or None,
        }

    def update_workflow(
        self,
        recording_id: str,
        request: AnnotationReviewWorkflowRequest,
        actor_id: str,
    ) -> ReviewDocument:
        self._require_allowed_actor(actor_id)
        manifest = self.required_manifest(recording_id)

        if request.action == "complete":
            return self._complete_recording(
                manifest,
                expected_revision=request.expected_revision,
                actor_id=actor_id,
            )

        def update(review: ReviewDocument) -> ReviewDocument:
            workflow = review.workflow
            action = request.action
            if action == "assign":
                if workflow.state not in {
                    ReviewWorkflowState.UNASSIGNED,
                    ReviewWorkflowState.IN_PROGRESS,
                }:
                    raise ValueError("当前状态不能领取")
                workflow = workflow_with_timestamp(
                    workflow,
                    state=ReviewWorkflowState.IN_PROGRESS,
                    annotator_id=actor_id,
                    last_editor_id=actor_id,
                )
            elif action == "reopen":
                if workflow.state != ReviewWorkflowState.COMPLETED:
                    raise ValueError("只有已完成的录制可以重开")
                if (
                    workflow.annotator_id != actor_id
                    and actor_id not in self.settings.identity.admins
                ):
                    raise ValueError("只有任务负责人或管理员可以重开")
                workflow = workflow_with_timestamp(
                    workflow,
                    state=ReviewWorkflowState.IN_PROGRESS,
                    annotator_id=actor_id,
                    last_editor_id=actor_id,
                )
            else:
                raise ValueError("不支持的工作流操作")
            return review.model_copy(
                update={
                    "workflow": workflow,
                    "active_export": None if action == "reopen" else review.active_export,
                }
            )

        return self.reviews.mutate(manifest, request.expected_revision, update)

    def _authoritative_calibration(self) -> CalibrationProfile:
        """从服务器私有配置和版本化证据文件构造唯一可信校准档案。"""

        configured = CalibrationProfile(
            profile_id=self.settings.imu.calibration_profile_id,
            verified=self.settings.imu.calibration_verified,
            accel_counts_per_g=self.settings.imu.accel_counts_per_g,
            gyro_counts_per_dps=self.settings.imu.gyro_counts_per_dps,
            accel_bias_counts=self.settings.imu.accel_bias_counts,
            gyro_bias_counts=self.settings.imu.gyro_bias_counts,
            raw_axis_order=self.settings.imu.raw_axis_order,
            axis_signs=self.settings.imu.axis_signs,
            method=self.settings.imu.calibration_method,
            evidence_sha256=self.settings.imu.calibration_evidence_sha256,
        )
        evidence = self.calibration_evidence
        calibration = evidence.get("calibration", {})
        if not configured.verified or calibration.get("status") != "engineering_verified":
            raise ValueError("服务器校准档案尚未达到 engineering_verified")
        if evidence.get("profile_id") != configured.profile_id:
            raise ValueError("服务器配置与校准证据的 profile_id 不一致")
        if configured.evidence_sha256 is None:
            raise ValueError("服务器校准证据缺少 SHA-256")
        actual_evidence_sha256 = sha256_file(self.settings.calibration_evidence_path)
        if configured.evidence_sha256 != actual_evidence_sha256:
            raise ValueError("服务器配置的校准证据 SHA-256 与实际证据文件不一致")
        evidence_fields = {
            "accel_counts_per_g": calibration.get("accel_counts_per_g"),
            "gyro_counts_per_dps": calibration.get("gyro_counts_per_dps"),
            "accel_bias_counts": calibration.get("accel_bias_counts_raw"),
            "gyro_bias_counts": calibration.get("gyro_bias_counts_raw"),
            "raw_axis_order": calibration.get("raw_axis_order"),
            "axis_signs": calibration.get("axis_signs"),
        }
        configured_fields = {
            "accel_counts_per_g": configured.accel_counts_per_g,
            "gyro_counts_per_dps": configured.gyro_counts_per_dps,
            "accel_bias_counts": list(configured.accel_bias_counts),
            "gyro_bias_counts": list(configured.gyro_bias_counts),
            "raw_axis_order": list(configured.raw_axis_order),
            "axis_signs": list(configured.axis_signs),
        }
        for name, expected in configured_fields.items():
            if evidence_fields[name] != expected:
                raise ValueError(f"服务器配置与校准证据的 {name} 不一致")
        return configured

    @staticmethod
    def _calibration_mismatch(
        actual: CalibrationProfile, expected: CalibrationProfile
    ) -> str | None:
        for name in (
            "profile_id",
            "verified",
            "method",
            "evidence_sha256",
            "raw_axis_order",
            "axis_signs",
            "accel_bias_counts",
            "gyro_bias_counts",
        ):
            if getattr(actual, name) != getattr(expected, name):
                return name
        for name in ("accel_counts_per_g", "gyro_counts_per_dps"):
            left = getattr(actual, name)
            right = getattr(expected, name)
            if left is None or right is None or not math.isclose(
                left, right, rel_tol=0.0, abs_tol=1e-9
            ):
                return name
        return None

    def _verify_export_source(
        self, manifest: CaptureManifestV2, h5_path: Path
    ) -> CalibrationProfile:
        """交叉核对对象、manifest、服务器档案和 H5 冻结属性。"""

        self._verify_manifest_objects(manifest)
        authoritative = self._authoritative_calibration()
        mismatch = self._calibration_mismatch(manifest.calibration, authoritative)
        if mismatch:
            raise ValueError(f"manifest 校准参数与服务器档案不一致：{mismatch}")

        def text(value: Any) -> str:
            return value.decode("utf-8") if isinstance(value, bytes) else str(value)

        with h5py.File(h5_path, "r") as handle:
            root_expected = {
                "recording_id": manifest.recording_id,
                "collection_id": manifest.collection_id,
                "participant_id": manifest.participant_id,
                "data_tier": manifest.data_tier.value,
                "body_location": manifest.body_location,
                "capture_schema_version": manifest.source_h5_schema_version,
                "started_at_utc": manifest.captured_at_utc,
            }
            for name, expected in root_expected.items():
                if text(handle.attrs.get(name, "")) != str(expected):
                    raise ValueError(f"H5 与 manifest 的 {name} 不一致")
            if int(handle.attrs.get("duration_ns", -1)) != manifest.duration_ns:
                raise ValueError("H5 与 manifest 的 duration_ns 不一致")
            if not bool(handle.attrs.get("calibration_verified", False)):
                raise ValueError("H5 冻结属性未确认校准")
            if text(handle.attrs.get("calibration_profile_id", "")) != authoritative.profile_id:
                raise ValueError("H5 根属性 calibration_profile_id 不一致")
            imu = handle["imu"].attrs
            h5_profile = CalibrationProfile(
                profile_id=text(imu.get("calibration_profile_id", "unverified")),
                verified=bool(handle.attrs.get("calibration_verified", False)),
                accel_counts_per_g=(
                    float(imu["accel_counts_per_g"])
                    if "accel_counts_per_g" in imu
                    else None
                ),
                gyro_counts_per_dps=(
                    float(imu["gyro_counts_per_dps"])
                    if "gyro_counts_per_dps" in imu
                    else None
                ),
                accel_bias_counts=tuple(
                    json.loads(text(imu.get("accel_bias_counts_json", "[]")))
                ),
                gyro_bias_counts=tuple(
                    json.loads(text(imu.get("gyro_bias_counts_json", "[]")))
                ),
                raw_axis_order=tuple(
                    json.loads(text(imu.get("raw_axis_order_json", "[]")))
                ),
                axis_signs=tuple(
                    json.loads(text(imu.get("axis_signs_json", "[]")))
                ),
                method=text(imu.get("calibration_method", "unverified")),
                evidence_sha256=text(imu.get("calibration_evidence_sha256", "")) or None,
            )
        mismatch = self._calibration_mismatch(h5_profile, authoritative)
        if mismatch:
            raise ValueError(f"H5 校准冻结属性与服务器档案不一致：{mismatch}")
        return authoritative

    def _build_training_export(
        self,
        manifest: CaptureManifestV2,
        review: ReviewDocument,
        expected_revision: int,
    ) -> TrainingExportReference:
        """为固定 review revision 构建并上传不可变 aligned25 对象。"""

        recording_id = manifest.recording_id
        if manifest.data_tier.value != "prod":
            raise ValueError("test 数据永久禁止导出到训练集")
        if review.revision != expected_revision:
            raise ReviewConflictError("review.json 已更新，请刷新后重试")
        manifest_artifacts = {item.role: item for item in manifest.artifacts}
        for source in review.sources:
            artifact = manifest_artifacts[source.role]
            if (
                source.filename != artifact.filename
                or source.size_bytes != artifact.size_bytes
                or source.sha256 != artifact.sha256
            ):
                raise ValueError(f"review.json 与 manifest 的 {source.role} 不一致")
        h5_path = self.cached_h5(manifest)
        authoritative = self._verify_export_source(manifest, h5_path)
        output = (
            self.cache_root
            / "exports"
            / recording_id
            / f"review-{expected_revision}"
            / "aligned25.h5"
        )
        try:
            export_taxonomy = self.taxonomy_definition(
                review.annotations.taxonomy_version
            )
        except KeyError as error:
            raise ValueError(
                f"找不到标注使用的 taxonomy 版本：{review.annotations.taxonomy_version}"
            ) from error
        export_aligned(
            review,
            h5_path,
            Path("video.mkv"),
            output,
            ImuSettings(
                calibration_profile_id=authoritative.profile_id,
                calibration_verified=authoritative.verified,
                accel_counts_per_g=authoritative.accel_counts_per_g,
                gyro_counts_per_dps=authoritative.gyro_counts_per_dps,
                accel_bias_counts=authoritative.accel_bias_counts,
                gyro_bias_counts=authoritative.gyro_bias_counts,
                raw_axis_order=authoritative.raw_axis_order,
                axis_signs=authoritative.axis_signs,
                calibration_method=authoritative.method,
                calibration_evidence_sha256=authoritative.evidence_sha256,
            ),
            export_taxonomy,
            source_hashes_verified=True,
        )
        digest = sha256_file(output)
        with h5py.File(output, "r") as handle:
            logical_digest = str(handle.attrs["logical_content_sha256"])
        key = (
            f"exports/{recording_id}/review-{expected_revision}/"
            f"aligned25-{logical_digest[:16]}.h5"
        )
        metadata = {
            "sha256": digest,
            "logical_content_sha256": logical_digest,
            "recording_id": recording_id,
            "source_review_revision": str(expected_revision),
            "calibration_profile_id": authoritative.profile_id,
            "calibration_evidence_sha256": authoritative.evidence_sha256 or "",
            "hdf5_schema_version": TRAINING_SCHEMA_VERSION,
            "sampling_rate_hz": "25",
        }
        try:
            info = self.store.put_file(
                output,
                key,
                content_type="application/x-hdf5",
                metadata=metadata,
            )
        except ObjectConflictError as error:
            info = self.store.stat(key)
            if (
                info is None
                or info.size_bytes != output.stat().st_size
                or any(info.metadata.get(name) != value for name, value in metadata.items())
            ):
                raise ValueError(
                    "同版本训练导出对象已存在但内容校验失败"
                ) from error
        return TrainingExportReference(
            export_schema_version="2.0.0",
            hdf5_schema_version=TRAINING_SCHEMA_VERSION,
            sampling_rate_hz=25.0,
            filename="aligned25.h5",
            source_review_revision=expected_revision,
            object_key=key,
            sha256=digest,
            logical_content_sha256=logical_digest,
            size_bytes=info.size_bytes,
            calibration_profile_id=authoritative.profile_id,
            calibration_evidence_sha256=authoritative.evidence_sha256 or "",
            created_at_utc=datetime.now(UTC).isoformat(),
        )

    def _complete_recording(
        self,
        manifest: CaptureManifestV2,
        *,
        expected_revision: int,
        actor_id: str,
    ) -> ReviewDocument:
        """验证、导出并在同一次乐观锁提交中把任务标记为已完成。"""

        review, _generation = self.reviews.load(manifest)
        if review.revision != expected_revision:
            raise ReviewConflictError("review.json 已更新，请刷新后重试")
        if review.workflow.state != ReviewWorkflowState.IN_PROGRESS:
            raise ValueError("只有进行中的标注可以完成")
        self._require_current_annotator(review, actor_id)
        if not review.annotations.finalized:
            raise ValueError("完成前必须定稿标注")
        if len(review.sync.anchors) != 2 or assess_conditional_fixed_offset(
            review.sync
        ).quality != "verified":
            raise ValueError("完成前必须验证同步")
        reference = self._build_training_export(manifest, review, expected_revision)

        def mark_completed(current: ReviewDocument) -> ReviewDocument:
            self._require_current_annotator(current, actor_id)
            if current.workflow.state != ReviewWorkflowState.IN_PROGRESS:
                raise ValueError("任务状态已经变化，请刷新后重试")
            return current.model_copy(
                update={
                    "workflow": workflow_with_timestamp(
                        current.workflow,
                        state=ReviewWorkflowState.COMPLETED,
                        last_editor_id=actor_id,
                    ),
                    "active_export": reference,
                }
            )

        return self.reviews.mutate(manifest, expected_revision, mark_completed)

    def active_export(
        self, recording_id: str
    ) -> tuple[TrainingExportReference, Any]:
        manifest = self.required_manifest(recording_id)
        review, _generation = self.reviews.load(manifest)
        reference = review.active_export
        if review.workflow.state != ReviewWorkflowState.COMPLETED or reference is None:
            raise FileNotFoundError("尚未生成当前 revision 的训练 HDF5")
        info = self.store.stat(reference.object_key)
        if info is None:
            raise ValueError("当前训练导出对象缺失")
        if (
            info.size_bytes != reference.size_bytes
            or info.metadata.get("sha256") != reference.sha256
            or info.metadata.get("logical_content_sha256")
            != reference.logical_content_sha256
        ):
            raise ValueError("当前训练导出对象与 review.json 不一致")
        return reference, info

    def _publish_benchmark_snapshot(
        self,
        files: list[tuple[str, str, Path]],
        *,
        snapshot_id: str,
        fingerprint: str,
        created_at_utc: str,
    ) -> dict[str, Any]:
        """发布不可变合并 HDF5，并用 generation 前置条件推进 team current。"""

        merged = self.cache_root / "benchmark-snapshots" / snapshot_id / "cw12eu.h5"
        merge_training_exports(files, merged)
        physical_sha256 = sha256_file(merged)

        def text(value: object) -> str:
            return value.decode("utf-8") if isinstance(value, bytes) else str(value)

        with h5py.File(merged, "r") as handle:
            sequences = np.asarray(handle["sequences"])
            annotations = np.asarray(handle["annotations"])
            logical_sha256 = str(handle.attrs["logical_content_sha256"])
            kinds = Counter(text(row["kind"]) for row in annotations)
            supervision = Counter(text(row["supervision_kind"]) for row in sequences)
            body_locations = Counter(text(row["body_location"]) for row in sequences)
            participants = {text(value) for value in sequences["participant_id"]}
            file_entry = {
                "dataset_id": "cw12eu",
                "object_key": (
                    f"benchmark-datasets/team/cw12eu/{snapshot_id}/datasets/cw12eu.h5"
                ),
                "filename": "cw12eu.h5",
                "size_bytes": merged.stat().st_size,
                "sha256": physical_sha256,
                "logical_content_sha256": logical_sha256,
                "hdf5_schema_version": TRAINING_SCHEMA_VERSION,
                "sampling_rate_hz": 25.0,
                "evaluation_role": "training_only",
                "sequences": len(sequences),
                "rows": len(handle["samples"]),
                "annotations": len(annotations),
                "events": kinds["onset"],
                "segments": kinds["activity"] + kinds["exclude"],
                "fall_sequences": int(np.count_nonzero(sequences["is_fall"])),
                "participants": len(participants),
                "supervision": dict(sorted(supervision.items())),
                "body_locations": dict(sorted(body_locations.items())),
            }
        file_metadata = {
            "sha256": physical_sha256,
            "logical_content_sha256": logical_sha256,
            "snapshot_id": snapshot_id,
            "hdf5_schema_version": TRAINING_SCHEMA_VERSION,
            "sampling_rate_hz": "25",
            "evaluation_role": "training_only",
        }
        try:
            uploaded = self.store.put_file(
                merged,
                str(file_entry["object_key"]),
                content_type="application/x-hdf5",
                metadata=file_metadata,
            )
        except ObjectConflictError as error:
            uploaded = self.store.stat(str(file_entry["object_key"]))
            if (
                uploaded is None
                or uploaded.size_bytes != merged.stat().st_size
                or any(uploaded.metadata.get(key) != value for key, value in file_metadata.items())
            ):
                raise ValueError("同一 benchmark snapshot 的 cw12eu.h5 内容不一致") from error

        benchmark_manifest = {
            "schema_version": "imu_benchmark_dataset_manifest_v1",
            "kind": "team",
            "contract_version": "imu_benchmark_contract_v2",
            "snapshot_id": snapshot_id,
            "created_at_utc": created_at_utc,
            "files": [file_entry],
            "source": {
                "repository": "imu-data-collector",
                "training_snapshot_id": snapshot_id,
                "content_fingerprint": fingerprint,
            },
        }
        manifest_key = f"benchmark-datasets/team/cw12eu/{snapshot_id}/manifest.json"
        manifest_bytes = (
            json.dumps(benchmark_manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        try:
            self.store.write_json(
                manifest_key,
                benchmark_manifest,
                if_generation_match=0,
            )
        except ObjectConflictError as error:
            existing, _generation = self.store.read_json(manifest_key)
            if existing != benchmark_manifest:
                raise ValueError("同一 benchmark snapshot 的 manifest 内容不一致") from error

        current_key = "benchmark-datasets/team/cw12eu/current.json"
        current = {
            "schema_version": "imu_benchmark_current_v1",
            "kind": "team",
            "snapshot_id": snapshot_id,
            "manifest_object": manifest_key,
            "manifest_sha256": manifest_sha256,
            "updated_at_utc": created_at_utc,
        }
        try:
            existing_current, generation = self.store.read_json(current_key)
        except FileNotFoundError:
            existing_current, generation = None, 0
        if existing_current != current:
            self.store.write_json(
                current_key,
                current,
                if_generation_match=generation,
            )
        return {
            "snapshot_id": snapshot_id,
            "hdf5_object_key": file_entry["object_key"],
            "hdf5_sha256": physical_sha256,
            "logical_content_sha256": logical_sha256,
            "hdf5_size_bytes": uploaded.size_bytes,
            "manifest_object_key": manifest_key,
            "manifest_sha256": manifest_sha256,
            "current_object_key": current_key,
        }

    def create_training_snapshot(self, actor_id: str) -> dict[str, Any]:
        """冻结点击时的已完成 prod 集合，并按内容幂等生成训练快照。"""

        self._require_allowed_actor(actor_id)
        with self._release_delete_lock:
            files: list[tuple[str, str, Path]] = []
            recordings: list[dict[str, Any]] = []
            incompatible_recordings: list[str] = []
            for manifest in self.catalog.list():
                if manifest.data_tier.value != "prod":
                    continue
                review, _generation = self.reviews.load(manifest)
                if (
                    review.workflow.state != ReviewWorkflowState.COMPLETED
                    or review.active_export is None
                ):
                    continue
                reference, info = self.active_export(manifest.recording_id)
                if (
                    reference.sampling_rate_hz != 25.0
                    or reference.hdf5_schema_version != TRAINING_SCHEMA_VERSION
                ):
                    incompatible_recordings.append(manifest.recording_id)
                    continue
                path = (
                    self.cache_root
                    / "release-inputs"
                    / reference.sha256
                    / reference.filename
                )
                with self._cache_lock(reference.sha256):
                    if (
                        not path.is_file()
                        or path.stat().st_size != reference.size_bytes
                        or sha256_file(path) != reference.sha256
                    ):
                        self.store.download_file(reference.object_key, path)
                    if (
                        path.stat().st_size != info.size_bytes
                        or sha256_file(path) != reference.sha256
                    ):
                        path.unlink(missing_ok=True)
                        raise ValueError("训练导出缓存的大小或 SHA-256 不匹配")
                files.append((manifest.participant_id, manifest.recording_id, path))
                recordings.append(
                    {
                        "participant_id": manifest.participant_id,
                        "recording_id": manifest.recording_id,
                        "source_review_revision": reference.source_review_revision,
                        "export_schema_version": reference.export_schema_version,
                        "hdf5_schema_version": reference.hdf5_schema_version,
                        "sampling_rate_hz": reference.sampling_rate_hz,
                        "aligned_object_key": reference.object_key,
                        "aligned_sha256": reference.sha256,
                        "logical_content_sha256": reference.logical_content_sha256,
                    }
                )
            if incompatible_recordings:
                raise ValueError(
                    "以下已完成录制仍引用历史 30 Hz 导出，需 reopen 后重新完成："
                    + "、".join(sorted(incompatible_recordings))
                )
            if not files:
                raise ValueError("没有已完成的正式录制可生成训练快照")
            recordings.sort(key=lambda item: (item["participant_id"], item["recording_id"]))
            fingerprint = hashlib.sha256(
                json.dumps(
                    recordings,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            timestamp = datetime.now(UTC)
            snapshot_id = f"snapshot-{fingerprint[:24]}"
            manifest_key = f"training-snapshots/{snapshot_id}/manifest.json"
            try:
                existing_payload, _existing_generation = self.store.read_json(manifest_key)
            except FileNotFoundError:
                existing_payload = None
            benchmark_manifest_key = (
                f"benchmark-datasets/team/cw12eu/{snapshot_id}/manifest.json"
            )
            try:
                existing_benchmark, _benchmark_generation = self.store.read_json(
                    benchmark_manifest_key
                )
            except FileNotFoundError:
                existing_benchmark = None
            created_at_utc = (
                str(existing_payload["created_at_utc"])
                if existing_payload is not None
                else (
                    str(existing_benchmark["created_at_utc"])
                    if existing_benchmark is not None
                    else timestamp.isoformat()
                )
            )
            path = self.cache_root / "training-snapshots" / f"cw12eu_{snapshot_id}.tar"
            create_training_snapshot_archive(files, path)
            key = f"training-snapshots/{snapshot_id}/{path.name}"
            archive_sha256 = sha256_file(path)
            archive_metadata = {
                "sha256": archive_sha256,
                "snapshot_id": snapshot_id,
                "content_fingerprint": fingerprint,
            }
            try:
                archive = self.store.put_file(
                    path,
                    key,
                    content_type="application/x-tar",
                    metadata=archive_metadata,
                )
            except ObjectConflictError as error:
                archive = self.store.stat(key)
                if (
                    archive is None
                    or archive.size_bytes != path.stat().st_size
                    or any(
                        archive.metadata.get(name) != value
                        for name, value in archive_metadata.items()
                    )
                ):
                    raise ValueError("同一训练快照 ID 的 TAR 内容不一致") from error
            benchmark = self._publish_benchmark_snapshot(
                files,
                snapshot_id=snapshot_id,
                fingerprint=fingerprint,
                created_at_utc=created_at_utc,
            )
            payload: dict[str, Any] = {
                "schema_version": "3.0.0",
                "snapshot_id": snapshot_id,
                "created_at_utc": created_at_utc,
                "created_by": actor_id,
                "content_fingerprint": fingerprint,
                "archive_object_key": key,
                "archive_sha256": archive_sha256,
                "archive_size_bytes": archive.size_bytes,
                "recordings": recordings,
                "benchmark": benchmark,
            }
            try:
                self.store.write_json(
                    manifest_key,
                    payload,
                    if_generation_match=0,
                )
                created = True
            except ObjectConflictError as error:
                existing, _generation = self.store.read_json(manifest_key)
                if (
                    existing.get("content_fingerprint") != fingerprint
                    or existing.get("archive_sha256") != archive_sha256
                    or existing.get("recordings") != recordings
                    or existing.get("benchmark") != benchmark
                ):
                    raise ValueError(
                        "同一训练快照 ID 的 manifest 内容不一致"
                    ) from error
                payload = existing
                created = False
            logger.info(
                "创建训练快照 snapshot_id=%s actor_id=%s recordings=%d",
                snapshot_id,
                actor_id,
                len(recordings),
            )
            return {**self._snapshot_summary(payload), "created": created}

    def list_training_snapshots(self) -> list[dict[str, Any]]:
        """列出按内容去重的训练快照。"""

        snapshots: list[dict[str, Any]] = []
        for info in self.store.list("training-snapshots/"):
            if not info.key.endswith("/manifest.json"):
                continue
            payload, _generation = self.store.read_json(info.key)
            snapshot_id = str(payload.get("snapshot_id", ""))
            if not snapshot_id:
                continue
            snapshots.append(self._snapshot_summary(payload))
        return sorted(
            snapshots,
            key=lambda item: str(item.get("created_at_utc") or item["snapshot_id"]),
            reverse=True,
        )

    @staticmethod
    def _snapshot_summary(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "snapshot_id": str(payload["snapshot_id"]),
            "created_at_utc": payload.get("created_at_utc"),
            "created_by": payload.get("created_by"),
            "content_fingerprint": payload.get("content_fingerprint"),
            "archive_object_key": str(payload["archive_object_key"]),
            "archive_sha256": str(payload["archive_sha256"]),
            "archive_size_bytes": int(payload.get("archive_size_bytes", 0)),
            "recording_count": len(payload.get("recordings", [])),
            "benchmark": payload.get("benchmark"),
        }

    def training_snapshot_download(self, snapshot_id: str) -> tuple[dict[str, Any], Any]:
        """返回训练快照清单和 TAR 对象信息。"""

        self._validate_snapshot_id(snapshot_id)
        try:
            payload, _generation = self.store.read_json(
                f"training-snapshots/{snapshot_id}/manifest.json"
            )
        except FileNotFoundError as error:
            raise KeyError(snapshot_id) from error
        archive = self.store.stat(str(payload["archive_object_key"]))
        if archive is None:
            raise ValueError("训练快照 TAR 缺失")
        if (
            archive.size_bytes != int(payload.get("archive_size_bytes", -1))
            or archive.metadata.get("sha256") != payload.get("archive_sha256")
            or archive.metadata.get("content_fingerprint")
            != payload.get("content_fingerprint")
        ):
            raise ValueError("训练快照 TAR 与 manifest 不一致")
        return payload, archive

    def benchmark_snapshot_download(self, snapshot_id: str) -> tuple[dict[str, Any], Any]:
        """返回已发布给 benchmark 的合并 cw12eu.h5。"""

        self._validate_snapshot_id(snapshot_id)
        try:
            payload, _generation = self.store.read_json(
                f"training-snapshots/{snapshot_id}/manifest.json"
            )
        except FileNotFoundError as error:
            raise KeyError(snapshot_id) from error
        benchmark = payload.get("benchmark")
        if not isinstance(benchmark, dict):
            raise FileNotFoundError("该历史训练快照没有 benchmark HDF5")
        key = str(benchmark["hdf5_object_key"])
        artifact = self.store.stat(key)
        if artifact is None:
            raise ValueError("benchmark HDF5 缺失")
        if (
            artifact.size_bytes != int(benchmark.get("hdf5_size_bytes", -1))
            or artifact.metadata.get("sha256") != benchmark.get("hdf5_sha256")
            or artifact.metadata.get("logical_content_sha256")
            != benchmark.get("logical_content_sha256")
        ):
            raise ValueError("benchmark HDF5 与训练快照 manifest 不一致")
        return payload, artifact

    def delete_training_snapshot(
        self,
        snapshot_id: str,
        *,
        actor_id: str,
        confirmation: str,
    ) -> dict[str, Any]:
        """管理员显式清理一个内容快照，不维护撤销或墓碑状态。"""

        self._require_allowed_actor(actor_id)
        if actor_id not in self.settings.identity.admins:
            raise ValueError("只有管理员可以清理训练快照")
        self._validate_snapshot_id(snapshot_id)
        if confirmation != f"DELETE {snapshot_id}":
            raise ValueError(f"二次确认必须完整输入 DELETE {snapshot_id}")
        with self._release_delete_lock:
            manifest_key = f"training-snapshots/{snapshot_id}/manifest.json"
            try:
                payload, generation = self.store.read_json(manifest_key)
            except FileNotFoundError as error:
                raise KeyError(snapshot_id) from error
            archive_key = str(payload["archive_object_key"])
            archive = self.store.stat(archive_key)
            if archive is not None:
                self.store.delete(archive_key, if_generation_match=archive.generation)
            # 先删大对象、最后删清单。若对象删除发生 generation 冲突，清单仍在，
            # 管理员可以用同一个确认文本安全重试；反向顺序会制造不可发现的孤儿 TAR。
            self.store.delete(manifest_key, if_generation_match=generation)
            logger.warning(
                "清理训练快照 snapshot_id=%s actor_id=%s",
                snapshot_id,
                actor_id,
            )
            return {"snapshot_id": snapshot_id, "deleted": True}

    @staticmethod
    def _validate_snapshot_id(snapshot_id: str) -> None:
        if not re.fullmatch(r"snapshot-[0-9a-f]{24}", snapshot_id):
            raise ValueError("snapshot_id 格式无效")

    def delete_recording(
        self,
        recording_id: str,
        *,
        actor_id: str,
        confirmation: str,
    ) -> dict[str, Any]:
        """删除普通录制；已经归档的训练快照保持自包含。"""

        self._require_allowed_actor(actor_id)
        if confirmation != f"DELETE {recording_id}":
            raise ValueError(f"二次确认必须完整输入 DELETE {recording_id}")
        with self._release_delete_lock:
            entry = self.catalog.get_for_deletion(recording_id)
            if entry is None:
                raise KeyError(recording_id)
            manifest, deletion_state = entry
            if deletion_state == "active":
                self.catalog.mark_deleting(recording_id)

            prefixes = (
                f"captures/{recording_id}/",
                f"reviews/{recording_id}/",
                f"exports/{recording_id}/",
            )
            objects = {
                info.key: info for prefix in prefixes for info in self.store.list(prefix)
            }
            deleted_objects = 0
            for info in sorted(
                objects.values(),
                key=lambda item: (
                    0 if item.key == f"captures/{recording_id}/manifest.json" else 1,
                    item.key,
                ),
            ):
                if self.store.delete(
                    info.key,
                    if_generation_match=info.generation,
                ):
                    deleted_objects += 1

            cache_directory = (self.cache_root / recording_id).resolve()
            if not cache_directory.is_relative_to(self.cache_root.resolve()):
                raise ValueError("缓存目录越出配置的数据根目录")
            if cache_directory.is_dir():
                shutil.rmtree(cache_directory)
            self.catalog.delete(recording_id)
            logger.warning(
                "删除标注录制 recording_id=%s actor_id=%s objects=%d",
                recording_id,
                actor_id,
                deleted_objects,
            )
            return {
                "recording_id": manifest.recording_id,
                "deleted_objects": deleted_objects,
                "deleted": True,
            }

    def cleanup_orphan_uploads(
        self,
        *,
        min_age: timedelta = timedelta(days=7),
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """清理长期没有 manifest 的中断上传，不触碰完整录制。"""

        if min_age <= timedelta(0):
            raise ValueError("孤儿对象最小保留时间必须大于 0")
        cutoff = (now or datetime.now(UTC)) - min_age
        groups: dict[str, list[Any]] = {}
        for info in self.store.list("captures/"):
            parts = info.key.split("/")
            if len(parts) < 3 or parts[0] != "captures":
                continue
            groups.setdefault(parts[1], []).append(info)

        orphan_recordings = 0
        orphan_exports = 0
        orphan_snapshots = 0
        orphan_receipts = 0
        orphan_upload_sessions = 0
        candidate_objects = 0
        candidate_bytes = 0
        deleted_objects = 0
        for recording_id, objects in sorted(groups.items()):
            manifest_key = f"captures/{recording_id}/manifest.json"
            if any(item.key == manifest_key for item in objects):
                continue
            timestamps = [item.updated_at_utc for item in objects]
            if not timestamps or any(value is None for value in timestamps):
                continue
            if max(value for value in timestamps if value is not None) > cutoff:
                continue
            orphan_recordings += 1
            candidate_objects += len(objects)
            candidate_bytes += sum(item.size_bytes for item in objects)
            if dry_run:
                continue
            for info in objects:
                if self.store.delete(
                    info.key,
                    if_generation_match=info.generation,
                ):
                    deleted_objects += 1
            cache_directory = (self.cache_root / recording_id).resolve()
            if cache_directory.is_relative_to(self.cache_root.resolve()):
                shutil.rmtree(cache_directory, ignore_errors=True)

        protected_exports: set[str] = set()
        protected_export_recordings: set[str] = set()
        for info in self.store.list("reviews/"):
            if not info.key.endswith("/review.json"):
                continue
            try:
                payload, _generation = self.store.read_json(info.key)
                review = ReviewDocument.model_validate(payload)
            except (FileNotFoundError, ValueError):
                parts = info.key.split("/")
                if len(parts) >= 3:
                    protected_export_recordings.add(parts[1])
                continue
            if review.active_export is not None:
                protected_exports.add(review.active_export.object_key)

        snapshot_groups: dict[str, list[Any]] = {}
        for info in self.store.list("training-snapshots/"):
            parts = info.key.split("/")
            if len(parts) < 3:
                continue
            snapshot_groups.setdefault(parts[1], []).append(info)
        for snapshot_id, objects in sorted(snapshot_groups.items()):
            manifest_key = f"training-snapshots/{snapshot_id}/manifest.json"
            if any(item.key == manifest_key for item in objects):
                continue
            timestamps = [item.updated_at_utc for item in objects]
            if (
                not timestamps
                or any(value is None for value in timestamps)
                or max(value for value in timestamps if value is not None) > cutoff
            ):
                continue
            orphan_snapshots += 1
            candidate_objects += len(objects)
            candidate_bytes += sum(item.size_bytes for item in objects)
            if not dry_run:
                for info in objects:
                    if self.store.delete(info.key, if_generation_match=info.generation):
                        deleted_objects += 1

        for info in self.store.list("exports/"):
            parts = info.key.split("/")
            recording_id = parts[1] if len(parts) >= 3 else ""
            if (
                info.key in protected_exports
                or recording_id in protected_export_recordings
                or info.updated_at_utc is None
                or info.updated_at_utc > cutoff
            ):
                continue
            orphan_exports += 1
            candidate_objects += 1
            candidate_bytes += info.size_bytes
            if not dry_run and self.store.delete(
                info.key, if_generation_match=info.generation
            ):
                deleted_objects += 1

        for info in self.store.list("index-receipts/"):
            if not info.key.endswith(".json"):
                continue
            recording_id = Path(info.key).stem
            if (
                self.store.stat(f"captures/{recording_id}/manifest.json") is not None
                or info.updated_at_utc is None
                or info.updated_at_utc > cutoff
            ):
                continue
            orphan_receipts += 1
            candidate_objects += 1
            candidate_bytes += info.size_bytes
            if not dry_run and self.store.delete(
                info.key, if_generation_match=info.generation
            ):
                deleted_objects += 1

        for info in self.store.list("_upload_sessions/"):
            if info.updated_at_utc is None or info.updated_at_utc > cutoff:
                continue
            orphan_upload_sessions += 1
            candidate_objects += 1
            candidate_bytes += info.size_bytes
            if not dry_run and self.store.delete(
                info.key, if_generation_match=info.generation
            ):
                deleted_objects += 1
        result = {
            "dry_run": dry_run,
            "cutoff_utc": cutoff.isoformat(),
            "orphan_recordings": orphan_recordings,
            "orphan_exports": orphan_exports,
            "orphan_snapshots": orphan_snapshots,
            "orphan_receipts": orphan_receipts,
            "orphan_upload_sessions": orphan_upload_sessions,
            "candidate_objects": candidate_objects,
            "candidate_bytes": candidate_bytes,
            "deleted_objects": deleted_objects,
        }
        logger.info("孤儿上传清理结果 %s", result)
        return result

    def status(self, recording_id: str) -> dict[str, Any]:
        manifest = self.required_manifest(recording_id)
        review, _generation = self.reviews.load(manifest)
        sync_quality = "missing"
        if len(review.sync.anchors) == 2:
            sync_quality = assess_conditional_fixed_offset(review.sync).quality
        return {
            "capture": "published",
            "sync": sync_quality,
            "annotation": review.workflow.state.value,
            "calibration": "verified" if manifest.calibration.verified else "unverified",
            "export": (
                "exported"
                if review.workflow.state == ReviewWorkflowState.COMPLETED
                and review.active_export is not None
                else "not_exported"
            ),
            "review_revision": review.revision,
        }

    def _require_allowed_actor(self, actor: str) -> None:
        if actor not in self.settings.identity.allowed_unikeys:
            raise ValueError("actor_id 不在允许名单")

    @staticmethod
    def _require_current_annotator(review: ReviewDocument, actor_id: str) -> None:
        if review.workflow.state != ReviewWorkflowState.IN_PROGRESS:
            raise ValueError("必须先领取处于进行中的标注任务")
        if review.workflow.annotator_id != actor_id:
            raise ValueError("该任务已由其他成员领取，请先接管并刷新页面")
