"""轻量本地索引；可迁移的录制数据仍以 H5 与 MKV 为准。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from imu_data_collector.models import RecordingState, RecordingSummary

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
        return self._from_row(row) if row else None

    def list(self) -> list[RecordingSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM recordings ORDER BY started_at_utc DESC"
            ).fetchall()
        return [self._from_row(row) for row in rows]

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
                "DELETE FROM upload_jobs WHERE recording_id = ?", (recording_id,)
            )
            connection.execute(
                "DELETE FROM recordings WHERE recording_id = ?", (recording_id,)
            )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> RecordingSummary:
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
        )
