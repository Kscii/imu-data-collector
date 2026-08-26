"""不依赖任何采集硬件的独立标注业务服务。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from imu_data_collector.annotation_catalog import AnnotationCatalog
from imu_data_collector.annotation_review import AnnotationReviewStore
from imu_data_collector.artifacts import create_training_release, export_aligned30
from imu_data_collector.config import ImuSettings, Settings, load_activity_taxonomy
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    AnnotationDocument,
    AnnotationEvent,
    AnnotationReviewWorkflowRequest,
    BinaryLabel,
    CaptureManifestV2,
    EventKind,
    ReviewDocument,
    ReviewPolicy,
    ReviewWorkflowState,
    SyncDocument,
    SyncExperimentDocument,
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
        self.catalog = AnnotationCatalog(settings.annotation.catalog_path)
        self.cache_root = settings.storage.cache_root
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.review_policy = ReviewPolicy(settings.annotation.review_policy)
        self.reviews = AnnotationReviewStore(store, self.taxonomy, self.review_policy)
        self._release_delete_lock = threading.RLock()
        self._catalog_refresh_lock = threading.Lock()

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
            if stored_sha and stored_sha != artifact.sha256:
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
        path = self.cache_root / manifest.recording_id / "capture.h5"
        if path.is_file() and sha256_file(path) == artifact.sha256:
            return path
        self.store.download_file(artifact.object_key, path)
        if sha256_file(path) != artifact.sha256:
            path.unlink(missing_ok=True)
            raise ValueError("下载后的 H5 SHA-256 不匹配")
        return path

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
            times = np.asarray(
                handle["imu/samples/recording_time_ns"], dtype=np.int64
            ) + offset_ns
            raw = np.asarray(handle["imu/samples/raw_counts"], dtype=np.int16)
            values_si = np.asarray(handle["imu/samples/values_si"], dtype=np.float32)
        calibrated = manifest.calibration.verified
        values = values_si if calibrated else raw.astype(np.float32)
        step = max(1, int(np.ceil(len(times) / max_points)))
        return {
            "time_s": (times[::step] / 1e9).tolist(),
            "values": values[::step].tolist(),
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
    ) -> AnnotationDocument:
        self._require_allowed_actor(actor_id)
        manifest = self.required_manifest(recording_id)
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
        current, _generation = self.reviews.load(manifest)
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
            actor = next(iter(annotators), actor_id)
            workflow = review.workflow
            if workflow.state == ReviewWorkflowState.UNASSIGNED:
                workflow = workflow_with_timestamp(
                    workflow,
                    state=ReviewWorkflowState.IN_PROGRESS,
                    annotator_id=actor,
                )
            return review.model_copy(
                update={"annotations": enriched, "workflow": workflow}
            )

        return self.reviews.mutate(manifest, current.revision, update).annotations

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
    ) -> dict[str, Any]:
        self._require_allowed_actor(actor_id)
        manifest = self.required_manifest(recording_id)
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
        current, _generation = self.reviews.load(manifest)
        expected = document.expected_revision
        if expected is None:
            expected = current.revision

        def update(review: ReviewDocument) -> ReviewDocument:
            if review.workflow.state in {
                ReviewWorkflowState.ACCEPTED,
                ReviewWorkflowState.EXPORTED,
            }:
                raise ValueError("已完成的同步必须先重开")
            return review.model_copy(update={"sync": document})

        self.reviews.mutate(manifest, expected, update)
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
                if actor_id not in self.settings.identity.admins:
                    raise ValueError("只有管理员可以重开")
                if workflow.state not in {
                    ReviewWorkflowState.ACCEPTED,
                    ReviewWorkflowState.EXPORTED,
                }:
                    raise ValueError("只有已完成或已导出的录制可以重开")
                workflow = workflow_with_timestamp(
                    workflow,
                    state=ReviewWorkflowState.IN_PROGRESS,
                    reviewer_id=None,
                    review_comment=request.comment.strip(),
                )
            else:
                raise ValueError("mark_exported 只能由导出任务设置")
            return review.model_copy(update={"workflow": workflow})

        return self.reviews.mutate(manifest, request.expected_revision, update)

    def export_training(self, recording_id: str, expected_revision: int) -> str:
        manifest = self.required_manifest(recording_id)
        if manifest.data_tier.value != "prod":
            raise ValueError("test 数据永久禁止导出到训练集")
        if not manifest.calibration.verified:
            raise ValueError("IMU 尺度校准未验证，禁止训练导出")
        review, _generation = self.reviews.load(manifest)
        if review.revision != expected_revision:
            raise ReviewConflictError("review.json 已更新，请刷新后重试")
        h5_path = self.cached_h5(manifest)
        output = h5_path.parent / "aligned30.h5"
        export_aligned30(
            review,
            h5_path,
            Path("video.mkv"),
            output,
            ImuSettings(
                accel_counts_per_g=manifest.calibration.accel_counts_per_g,
                gyro_counts_per_dps=manifest.calibration.gyro_counts_per_dps,
            ),
            self.taxonomy,
            source_hashes_verified=True,
        )
        key = f"exports/{recording_id}/aligned30.h5"
        digest = sha256_file(output)
        if self.store.stat(key) is None:
            self.store.put_file(
                output,
                key,
                content_type="application/x-hdf5",
                metadata={"sha256": digest, "recording_id": recording_id},
            )

        def mark_exported(current: ReviewDocument) -> ReviewDocument:
            return current.model_copy(
                update={
                    "workflow": workflow_with_timestamp(
                        current.workflow, state=ReviewWorkflowState.EXPORTED
                    )
                }
            )

        self.reviews.mutate(manifest, expected_revision, mark_exported)
        return key

    def create_release(self, actor_id: str) -> dict[str, Any]:
        """把当前全部已导出 prod 数据发布为幂等的不可变快照。"""

        self._require_allowed_actor(actor_id)
        with self._release_delete_lock:
            files: list[tuple[str, str, Path]] = []
            recordings: list[dict[str, str]] = []
            for manifest in self.catalog.list():
                if manifest.data_tier.value != "prod":
                    continue
                review, _generation = self.reviews.load(manifest)
                if review.workflow.state != ReviewWorkflowState.EXPORTED:
                    continue
                key = f"exports/{manifest.recording_id}/aligned30.h5"
                path = self.cache_root / manifest.recording_id / "aligned30.h5"
                self.store.download_file(key, path)
                digest = sha256_file(path)
                files.append((manifest.participant_id, manifest.recording_id, path))
                recordings.append(
                    {
                        "participant_id": manifest.participant_id,
                        "recording_id": manifest.recording_id,
                        "aligned30_sha256": digest,
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
            for existing in self.list_releases():
                if existing.get("content_fingerprint") == fingerprint:
                    return {**existing, "created": False}

            timestamp = datetime.now(UTC)
            release_id = f"{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}-{fingerprint[:8]}"
            path = self.cache_root / "releases" / f"cw12eu_{release_id}.tar"
            create_training_release(files, path)
            key = f"releases/{release_id}/{path.name}"
            archive_sha256 = sha256_file(path)
            self.store.put_file(
                path,
                key,
                content_type="application/x-tar",
                metadata={"sha256": archive_sha256, "release_id": release_id},
            )
            payload: dict[str, Any] = {
                "schema_version": "2.0.0",
                "release_id": release_id,
                "created_at_utc": timestamp.isoformat(),
                "created_by": actor_id,
                "content_fingerprint": fingerprint,
                "archive_object_key": key,
                "archive_sha256": archive_sha256,
                "archive_size_bytes": path.stat().st_size,
                "recordings": recordings,
            }
            self.store.write_json(
                f"releases/{release_id}/manifest.json",
                payload,
                if_generation_match=0,
            )
            logger.info(
                "创建训练发布 release_id=%s actor_id=%s recordings=%d",
                release_id,
                actor_id,
                len(recordings),
            )
            return {**self._release_summary(payload), "created": True}

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
        result = {
            "dry_run": dry_run,
            "cutoff_utc": cutoff.isoformat(),
            "orphan_recordings": orphan_recordings,
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
                else "not_exported"
            ),
            "review_revision": review.revision,
        }

    def _require_allowed_actor(self, actor: str) -> None:
        if actor not in self.settings.identity.allowed_unikeys:
            raise ValueError("actor_id 不在允许名单")
