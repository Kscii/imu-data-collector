"""不依赖任何采集硬件的独立标注业务服务。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import re
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from imu_data_collector.annotation_catalog import AnnotationCatalog
from imu_data_collector.annotation_review import AnnotationReviewStore
from imu_data_collector.artifacts import create_training_release, export_aligned30
from imu_data_collector.config import (
    ImuSettings,
    Settings,
    load_activity_taxonomy,
    load_calibration_evidence,
)
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    AnnotationDocument,
    AnnotationEvent,
    AnnotationReviewWorkflowRequest,
    BinaryLabel,
    CalibrationProfile,
    CaptureManifestV2,
    EventKind,
    ReviewDocument,
    ReviewPolicy,
    ReviewWorkflowState,
    SyncDocument,
    SyncExperimentDocument,
    TrainingExportReference,
)
from imu_data_collector.review import ReviewConflictError, workflow_with_timestamp
from imu_data_collector.storage import ObjectConflictError, ObjectStore
from imu_data_collector.sync import assess_conditional_fixed_offset
from imu_data_collector.sync_experiment import read_frame_times, read_sync_window
from imu_data_collector.validation import validate_annotations

logger = logging.getLogger(__name__)


class AnnotationService:
    def __init__(self, settings: Settings, store: ObjectStore) -> None:
        self.settings = settings
        self.store = store
        self.taxonomy = load_activity_taxonomy(settings.activity_taxonomy_path)
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
        self.review_policy = ReviewPolicy(settings.annotation.review_policy)
        self.reviews = AnnotationReviewStore(store, self.taxonomy, self.review_policy)
        self._release_delete_lock = threading.RLock()
        self._catalog_refresh_lock = threading.Lock()

    @contextmanager
    def _cache_lock(self, digest: str) -> Iterator[None]:
        """跨线程、跨 worker 串行化同一不可变对象的首次下载。"""

        lock_path = self.cache_root / "locks" / f"{digest}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def refresh(self) -> dict[str, int]:
        """扫描 Bucket，并只校验新增或 generation 已变化的 manifest。"""

        with self._catalog_refresh_lock:
            imported = 0
            skipped = 0
            for info in self.store.list("captures/"):
                if not info.key.endswith("/manifest.json"):
                    continue
                recording_id = info.key.removeprefix("captures/").removesuffix(
                    "/manifest.json"
                )
                if not recording_id or "/" in recording_id:
                    skipped += 1
                    continue
                if self.catalog.manifest_generation(recording_id) == info.generation:
                    continue
                try:
                    payload, generation = self.store.read_json(info.key)
                    manifest = CaptureManifestV2.model_validate(payload)
                    self._verify_manifest_objects(manifest)
                    self.catalog.upsert(manifest, generation)
                    imported += 1
                except (FileNotFoundError, ValueError):
                    skipped += 1
            return {"imported": imported, "skipped": skipped}

    def _verify_manifest_objects(self, manifest: CaptureManifestV2) -> None:
        for artifact in manifest.artifacts:
            info = self.store.stat(artifact.object_key)
            if info is None:
                raise ValueError(f"manifest 缺少制品：{artifact.role}")
            if info.size_bytes != artifact.size_bytes:
                raise ValueError(f"制品大小不匹配：{artifact.role}")
            stored_sha = info.metadata.get("sha256")
            if not stored_sha:
                raise ValueError(f"制品缺少 SHA-256 metadata：{artifact.role}")
            if stored_sha != artifact.sha256:
                raise ValueError(f"制品 SHA-256 不匹配：{artifact.role}")

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

        active = {item.recording_id for item in self.catalog.list()}
        return {
            **self.calibration_evidence,
            "evidence": [
                {**item, "available": str(item["recording_id"]) in active}
                for item in self.calibration_evidence.get("evidence", [])
            ],
        }

    def sync_experiment(self, experiment_id: str) -> SyncExperimentDocument:
        """读取诊断观察；它不参与正式同步或训练导出。"""

        key = f"diagnostics/sync-experiments/{experiment_id}.json"
        try:
            payload, _generation = self.store.read_json(key)
            return SyncExperimentDocument.model_validate(payload)
        except FileNotFoundError:
            return SyncExperimentDocument(experiment_id=experiment_id)

    def save_sync_experiment(
        self,
        experiment_id: str,
        document: SyncExperimentDocument,
        actor_id: str,
    ) -> SyncExperimentDocument:
        self._require_allowed_actor(actor_id)
        if document.experiment_id != experiment_id:
            raise ValueError("experiment_id 与 URL 不一致")
        current = self.sync_experiment(experiment_id)
        if document.revision != current.revision:
            raise ReviewConflictError("同步诊断观察已更新，请刷新后重试")
        observations = [
            observation.model_copy(update={"reviewer_id": actor_id})
            for observation in document.observations
        ]
        for observation in observations:
            self.required_manifest(observation.recording_id)
        saved = document.model_copy(
            update={
                "revision": current.revision + 1,
                "updated_at_utc": datetime.now(UTC).isoformat(),
                "observations": observations,
            }
        )
        key = f"diagnostics/sync-experiments/{experiment_id}.json"
        try:
            _payload, generation = self.store.read_json(key)
        except FileNotFoundError:
            generation = 0
        try:
            self.store.write_json(
                key,
                saved.model_dump(mode="json"),
                if_generation_match=generation,
            )
        except ObjectConflictError as error:
            raise ReviewConflictError("同步诊断观察已被其他用户更新") from error
        return saved

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
        h5_path = self.cached_h5(manifest)
        document = document.model_copy(
            update={
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
        issues = validate_annotations(enriched, self.taxonomy, manifest.duration_ns)
        if issues:
            raise ValueError("；".join(issues))
        if enriched.revision != current.annotations.revision + 1:
            raise ReviewConflictError(
                f"标注已更新：下一 revision 应为 {current.annotations.revision + 1}"
            )

        def update(review: ReviewDocument) -> ReviewDocument:
            if review.workflow.state in {
                ReviewWorkflowState.SUBMITTED,
                ReviewWorkflowState.ACCEPTED,
                ReviewWorkflowState.EXPORTED,
            }:
                raise ValueError("已提交或完成的标注必须先重开")
            self._require_current_annotator(review, actor_id)
            return review.model_copy(update={"annotations": enriched})

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
            if review.workflow.state in {
                ReviewWorkflowState.SUBMITTED,
                ReviewWorkflowState.ACCEPTED,
                ReviewWorkflowState.EXPORTED,
            }:
                raise ValueError("已提交或完成的同步必须先重开")
            self._require_current_annotator(review, actor_id)
            return review.model_copy(update={"sync": document})

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
                    reviewer_id=None,
                    review_comment="",
                )
            elif action == "submit":
                if workflow.state != ReviewWorkflowState.IN_PROGRESS:
                    raise ValueError("只有进行中的标注可以完成")
                if workflow.annotator_id != actor_id:
                    raise ValueError("只有当前标注者可以完成")
                if not review.annotations.finalized:
                    raise ValueError("完成前必须定稿标注")
                if len(review.sync.anchors) != 2 or assess_conditional_fixed_offset(
                    review.sync
                ).quality != "verified":
                    raise ValueError("完成前必须验证同步")
                target = (
                    ReviewWorkflowState.ACCEPTED
                    if self.review_policy == ReviewPolicy.SINGLE_USER
                    else ReviewWorkflowState.SUBMITTED
                )
                workflow = workflow_with_timestamp(workflow, state=target)
            elif action in {"accept", "reject"}:
                if self.review_policy != ReviewPolicy.TWO_PERSON:
                    raise ValueError("单人策略没有独立审核步骤")
                if workflow.state != ReviewWorkflowState.SUBMITTED:
                    raise ValueError("只有已提交标注可以审核")
                if workflow.annotator_id == actor_id:
                    raise ValueError("标注者不能审核自己的标注")
                if action == "reject" and not request.comment.strip():
                    raise ValueError("驳回必须填写意见")
                workflow = workflow_with_timestamp(
                    workflow,
                    state=(
                        ReviewWorkflowState.ACCEPTED
                        if action == "accept"
                        else ReviewWorkflowState.IN_PROGRESS
                    ),
                    reviewer_id=actor_id,
                    review_comment=request.comment.strip(),
                )
            elif action == "reopen":
                if workflow.state not in {
                    ReviewWorkflowState.ACCEPTED,
                    ReviewWorkflowState.EXPORTED,
                }:
                    raise ValueError("只有已完成或已导出的录制可以重开")
                if not request.comment.strip():
                    raise ValueError("重开必须填写原因")
                workflow = workflow_with_timestamp(
                    workflow,
                    state=ReviewWorkflowState.IN_PROGRESS,
                    annotator_id=actor_id,
                    reviewer_id=None,
                    review_comment=request.comment.strip(),
                )
            else:
                raise ValueError("mark_exported 只能由导出任务设置")
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

    def export_training(
        self, recording_id: str, expected_revision: int
    ) -> TrainingExportReference:
        manifest = self.required_manifest(recording_id)
        if manifest.data_tier.value != "prod":
            raise ValueError("test 数据永久禁止导出到训练集")
        review, _generation = self.reviews.load(manifest)
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
            / "aligned30.h5"
        )
        export_aligned30(
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
            self.taxonomy,
            source_hashes_verified=True,
        )
        digest = sha256_file(output)
        with h5py.File(output, "r") as handle:
            logical_digest = str(handle.attrs["logical_content_sha256"])
        key = (
            f"exports/{recording_id}/review-{expected_revision}/"
            f"aligned30-{logical_digest[:16]}.h5"
        )
        metadata = {
            "sha256": digest,
            "logical_content_sha256": logical_digest,
            "recording_id": recording_id,
            "source_review_revision": str(expected_revision),
            "calibration_profile_id": authoritative.profile_id,
            "calibration_evidence_sha256": authoritative.evidence_sha256 or "",
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
        reference = TrainingExportReference(
            source_review_revision=expected_revision,
            object_key=key,
            sha256=digest,
            logical_content_sha256=logical_digest,
            size_bytes=info.size_bytes,
            calibration_profile_id=authoritative.profile_id,
            calibration_evidence_sha256=authoritative.evidence_sha256 or "",
            created_at_utc=datetime.now(UTC).isoformat(),
        )

        def mark_exported(current: ReviewDocument) -> ReviewDocument:
            return current.model_copy(
                update={
                    "workflow": workflow_with_timestamp(
                        current.workflow, state=ReviewWorkflowState.EXPORTED
                    ),
                    "active_export": reference,
                }
            )

        saved = self.reviews.mutate(manifest, expected_revision, mark_exported)
        assert saved.active_export is not None
        return saved.active_export

    def active_export(
        self, recording_id: str
    ) -> tuple[TrainingExportReference, Any]:
        manifest = self.required_manifest(recording_id)
        review, _generation = self.reviews.load(manifest)
        reference = review.active_export
        if review.workflow.state != ReviewWorkflowState.EXPORTED or reference is None:
            raise FileNotFoundError("尚未生成当前 revision 的 aligned30.h5")
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

    def create_release(self, actor_id: str) -> dict[str, Any]:
        """把当前全部已导出 prod 数据发布为幂等的不可变快照。"""

        self._require_allowed_actor(actor_id)
        with self._release_delete_lock:
            files: list[tuple[str, str, Path]] = []
            recordings: list[dict[str, Any]] = []
            for manifest in self.catalog.list():
                if manifest.data_tier.value != "prod":
                    continue
                review, _generation = self.reviews.load(manifest)
                if (
                    review.workflow.state != ReviewWorkflowState.EXPORTED
                    or review.active_export is None
                ):
                    continue
                reference, info = self.active_export(manifest.recording_id)
                path = (
                    self.cache_root
                    / "release-inputs"
                    / reference.sha256
                    / "aligned30.h5"
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
                        "aligned30_object_key": reference.object_key,
                        "aligned30_sha256": reference.sha256,
                        "logical_content_sha256": reference.logical_content_sha256,
                    }
                )
            if not files:
                raise ValueError("没有可发布的 prod 导出")
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
            release_id = f"release-{fingerprint[:24]}"
            path = self.cache_root / "releases" / f"cw12eu_{release_id}.tar"
            create_training_release(files, path)
            key = f"releases/{release_id}/{path.name}"
            archive_sha256 = sha256_file(path)
            archive_metadata = {
                "sha256": archive_sha256,
                "release_id": release_id,
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
                    raise ValueError("同一训练发布 ID 的 TAR 内容不一致") from error
            payload: dict[str, Any] = {
                "schema_version": "2.0.0",
                "release_id": release_id,
                "created_at_utc": timestamp.isoformat(),
                "created_by": actor_id,
                "content_fingerprint": fingerprint,
                "archive_object_key": key,
                "archive_sha256": archive_sha256,
                "archive_size_bytes": archive.size_bytes,
                "recordings": recordings,
            }
            manifest_key = f"releases/{release_id}/manifest.json"
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
                ):
                    raise ValueError(
                        "同一训练发布 ID 的 manifest 内容不一致"
                    ) from error
                payload = existing
                created = False
            logger.info(
                "创建训练发布 release_id=%s actor_id=%s recordings=%d",
                release_id,
                actor_id,
                len(recordings),
            )
            return {**self._release_summary(payload), "created": created}

    def list_releases(self) -> list[dict[str, Any]]:
        """列出仍可下载的训练发布；撤销项不出现在普通列表。"""

        tombstoned = {
            item.key.removeprefix("release-tombstones/").removesuffix(".json")
            for item in self.store.list("release-tombstones/")
            if item.key.endswith(".json")
        }
        releases: list[dict[str, Any]] = []
        for info in self.store.list("releases/"):
            if not info.key.endswith("/manifest.json"):
                continue
            payload, _generation = self.store.read_json(info.key)
            release_id = str(payload.get("release_id", ""))
            if not release_id or release_id in tombstoned:
                continue
            releases.append(self._release_summary(payload))
        return sorted(
            releases,
            key=lambda item: str(item.get("created_at_utc") or item["release_id"]),
            reverse=True,
        )

    @staticmethod
    def _release_summary(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "release_id": str(payload["release_id"]),
            "created_at_utc": payload.get("created_at_utc"),
            "created_by": payload.get("created_by"),
            "content_fingerprint": payload.get("content_fingerprint"),
            "archive_object_key": str(payload["archive_object_key"]),
            "archive_sha256": str(payload["archive_sha256"]),
            "archive_size_bytes": int(payload.get("archive_size_bytes", 0)),
            "recording_count": len(payload.get("recordings", [])),
            "status": "active",
        }

    def release_download(self, release_id: str) -> tuple[dict[str, Any], Any]:
        """返回有效发布清单和 TAR 对象信息。"""

        self._validate_release_id(release_id)
        if self.store.stat(f"release-tombstones/{release_id}.json") is not None:
            raise RuntimeError("该训练发布已经撤销")
        try:
            payload, _generation = self.store.read_json(
                f"releases/{release_id}/manifest.json"
            )
        except FileNotFoundError as error:
            raise KeyError(release_id) from error
        archive = self.store.stat(str(payload["archive_object_key"]))
        if archive is None:
            raise ValueError("训练发布 TAR 缺失")
        if (
            archive.size_bytes != int(payload.get("archive_size_bytes", -1))
            or archive.metadata.get("sha256") != payload.get("archive_sha256")
            or archive.metadata.get("content_fingerprint")
            != payload.get("content_fingerprint")
        ):
            raise ValueError("训练发布 TAR 与 manifest 不一致")
        return payload, archive

    def release_status(self, release_id: str) -> dict[str, Any]:
        self._validate_release_id(release_id)
        tombstone_key = f"release-tombstones/{release_id}.json"
        try:
            tombstone, _generation = self.store.read_json(tombstone_key)
            return {
                "release_id": release_id,
                "status": str(tombstone.get("status", "revoked")),
                "revoked_at_utc": tombstone.get("revoked_at_utc"),
                "revoked_by": tombstone.get("revoked_by"),
                "reason": tombstone.get("reason"),
                "archive_sha256": tombstone.get("archive_sha256"),
            }
        except FileNotFoundError:
            pass
        try:
            payload, _generation = self.store.read_json(
                f"releases/{release_id}/manifest.json"
            )
        except FileNotFoundError as error:
            raise KeyError(release_id) from error
        return self._release_summary(payload)

    def revoke_release(
        self,
        release_id: str,
        *,
        actor_id: str,
        confirmation: str,
        reason: str,
    ) -> dict[str, Any]:
        """撤销错误发布，并让平台立即停止展示和下载。"""

        self._require_allowed_actor(actor_id)
        if actor_id not in self.settings.identity.admins:
            raise ValueError("只有管理员可以撤销训练发布")
        self._validate_release_id(release_id)
        if confirmation != f"REVOKE {release_id}":
            raise ValueError(f"二次确认必须完整输入 REVOKE {release_id}")
        if not reason.strip():
            raise ValueError("撤销训练发布必须填写原因")
        with self._release_delete_lock:
            tombstone_key = f"release-tombstones/{release_id}.json"
            try:
                tombstone, tombstone_generation = self.store.read_json(tombstone_key)
                if tombstone.get("status") == "revoked":
                    return {
                        "release_id": release_id,
                        "status": "revoked",
                        "revoked": True,
                    }
            except FileNotFoundError:
                tombstone = None
                tombstone_generation = None
            manifest_key = f"releases/{release_id}/manifest.json"
            manifest_generation: int | None = None
            if tombstone is None:
                try:
                    payload, manifest_generation = self.store.read_json(manifest_key)
                except FileNotFoundError as error:
                    raise KeyError(release_id) from error
                tombstone = {
                    "schema_version": "1.0.0",
                    "release_id": release_id,
                    "status": "revoking",
                    "revoked_at_utc": datetime.now(UTC).isoformat(),
                    "revoked_by": actor_id,
                    "reason": reason.strip(),
                    "manifest_object_key": manifest_key,
                    "archive_object_key": payload["archive_object_key"],
                    "archive_sha256": payload.get("archive_sha256"),
                    "content_fingerprint": payload.get("content_fingerprint"),
                    "recordings": payload.get("recordings", []),
                }
                written = self.store.write_json(
                    tombstone_key,
                    tombstone,
                    if_generation_match=0,
                )
                tombstone_generation = written.generation
            else:
                manifest = self.store.stat(manifest_key)
                if manifest is not None:
                    manifest_generation = manifest.generation

            archive_key = str(tombstone.get("archive_object_key", ""))
            if not archive_key:
                candidates = [
                    item
                    for item in self.store.list(f"releases/{release_id}/")
                    if item.key.endswith(".tar")
                ]
                if len(candidates) != 1:
                    raise ValueError("撤销状态缺少唯一的训练发布 TAR，需人工检查")
                archive_key = candidates[0].key
                tombstone["archive_object_key"] = archive_key
            archive = self.store.stat(archive_key)
            if manifest_generation is not None:
                self.store.delete(
                    manifest_key,
                    if_generation_match=manifest_generation,
                )
            if archive is not None:
                self.store.delete(
                    archive_key,
                    if_generation_match=archive.generation,
                )
            tombstone["status"] = "revoked"
            self.store.write_json(
                tombstone_key,
                tombstone,
                if_generation_match=tombstone_generation,
            )
            logger.warning(
                "撤销训练发布 release_id=%s actor_id=%s reason=%s",
                release_id,
                actor_id,
                reason.strip(),
            )
            return {"release_id": release_id, "status": "revoked", "revoked": True}

    @staticmethod
    def _validate_release_id(release_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", release_id):
            raise ValueError("release_id 格式无效")

    def delete_recording(
        self,
        recording_id: str,
        *,
        actor_id: str,
        confirmation: str,
    ) -> dict[str, Any]:
        """删除尚未进入有效训练发布的完整录制。"""

        self._require_allowed_actor(actor_id)
        if confirmation != f"DELETE {recording_id}":
            raise ValueError(f"二次确认必须完整输入 DELETE {recording_id}")
        with self._release_delete_lock:
            entry = self.catalog.get_for_deletion(recording_id)
            if entry is None:
                raise KeyError(recording_id)
            manifest, deletion_state = entry
            self._assert_recording_not_released(recording_id)
            if deletion_state == "active":
                self.catalog.mark_deleting(recording_id)

            self._remove_sync_experiment_references(recording_id)
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

    def _assert_recording_not_released(self, recording_id: str) -> None:
        release_objects = self.store.list("releases/")
        tombstoned = {
            item.key.removeprefix("release-tombstones/").removesuffix(".json")
            for item in self.store.list("release-tombstones/")
            if item.key.endswith(".json")
        }
        manifest_keys = {
            item.key
            for item in release_objects
            if item.key.endswith("/manifest.json")
            and item.key.split("/", 2)[1] not in tombstoned
        }
        for key in sorted(manifest_keys):
            payload, _generation = self.store.read_json(key)
            released_ids = {
                str(item.get("recording_id"))
                for item in payload.get("recordings", [])
                if isinstance(item, dict) and item.get("recording_id")
            }
            if recording_id in released_ids:
                raise ValueError("该录制已经进入不可变训练发布，禁止永久删除")
        for item in release_objects:
            if not item.key.endswith(".tar"):
                continue
            parent = item.key.rsplit("/", 1)[0]
            release_id = parent.split("/", 1)[1]
            if release_id in tombstoned:
                continue
            if f"{parent}/manifest.json" not in manifest_keys:
                raise ValueError(
                    "存在缺少发布清单的旧训练 TAR，无法证明该录制未发布，拒绝删除"
                )

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
        orphan_releases = 0
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

        release_groups: dict[str, list[Any]] = {}
        release_manifest_uncertain = False
        for info in self.store.list("releases/"):
            parts = info.key.split("/")
            if len(parts) < 3:
                continue
            release_groups.setdefault(parts[1], []).append(info)
        for release_id, objects in sorted(release_groups.items()):
            manifest_key = f"releases/{release_id}/manifest.json"
            if any(item.key == manifest_key for item in objects):
                try:
                    payload, _generation = self.store.read_json(manifest_key)
                    for item in payload.get("recordings", []):
                        if isinstance(item, dict) and item.get("aligned30_object_key"):
                            protected_exports.add(str(item["aligned30_object_key"]))
                except (FileNotFoundError, ValueError):
                    release_manifest_uncertain = True
                continue
            timestamps = [item.updated_at_utc for item in objects]
            if (
                not timestamps
                or any(value is None for value in timestamps)
                or max(value for value in timestamps if value is not None) > cutoff
            ):
                continue
            orphan_releases += 1
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
                release_manifest_uncertain
                or info.key in protected_exports
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
        result = {
            "dry_run": dry_run,
            "cutoff_utc": cutoff.isoformat(),
            "orphan_recordings": orphan_recordings,
            "orphan_exports": orphan_exports,
            "orphan_releases": orphan_releases,
            "candidate_objects": candidate_objects,
            "candidate_bytes": candidate_bytes,
            "deleted_objects": deleted_objects,
        }
        logger.info("孤儿上传清理结果 %s", result)
        return result

    def _remove_sync_experiment_references(self, recording_id: str) -> None:
        for info in self.store.list("diagnostics/sync-experiments/"):
            if not info.key.endswith(".json"):
                continue
            payload, generation = self.store.read_json(info.key)
            document = SyncExperimentDocument.model_validate(payload)
            observations = [
                item
                for item in document.observations
                if item.recording_id != recording_id
            ]
            sources = [
                item for item in document.sources if item.recording_id != recording_id
            ]
            if (
                len(observations) == len(document.observations)
                and len(sources) == len(document.sources)
            ):
                continue
            updated = document.model_copy(
                update={
                    "revision": document.revision + 1,
                    "updated_at_utc": datetime.now(UTC).isoformat(),
                    "observations": observations,
                    "sources": sources,
                }
            )
            self.store.write_json(
                info.key,
                updated.model_dump(mode="json"),
                if_generation_match=generation,
            )

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
            "review_policy": review.workflow.review_policy.value,
            "calibration": "verified" if manifest.calibration.verified else "unverified",
            "export": (
                "exported"
                if review.workflow.state == ReviewWorkflowState.EXPORTED
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
