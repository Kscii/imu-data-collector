"""轻量本地索引；可迁移的录制数据仍以 H5 与 MKV 为准。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from imu_data_collector.models import (
    BackgroundJobKind,
    BackgroundJobState,
    BackgroundJobStatus,
    RecordingState,
    RecordingSummary,
)

LEGACY_PACKET_RESIDUAL_ISSUE = (
    "IMU packet timestamp maximum residual exceeds 0.2 seconds"
)


class RecordingCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS recordings (
                    recording_id TEXT PRIMARY KEY,
                    collection_id TEXT NOT NULL,
                    participant_id TEXT NOT NULL,
                    data_tier TEXT NOT NULL DEFAULT 'test',
                    state TEXT NOT NULL,
                    started_at_utc TEXT NOT NULL,
                    ended_at_utc TEXT,
                    duration_ns INTEGER,
                    h5_path TEXT,
                    mkv_path TEXT,
                    issues_json TEXT NOT NULL DEFAULT '[]',
                    validation_issues_json TEXT NOT NULL DEFAULT '[]',
                    quality_warnings_json TEXT NOT NULL DEFAULT '[]',
                    upload_state TEXT NOT NULL DEFAULT 'not_requested',
                    index_state TEXT NOT NULL DEFAULT 'not_requested',
                    index_message TEXT NOT NULL DEFAULT '',
                    manifest_generation INTEGER
                );
                CREATE INDEX IF NOT EXISTS recordings_collection_idx
                    ON recordings(collection_id, started_at_utc);
                CREATE TABLE IF NOT EXISTS upload_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recording_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    UNIQUE(recording_id, generation)
                );
                CREATE TABLE IF NOT EXISTS recording_jobs (
                    recording_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 4,
                    next_attempt_at_utc TEXT,
                    last_error TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY(recording_id, kind),
                    FOREIGN KEY(recording_id) REFERENCES recordings(recording_id)
                );
                CREATE INDEX IF NOT EXISTS recording_jobs_ready_idx
                    ON recording_jobs(state, next_attempt_at_utc, updated_at_utc);
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(recordings)").fetchall()
            }
            if "data_tier" not in columns:
                connection.execute(
                    "ALTER TABLE recordings ADD COLUMN data_tier "
                    "TEXT NOT NULL DEFAULT 'test'"
                )
            for name, definition in (
                ("index_state", "TEXT NOT NULL DEFAULT 'not_requested'"),
                ("index_message", "TEXT NOT NULL DEFAULT ''"),
                ("manifest_generation", "INTEGER"),
                ("validation_issues_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("quality_warnings_json", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE recordings ADD COLUMN {name} {definition}"
                    )
            # 旧库把运行故障和验证结论混在 issues_json。这里只迁移本次策略
            # 明确重分类的旧文本，其余未知问题全部按运行故障保留。
            for row in connection.execute(
                "SELECT recording_id, issues_json, validation_issues_json FROM recordings"
            ).fetchall():
                operational = json.loads(row["issues_json"])
                validation = json.loads(row["validation_issues_json"])
                if LEGACY_PACKET_RESIDUAL_ISSUE not in operational:
                    continue
                operational = [
                    item for item in operational if item != LEGACY_PACKET_RESIDUAL_ISSUE
                ]
                if LEGACY_PACKET_RESIDUAL_ISSUE not in validation:
                    validation.append(LEGACY_PACKET_RESIDUAL_ISSUE)
                connection.execute(
                    "UPDATE recordings SET issues_json = ?, validation_issues_json = ? "
                    "WHERE recording_id = ?",
                    (
                        json.dumps(operational, ensure_ascii=False),
                        json.dumps(validation, ensure_ascii=False),
                        row["recording_id"],
                    ),
                )

    def upsert(self, summary: RecordingSummary) -> None:
        payload = summary.model_dump(mode="json")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO recordings (
                    recording_id, collection_id, participant_id, data_tier, state,
                    started_at_utc, ended_at_utc, duration_ns, h5_path, mkv_path,
                    issues_json, validation_issues_json, quality_warnings_json,
                    upload_state, index_state, index_message, manifest_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recording_id) DO UPDATE SET
                    collection_id=excluded.collection_id,
                    participant_id=excluded.participant_id,
                    data_tier=excluded.data_tier,
                    state=excluded.state,
                    started_at_utc=excluded.started_at_utc,
                    ended_at_utc=excluded.ended_at_utc,
                    duration_ns=excluded.duration_ns,
                    h5_path=excluded.h5_path,
                    mkv_path=excluded.mkv_path,
                    issues_json=excluded.issues_json,
                    validation_issues_json=excluded.validation_issues_json,
                    quality_warnings_json=excluded.quality_warnings_json,
                    upload_state=excluded.upload_state,
                    index_state=excluded.index_state,
                    index_message=excluded.index_message,
                    manifest_generation=excluded.manifest_generation
                """,
                (
                    payload["recording_id"],
                    payload["collection_id"],
                    payload["participant_id"],
                    payload["data_tier"],
                    payload["state"],
                    payload["started_at_utc"],
                    payload["ended_at_utc"],
                    payload["duration_ns"],
                    payload["h5_path"],
                    payload["mkv_path"],
                    json.dumps(payload["issues"], ensure_ascii=False),
                    json.dumps(payload["validation_issues"], ensure_ascii=False),
                    json.dumps(payload["quality_warnings"], ensure_ascii=False),
                    payload["upload_state"],
                    payload["index_state"],
                    payload["index_message"],
                    payload["manifest_generation"],
                ),
            )

    def get(self, recording_id: str) -> RecordingSummary | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE recording_id = ?", (recording_id,)
            ).fetchone()
            jobs = self._jobs_for_recordings(connection, [recording_id])
        return self._from_row(row, jobs.get(recording_id, {})) if row else None

    def list(self) -> list[RecordingSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM recordings ORDER BY started_at_utc DESC"
            ).fetchall()
            jobs = self._jobs_for_recordings(
                connection, [str(row["recording_id"]) for row in rows]
            )
        return [
            self._from_row(row, jobs.get(str(row["recording_id"]), {}))
            for row in rows
        ]

    @staticmethod
    def _jobs_for_recordings(
        connection: sqlite3.Connection, recording_ids: list[str]
    ) -> dict[str, dict[str, BackgroundJobStatus]]:
        if not recording_ids:
            return {}
        placeholders = ",".join("?" for _ in recording_ids)
        rows = connection.execute(
            f"SELECT * FROM recording_jobs WHERE recording_id IN ({placeholders})",
            recording_ids,
        ).fetchall()
        result: dict[str, dict[str, BackgroundJobStatus]] = {}
        for row in rows:
            result.setdefault(str(row["recording_id"]), {})[str(row["kind"])] = (
                RecordingCatalog._job_status(row)
            )
        return result

    @staticmethod
    def _job_status(row: sqlite3.Row) -> BackgroundJobStatus:
        return BackgroundJobStatus(
            kind=BackgroundJobKind(row["kind"]),
            state=BackgroundJobState(row["state"]),
            phase=row["phase"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            next_attempt_at_utc=row["next_attempt_at_utc"],
            last_error=row["last_error"],
            created_at_utc=row["created_at_utc"],
            updated_at_utc=row["updated_at_utc"],
        )

    def get_job(
        self, recording_id: str, kind: BackgroundJobKind
    ) -> tuple[BackgroundJobStatus, dict[str, Any]] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recording_jobs WHERE recording_id = ? AND kind = ?",
                (recording_id, kind.value),
            ).fetchone()
        if row is None:
            return None
        return self._job_status(row), json.loads(row["payload_json"])

    def enqueue_job(
        self,
        recording_id: str,
        kind: BackgroundJobKind,
        payload: dict[str, Any] | None = None,
        *,
        max_attempts: int = 4,
        reset: bool = False,
    ) -> BackgroundJobStatus:
        """幂等入队；人工重试通过 reset 明确重置自动重试额度。"""

        now = datetime.now(UTC).isoformat()
        encoded = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM recording_jobs WHERE recording_id = ? AND kind = ?",
                (recording_id, kind.value),
            ).fetchone()
            if existing is not None and not reset:
                state = BackgroundJobState(existing["state"])
                if state in {
                    BackgroundJobState.QUEUED,
                    BackgroundJobState.RUNNING,
                    BackgroundJobState.RETRY_WAIT,
                    BackgroundJobState.SUCCEEDED,
                }:
                    return self._job_status(existing)
            connection.execute(
                """
                INSERT INTO recording_jobs (
                    recording_id, kind, state, phase, attempts, max_attempts,
                    next_attempt_at_utc, last_error, payload_json,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, 'queued', 'queued', 0, ?, NULL, NULL, ?, ?, ?)
                ON CONFLICT(recording_id, kind) DO UPDATE SET
                    state='queued', phase='queued', attempts=0,
                    max_attempts=excluded.max_attempts,
                    next_attempt_at_utc=NULL, last_error=NULL,
                    payload_json=excluded.payload_json,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (recording_id, kind.value, max_attempts, encoded, now, now),
            )
            row = connection.execute(
                "SELECT * FROM recording_jobs WHERE recording_id = ? AND kind = ?",
                (recording_id, kind.value),
            ).fetchone()
        assert row is not None
        return self._job_status(row)

    def claim_next_job(
        self,
    ) -> tuple[str, BackgroundJobStatus, dict[str, Any]] | None:
        """原子领取一个可运行任务；收尾优先于发布。"""

        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM recording_jobs
                WHERE state = 'queued'
                   OR (state = 'retry_wait' AND next_attempt_at_utc <= ?)
                ORDER BY CASE kind WHEN 'finalize' THEN 0 ELSE 1 END,
                         updated_at_utc, recording_id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE recording_jobs
                SET state='running', phase='starting', attempts=attempts + 1,
                    next_attempt_at_utc=NULL, updated_at_utc=?
                WHERE recording_id=? AND kind=?
                """,
                (now, row["recording_id"], row["kind"]),
            )
            claimed = connection.execute(
                "SELECT * FROM recording_jobs WHERE recording_id=? AND kind=?",
                (row["recording_id"], row["kind"]),
            ).fetchone()
            connection.commit()
        assert claimed is not None
        return (
            str(claimed["recording_id"]),
            self._job_status(claimed),
            json.loads(claimed["payload_json"]),
        )

    def update_job_phase(
        self, recording_id: str, kind: BackgroundJobKind, phase: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE recording_jobs SET phase=?, updated_at_utc=?
                WHERE recording_id=? AND kind=? AND state='running'
                """,
                (
                    phase,
                    datetime.now(UTC).isoformat(),
                    recording_id,
                    kind.value,
                ),
            )

    def complete_job(self, recording_id: str, kind: BackgroundJobKind) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE recording_jobs
                SET state='succeeded', phase='completed', next_attempt_at_utc=NULL,
                    last_error=NULL, updated_at_utc=?
                WHERE recording_id=? AND kind=?
                """,
                (datetime.now(UTC).isoformat(), recording_id, kind.value),
            )

    def commit_finalization(self, summary: RecordingSummary) -> None:
        """在一个事务中发布最终路径并完成收尾任务。"""

        payload = summary.model_dump(mode="json")
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recording_update = connection.execute(
                """
                UPDATE recordings SET
                    collection_id=?, participant_id=?, data_tier=?, state=?,
                    started_at_utc=?, ended_at_utc=?, duration_ns=?, h5_path=?,
                    mkv_path=?, issues_json=?, validation_issues_json=?,
                    quality_warnings_json=?, upload_state=?, index_state=?,
                    index_message=?, manifest_generation=?
                WHERE recording_id=?
                """,
                (
                    payload["collection_id"],
                    payload["participant_id"],
                    payload["data_tier"],
                    payload["state"],
                    payload["started_at_utc"],
                    payload["ended_at_utc"],
                    payload["duration_ns"],
                    payload["h5_path"],
                    payload["mkv_path"],
                    json.dumps(payload["issues"], ensure_ascii=False),
                    json.dumps(payload["validation_issues"], ensure_ascii=False),
                    json.dumps(payload["quality_warnings"], ensure_ascii=False),
                    payload["upload_state"],
                    payload["index_state"],
                    payload["index_message"],
                    payload["manifest_generation"],
                    payload["recording_id"],
                ),
            )
            job_update = connection.execute(
                """
                UPDATE recording_jobs
                SET state='succeeded', phase='completed', next_attempt_at_utc=NULL,
                    last_error=NULL, updated_at_utc=?
                WHERE recording_id=? AND kind='finalize' AND state='running'
                """,
                (now, payload["recording_id"]),
            )
            if recording_update.rowcount != 1 or job_update.rowcount != 1:
                connection.rollback()
                raise RuntimeError("收尾提交时录制或任务状态已变化")
            connection.commit()

    def fail_job(
        self,
        recording_id: str,
        kind: BackgroundJobKind,
        error: str,
        retry_delays_seconds: tuple[float, ...],
    ) -> BackgroundJobStatus:
        now = datetime.now(UTC)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recording_jobs WHERE recording_id=? AND kind=?",
                (recording_id, kind.value),
            ).fetchone()
            if row is None:
                raise KeyError((recording_id, kind.value))
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            retry_index = attempts - 1
            can_retry = attempts < max_attempts and retry_index < len(
                retry_delays_seconds
            )
            state = "retry_wait" if can_retry else "failed"
            next_attempt = (
                now + timedelta(seconds=retry_delays_seconds[retry_index])
                if can_retry
                else None
            )
            connection.execute(
                """
                UPDATE recording_jobs
                SET state=?, phase='failed', next_attempt_at_utc=?, last_error=?,
                    updated_at_utc=?
                WHERE recording_id=? AND kind=?
                """,
                (
                    state,
                    next_attempt.isoformat() if next_attempt else None,
                    error,
                    now.isoformat(),
                    recording_id,
                    kind.value,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM recording_jobs WHERE recording_id=? AND kind=?",
                (recording_id, kind.value),
            ).fetchone()
        assert updated is not None
        return self._job_status(updated)

    def requeue_interrupted_jobs(self) -> int:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE recording_jobs
                SET state='queued', phase='queued', next_attempt_at_utc=NULL,
                    last_error='服务重启，任务已重新排队', updated_at_utc=?
                WHERE state='running'
                """,
                (now,),
            )
        return int(result.rowcount)

    def has_active_job(self, recording_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM recording_jobs
                WHERE recording_id=? AND state IN ('queued', 'running', 'retry_wait')
                LIMIT 1
                """,
                (recording_id,),
            ).fetchone()
        return row is not None

    def job_counts(self) -> dict[str, int]:
        output = {"queued": 0, "running": 0, "retry_wait": 0, "failed": 0}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT state, COUNT(*) AS count FROM recording_jobs
                WHERE state != 'succeeded' GROUP BY state
                """
            ).fetchall()
        for row in rows:
            output[str(row["state"])] = int(row["count"])
        return output

    def enqueue_upload(self, recording_id: str, generation: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO upload_jobs(recording_id, generation, state)
                VALUES (?, ?, 'pending')
                ON CONFLICT(recording_id, generation) DO UPDATE SET state='pending'
                """,
                (recording_id, generation),
            )

    def delete(self, recording_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM recording_jobs WHERE recording_id = ?", (recording_id,)
            )
            connection.execute(
                "DELETE FROM upload_jobs WHERE recording_id = ?", (recording_id,)
            )
            connection.execute(
                "DELETE FROM recordings WHERE recording_id = ?", (recording_id,)
            )

    @staticmethod
    def _from_row(
        row: sqlite3.Row, jobs: dict[str, BackgroundJobStatus] | None = None
    ) -> RecordingSummary:
        jobs = jobs or {}
        return RecordingSummary(
            recording_id=row["recording_id"],
            collection_id=row["collection_id"],
            participant_id=row["participant_id"],
            data_tier=row["data_tier"],
            state=RecordingState(row["state"]),
            started_at_utc=row["started_at_utc"],
            ended_at_utc=row["ended_at_utc"],
            duration_ns=row["duration_ns"],
            h5_path=row["h5_path"],
            mkv_path=row["mkv_path"],
            issues=json.loads(row["issues_json"]),
            validation_issues=json.loads(row["validation_issues_json"]),
            quality_warnings=json.loads(row["quality_warnings_json"]),
            upload_state=row["upload_state"],
            index_state=row["index_state"],
            index_message=row["index_message"],
            manifest_generation=row["manifest_generation"],
            finalization_job=jobs.get(BackgroundJobKind.FINALIZE.value),
            upload_job=jobs.get(BackgroundJobKind.PUBLISH.value),
        )
