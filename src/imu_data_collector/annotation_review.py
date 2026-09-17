"""标注端独占的 review.json 存储与并发控制。"""

from __future__ import annotations

import logging
from collections.abc import Callable

from imu_data_collector.models import (
    AnnotationDocument,
    CaptureManifestV2,
    ParticipantAssignment,
    ParticipantAssignmentStatus,
    ParticipantEvidence,
    ReviewDocument,
    ReviewWorkflow,
    SourceArtifact,
    SyncDocument,
)
from imu_data_collector.review import ReviewConflictError
from imu_data_collector.storage import ObjectConflictError, ObjectStore

logger = logging.getLogger(__name__)


class AnnotationReviewStore:
    def __init__(
        self,
        store: ObjectStore,
        taxonomy: dict,
        on_change: Callable[[ReviewDocument, int], None] | None = None,
    ) -> None:
        self.store = store
        self.taxonomy = taxonomy
        self.on_change = on_change

    def _notify(self, review: ReviewDocument, generation: int) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(review, generation)
        except Exception:
            # The immutable review write is authoritative. A local read-model
            # failure must not make a successful write look retryable.
            logger.exception("更新录制任务索引失败：%s", review.recording_id)

    @staticmethod
    def key(recording_id: str) -> str:
        return f"reviews/{recording_id}/review.json"

    def _initial(self, manifest: CaptureManifestV2) -> ReviewDocument:
        by_role = {item.role: item for item in manifest.artifacts}
        legacy_identity = (
            manifest.schema_version not in {"3.0.0", "3.1.0", "3.2.0"}
            and manifest.participant_id
        )
        return ReviewDocument(
            schema_version="2.0.0" if legacy_identity else "3.0.0",
            recording_id=manifest.recording_id,
            sources=[
                SourceArtifact(
                    role="capture_h5",
                    filename=by_role["capture_h5"].filename,
                    size_bytes=by_role["capture_h5"].size_bytes,
                    sha256=by_role["capture_h5"].sha256,
                ),
                SourceArtifact(
                    role="video_mkv",
                    filename=by_role["video_mkv"].filename,
                    size_bytes=by_role["video_mkv"].size_bytes,
                    sha256=by_role["video_mkv"].sha256,
                ),
            ],
            sync=SyncDocument(),
            annotations=AnnotationDocument(
                taxonomy_id=str(self.taxonomy["taxonomy_id"]),
                taxonomy_version=str(self.taxonomy["version"]),
            ),
            workflow=ReviewWorkflow(),
            participant_assignment=(
                ParticipantAssignment(
                    status=ParticipantAssignmentStatus.CONFIRMED,
                    participant_id=manifest.participant_id,
                    evidence=ParticipantEvidence(
                        video_frame_index=0,
                        video_time_ns=0,
                    ),
                    selected_by=manifest.participant_id,
                    selected_at_utc=manifest.captured_at_utc,
                    confirmed_by=manifest.participant_id,
                    confirmed_at_utc=manifest.captured_at_utc,
                )
                if legacy_identity
                else ParticipantAssignment()
            ),
        )

    def load(self, manifest: CaptureManifestV2) -> tuple[ReviewDocument, int]:
        key = self.key(manifest.recording_id)
        try:
            payload, generation = self.store.read_json(key)
            review = ReviewDocument.model_validate(payload)
            self._notify(review, generation)
            return review, generation
        except FileNotFoundError:
            initial = self._initial(manifest)
            try:
                info = self.store.write_json(
                    key,
                    initial.model_dump(mode="json"),
                    if_generation_match=0,
                )
                self._notify(initial, info.generation)
                return initial, info.generation
            except ObjectConflictError:
                payload, generation = self.store.read_json(key)
                review = ReviewDocument.model_validate(payload)
                self._notify(review, generation)
                return review, generation

    def mutate(
        self,
        manifest: CaptureManifestV2,
        expected_revision: int,
        update: Callable[[ReviewDocument], ReviewDocument],
    ) -> ReviewDocument:
        current, generation = self.load(manifest)
        if current.revision != expected_revision:
            raise ReviewConflictError("review.json 已更新，请刷新后重试")
        updated = update(current).model_copy(update={"revision": current.revision + 1})
        try:
            info = self.store.write_json(
                self.key(manifest.recording_id),
                updated.model_dump(mode="json"),
                if_generation_match=generation,
            )
        except ObjectConflictError as error:
            raise ReviewConflictError("review.json 已被另一位用户更新") from error
        self._notify(updated, info.generation)
        return updated
