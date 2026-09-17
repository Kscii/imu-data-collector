"""标注应用自己的可重建索引；不与采集 catalog 共用数据库。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from imu_data_collector.models import CaptureManifestV2, ReviewDocument


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
                CREATE TABLE IF NOT EXISTS claim_pauses (
                    collection_id TEXT PRIMARY KEY,
                    paused INTEGER NOT NULL,
                    changed_by TEXT NOT NULL,
                    changed_at_utc TEXT NOT NULL
                );
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
            for name, definition in {
                "workflow_state": "TEXT NOT NULL DEFAULT 'unassigned'",
                "annotator_id": "TEXT",
                "review_participant_id": "TEXT",
                "participant_status": "TEXT NOT NULL DEFAULT 'unassigned'",
                "review_generation": "INTEGER NOT NULL DEFAULT -1",
                "review_indexed_at_utc": "TEXT",
            }.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE recordings ADD COLUMN {name} {definition}")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS annotation_work_items_idx "
                "ON recordings(deletion_state, workflow_state, annotator_id, "
                "captured_at_utc DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS annotation_collection_idx "
                               "ON recordings(deletion_state, collection_id, captured_at_utc DESC)")
            connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS recording_search "
                               "USING fts5(recording_id UNINDEXED, participant_id, collection_id)")
            if (connection.execute("SELECT COUNT(*) FROM recording_search").fetchone()[0]
                    != connection.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]):
                connection.execute("DELETE FROM recording_search")
                connection.execute("""INSERT INTO recording_search
                    SELECT r.recording_id, COALESCE(r.review_participant_id, r.participant_id),
                    r.collection_id FROM recordings AS r""")

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
                    participant_id=excluded.participant_id,
                    collection_id=excluded.collection_id,
                    data_tier=excluded.data_tier,
                    captured_at_utc=excluded.captured_at_utc,
                    manifest_generation=excluded.manifest_generation,
                    manifest_json=excluded.manifest_json
                """,
                (
                    manifest.recording_id,
                    manifest.participant_id or "",
                    manifest.collection_id,
                    manifest.data_tier.value,
                    manifest.captured_at_utc,
                    generation,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            connection.execute("DELETE FROM recording_search WHERE recording_id=?",
                               (manifest.recording_id,))
            connection.execute("INSERT INTO recording_search VALUES (?, ?, ?)",
                               (manifest.recording_id, manifest.participant_id or "",
                                manifest.collection_id))

    def index_review(self, review: ReviewDocument, generation: int) -> None:
        """Keep queue fields in the local read model after a review is read or written."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT review_generation FROM recordings WHERE recording_id=?",
                (review.recording_id,)).fetchone()
            if row is None or row[0] == generation:
                return
            assignment = review.participant_assignment
            connection.execute("""UPDATE recordings SET workflow_state=?, annotator_id=?,
                review_participant_id=?, participant_status=?, review_generation=?,
                review_indexed_at_utc=? WHERE recording_id=?""",
                (review.workflow.state.value, review.workflow.annotator_id,
                 assignment.participant_id, assignment.status.value, generation,
                 datetime.now(UTC).isoformat(), review.recording_id))
            connection.execute("DELETE FROM recording_search WHERE recording_id=?",
                               (review.recording_id,))
            connection.execute("""INSERT INTO recording_search
                SELECT recording_id, COALESCE(review_participant_id, participant_id), collection_id
                FROM recordings WHERE recording_id=?""", (review.recording_id,))

    def pending_review_ids(self, limit: int = 100) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute("""SELECT recording_id FROM recordings
                WHERE deletion_state='active' AND review_generation=-1
                ORDER BY captured_at_utc DESC LIMIT ?""", (limit,)).fetchall()
        return [row[0] for row in rows]

    def claim_paused(self, collection_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT paused FROM claim_pauses WHERE collection_id=?",
                                     (collection_id,)).fetchone()
        return bool(row[0]) if row else False

    def set_claim_paused(self, collection_id: str, paused: bool, actor: str) -> None:
        with self._connect() as connection:
            if not connection.execute("SELECT 1 FROM recordings WHERE collection_id=? "
                                      "AND deletion_state='active' LIMIT 1",
                                      (collection_id,)).fetchone():
                raise KeyError(collection_id)
            connection.execute("""INSERT INTO claim_pauses VALUES (?, ?, ?, ?)
                ON CONFLICT(collection_id) DO UPDATE SET paused=excluded.paused,
                changed_by=excluded.changed_by, changed_at_utc=excluded.changed_at_utc""",
                (collection_id, int(paused), actor, datetime.now(UTC).isoformat()))

    def work_items(self, *, actor: str, view: str, search: str = "",
                   collection: str = "", tier: str = "", status: str = "",
                   limit: int = 50,
                   before: tuple[str, str] | None = None) -> tuple[list[dict], int]:
        clauses = ["r.deletion_state='active'"]
        params: list[object] = []
        if view == "mine":
            clauses.append("r.review_generation>=0 AND "
                           "r.workflow_state='in_progress' AND r.annotator_id=?")
            params.append(actor)
        elif view == "claimable":
            clauses.append("r.review_generation>=0 AND r.workflow_state='unassigned' "
                           "AND COALESCE(p.paused, 0)=0")
        elif view == "completed":
            clauses.append("r.review_generation>=0 AND r.workflow_state='completed'")
        elif view != "all":
            raise ValueError("Invalid work-item view")
        if collection:
            clauses.append("r.collection_id=?")
            params.append(collection)
        if tier:
            clauses.append("r.data_tier=?")
            params.append(tier)
        if status:
            clauses.append("r.workflow_state=?")
            params.append(status)
        words = [part.replace('"', "") for part in search.split() if part.strip('"')]
        if words:
            clauses.append("r.recording_id IN (SELECT recording_id FROM "
                           "recording_search WHERE recording_search MATCH ?)")
            params.append(" ".join('"' + word + '"*' for word in words))
        where = " AND ".join(clauses)
        joins = " LEFT JOIN claim_pauses AS p ON p.collection_id=r.collection_id"
        with self._connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM recordings AS r{joins} WHERE {where}",
                                       params).fetchone()[0]
            page_where = where
            page_params = list(params)
            if before:
                page_where += " AND (r.captured_at_utc, r.recording_id) < (?, ?)"
                page_params.extend(before)
            rows = connection.execute(f"""SELECT r.recording_id, r.collection_id,
                COALESCE(r.review_participant_id, r.participant_id) AS participant_id,
                r.participant_status, r.workflow_state, r.annotator_id, r.data_tier,
                r.captured_at_utc, r.review_indexed_at_utc,
                r.review_generation,
                json_extract(r.manifest_json, '$.duration_ns') AS duration_ns,
                COALESCE(p.paused, 0) AS claim_paused
                FROM recordings AS r{joins}
                WHERE {page_where} ORDER BY r.captured_at_utc DESC, r.recording_id DESC
                LIMIT ?""", [*page_params, limit]).fetchall()
        return [dict(row) for row in rows], total

    def collections(self, limit: int = 200) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("""SELECT r.collection_id AS name, COUNT(*) AS count,
                COALESCE(p.paused, 0) AS paused FROM recordings AS r
                LEFT JOIN claim_pauses AS p ON p.collection_id=r.collection_id
                WHERE r.deletion_state='active' GROUP BY r.collection_id
                ORDER BY count DESC, name LIMIT ?""", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def index_progress(self) -> dict:
        with self._connect() as connection:
            row = connection.execute("""SELECT COUNT(*) AS total,
                SUM(review_generation>=0) AS ready,
                MAX(review_indexed_at_utc) AS indexed_at_utc,
                MAX(captured_at_utc) AS latest_published_at_utc
                FROM recordings WHERE deletion_state='active'""").fetchone()
        return {"published": row["total"], "indexed": row["ready"] or 0,
                "indexed_at_utc": row["indexed_at_utc"],
                "latest_published_at_utc": row["latest_published_at_utc"]}

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
