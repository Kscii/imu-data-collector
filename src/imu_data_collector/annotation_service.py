"""不依赖任何采集硬件的独立标注业务服务。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
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
    CaptureManifestV2,
    ReviewDocument,
    ReviewPolicy,
    ReviewWorkflowRequest,
    ReviewWorkflowState,
    SyncDocument,
    SyncExperimentDocument,
)
from imu_data_collector.review import ReviewConflictError, workflow_with_timestamp
from imu_data_collector.storage import ObjectConflictError, ObjectStore
from imu_data_collector.sync import assess_conditional_fixed_offset
from imu_data_collector.sync_experiment import read_frame_times, read_sync_window
from imu_data_collector.validation import validate_annotations


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

    def refresh(self) -> dict[str, int]:
        imported = 0
        skipped = 0
        for info in self.store.list("captures/"):
            if not info.key.endswith("/manifest.json"):
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
        self, experiment_id: str, document: SyncExperimentDocument
    ) -> SyncExperimentDocument:
        if document.experiment_id != experiment_id:
            raise ValueError("experiment_id 与 URL 不一致")
        current = self.sync_experiment(experiment_id)
        if document.revision != current.revision:
            raise ReviewConflictError("同步诊断观察已更新，请刷新后重试")
        for observation in document.observations:
            self._require_allowed_actor(observation.reviewer_id)
            self.required_manifest(observation.recording_id)
        saved = document.model_copy(
            update={
                "revision": current.revision + 1,
                "updated_at_utc": datetime.now(UTC).isoformat(),
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
        self, recording_id: str, document: AnnotationDocument
    ) -> AnnotationDocument:
        manifest = self.required_manifest(recording_id)
        annotators = {
            item.annotator_id
            for item in (*document.segments, *document.events, *document.exclusions)
        }
        if len(annotators) > 1:
            raise ValueError("一个 review.json 修订只能由一名标注者保存")
        for actor in annotators:
            self._require_allowed_actor(actor)
        h5_path = self.cached_h5(manifest)
        enriched = self._enrich_event_indices(h5_path, document)
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
            actor = next(iter(annotators), review.workflow.annotator_id)
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
    def _enrich_event_indices(
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

        events = [
            AnnotationEvent(
                **event.model_dump(exclude={"source_video_frame", "source_imu_sample"}),
                source_video_frame=nearest(video, event.time_ns),
                source_imu_sample=nearest(imu, event.time_ns),
            )
            for event in document.events
        ]
        return document.model_copy(update={"events": events})

    def save_sync(self, recording_id: str, document: SyncDocument) -> dict[str, Any]:
        manifest = self.required_manifest(recording_id)
        for actor in {
            value
            for value in (
                document.reviewer_id,
                *(item.reviewer_id for item in document.anchors),
            )
            if value
        }:
            self._require_allowed_actor(actor)
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
        self, recording_id: str, request: ReviewWorkflowRequest
    ) -> ReviewDocument:
        self._require_allowed_actor(request.actor_id)
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
                    annotator_id=request.actor_id,
                    reviewer_id=None,
                    review_comment="",
                )
            elif action == "submit":
                if workflow.state != ReviewWorkflowState.IN_PROGRESS:
                    raise ValueError("只有进行中的标注可以完成")
                if workflow.annotator_id != request.actor_id:
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
                if workflow.annotator_id == request.actor_id:
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
                    reviewer_id=request.actor_id,
                    review_comment=request.comment.strip(),
                )
            elif action == "reopen":
                if request.actor_id not in self.settings.identity.admins:
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

    def create_release(self) -> str:
        files: list[tuple[str, str, Path]] = []
        for manifest in self.catalog.list():
            review, _generation = self.reviews.load(manifest)
            if review.workflow.state != ReviewWorkflowState.EXPORTED:
                continue
            key = f"exports/{manifest.recording_id}/aligned30.h5"
            path = self.cache_root / manifest.recording_id / "aligned30.h5"
            self.store.download_file(key, path)
            files.append((manifest.participant_id, manifest.recording_id, path))
        if not files:
            raise ValueError("没有可发布的 prod 导出")
        release_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.cache_root / "releases" / f"cw12eu_{release_id}.tar"
        create_training_release(files, path)
        key = f"releases/{release_id}/{path.name}"
        self.store.put_file(
            path,
            key,
            content_type="application/x-tar",
            metadata={"sha256": sha256_file(path), "release_id": release_id},
        )
        return key

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
