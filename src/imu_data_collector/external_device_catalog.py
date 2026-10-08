"""Rebuildable external history index and durable, single-worker job queue."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from imu_data_collector.external_device_aggregation import LEVELS, millis, refresh_minutes
from imu_data_collector.external_device_domain import (
    device_id,
    digest,
    is_synthetic,
    json_bytes,
    merged_intervals,
    metrics,
    now,
    record_id,
    timestamp,
    utc,
)


class ExternalDeviceCatalog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, history_no TEXT NOT NULL,
                    device_no TEXT NOT NULL, product_key TEXT, display_name TEXT NOT NULL,
                    listed INTEGER NOT NULL DEFAULT 1, first_seen TEXT NOT NULL,
                    scheduled_until TEXT NOT NULL, history_start TEXT,
                    UNIQUE(kind, history_no)
                );
                CREATE TABLE IF NOT EXISTS windows (
                    id INTEGER PRIMARY KEY, device_id TEXT NOT NULL,
                    start TEXT NOT NULL, end TEXT NOT NULL, lane TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at TEXT NOT NULL DEFAULT '', error TEXT, updated_at TEXT NOT NULL,
                    UNIQUE(device_id, start, end)
                );
                CREATE INDEX IF NOT EXISTS external_window_queue
                    ON windows(state, retry_at, lane, end);
                CREATE TABLE IF NOT EXISTS archives (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, start TEXT NOT NULL,
                    end TEXT NOT NULL, fetched_at TEXT NOT NULL, manifest_key TEXT NOT NULL,
                    object_key TEXT NOT NULL, sha256 TEXT NOT NULL, total INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS external_coverage ON archives(device_id,start,end);
                CREATE TABLE IF NOT EXISTS coverage (
                    device_id TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,
                    PRIMARY KEY(device_id,start)
                );
                CREATE TABLE IF NOT EXISTS device_stats (
                    device_id TEXT PRIMARY KEY,total INTEGER NOT NULL DEFAULT 0,
                    synthetic_records INTEGER NOT NULL DEFAULT 0,
                    first_record TEXT,last_record TEXT,last_success TEXT
                );
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    message_type TEXT NOT NULL, received_at TEXT NOT NULL,
                    metric_json TEXT NOT NULL, synthetic INTEGER NOT NULL,
                    hash TEXT NOT NULL, archive_id TEXT NOT NULL, position INTEGER NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS external_record_time
                    ON records(device_id,received_at,id);
                CREATE TABLE IF NOT EXISTS metric_rollups (
                    device_id TEXT NOT NULL,level INTEGER NOT NULL,bucket INTEGER NOT NULL,
                    metric TEXT NOT NULL,synthetic INTEGER NOT NULL,stats TEXT NOT NULL,
                    PRIMARY KEY(device_id,level,bucket,metric,synthetic)
                );
                CREATE INDEX IF NOT EXISTS external_metric_range
                    ON metric_rollups(device_id,level,metric,bucket);
                CREATE TABLE IF NOT EXISTS metric_metadata (
                    device_id TEXT NOT NULL,metric TEXT NOT NULL,count INTEGER NOT NULL,
                    first_record TEXT NOT NULL,last_record TEXT NOT NULL,
                    PRIMARY KEY(device_id,metric)
                );
                CREATE TABLE IF NOT EXISTS versions (
                    record_id TEXT NOT NULL, hash TEXT NOT NULL, archive_id TEXT NOT NULL,
                    position INTEGER NOT NULL, fetched_at TEXT NOT NULL,
                    PRIMARY KEY(record_id, hash)
                );
                CREATE TABLE IF NOT EXISTS exports (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, start TEXT NOT NULL,
                    end TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    object_key TEXT, sha256 TEXT, size_bytes INTEGER, error TEXT
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(exports)")}
            if "scope_json" not in columns:
                db.execute("ALTER TABLE exports ADD COLUMN scope_json TEXT")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(windows)")}
            if "task_id" not in columns:
                db.execute("ALTER TABLE windows ADD COLUMN task_id TEXT")
            if not db.execute("SELECT 1 FROM records LIMIT 1").fetchone():
                db.execute("INSERT OR IGNORE INTO meta VALUES ('aggregation_version','1')")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, params)]

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.connect() as db:
            db.execute(sql, params)

    def get_meta(self, key: str, default=None):
        rows = self.rows("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_meta(self, key: str, value) -> None:
        self.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def device(self, identifier: str) -> dict:
        rows = self.rows("SELECT * FROM devices WHERE id=?", (identifier,))
        if not rows:
            raise KeyError("Device not found")
        return rows[0]

    def discover(self, kind: str, devices: list[dict], discovered_at: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE devices SET listed=0 WHERE kind=?", (kind,))
            for item in devices:
                identifier = device_id(kind, item["historyDeviceNo"])
                db.execute(
                    """INSERT INTO devices
                    (id,kind,history_no,device_no,product_key,display_name,first_seen,
                     scheduled_until,history_start) VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,listed=1""",
                    (
                        identifier,
                        kind,
                        item["historyDeviceNo"],
                        item["deviceNo"],
                        item.get("productKey"),
                        item["displayName"],
                        discovered_at,
                        discovered_at,
                        self.get_meta("confirmed_history_start"),
                    ),
                )

    def enqueue_window(
        self, device: str, start: str, end: str, lane: str, *, refresh: bool = False
    ) -> None:
        if start >= end:
            return
        with self.connect() as db:
            db.execute(
                """INSERT INTO windows(device_id,start,end,lane,updated_at)
                VALUES (?,?,?,?,?) ON CONFLICT(device_id,start,end) DO NOTHING""",
                (device, start, end, lane, now()),
            )
            if refresh:
                parent = db.execute(
                    "SELECT state FROM windows WHERE device_id=? AND start=? AND end=?",
                    (device, start, end),
                ).fetchone()
                if parent and parent["state"] == "split":
                    db.execute(
                        """UPDATE windows SET state='pending',retry_at='',error=NULL,
                        updated_at=? WHERE device_id=? AND start>=? AND end<=? AND state='done'""",
                        (now(), device, start, end),
                    )
                db.execute(
                    """UPDATE windows SET state='pending',retry_at='',error=NULL,
                    updated_at=? WHERE device_id=? AND start=? AND end=? AND state='done'""",
                    (now(), device, start, end),
                )

    def coverage(self, device: str, start: str, end: str, *, db=None) -> dict:
        sql = "SELECT start,end FROM coverage WHERE device_id=? AND end>? AND start<?"
        params = (device, start, end)
        rows = [dict(row) for row in db.execute(sql, params)] if db else self.rows(sql, params)
        intervals = merged_intervals([(max(r["start"], start), min(r["end"], end)) for r in rows])
        cursor = start
        gaps = []
        for lower, upper in intervals:
            if lower > cursor:
                gaps.append([cursor, lower])
            cursor = max(cursor, upper)
        if cursor < end:
            gaps.append([cursor, end])
        return {
            "start": start,
            "end": end,
            "complete": not gaps,
            "covered": intervals,
            "gaps": gaps,
        }

    def contiguous_until(self, device: str, start: str, end: str) -> str:
        coverage = self.coverage(device, start, end)
        return coverage["gaps"][0][0] if coverage["gaps"] else end

    def schedule_live(self, cutoff: str, window_s: int, overlap_s: int) -> None:
        for device in self.rows("SELECT * FROM devices WHERE listed=1"):
            if device["scheduled_until"] >= cutoff:
                continue
            start = max(
                timestamp(device["first_seen"]),
                timestamp(device["scheduled_until"]) - timedelta(seconds=overlap_s),
            )
            while start < timestamp(cutoff):
                end = min(start + timedelta(seconds=window_s), timestamp(cutoff))
                self.enqueue_window(device["id"], utc(start), utc(end), "live", refresh=True)
                start = end
            self.execute("UPDATE devices SET scheduled_until=? WHERE id=?", (cutoff, device["id"]))

    def schedule_history(self, window_s: int) -> None:
        for device in self.rows("SELECT * FROM devices WHERE history_start IS NOT NULL"):
            start = self.contiguous_until(
                device["id"], device["history_start"], device["first_seen"]
            )
            end = min(utc(timestamp(start) + timedelta(seconds=window_s)), device["first_seen"])
            self.enqueue_window(device["id"], start, end, "history")

    def confirm_history(self, start: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM meta WHERE key='confirmed_history_start'"
            ).fetchone()
            if row and start > json.loads(row["value"]):
                raise ValueError("History origin can only be extended earlier")
            db.execute(
                "INSERT INTO meta VALUES ('confirmed_history_start',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(start),),
            )
            db.execute("UPDATE devices SET history_start=MIN(?,first_seen)", (start,))

    def index_archive(self, manifest: dict, records: list[dict]) -> None:
        device = manifest["device"]
        identifier = device["id"]
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='archive_sequence'").fetchone()
            sequence = max(json.loads(row["value"]) if row else 0, manifest.get("sequence", 0))
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('archive_sequence',?)", (json.dumps(sequence),)
            )
            if db.execute("SELECT 1 FROM archives WHERE id=?", (manifest["id"],)).fetchone():
                return
            db.execute(
                """INSERT INTO devices
                (id,kind,history_no,device_no,product_key,display_name,listed,first_seen,scheduled_until)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
                (
                    identifier,
                    device["kind"],
                    device["history_no"],
                    device["device_no"],
                    device["product_key"],
                    device["display_name"],
                    device["listed"],
                    device["first_seen"],
                    device["first_seen"],
                ),
            )
            db.execute(
                "INSERT INTO archives VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    manifest["id"],
                    identifier,
                    manifest["start"],
                    manifest["end"],
                    manifest["fetched_at"],
                    manifest["manifest_key"],
                    manifest["object_key"],
                    manifest["sha256"],
                    len(records),
                ),
            )
            db.execute("INSERT OR IGNORE INTO device_stats(device_id) VALUES (?)", (identifier,))
            added, synthetic_delta = 0, 0
            dirty_minutes = set()
            for index, record in enumerate(records):
                message = record["messageType" if device["kind"] == "mattress" else "operation"]
                rid = record_id(
                    identifier, message if device["kind"] == "mattress" else "", record["id"]
                )
                sha = digest(json_bytes(record))
                previous = db.execute(
                    "SELECT synthetic,received_at,hash FROM records WHERE id=?", (rid,)
                ).fetchone()
                if previous and manifest.get("append_only", False):
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO versions VALUES (?,?,?,?,?)",
                    (rid, sha, manifest["id"], index, manifest["fetched_at"]),
                )
                changed = db.execute(
                    """INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                    message_type=excluded.message_type,received_at=excluded.received_at,
                    metric_json=excluded.metric_json,synthetic=excluded.synthetic,
                    hash=excluded.hash,archive_id=excluded.archive_id,
                    position=excluded.position,fetched_at=excluded.fetched_at
                    WHERE (excluded.fetched_at,excluded.archive_id) >=
                          (records.fetched_at,records.archive_id)
                    """,
                    (
                        rid,
                        identifier,
                        str(record["id"]),
                        message,
                        utc(timestamp(record["receivedAt"])),
                        json.dumps(metrics(record, device["kind"])),
                        int(is_synthetic(record)),
                        sha,
                        manifest["id"],
                        index,
                        manifest["fetched_at"],
                    ),
                )
                if changed.rowcount:
                    added += int(previous is None)
                    synthetic_delta += int(is_synthetic(record)) - (
                        previous["synthetic"] if previous else 0
                    )
                    if previous is None or previous["hash"] != sha:
                        dirty_minutes.add(millis(record["receivedAt"]) // LEVELS[0] * LEVELS[0])
                        if previous:
                            dirty_minutes.add(
                                millis(previous["received_at"]) // LEVELS[0] * LEVELS[0]
                            )

            refresh_minutes(db, identifier, dirty_minutes)

            db.execute(
                """UPDATE device_stats SET total=total+?,
                synthetic_records=synthetic_records+?,
                first_record=(SELECT received_at FROM records WHERE device_id=?
                              ORDER BY received_at,id LIMIT 1),
                last_record=(SELECT received_at FROM records WHERE device_id=?
                             ORDER BY received_at DESC,id DESC LIMIT 1),
                last_success=MAX(COALESCE(last_success,''),?) WHERE device_id=?""",
                (
                    added,
                    synthetic_delta,
                    identifier,
                    identifier,
                    manifest["fetched_at"],
                    identifier,
                ),
            )

            overlaps = db.execute(
                "SELECT start,end FROM coverage WHERE device_id=? AND end>=? AND start<=?",
                (identifier, manifest["start"], manifest["end"]),
            ).fetchall()
            start = min([manifest["start"], *(row["start"] for row in overlaps)])
            end = max([manifest["end"], *(row["end"] for row in overlaps)])
            db.execute(
                "DELETE FROM coverage WHERE device_id=? AND end>=? AND start<=?",
                (identifier, start, end),
            )
            db.execute("INSERT INTO coverage VALUES (?,?,?)", (identifier, start, end))

    def recover_running(self) -> None:
        self.execute("UPDATE windows SET state='pending' WHERE state='running'")
        self.execute("UPDATE exports SET state='pending' WHERE state='running'")

    def next_window(self) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM windows WHERE state IN ('pending','failed')
                AND retry_at<=? ORDER BY CASE lane WHEN 'live' THEN 0 ELSE 1 END,
                start LIMIT 1""",
                (now(),),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE windows SET state='running',attempts=attempts+1,updated_at=? WHERE id=?",
                (now(), row["id"]),
            )
            result = dict(row)
            result["attempts"] += 1
            return result

    def fail_window(self, job: dict, error: str) -> None:
        retry = utc(
            timestamp(now()) + timedelta(seconds=min(3600, 30 * 2 ** min(job["attempts"], 7)))
        )
        self.execute(
            "UPDATE windows SET state='failed',error=?,retry_at=?,updated_at=? WHERE id=?",
            (error, retry, now(), job["id"]),
        )

    def export_job(self, device: str, start: str, end: str) -> dict:
        self.device(device)
        existing = self.rows(
            """SELECT * FROM exports WHERE device_id=? AND start=? AND end=?
            AND state IN ('pending','running') LIMIT 1""",
            (device, start, end),
        )
        if existing:
            return existing[0]
        identifier = uuid.uuid4().hex
        self.execute(
            """INSERT INTO exports(id,device_id,start,end,created_at,updated_at)
            VALUES (?,?,?,?,?,?)""",
            (identifier, device, start, end, now(), now()),
        )
        return self.rows("SELECT * FROM exports WHERE id=?", (identifier,))[0]

    def full_export_job(self, *, device: str | None = None, kind: str | None = None) -> dict:
        if device:
            devices = [self.device(device)]
        else:
            devices = self.rows("SELECT * FROM devices WHERE kind=? ORDER BY id", (kind,))
        if not devices:
            raise KeyError("No archived devices in this category")
        scope = json.dumps(
            {
                "scope": "device" if device else "kind",
                "kind": kind,
                "device_ids": [item["id"] for item in devices],
            },
            sort_keys=True,
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM exports WHERE scope_json=? "
                "AND state IN ('pending','running') LIMIT 1",
                (scope,),
            ).fetchone()
            if existing:
                return dict(existing)
            identifier, at = uuid.uuid4().hex, now()
            db.execute(
                "INSERT INTO exports(id,device_id,start,end,created_at,updated_at,scope_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (identifier, device or "", "", "", at, at, scope),
            )
            return dict(db.execute("SELECT * FROM exports WHERE id=?", (identifier,)).fetchone())

    def records(
        self,
        device: str,
        start: str,
        end: str,
        *,
        limit: int = 100,
        before: tuple[str, str] | None = None,
        message: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM records WHERE device_id=? AND received_at>=? AND received_at<?"
        params: list = [device, start, end]
        if before:
            sql += " AND (received_at,id)>(?,?)"
            params.extend(before)
        if message:
            sql += " AND message_type=?"
            params.append(message)
        sql += " ORDER BY received_at,id LIMIT ?"
        params.append(limit)
        return self.rows(sql, tuple(params))
