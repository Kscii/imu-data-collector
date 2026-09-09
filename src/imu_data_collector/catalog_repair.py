"""身份 v3 本地 catalog 的只读对账与一次性可审计修复。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import h5py

from imu_data_collector.catalog import RecordingCatalog
from imu_data_collector.models import IndexReceipt, PublishTarget
from imu_data_collector.storage import ObjectStore

REPAIR_CONFIRMATION = "REPAIR LOCAL IDENTITY CATALOG"
_REMOTE_STATES = {"uploaded", "published", "verified", "legacy_published"}
_PACKET_TIMING_MARKER = "IMU packet timestamp maximum residual"


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _rows_by_id(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"catalog 备份不存在：{path}")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        recordings = {
            str(row["recording_id"]): dict(row)
            for row in connection.execute("SELECT * FROM recordings").fetchall()
        }
        jobs: dict[str, Any] = {"recording_jobs": {}, "upload_jobs": {}}
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in jobs:
            if table not in tables:
                continue
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in connection.execute(f"SELECT * FROM {table}").fetchall():
                grouped.setdefault(str(row["recording_id"]), []).append(dict(row))
            jobs[table] = grouped
    return recordings, jobs


def _read_indexed_manifest(
    store: ObjectStore, recording_id: str
) -> tuple[int, IndexReceipt] | None:
    manifest_key = f"captures/{recording_id}/manifest.json"
    receipt_key = f"index-receipts/{recording_id}.json"
    if store.stat(manifest_key) is None:
        return None
    _manifest, generation = store.read_json(manifest_key)
    try:
        payload, _receipt_generation = store.read_json(receipt_key)
    except FileNotFoundError as error:
        raise ValueError(f"manifest 已存在但索引回执缺失：{recording_id}") from error
    receipt = IndexReceipt.model_validate(payload)
    if receipt.recording_id != recording_id:
        raise ValueError(f"索引回执 recording_id 不匹配：{recording_id}")
    if receipt.manifest_generation != generation:
        raise ValueError(f"索引回执不属于当前 manifest：{recording_id}")
    return generation, receipt


def _json_list(row: dict[str, Any], key: str) -> list[str]:
    value = row.get(key, "[]")
    decoded = json.loads(value or "[]")
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise ValueError(f"历史 catalog 字段格式无效：{key}")
    return decoded


def build_catalog_repair_plan(
    *,
    data_root: Path,
    catalog: RecordingCatalog,
    backup_catalog_path: Path,
    migration_plan_path: Path,
    store: ObjectStore,
    calibration_recording_ids: set[str],
    publish_target: PublishTarget,
) -> dict[str, Any]:
    """只读生成逐录制修复计划；远端 manifest 与回执是发布状态权威。"""

    migration = json.loads(migration_plan_path.read_text(encoding="utf-8"))
    historical, jobs = _rows_by_id(backup_catalog_path)
    current = {item.recording_id: item for item in catalog.list()}
    captures: list[dict[str, Any]] = []
    counts = {
        "current_published": 0,
        "legacy_calibration_published": 0,
        "remote_missing": 0,
        "not_requested": 0,
        "quality_warning_recordings": 0,
        "blocking_validation_recordings": 0,
        "recording_jobs": 0,
        "upload_jobs": 0,
    }
    for mapping in migration["captures"]:
        old_id = str(mapping["old_recording_id"])
        new_id = str(mapping["new_recording_id"])
        old_row = historical.get(old_id)
        current_summary = current.get(new_id)
        if old_row is None:
            raise ValueError(f"迁移前 catalog 缺少录制：{old_id}")
        if current_summary is None:
            raise ValueError(f"当前 catalog 缺少迁移后录制：{new_id}")
        h5_path = Path(current_summary.h5_path or "")
        mkv_path = Path(current_summary.mkv_path or "")
        if not h5_path.is_file() or not mkv_path.is_file():
            raise ValueError(f"迁移后本地制品缺失：{new_id}")
        if not h5_path.resolve().is_relative_to(data_root.resolve()):
            raise ValueError(f"迁移后 H5 越出数据根目录：{new_id}")

        remote_id: str | None = None
        remote = _read_indexed_manifest(store, new_id)
        classification = "not_requested"
        if remote is not None:
            classification = "current_published"
            remote_id = new_id
        elif old_id in calibration_recording_ids:
            remote = _read_indexed_manifest(store, old_id)
            if remote is not None:
                classification = "legacy_calibration_published"
                remote_id = old_id
        if remote is None and str(old_row.get("upload_state")) in _REMOTE_STATES:
            classification = "remote_missing"
            remote_id = old_id

        warnings = _json_list(old_row, "quality_warnings_json")
        validation = _json_list(old_row, "validation_issues_json")
        issues = _json_list(old_row, "issues_json")
        expected_packet_findings = {
            item
            for item in (*warnings, *validation)
            if _PACKET_TIMING_MARKER in item
        }
        if expected_packet_findings:
            with h5py.File(h5_path, "r") as handle:
                fit_max_ns = int(
                    handle["imu"].attrs.get(
                        "packet_end_fit_residual_max_abs_ns", 0
                    )
                )
            if fit_max_ns > 500_000_000:
                actual_packet_findings = {
                    "IMU packet timestamp maximum residual exceeds 0.5 seconds"
                }
            elif fit_max_ns > 200_000_000:
                actual_packet_findings = {
                    "IMU packet timestamp maximum residual is "
                    f"{fit_max_ns / 1e6:.3f} ms; warning threshold is 200 ms"
                }
            else:
                actual_packet_findings = set()
            if not expected_packet_findings.issubset(actual_packet_findings):
                raise ValueError(f"原始 H5 无法复核历史包延迟结论：{new_id}")

        generation = remote[0] if remote is not None else None
        receipt = remote[1] if remote is not None else None
        recording_jobs = jobs["recording_jobs"].get(old_id, [])
        upload_jobs = jobs["upload_jobs"].get(old_id, [])
        counts[classification] += 1
        counts["quality_warning_recordings"] += int(bool(warnings))
        counts["blocking_validation_recordings"] += int(bool(validation))
        counts["recording_jobs"] += len(recording_jobs)
        counts["upload_jobs"] += len(upload_jobs)
        captures.append(
            {
                "old_recording_id": old_id,
                "new_recording_id": new_id,
                "classification": classification,
                "publication_recording_id": remote_id,
                "manifest_generation": generation,
                "index_status": receipt.status if receipt is not None else None,
                "index_message": (
                    (receipt.message or receipt.code) if receipt is not None else ""
                ),
                "historical": {
                    "data_tier": old_row.get("data_tier", "test"),
                    "state": old_row["state"],
                    "started_at_utc": old_row["started_at_utc"],
                    "ended_at_utc": old_row.get("ended_at_utc"),
                    "duration_ns": old_row.get("duration_ns"),
                    "issues_json": json.dumps(issues, ensure_ascii=False),
                    "validation_issues_json": json.dumps(validation, ensure_ascii=False),
                    "quality_warnings_json": json.dumps(warnings, ensure_ascii=False),
                    "publish_target": old_row.get("publish_target", "disabled"),
                },
                "current": {
                    "collection_id": current_summary.collection_id,
                    "h5_path": str(h5_path),
                    "mkv_path": str(mkv_path),
                },
                "recording_jobs": recording_jobs,
                "upload_jobs": upload_jobs,
            }
        )

    identity = {
        "contract": "local-catalog-identity-v3-repair",
        "source_migration_id": migration["migration_id"],
        "backup_catalog_path": str(backup_catalog_path.resolve()),
        "publish_target": publish_target.value,
        "captures": captures,
    }
    token = _canonical_sha256(identity)
    return {
        **identity,
        "plan_token": token,
        "repair_id": f"local-catalog-identity-v3-repair-{token[:16]}",
        "recording_count": len(captures),
        "counts": counts,
    }


def _copy_jobs(
    connection: sqlite3.Connection,
    table: str,
    recording_id: str,
    rows: list[dict[str, Any]],
) -> None:
    connection.execute(f"DELETE FROM {table} WHERE recording_id=?", (recording_id,))
    if not rows:
        return
    columns = [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    ]
    for source in rows:
        values = dict(source)
        values["recording_id"] = recording_id
        selected = [name for name in columns if name in values and name != "id"]
        placeholders = ",".join("?" for _ in selected)
        connection.execute(
            f"INSERT INTO {table} ({','.join(selected)}) VALUES ({placeholders})",
            [values[name] for name in selected],
        )


def apply_catalog_repair(
    *,
    catalog: RecordingCatalog,
    migration_plan_path: Path,
    plan: dict[str, Any],
    plan_token: str,
    confirmation: str,
) -> dict[str, Any]:
    if plan["plan_token"] != plan_token:
        raise ValueError("catalog 修复计划已经变化，请重新 dry-run")
    if confirmation != REPAIR_CONFIRMATION:
        raise ValueError(f"二次确认必须完整输入 {REPAIR_CONFIRMATION}")

    repair_root = migration_plan_path.parent / "catalog-repairs" / plan["repair_id"]
    repair_root.mkdir(parents=True, exist_ok=True)
    receipt_path = repair_root / "receipt.json"
    previous_receipt = (
        json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt_path.is_file()
        else None
    )
    if previous_receipt is not None and previous_receipt.get("plan_token") != plan_token:
        raise ValueError("已有 catalog 修复回执属于另一份计划")
    plan_path = repair_root / "plan.json"
    if not plan_path.exists():
        plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_path = repair_root / f"catalog-before-{stamp}.sqlite3"
    with sqlite3.connect(catalog.path) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)

    with catalog._connect() as connection:  # noqa: SLF001 - 同模块级维护事务
        connection.execute("BEGIN IMMEDIATE")
        try:
            for item in plan["captures"]:
                recording_id = item["new_recording_id"]
                row = connection.execute(
                    "SELECT * FROM recordings WHERE recording_id=?", (recording_id,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"应用期间当前录制消失：{recording_id}")
                active = connection.execute(
                    "SELECT 1 FROM recording_jobs WHERE recording_id=? "
                    "AND state IN ('queued','running','retry_wait','waiting_auth')",
                    (recording_id,),
                ).fetchone()
                if active is not None:
                    raise ValueError(f"录制仍有活动后台任务：{recording_id}")
                classification = item["classification"]
                if classification == "current_published":
                    upload_state = "uploaded"
                    target = plan["publish_target"]
                    index_state = (
                        "indexed" if item["index_status"] == "indexed" else "rejected"
                    )
                elif classification == "legacy_calibration_published":
                    upload_state = "legacy_published"
                    target = plan["publish_target"]
                    index_state = (
                        "indexed" if item["index_status"] == "indexed" else "rejected"
                    )
                elif classification == "remote_missing":
                    upload_state = "remote_missing"
                    target = item["historical"]["publish_target"]
                    index_state = "not_requested"
                else:
                    upload_state = "not_requested"
                    target = item["historical"]["publish_target"]
                    index_state = "not_requested"
                index_message = item["index_message"]
                if classification == "legacy_calibration_published":
                    index_message = "校准证据保留在不可变历史发布 ID 下"
                elif classification == "remote_missing":
                    index_message = "迁移对账未在云端找到历史 manifest；未自动重传"
                connection.execute(
                    """
                    UPDATE recordings SET participant_id='', data_tier=?, state=?,
                        started_at_utc=?, ended_at_utc=?, duration_ns=?,
                        collection_id=?, h5_path=?, mkv_path=?, issues_json=?,
                        validation_issues_json=?, quality_warnings_json=?,
                        upload_state=?, publish_target=?, index_state=?, index_message=?,
                        manifest_generation=?, publication_recording_id=?
                    WHERE recording_id=?
                    """,
                    (
                        item["historical"]["data_tier"],
                        item["historical"]["state"],
                        item["historical"]["started_at_utc"],
                        item["historical"]["ended_at_utc"],
                        item["historical"]["duration_ns"],
                        item["current"]["collection_id"],
                        item["current"]["h5_path"],
                        item["current"]["mkv_path"],
                        item["historical"]["issues_json"],
                        item["historical"]["validation_issues_json"],
                        item["historical"]["quality_warnings_json"],
                        upload_state,
                        target,
                        index_state,
                        index_message,
                        item["manifest_generation"],
                        item["publication_recording_id"],
                        recording_id,
                    ),
                )
                _copy_jobs(
                    connection,
                    "recording_jobs",
                    recording_id,
                    item["recording_jobs"],
                )
                _copy_jobs(
                    connection,
                    "upload_jobs",
                    recording_id,
                    item["upload_jobs"],
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    receipt = {
        "schema_version": "1.0.0",
        "repair_id": plan["repair_id"],
        "status": "complete",
        "plan_token": plan_token,
        "recording_count": plan["recording_count"],
        "counts": plan["counts"],
        "catalog_backup": str(backup_path),
        "completed_at_utc": datetime.now(UTC).isoformat(),
    }
    if previous_receipt is not None:
        receipt["first_completed_at_utc"] = previous_receipt.get(
            "first_completed_at_utc", previous_receipt.get("completed_at_utc")
        )
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt
