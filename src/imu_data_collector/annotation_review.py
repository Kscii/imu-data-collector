"""标注端独占的 review.json 存储与并发控制。"""

from __future__ import annotations

from collections.abc import Callable

from imu_data_collector.models import (
    AnnotationDocument,
    CaptureManifestV2,
    ReviewDocument,
    ReviewPolicy,
    ReviewWorkflow,
    SourceArtifact,
    SyncDocument,
)
from imu_data_collector.review import ReviewConflictError
from imu_data_collector.storage import ObjectConflictError, ObjectStore


class AnnotationReviewStore:
    def __init__(
        self,
        store: ObjectStore,
        taxonomy: dict,
        review_policy: ReviewPolicy,
    ) -> None:
        self.store = store
        self.taxonomy = taxonomy
        self.review_policy = review_policy

    @staticmethod
    def key(recording_id: str) -> str:
        return f"reviews/{recording_id}/review.json"

    def _initial(self, manifest: CaptureManifestV2) -> ReviewDocument:
        by_role = {item.role: item for item in manifest.artifacts}
        return ReviewDocument(
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
            workflow=ReviewWorkflow(review_policy=self.review_policy),
        )

    def load(self, manifest: CaptureManifestV2) -> tuple[ReviewDocument, int]:
        key = self.key(manifest.recording_id)
        try:
            payload, generation = self.store.read_json(key)
            return ReviewDocument.model_validate(payload), generation
        except FileNotFoundError:
            initial = self._initial(manifest)
            try:
                info = self.store.write_json(
                    key,
                    initial.model_dump(mode="json"),
                    if_generation_match=0,
                )
                return initial, info.generation
            except ObjectConflictError:
                payload, generation = self.store.read_json(key)
                return ReviewDocument.model_validate(payload), generation

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
            self.store.write_json(
                self.key(manifest.recording_id),
                updated.model_dump(mode="json"),
                if_generation_match=generation,
            )
        except ObjectConflictError as error:
            raise ReviewConflictError("review.json 已被另一位用户更新") from error
        return updated
