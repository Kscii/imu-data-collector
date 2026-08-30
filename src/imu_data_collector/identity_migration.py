"""参与者身份 v3 的可审计、可恢复存量迁移。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import h5py

from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    ArtifactDescriptor,
    CaptureManifestV2,
    ParticipantAssignment,
    ReviewDocument,
    ReviewWorkflowState,
)
from imu_data_collector.storage import ObjectConflictError, ObjectInfo, ObjectStore

RECORDING_ID_RE = re.compile(
    r"^(?P<timestamp>\d{8}T\d{6}\.\d{6}Z)(?:_(?P<participant>[a-z][a-z0-9]{2,31}))?$"
)
CONFIRMATION = "MIGRATE PARTICIPANT IDENTITY V3"


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _object_dict(info: ObjectInfo) -> dict[str, Any]:
    return {
        "key": info.key,
        "size_bytes": info.size_bytes,
        "generation": info.generation,
        "content_type": info.content_type,
        "metadata": dict(sorted(info.metadata.items())),
    }


def _new_recording_id(recording_id: str) -> str:
    match = RECORDING_ID_RE.fullmatch(recording_id)
    if match is None:
        raise ValueError(f"录制 ID 不能无损转换为纯 UTC 时间戳：{recording_id}")
    return match.group("timestamp")


def _collection_mapping(
    manifests: list[CaptureManifestV2],
) -> dict[tuple[str, str], str]:
    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    for manifest in manifests:
        day = _new_recording_id(manifest.recording_id)[:8]
        first = grouped[day].get(manifest.collection_id)
        if first is None or manifest.captured_at_utc < first:
            grouped[day][manifest.collection_id] = manifest.captured_at_utc
    output: dict[tuple[str, str], str] = {}
    for day, collections in sorted(grouped.items()):
        ordered = sorted(collections, key=lambda item: (collections[item], item))
        if len(ordered) > 99:
            raise ValueError(f"{day} 的采集场次超过 99 个")
        for ordinal, old_id in enumerate(ordered, start=1):
            output[(day, old_id)] = f"{day}_session_{ordinal:02d}"
    return output


def _dataset_contract(path: Path) -> dict[str, tuple[str, tuple[int, ...], str]]:
    output: dict[str, tuple[str, tuple[int, ...], str]] = {}
    with h5py.File(path, "r") as handle:
        def visit(name: str, item: h5py.Dataset | h5py.Group) -> None:
            if not isinstance(item, h5py.Dataset):
                return
            digest = hashlib.sha256()
            if item.size == 0:
                # h5py rejects iter_chunks() for chunked datasets whose current
                # extent is zero.  Their logical content is nevertheless the
                # well-defined empty byte sequence; dtype and shape are checked
                # separately in the contract tuple below.
                pass
            elif item.shape:
                blocks = (
                    item.iter_chunks()
                    if item.chunks
                    else (tuple(slice(None) for _ in item.shape),)
                )
                for block in blocks:
                    digest.update(item[block].tobytes())
            else:
                digest.update(item[()].tobytes())
            output[name] = (item.dtype.str, tuple(item.shape), digest.hexdigest())

        handle.visititems(visit)
    return output


def neutralize_capture_h5(
    source: Path,
    destination: Path,
    *,
    recording_id: str,
    collection_id: str,
) -> None:
    """只改身份覆盖层；逐数据集验证数值内容完全不变。"""

    before = _dataset_contract(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    with h5py.File(destination, "r+") as handle:
        handle.attrs["recording_id"] = recording_id
        handle.attrs["collection_id"] = collection_id
        if "participant_id" in handle.attrs:
            del handle.attrs["participant_id"]
        handle.attrs["identity_contract_version"] = "2.0.0"
        handle.attrs["participant_assignment_source"] = "review"
        if "video" in handle and "path" in handle["video"].attrs:
            handle["video"].attrs["path"] = f"{recording_id}.mkv"
        handle.flush()
    if _dataset_contract(destination) != before:
        destination.unlink(missing_ok=True)
        raise ValueError("身份迁移意外改变了 H5 数据集内容")


class CloudIdentityMigration:
    def __init__(self, store: ObjectStore, calibration_recording_ids: set[str]) -> None:
        self.store = store
        self.calibration_recording_ids = calibration_recording_ids

    def build_plan(self) -> dict[str, Any]:
        manifests: list[tuple[CaptureManifestV2, int]] = []
        for info in self.store.list("captures/"):
            if not info.key.endswith("/manifest.json"):
                continue
            payload, generation = self.store.read_json(info.key)
            manifest = CaptureManifestV2.model_validate(payload)
            if manifest.recording_id in self.calibration_recording_ids:
                continue
            if manifest.schema_version == "3.0.0":
                continue
            manifests.append((manifest, generation))
        manifests.sort(key=lambda item: item[0].recording_id)
        if not manifests:
            raise ValueError("没有可迁移的普通录制")
        new_ids = [_new_recording_id(item.recording_id) for item, _ in manifests]
        if len(set(new_ids)) != len(new_ids):
            raise ValueError("移除 UniKey 后 recording_id 发生冲突")
        collections = _collection_mapping([item for item, _ in manifests])
        captures = [
            {
                "old_recording_id": manifest.recording_id,
                "new_recording_id": _new_recording_id(manifest.recording_id),
                "old_collection_id": manifest.collection_id,
                "new_collection_id": collections[
                    (_new_recording_id(manifest.recording_id)[:8], manifest.collection_id)
                ],
                "manifest_generation": generation,
            }
            for manifest, generation in manifests
        ]
        archive_keys: set[str] = set()
        for item in captures:
            old_id = item["old_recording_id"]
            for prefix in (
                f"captures/{old_id}/",
                f"reviews/{old_id}/",
                f"exports/{old_id}/",
            ):
                archive_keys.update(info.key for info in self.store.list(prefix))
            receipt = f"index-receipts/{old_id}.json"
            if self.store.stat(receipt) is not None:
                archive_keys.add(receipt)
        for prefix in ("training-snapshots/", "benchmark-datasets/team/"):
            archive_keys.update(info.key for info in self.store.list(prefix))
        inventory = []
        for key in sorted(archive_keys):
            info = self.store.stat(key)
            if info is None:
                raise ValueError(f"计划期间对象消失：{key}")
            inventory.append(_object_dict(info))
        identity = {
            "contract": "participant-identity-v3",
            "captures": captures,
            "inventory": inventory,
        }
        token = _canonical_sha256(identity)
        return {
            **identity,
            "plan_token": token,
            "migration_id": f"participant-identity-v3-{token[:16]}",
            "recording_count": len(captures),
            "archive_object_count": len(inventory),
            "archive_size_bytes": sum(item["size_bytes"] for item in inventory),
        }

    @staticmethod
    def _plan_key(migration_id: str) -> str:
        return f"identity-migrations/{migration_id}/plan.json"

    @staticmethod
    def _receipt_key(migration_id: str) -> str:
        return f"identity-migrations/{migration_id}/receipt.json"

    @staticmethod
    def _archive_key(migration_id: str, source_key: str) -> str:
        return f"identity-migrations/{migration_id}/archive/{source_key}"

    def _copy_verified(
        self,
        source: dict[str, Any],
        destination_key: str,
        *,
        fallback_migration_id: str | None = None,
    ) -> ObjectInfo:
        existing = self.store.stat(destination_key)
        if existing is not None:
            if (
                existing.size_bytes != source["size_bytes"]
                or existing.metadata != source["metadata"]
            ):
                raise ValueError(f"已有目标对象与源不一致：{destination_key}")
            return existing
        source_key = source["key"]
        source_generation = source["generation"]
        if self.store.stat(source_key) is None and fallback_migration_id is not None:
            source_key = self._archive_key(fallback_migration_id, source_key)
            archived = self.store.stat(source_key)
            if archived is None:
                raise ValueError(f"源对象及归档均缺失：{source['key']}")
            source_generation = archived.generation
        copied = self.store.copy(
            source_key,
            destination_key,
            if_source_generation_match=source_generation,
        )
        if copied.size_bytes != source["size_bytes"] or copied.metadata != source["metadata"]:
            raise ValueError(f"归档副本校验失败：{destination_key}")
        return copied

    def _write_json_idempotent(self, key: str, payload: dict[str, Any]) -> ObjectInfo:
        try:
            existing, generation = self.store.read_json(key)
        except FileNotFoundError:
            return self.store.write_json(key, payload, if_generation_match=0)
        if existing != payload:
            raise ValueError(f"已有对象内容不一致：{key}")
        info = self.store.stat(key)
        if info is None or info.generation != generation:
            raise ValueError(f"已有对象 generation 不稳定：{key}")
        return info

    def _read_source_json(
        self,
        migration_id: str,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            payload, generation = self.store.read_json(source["key"])
            if generation != source["generation"]:
                raise ObjectConflictError(f"源 JSON 已更新：{source['key']}")
            return payload
        except FileNotFoundError:
            payload, _ = self.store.read_json(
                self._archive_key(migration_id, source["key"])
            )
            return payload

    def apply(self, plan_token: str, confirmation: str) -> dict[str, Any]:
        if confirmation != CONFIRMATION:
            raise ValueError(f"二次确认必须完整输入 {CONFIRMATION}")
        migration_id = f"participant-identity-v3-{plan_token[:16]}"
        try:
            plan, _ = self.store.read_json(self._plan_key(migration_id))
        except FileNotFoundError:
            plan = self.build_plan()
            if plan["plan_token"] != plan_token:
                raise ValueError("计划已经变化，请重新 dry-run") from None
            self.store.write_json(
                self._plan_key(migration_id), plan, if_generation_match=0
            )
        if plan.get("plan_token") != plan_token:
            raise ValueError("持久化计划 token 不匹配")
        try:
            receipt, _ = self.store.read_json(self._receipt_key(migration_id))
            if receipt.get("status") == "complete":
                return receipt
        except FileNotFoundError:
            pass

        inventory = {item["key"]: item for item in plan["inventory"]}
        for source in plan["inventory"]:
            self._copy_verified(source, self._archive_key(migration_id, source["key"]))

        new_objects: dict[str, dict[str, Any]] = {}
        review_baselines: dict[str, int] = {}
        with tempfile.TemporaryDirectory(prefix="imu-identity-v3-") as temporary:
            work = Path(temporary)
            for item in plan["captures"]:
                old_id = item["old_recording_id"]
                new_id = item["new_recording_id"]
                manifest_key = f"captures/{old_id}/manifest.json"
                manifest_payload = self._read_source_json(
                    migration_id, inventory[manifest_key]
                )
                manifest = CaptureManifestV2.model_validate(manifest_payload)
                by_role = {artifact.role: artifact for artifact in manifest.artifacts}

                source_h5 = work / f"{old_id}.source.h5"
                neutral_h5 = work / f"{new_id}.h5"
                h5_source_key = by_role["capture_h5"].object_key
                if self.store.stat(h5_source_key) is None:
                    h5_source_key = self._archive_key(migration_id, h5_source_key)
                self.store.download_file(h5_source_key, source_h5)
                if sha256_file(source_h5) != by_role["capture_h5"].sha256:
                    raise ValueError(f"源 H5 SHA-256 不匹配：{old_id}")
                neutralize_capture_h5(
                    source_h5,
                    neutral_h5,
                    recording_id=new_id,
                    collection_id=item["new_collection_id"],
                )
                h5_sha = sha256_file(neutral_h5)
                h5_key = f"captures/{new_id}/{new_id}.h5"
                try:
                    h5_info = self.store.put_file(
                        neutral_h5,
                        h5_key,
                        content_type="application/x-hdf5",
                        metadata={"sha256": h5_sha},
                    )
                except ObjectConflictError:
                    h5_info = self.store.stat(h5_key)
                    if (
                        h5_info is None
                        or h5_info.size_bytes != neutral_h5.stat().st_size
                        or h5_info.metadata.get("sha256") != h5_sha
                    ):
                        raise ValueError(
                            f"已有中立 H5 内容不一致：{new_id}"
                        ) from None

                descriptors = [
                    ArtifactDescriptor(
                        role="capture_h5",
                        object_key=h5_key,
                        filename=f"{new_id}.h5",
                        size_bytes=h5_info.size_bytes,
                        sha256=h5_sha,
                        content_type="application/x-hdf5",
                    )
                ]
                for role, filename in (
                    ("video_mkv", f"{new_id}.mkv"),
                    ("preview_mp4", "preview.mp4"),
                ):
                    source_artifact = by_role[role]
                    destination_key = f"captures/{new_id}/{filename}"
                    copied = self._copy_verified(
                        inventory[source_artifact.object_key],
                        destination_key,
                        fallback_migration_id=migration_id,
                    )
                    descriptors.append(
                        ArtifactDescriptor(
                            role=role,
                            object_key=destination_key,
                            filename=filename,
                            size_bytes=copied.size_bytes,
                            sha256=source_artifact.sha256,
                            content_type=source_artifact.content_type,
                        )
                    )
                migrated_manifest = manifest.model_copy(
                    update={
                        "schema_version": "3.0.0",
                        "recording_id": new_id,
                        "collection_id": item["new_collection_id"],
                        "participant_id": None,
                        "identity_mode": "annotation_required",
                        "artifacts": descriptors,
                    }
                )

                old_review_key = f"reviews/{old_id}/review.json"
                if old_review_key in inventory:
                    review_payload = self._read_source_json(
                        migration_id, inventory[old_review_key]
                    )
                    review = ReviewDocument.model_validate(review_payload)
                    workflow = review.workflow
                    if workflow.state == ReviewWorkflowState.COMPLETED:
                        workflow = workflow.model_copy(
                            update={"state": ReviewWorkflowState.IN_PROGRESS}
                        )
                    sources = []
                    for source in review.sources:
                        if source.role == "capture_h5":
                            sources.append(
                                source.model_copy(
                                    update={
                                        "filename": f"{new_id}.h5",
                                        "size_bytes": h5_info.size_bytes,
                                        "sha256": h5_sha,
                                    }
                                )
                            )
                        else:
                            sources.append(
                                source.model_copy(update={"filename": f"{new_id}.mkv"})
                            )
                    migrated_review = review.model_copy(
                        update={
                            "schema_version": "3.0.0",
                            "recording_id": new_id,
                            "revision": review.revision + 1,
                            "sources": sources,
                            "workflow": workflow,
                            "participant_assignment": ParticipantAssignment(),
                            "active_export": None,
                        }
                    )
                    review_info = self._write_json_idempotent(
                        f"reviews/{new_id}/review.json",
                        migrated_review.model_dump(mode="json"),
                    )
                    review_baselines[new_id] = review_info.generation

                manifest_info = self._write_json_idempotent(
                    f"captures/{new_id}/manifest.json",
                    migrated_manifest.model_dump(mode="json", exclude_none=True),
                )
                for info in (h5_info, manifest_info):
                    new_objects[info.key] = _object_dict(info)
                for artifact in descriptors[1:]:
                    info = self.store.stat(artifact.object_key)
                    if info is not None:
                        new_objects[info.key] = _object_dict(info)
                if new_id in review_baselines:
                    info = self.store.stat(f"reviews/{new_id}/review.json")
                    if info is not None:
                        new_objects[info.key] = _object_dict(info)

        mapping_payload = {
            "schema_version": "1.0.0",
            "migration_id": migration_id,
            "recordings": {
                item["old_recording_id"]: item["new_recording_id"]
                for item in plan["captures"]
            },
            "collections": {
                f"{item['new_recording_id'][:8]}:{item['old_collection_id']}": (
                    item["new_collection_id"]
                )
                for item in plan["captures"]
            },
        }
        mapping_info = self._write_json_idempotent(
            f"identity-migrations/{migration_id}/mapping.json", mapping_payload
        )
        new_objects[mapping_info.key] = _object_dict(mapping_info)

        # 新合同对象全部就绪后，才用原 generation 删除旧活动命名空间。
        for source in plan["inventory"]:
            if source["key"].startswith("identity-migrations/"):
                continue
            self.store.delete(
                source["key"], if_generation_match=source["generation"]
            )

        receipt = {
            "schema_version": "1.0.0",
            "migration_id": migration_id,
            "status": "complete",
            "plan_token": plan_token,
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "recording_count": len(plan["captures"]),
            "new_objects": list(new_objects.values()),
            "review_generation_baselines": review_baselines,
            "rollback_allowed_until_review_changes": True,
        }
        self._write_json_idempotent(self._receipt_key(migration_id), receipt)
        return receipt

    def rollback(self, migration_id: str, confirmation: str) -> dict[str, Any]:
        expected = f"ROLLBACK {migration_id}"
        if confirmation != expected:
            raise ValueError(f"二次确认必须完整输入 {expected}")
        plan, _ = self.store.read_json(self._plan_key(migration_id))
        receipt, generation = self.store.read_json(self._receipt_key(migration_id))
        if receipt.get("status") != "complete":
            raise ValueError("只有完成的迁移可以回滚")
        for recording_id, baseline in receipt["review_generation_baselines"].items():
            info = self.store.stat(f"reviews/{recording_id}/review.json")
            if info is None or info.generation != baseline:
                raise ValueError("新标注已经发生，禁止自动回滚")
        for item in receipt["new_objects"]:
            self.store.delete(item["key"], if_generation_match=item["generation"])
        for source in plan["inventory"]:
            archive_key = self._archive_key(migration_id, source["key"])
            existing = self.store.stat(source["key"])
            if existing is None:
                archived = self.store.stat(archive_key)
                if archived is None:
                    raise ValueError(f"归档对象缺失：{archive_key}")
                restored = self.store.copy(
                    archive_key,
                    source["key"],
                    if_source_generation_match=archived.generation,
                )
                if restored.size_bytes != source["size_bytes"]:
                    raise ValueError(f"恢复对象大小不一致：{source['key']}")
        rolled_back = {
            **receipt,
            "status": "rolled_back",
            "rolled_back_at_utc": datetime.now(UTC).isoformat(),
        }
        self.store.write_json(
            self._receipt_key(migration_id),
            rolled_back,
            if_generation_match=generation,
        )
        return rolled_back


def build_local_plan(data_root: Path) -> dict[str, Any]:
    captures: list[dict[str, Any]] = []
    manifests: list[CaptureManifestV2] = []
    for h5_path in sorted(data_root.rglob("*.h5")):
        relative = h5_path.relative_to(data_root)
        if any(part.startswith("_") for part in relative.parts):
            continue
        if h5_path.name.endswith((".partial.h5", ".annotating.h5", ".syncing.h5")):
            continue
        try:
            handle = h5py.File(h5_path, "r")
        except OSError:
            continue
        with handle:
            recording_id = str(handle.attrs.get("recording_id", ""))
            collection_id = str(handle.attrs.get("collection_id", ""))
            match = RECORDING_ID_RE.fullmatch(recording_id)
            if match is None:
                continue
            if (
                match.group("participant") is None
                and "participant_id" not in handle.attrs
            ):
                continue
            if str(handle.attrs.get("recording_kind", "capture")) != "capture":
                continue
            captured = str(handle.attrs.get("started_at_utc", recording_id))
        manifests.append(
            CaptureManifestV2.model_construct(
                recording_id=recording_id,
                collection_id=collection_id,
                captured_at_utc=captured,
            )
        )
        directory = h5_path.parent
        captures.append(
            {
                "old_recording_id": recording_id,
                "new_recording_id": _new_recording_id(recording_id),
                "old_collection_id": collection_id,
                "source_directory": str(relative.parent),
                "files": [
                    {
                        "path": path.name,
                        "size_bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                    for path in sorted(directory.iterdir())
                    if path.is_file()
                ],
            }
        )
    if not captures:
        raise ValueError("本地没有可迁移的普通录制")
    collections = _collection_mapping(manifests)
    for item in captures:
        item["new_collection_id"] = collections[
            (item["new_recording_id"][:8], item["old_collection_id"])
        ]
    new_ids = [item["new_recording_id"] for item in captures]
    if len(set(new_ids)) != len(new_ids):
        raise ValueError("本地移除 UniKey 后 recording_id 发生冲突")
    identity = {"contract": "participant-identity-v3-local", "captures": captures}
    token = _canonical_sha256(identity)
    return {
        **identity,
        "plan_token": token,
        "migration_id": f"participant-identity-v3-local-{token[:16]}",
        "recording_count": len(captures),
    }


def apply_local_plan(
    data_root: Path,
    plan: dict[str, Any],
    *,
    plan_token: str,
    confirmation: str,
) -> dict[str, Any]:
    if plan["plan_token"] != plan_token:
        raise ValueError("本地计划已经变化，请重新 dry-run")
    if confirmation != CONFIRMATION:
        raise ValueError(f"二次确认必须完整输入 {CONFIRMATION}")
    migration_root = data_root / "_identity_migrations" / plan["migration_id"]
    archive_root = migration_root / "archive"
    plan_path = migration_root / "plan.json"
    receipt_path = migration_root / "receipt.json"
    migration_root.mkdir(parents=True, exist_ok=True)
    if not plan_path.exists():
        plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if receipt_path.is_file():
        return json.loads(receipt_path.read_text(encoding="utf-8"))
    for item in plan["captures"]:
        source = (data_root / item["source_directory"]).resolve()
        if not source.is_relative_to(data_root.resolve()):
            raise ValueError("本地源目录越出数据根目录")
        archived = archive_root / item["source_directory"]
        if source.exists() and not archived.exists():
            archived.parent.mkdir(parents=True, exist_ok=True)
            source.replace(archived)
        if not archived.is_dir():
            raise ValueError(f"本地归档目录缺失：{archived}")
        target = (
            data_root
            / item["new_collection_id"]
            / item["new_recording_id"]
        )
        if not target.exists():
            target.mkdir(parents=True)
            for old_file in item["files"]:
                source_file = archived / old_file["path"]
                name = old_file["path"].replace(
                    item["old_recording_id"], item["new_recording_id"]
                )
                destination = target / name
                if source_file.suffix == ".h5":
                    neutralize_capture_h5(
                        source_file,
                        destination,
                        recording_id=item["new_recording_id"],
                        collection_id=item["new_collection_id"],
                    )
                else:
                    shutil.copy2(source_file, destination)
    receipt = {
        "schema_version": "1.0.0",
        "migration_id": plan["migration_id"],
        "status": "complete",
        "plan_token": plan_token,
        "recording_count": len(plan["captures"]),
        "completed_at_utc": datetime.now(UTC).isoformat(),
    }
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def auto_migrate_local_identity(data_root: Path) -> dict[str, Any] | None:
    """桌面升级时幂等迁移；无旧命名录制时不产生任何写入。"""

    migration_parent = data_root / "_identity_migrations"
    for plan_path in sorted(migration_parent.glob("*/plan.json")):
        receipt_path = plan_path.with_name("receipt.json")
        if receipt_path.is_file():
            continue
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        receipt = apply_local_plan(
            data_root,
            plan,
            plan_token=plan["plan_token"],
            confirmation=CONFIRMATION,
        )
        return {"plan": plan, "receipt": receipt}
    try:
        plan = build_local_plan(data_root)
    except ValueError as error:
        if str(error) == "本地没有可迁移的普通录制":
            return None
        raise
    receipt = apply_local_plan(
        data_root,
        plan,
        plan_token=plan["plan_token"],
        confirmation=CONFIRMATION,
    )
    return {"plan": plan, "receipt": receipt}
