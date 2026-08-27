"""外置同步与标注快照；原始 H5/MKV 在录制完成后保持不可变。"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import h5py

from imu_data_collector.hdf5_store import read_annotations, sha256_file
from imu_data_collector.models import (
    AnnotationDocument,
    ReviewDocument,
    ReviewWorkflow,
    SourceArtifact,
    SyncAnchor,
    SyncDocument,
)

REVIEW_FILENAME = "review.json"


class ReviewConflictError(ValueError):
    """调用者基于过期 revision 保存。"""


def review_path_for(h5_path: Path) -> Path:
    return h5_path.parent / REVIEW_FILENAME


def _source_artifact(path: Path, role: str) -> SourceArtifact:
    if not path.is_file():
        raise ValueError(f"缺少源文件：{path}")
    return SourceArtifact(
        role=role,
        filename=path.name,
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
    )


def _optional_int(group: h5py.Group, name: str, index: int) -> int | None:
    if name not in group:
        return None
    value = int(group[name][index])
    return None if value < 0 else value


def _read_embedded_sync(h5_path: Path) -> SyncDocument:
    with h5py.File(h5_path, "r") as handle:
        if "sync" not in handle:
            return SyncDocument()
        group = handle["sync"]
        imu = group["imu_anchor_ns"][:] if "imu_anchor_ns" in group else []
        video = group["video_anchor_ns"][:] if "video_anchor_ns" in group else []
        labels = (
            [str(item) for item in group["labels"].asstr()[:]]
            if "labels" in group
            else ["tap"] * len(imu)
        )
        roles = (
            [str(item) for item in group["roles"].asstr()[:]]
            if "roles" in group
            else ["legacy"] * len(imu)
        )
        reviewer_ids = (
            [str(item) for item in group["reviewer_ids"].asstr()[:]]
            if "reviewer_ids" in group
            else [""] * len(imu)
        )
        anchors = [
            SyncAnchor(
                imu_time_ns=int(imu[index]),
                video_time_ns=int(video[index]),
                label=labels[index],
                role=roles[index],
                source_video_frame=_optional_int(group, "source_video_frame", index),
                source_imu_sample=_optional_int(group, "source_imu_sample", index),
                video_interval_start_ns=_optional_int(
                    group, "video_interval_start_ns", index
                ),
                imu_interval_start_ns=_optional_int(
                    group, "imu_interval_start_ns", index
                ),
                reviewer_id=reviewer_ids[index] or None,
            )
            for index in range(len(imu))
        ]
        applied_offset = int(group.attrs.get("applied_offset_ns", 0))
    return SyncDocument(
        anchors=anchors,
        apply_fixed_offset=applied_offset != 0,
        reviewer_id=next((item.reviewer_id for item in anchors if item.reviewer_id), None),
    )


def _default_annotations(h5_path: Path, taxonomy: dict) -> AnnotationDocument:
    try:
        return read_annotations(h5_path)
    except (KeyError, OSError, ValueError):
        return AnnotationDocument(
            taxonomy_id=str(taxonomy["taxonomy_id"]),
            taxonomy_version=str(taxonomy["version"]),
        )


def _new_review(h5_path: Path, mkv_path: Path, taxonomy: dict) -> ReviewDocument:
    with h5py.File(h5_path, "r") as handle:
        recording_id = str(handle.attrs["recording_id"])
    return ReviewDocument(
        recording_id=recording_id,
        sources=[
            _source_artifact(h5_path, "capture_h5"),
            _source_artifact(mkv_path, "video_mkv"),
        ],
        sync=_read_embedded_sync(h5_path),
        annotations=_default_annotations(h5_path, taxonomy),
        workflow=ReviewWorkflow(),
    )


def _atomic_write(path: Path, document: ReviewDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = document.model_dump_json(indent=2, exclude_none=True)
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_review(h5_path: Path, mkv_path: Path, taxonomy: dict) -> ReviewDocument:
    path = review_path_for(h5_path)
    with _exclusive_lock(path):
        if path.is_file():
            document = ReviewDocument.model_validate_json(path.read_text(encoding="utf-8"))
        else:
            document = _new_review(h5_path, mkv_path, taxonomy)
            _atomic_write(path, document)
    with h5py.File(h5_path, "r") as handle:
        recording_id = str(handle.attrs["recording_id"])
    if document.recording_id != recording_id:
        raise ValueError("review.json 与原始 H5 的 recording_id 不一致")
    return document


def mutate_review(
    h5_path: Path,
    mkv_path: Path,
    taxonomy: dict,
    expected_revision: int,
    mutate: Callable[[ReviewDocument], ReviewDocument],
) -> ReviewDocument:
    path = review_path_for(h5_path)
    with _exclusive_lock(path):
        current = (
            ReviewDocument.model_validate_json(path.read_text(encoding="utf-8"))
            if path.is_file()
            else _new_review(h5_path, mkv_path, taxonomy)
        )
        if current.revision != expected_revision:
            raise ReviewConflictError(
                f"review.json 已更新：期望 revision {expected_revision}，"
                f"当前为 {current.revision}"
            )
        updated = mutate(current).model_copy(update={"revision": current.revision + 1})
        _atomic_write(path, updated)
        return updated


def verify_source_artifacts(
    document: ReviewDocument, h5_path: Path, mkv_path: Path
) -> list[str]:
    actual_paths = {"capture_h5": h5_path, "video_mkv": mkv_path}
    issues: list[str] = []
    for artifact in document.sources:
        path = actual_paths[artifact.role]
        if not path.is_file():
            issues.append(f"源文件缺失：{path.name}")
            continue
        if path.stat().st_size != artifact.size_bytes:
            issues.append(f"源文件大小已变化：{path.name}")
            continue
        if sha256_file(path) != artifact.sha256:
            issues.append(f"源文件校验和已变化：{path.name}")
    return issues


def workflow_with_timestamp(workflow: ReviewWorkflow, **updates: object) -> ReviewWorkflow:
    return workflow.model_copy(
        update={**updates, "updated_at_utc": datetime.now(UTC).isoformat()}
    )
