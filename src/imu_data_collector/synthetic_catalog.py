"""Rebuildable local index and short leases for the synthetic review queue."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from uuid import uuid4

from imu_data_collector.storage import ObjectConflictError


class SyntheticCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.last_scan = 0.0
        self._refresh_lock = threading.Lock()
        with self._connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    published_at_utc TEXT NOT NULL,
                    commit_json TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT 'unreviewed',
                    revision INTEGER NOT NULL DEFAULT 0,
                    review_json TEXT,
                    label_json TEXT,
                    PRIMARY KEY(candidate_id, version_id)
                );
                CREATE INDEX IF NOT EXISTS synthetic_queue_idx
                    ON candidates(decision, published_at_utc);
                CREATE TABLE IF NOT EXISTS leases (
                    candidate_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY(candidate_id, version_id)
                );
                CREATE TABLE IF NOT EXISTS skipped (
                    candidate_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY(candidate_id, version_id, actor)
                );
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def refresh(self, store, prefix: str, validate_commit, *,
                minimum_interval_s: float = 60.0) -> dict:
        with self._refresh_lock:
            return self._refresh_unlocked(
                store, prefix, validate_commit,
                minimum_interval_s=minimum_interval_s)

    def _refresh_unlocked(self, store, prefix: str, validate_commit, *,
                          minimum_interval_s: float) -> dict:
        now = time.monotonic()
        if now - self.last_scan < minimum_interval_s:
            return {"new": 0, "scanned": False}
        imported = 0
        listing = store.list(prefix + "/candidates/")
        with self._connect() as db:
            for info in listing:
                parts = info.key.removeprefix(prefix + "/candidates/").split("/")
                if len(parts) != 2 or not parts[1].endswith(".json"):
                    continue
                candidate_id, version_id = parts[0], parts[1][:-5]
                if db.execute(
                    "SELECT 1 FROM candidates WHERE candidate_id=? AND version_id=?",
                    (candidate_id, version_id)).fetchone():
                    continue
                try:
                    commit = validate_commit(candidate_id, version_id)
                    review = None
                    label = None
                    try:
                        review = store.read_json(
                            f"{prefix}/reviews/{candidate_id}/{version_id}/latest.json")[0]
                    except FileNotFoundError:
                        pass
                    try:
                        label_pointer = store.read_json(
                            f"{prefix}/labels/{candidate_id}/{version_id}/latest.json")[0]
                        label = store.read_json(label_pointer["revision_key"])[0]["label"]
                    except FileNotFoundError:
                        pass
                    if label is None and review:
                        legacy = store.read_json(review["revision_key"])[0].get("labels") or []
                        if legacy:
                            label = {**legacy[0], "origin": "legacy-human",
                                     "verification": "human",
                                     "taxonomy_id": "legacy-synthetic",
                                     "taxonomy_version": "1.0.0"}
                except (FileNotFoundError, ValueError):
                    continue
                db.execute(
                    "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (candidate_id, version_id, commit["published_at_utc"],
                     json.dumps(commit, ensure_ascii=False),
                     review["decision"] if review else "unreviewed",
                     review["revision"] if review else 0,
                     json.dumps(review) if review else None,
                     json.dumps(label) if label else None))
                imported += 1
        self.last_scan = time.monotonic()
        return {"new": imported, "scanned": True}

    def rows(self, *, decision: str | None = None, search: str = "",
             high_risk: bool = False, limit: int = 200, offset: int = 0) -> list[dict]:
        clauses = []
        params: list[object] = []
        if decision and decision != "all":
            clauses.append("decision=?"); params.append(decision)
        if search:
            clauses.append("(candidate_id LIKE ? OR commit_json LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if high_risk:
            clauses.append("json_extract(commit_json, '$.risk_tier')='high'")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = ("SELECT * FROM candidates" + where +
               " ORDER BY published_at_utc, candidate_id LIMIT ? OFFSET ?")
        with self._connect() as db:
            rows = db.execute(sql, [*params, limit, offset]).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        return {
            "commit": json.loads(row["commit_json"]),
            "review": json.loads(row["review_json"]) if row["review_json"] else None,
            "label": json.loads(row["label_json"]) if row["label_json"] else None,
            "decision": row["decision"], "revision": row["revision"],
        }

    def get(self, candidate_id: str, version_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM candidates WHERE candidate_id=? AND version_id=?",
                             (candidate_id, version_id)).fetchone()
        if row is None:
            raise FileNotFoundError(candidate_id)
        return self._decode(row)

    def counts(self) -> dict:
        with self._connect() as db:
            rows = db.execute("SELECT decision, COUNT(*) AS n FROM candidates GROUP BY decision").fetchall()
        result = {row["decision"]: row["n"] for row in rows}
        return {"published": sum(result.values()),
                "unreviewed": result.get("unreviewed", 0),
                "passed": result.get("pass", 0),
                "rejected": result.get("reject", 0)}

    def set_review(self, candidate_id: str, version_id: str, pointer: dict) -> None:
        with self._connect() as db:
            db.execute("UPDATE candidates SET decision=?, revision=?, review_json=? "
                       "WHERE candidate_id=? AND version_id=?",
                       (pointer["decision"], pointer["revision"], json.dumps(pointer),
                        candidate_id, version_id))
            db.execute("DELETE FROM leases WHERE candidate_id=? AND version_id=?",
                       (candidate_id, version_id))

    def set_label(self, candidate_id: str, version_id: str, label: dict) -> None:
        with self._connect() as db:
            db.execute("UPDATE candidates SET label_json=? WHERE candidate_id=? AND version_id=?",
                       (json.dumps(label), candidate_id, version_id))

    def claim(self, actor: str, *, batch_size: int = 5, search: str = "",
              high_risk: bool = False) -> list[dict]:
        now = time.time()
        extra = ""
        params: list[object] = [actor]
        filter_params: list[object] = []
        if search:
            extra += " AND (c.candidate_id LIKE ? OR c.commit_json LIKE ?)"
            filter_params.extend([f"%{search}%", f"%{search}%"])
            params.extend(filter_params)
        if high_risk:
            extra += " AND json_extract(c.commit_json, '$.risk_tier')='high'"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM leases WHERE expires_at<?", (now,))
            db.execute("DELETE FROM skipped WHERE expires_at<?", (now,))
            existing = db.execute("""
                SELECT l.candidate_id, l.version_id, l.token FROM leases AS l
                JOIN candidates AS c ON c.candidate_id=l.candidate_id
                    AND c.version_id=l.version_id
                WHERE l.actor=? AND c.decision='unreviewed'
            """ + extra + " ORDER BY c.published_at_utc, c.candidate_id LIMIT ?",
                                  [actor, *filter_params, batch_size]).fetchall()
            claimed = [{"candidate_id": row["candidate_id"],
                        "version_id": row["version_id"],
                        "lease_token": row["token"]} for row in existing]
            remaining = batch_size - len(claimed)
            if not remaining:
                return claimed
            rows = db.execute("""
                SELECT c.candidate_id, c.version_id FROM candidates AS c
                WHERE c.decision='unreviewed'
                  AND NOT EXISTS (SELECT 1 FROM leases AS l
                      WHERE l.candidate_id=c.candidate_id AND l.version_id=c.version_id)
                  AND NOT EXISTS (SELECT 1 FROM skipped AS s
                      WHERE s.candidate_id=c.candidate_id AND s.version_id=c.version_id
                        AND s.actor=?)
                  AND NOT EXISTS (SELECT 1 FROM candidates AS newer
                      WHERE newer.candidate_id=c.candidate_id
                        AND (newer.published_at_utc>c.published_at_utc
                          OR (newer.published_at_utc=c.published_at_utc
                              AND newer.version_id>c.version_id)))
            """ + extra + " ORDER BY c.published_at_utc, c.candidate_id LIMIT ?",
                              [*params, remaining]).fetchall()
            for row in rows:
                token = uuid4().hex
                db.execute("INSERT INTO leases VALUES (?, ?, ?, ?, ?)",
                           (row["candidate_id"], row["version_id"], actor, token, now + 900))
                claimed.append({"candidate_id": row["candidate_id"],
                                "version_id": row["version_id"], "lease_token": token})
        return claimed

    def check_lease(self, candidate_id: str, version_id: str,
                    actor: str, token: str) -> None:
        with self._connect() as db:
            row = db.execute("SELECT token, actor, expires_at FROM leases "
                             "WHERE candidate_id=? AND version_id=?",
                             (candidate_id, version_id)).fetchone()
        if (not row or row["actor"] != actor or row["token"] != token
                or row["expires_at"] <= time.time()):
            raise ObjectConflictError("审核任务已过期或已分配给其他人")

    def renew(self, actor: str, token: str) -> None:
        with self._connect() as db:
            updated = db.execute("UPDATE leases SET expires_at=? WHERE actor=? AND token=? "
                                 "AND expires_at>?", (time.time() + 900, actor, token,
                                                      time.time())).rowcount
        if updated != 1:
            raise ObjectConflictError("审核任务已过期")

    def release(self, actor: str, token: str, *, skip: bool = False) -> None:
        with self._connect() as db:
            row = db.execute("SELECT candidate_id, version_id FROM leases "
                             "WHERE actor=? AND token=?", (actor, token)).fetchone()
            if row is None:
                raise ObjectConflictError("审核任务已过期")
            db.execute("DELETE FROM leases WHERE actor=? AND token=?", (actor, token))
            if skip:
                db.execute("INSERT OR REPLACE INTO skipped VALUES (?, ?, ?, ?)",
                           (row["candidate_id"], row["version_id"], actor,
                            time.time() + 3600))
