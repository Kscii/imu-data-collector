"""本地目录重建、不完整文件隔离与受限硬删除。"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import h5py

from imu_data_collector.catalog import RecordingCatalog
from imu_data_collector.models import RecordingState, RecordingSummary

INCOMPLETE_SUFFIXES = (
    ".partial.h5",
    ".partial.mkv",
    ".normalizing.mkv",
    ".annotating.h5",
    ".syncing.h5",
    ".partial",
)


def scan_incomplete_files(data_root: Path) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    quarantine = (data_root / "_quarantine").resolve()
    for path in sorted(data_root.rglob("*")):
        if not path.is_file() or path.resolve().is_relative_to(quarantine):
            continue
        reason = next(
            (suffix for suffix in INCOMPLETE_SUFFIXES if path.name.endswith(suffix)),
            None,
        )
        if reason is None and path.suffix in {".h5", ".mkv", ".tar"}:
            try:
                empty = path.stat().st_size == 0
            except OSError:
                empty = True
            reason = "empty" if empty else None
        if reason:
            output.append(
                {
                    "path": str(path),
                    "relative_path": str(path.relative_to(data_root)),
                    "size_bytes": path.stat().st_size,
                    "reason": reason,
                }
            )
    return output


def quarantine_incomplete(data_root: Path, relative_path: str) -> Path:
    source = (data_root / relative_path).resolve()
    root = data_root.resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise ValueError("待隔离文件不存在或越出数据根目录")
    candidates = {Path(item["path"]).resolve() for item in scan_incomplete_files(data_root)}
    if source not in candidates:
        raise ValueError("该文件未被当前扫描标记为不完整")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    target = data_root / "_quarantine" / stamp / source.relative_to(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    source.replace(target)
    return target


def rebuild_catalog(data_root: Path, catalog: RecordingCatalog) -> dict[str, int]:
    imported = 0
    skipped = 0
    for h5_path in sorted(data_root.rglob("*.h5")):
        if any(part.startswith("_") for part in h5_path.relative_to(data_root).parts):
            continue
        if h5_path.name.endswith((".partial.h5", ".annotating.h5", ".syncing.h5")):
            continue
        try:
            with h5py.File(h5_path, "r") as handle:
                if "recording_id" not in handle.attrs or "collection_id" not in handle.attrs:
                    skipped += 1
                    continue
                recording_id = str(handle.attrs["recording_id"])
                mkv_path = h5_path.with_name(f"{recording_id}.mkv")
                issues: list[str] = []
                if not mkv_path.is_file():
                    issues.append("video file is missing")
                summary = RecordingSummary(
                    recording_id=recording_id,
                    collection_id=str(handle.attrs["collection_id"]),
                    participant_id=(
                        str(handle.attrs["participant_id"])
                        if "participant_id" in handle.attrs
                        else None
                    ),
                    # 旧本地文件缺少用途字段时只允许回收到安全的 test 档，
                    # 绝不因目录重建而获得训练资格。
                    data_tier=str(handle.attrs.get("data_tier", "test")),
                    state=(
                        RecordingState.READY
                        if not issues
                        else RecordingState.NEEDS_ATTENTION
                    ),
                    started_at_utc=str(handle.attrs.get("started_at_utc", "")),
                    ended_at_utc=str(handle.attrs.get("ended_at_utc", "")) or None,
                    duration_ns=int(handle.attrs.get("duration_ns", 0)) or None,
                    h5_path=str(h5_path),
                    mkv_path=str(mkv_path),
                    issues=issues,
                )
            catalog.upsert(summary)
            imported += 1
        except (OSError, KeyError, TypeError, ValueError):
            skipped += 1
    return {"imported": imported, "skipped": skipped}


def hard_delete_recording(
    data_root: Path,
    summary: RecordingSummary,
    confirmation: str,
) -> Path:
    if confirmation != summary.recording_id:
        raise ValueError("确认文本必须与 recording_id 完全一致")
    if summary.state in {
        RecordingState.ARMING,
        RecordingState.RECORDING,
        RecordingState.FINALIZING,
    }:
        raise ValueError("当前录制仍在使用中，不能删除")
    if summary.upload_state == "verified":
        raise ValueError("已发布的源录制不能在本地逐条删除")
    h5_path = Path(summary.h5_path or "").resolve()
    root = data_root.resolve()
    directory = h5_path.parent
    if not directory.is_relative_to(root) or directory == root:
        raise ValueError("录制目录越出数据根目录")
    if directory.name != summary.recording_id:
        raise ValueError("录制目录名与 recording_id 不一致，拒绝递归删除")
    shutil.rmtree(directory)
    return directory
