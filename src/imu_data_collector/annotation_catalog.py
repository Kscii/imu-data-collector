"""标注应用自己的可重建索引；不与采集 catalog 共用数据库。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from imu_data_collector.models import CaptureManifestV2


class AnnotationCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS recordings (
                    recording_id TEXT PRIMARY KEY,
                    participant_id TEXT NOT NULL,
                    collection_id TEXT NOT NULL,
                    data_tier TEXT NOT NULL,
                    captured_at_utc TEXT NOT NULL,
                    manifest_generation INTEGER NOT NULL,
                    manifest_json TEXT NOT NULL,
                    deletion_state TEXT NOT NULL DEFAULT 'active'
                );
                CREATE INDEX IF NOT EXISTS annotation_recordings_time_idx
                    ON recordings(captured_at_utc DESC);
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(recordings)").fetchall()
            }
            if "deletion_state" not in columns:
                connection.execute(
                    "ALTER TABLE recordings ADD COLUMN deletion_state "
                    "TEXT NOT NULL DEFAULT 'active'"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def upsert(self, manifest: CaptureManifestV2, generation: int) -> None:
        payload = manifest.model_dump(mode="json")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO recordings (
                    recording_id, participant_id, collection_id, data_tier,
                    captured_at_utc, manifest_generation, manifest_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recording_id) DO UPDATE SET
                    manifest_generation=excluded.manifest_generation,
                    manifest_json=excluded.manifest_json
                """,
                (
                    manifest.recording_id,
                    manifest.participant_id,
                    manifest.collection_id,
                    manifest.data_tier.value,
                    manifest.captured_at_utc,
                    generation,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def get(self, recording_id: str) -> CaptureManifestV2 | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM recordings "
                "WHERE recording_id = ? AND deletion_state = 'active'",
                (recording_id,),
            ).fetchone()
        return CaptureManifestV2.model_validate_json(row[0]) if row else None

    def manifest_generation(self, recording_id: str) -> int | None:
        """返回已索引 manifest 的对象 generation，用于跳过未变化录制。"""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT manifest_generation FROM recordings WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()
        return int(row[0]) if row else None

    def get_for_deletion(
        self, recording_id: str
    ) -> tuple[CaptureManifestV2, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT manifest_json, deletion_state FROM recordings "
                "WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()
        if row is None:
            return None
        return CaptureManifestV2.model_validate_json(row[0]), str(row[1])

    def mark_deleting(self, recording_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE recordings SET deletion_state = 'deleting' "
                "WHERE recording_id = ?",
                (recording_id,),
            )
            if cursor.rowcount != 1:
                raise KeyError(recording_id)

    def delete(self, recording_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM recordings WHERE recording_id = ?",
                (recording_id,),
            )

    def list(self) -> list[CaptureManifestV2]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT manifest_json FROM recordings WHERE deletion_state = 'active' "
                "ORDER BY captured_at_utc DESC"
            ).fetchall()
        return [CaptureManifestV2.model_validate_json(row[0]) for row in rows]
