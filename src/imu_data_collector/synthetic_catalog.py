"""Rebuildable local index and short leases for the synthetic review queue."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from imu_data_collector.storage import ObjectConflictError


class SyntheticCatalog:
    @staticmethod
    def _fts_query(search: str) -> str:
        words = [part.replace('"', "") for part in search.split() if part.strip('"')]
        return " ".join('"' + word + '"*' for word in words)

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
                CREATE TABLE IF NOT EXISTS claim_pauses (
                    source_dataset TEXT PRIMARY KEY,
                    paused INTEGER NOT NULL,
                    changed_by TEXT NOT NULL,
                    changed_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
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
            columns = {row[1] for row in db.execute("PRAGMA table_info(candidates)")}
            for name, definition in {
                "source_dataset": "TEXT",
                "source_member": "TEXT",
                "risk_tier": "TEXT",
                "effective_label_code": "TEXT",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE candidates ADD COLUMN {name} {definition}")
            db.execute("""UPDATE candidates SET
                source_dataset=json_extract(commit_json, '$.source_dataset'),
                source_member=json_extract(commit_json, '$.source_member'),
                risk_tier=COALESCE(json_extract(commit_json, '$.risk_tier'), 'unknown'),
                effective_label_code=json_extract(label_json, '$.code')
                WHERE source_dataset IS NULL""")
            db.execute("CREATE INDEX IF NOT EXISTS synthetic_source_idx "
                       "ON candidates(source_dataset, decision, published_at_utc)")
            db.execute("CREATE INDEX IF NOT EXISTS synthetic_label_idx "
                       "ON candidates(decision, effective_label_code, published_at_utc)")
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS candidate_search "
                       "USING fts5(item_key UNINDEXED, candidate_id, source_dataset, source_member)")
            if (db.execute("SELECT COUNT(*) FROM candidate_search").fetchone()[0]
                    != db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]):
                db.execute("DELETE FROM candidate_search")
                db.execute("""INSERT INTO candidate_search
                    SELECT candidate_id || '/' || version_id, candidate_id,
                    source_dataset, source_member FROM candidates""")

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
        now_utc = datetime.now(UTC)
        with self._connect() as db:
            last_full = db.execute("SELECT value FROM catalog_meta WHERE key='last_full_scan_at_utc'").fetchone()
            feed_cursor = db.execute("SELECT value FROM catalog_meta WHERE key='feed_last_key'").fetchone()
        full_scan = last_full is None or (
            now_utc - datetime.fromisoformat(last_full[0]) >= timedelta(days=1))
        identities: list[tuple[str, str, str | None]] = []
        feed_listing = []
        had_import_failure = False
        if full_scan:
            for info in store.list(prefix + "/candidates/"):
                parts = info.key.removeprefix(prefix + "/candidates/").split("/")
                if len(parts) == 2 and parts[1].endswith(".json"):
                    identities.append((parts[0], parts[1][:-5], None))
        else:
            after_key = feed_cursor[0] if feed_cursor else None
            feed_prefix = prefix + "/index-feed/"
            feed_listing = (store.list_after(feed_prefix, after_key)
                            if hasattr(store, "list_after") else
                            [info for info in store.list(feed_prefix)
                             if after_key is None or info.key > after_key])
            for info in feed_listing:
                if not info.key.endswith(".json"):
                    continue
                try:
                    feed = store.read_json(info.key)[0]
                    if feed.get("schema") != "imu_motion_simulator.candidate_index_feed.v1" \
                            or feed.get("feed_key") != info.key:
                        had_import_failure = True
                        continue
                    candidate_id, version_id = feed["candidate_id"], feed["version_id"]
                    if feed.get("commit_key") != (
                            f"{prefix}/candidates/{candidate_id}/{version_id}.json"):
                        had_import_failure = True
                        continue
                    identities.append((candidate_id, version_id, feed["commit_sha256"]))
                except (FileNotFoundError, KeyError, TypeError, ValueError):
                    had_import_failure = True
                    continue
        with self._connect() as db:
            for candidate_id, version_id, expected_digest in identities:
                if db.execute(
                    "SELECT 1 FROM candidates WHERE candidate_id=? AND version_id=?",
                    (candidate_id, version_id)).fetchone():
                    continue
                try:
                    commit = validate_commit(candidate_id, version_id)
                    if expected_digest and hashlib.sha256((json.dumps(
                            commit, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False) + "\n").encode()).hexdigest() \
                            != expected_digest:
                        had_import_failure = True
                        continue
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
                    had_import_failure = True
                    continue
                db.execute(
                    """INSERT INTO candidates (candidate_id, version_id, published_at_utc,
                    commit_json, decision, revision, review_json, label_json,
                    source_dataset, source_member, risk_tier, effective_label_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (candidate_id, version_id, commit["published_at_utc"],
                     json.dumps(commit, ensure_ascii=False),
                     review["decision"] if review else "unreviewed",
                     review["revision"] if review else 0,
                     json.dumps(review) if review else None,
                     json.dumps(label) if label else None,
                     commit["source_dataset"], commit["source_member"],
                     commit.get("risk_tier", "unknown"), label.get("code") if label else None))
                db.execute("INSERT INTO candidate_search VALUES (?, ?, ?, ?)",
                           (candidate_id + "/" + version_id, candidate_id,
                            commit["source_dataset"], commit["source_member"]))
                imported += 1
        self.last_scan = time.monotonic()
        with self._connect() as db:
            db.execute("""INSERT INTO catalog_meta VALUES ('last_scan_at_utc', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),))
            if full_scan:
                db.execute("""INSERT INTO catalog_meta VALUES ('last_full_scan_at_utc', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (now_utc.isoformat(),))
            elif feed_listing and not had_import_failure:
                db.execute("""INSERT INTO catalog_meta VALUES ('feed_last_key', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (feed_listing[-1].key,))
        return {"new": imported, "scanned": True}

    def rows(self, *, decision: str | None = None, search: str = "",
             high_risk: bool = False, label_state: str | None = None,
             limit: int = 200, offset: int = 0) -> list[dict]:
        clauses = []
        params: list[object] = []
        if decision and decision != "all":
            clauses.append("decision=?")
            params.append(decision)
        if search:
            query = self._fts_query(search)
            if query:
                clauses.append("candidate_id || '/' || version_id IN "
                               "(SELECT item_key FROM candidate_search WHERE candidate_search MATCH ?)")
                params.append(query)
        if high_risk:
            clauses.append("risk_tier='high'")
        if label_state == "pending":
            clauses.append("effective_label_code IS NULL")
        elif label_state == "labeled":
            clauses.append("effective_label_code IS NOT NULL")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        priority = (" CASE WHEN decision='unreviewed' AND revision>0 THEN 0 ELSE 1 END,"
                    " json_extract(review_json, '$.returned_at_utc') DESC,") \
            if decision == "unreviewed" else ""
        sql = ("SELECT * FROM candidates" + where +
               " ORDER BY" + priority + " published_at_utc, candidate_id LIMIT ? OFFSET ?")
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
            rows = db.execute(
                "SELECT decision, COUNT(*) AS n FROM candidates GROUP BY decision"
            ).fetchall()
        result = {row["decision"]: row["n"] for row in rows}
        return {"published": sum(result.values()),
                "unreviewed": result.get("unreviewed", 0),
                "passed": result.get("pass", 0),
                "rejected": result.get("reject", 0)}

    def summary(self) -> dict:
        with self._connect() as db:
            row = db.execute("""SELECT COUNT(*) AS published,
                SUM(decision='unreviewed') AS unreviewed,
                SUM(decision='pass') AS passed,
                SUM(decision='reject') AS rejected,
                SUM(decision='pass' AND effective_label_code IS NULL) AS label_pending,
                SUM(decision='pass' AND effective_label_code IS NOT NULL) AS snapshot_eligible,
                MAX(published_at_utc) AS latest_published_at_utc FROM candidates""").fetchone()
            scan = db.execute("SELECT value FROM catalog_meta WHERE key='last_scan_at_utc'").fetchone()
        return {**{key: (row[key] or 0) if key != "latest_published_at_utc" else row[key]
                   for key in row.keys()}, "indexed_at_utc": scan[0] if scan else None}

    def work_items(self, *, actor: str, view: str, search: str = "",
                   source: str = "", risk: str = "", status: str = "",
                   label_state: str = "", limit: int = 50,
                   before: tuple[str, str, str] | None = None) -> tuple[list[dict], int]:
        clauses = ["1=1"]
        params: list[object] = []
        now = time.time()
        if view == "mine":
            clauses.append("EXISTS (SELECT 1 FROM leases AS l WHERE l.candidate_id=c.candidate_id "
                           "AND l.version_id=c.version_id AND l.actor=? AND l.expires_at>?)")
            params.extend([actor, now])
        elif view == "claimable":
            clauses.append("(c.decision='pass' AND c.effective_label_code IS NULL OR "
                           "c.decision='unreviewed' AND NOT EXISTS (SELECT 1 FROM skipped AS s "
                           "WHERE s.candidate_id=c.candidate_id AND s.version_id=c.version_id "
                           "AND s.actor=? AND s.expires_at>?))")
            params.extend([actor, now])
            clauses.append("NOT EXISTS (SELECT 1 FROM leases AS l WHERE "
                           "l.candidate_id=c.candidate_id AND l.version_id=c.version_id "
                           "AND l.expires_at>?)")
            params.append(now)
            clauses.append("NOT EXISTS (SELECT 1 FROM candidates AS newer "
                           "WHERE newer.candidate_id=c.candidate_id "
                           "AND (newer.published_at_utc>c.published_at_utc "
                           "OR (newer.published_at_utc=c.published_at_utc "
                           "AND newer.version_id>c.version_id)))")
            clauses.append("COALESCE(p.paused, 0)=0")
        elif view == "completed":
            clauses.append("(c.decision='reject' OR c.decision='pass' "
                           "AND c.effective_label_code IS NOT NULL)")
        elif view != "all":
            raise ValueError("Invalid work-item view")
        if source:
            clauses.append("c.source_dataset=?")
            params.append(source)
        if risk:
            clauses.append("c.risk_tier=?")
            params.append(risk)
        if status:
            clauses.append("c.decision=?")
            params.append(status)
        if label_state == "pending":
            clauses.append("c.effective_label_code IS NULL")
        elif label_state == "labeled":
            clauses.append("c.effective_label_code IS NOT NULL")
        words = [part.replace('"', "") for part in search.split() if part.strip('"')]
        if words:
            clauses.append("c.candidate_id || '/' || c.version_id IN "
                           "(SELECT item_key FROM candidate_search WHERE candidate_search MATCH ?)")
            params.append(" ".join('"' + word + '"*' for word in words))
        joins = " LEFT JOIN claim_pauses AS p ON p.source_dataset=c.source_dataset"
        where = " AND ".join(clauses)
        with self._connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM candidates AS c{joins} WHERE {where}",
                               params).fetchone()[0]
            page_where = where
            page_params = list(params)
            if before:
                page_where += " AND (c.published_at_utc, c.candidate_id, c.version_id) < (?, ?, ?)"
                page_params.extend(before)
            rows = db.execute(f"""SELECT c.candidate_id, c.version_id,
                c.published_at_utc, c.source_dataset, c.source_member,
                c.risk_tier, c.decision, c.revision, c.effective_label_code,
                json_extract(c.commit_json, '$.warning_flags') AS warning_flags,
                COALESCE(p.paused, 0) AS claim_paused
                FROM candidates AS c{joins} WHERE {page_where}
                ORDER BY c.published_at_utc DESC, c.candidate_id DESC, c.version_id DESC
                LIMIT ?""", [*page_params, limit]).fetchall()
        return [dict(row) for row in rows], total

    def sources(self, limit: int = 200) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("""SELECT c.source_dataset AS name,
                COUNT(*) AS count, COALESCE(p.paused, 0) AS paused
                FROM candidates AS c LEFT JOIN claim_pauses AS p
                ON p.source_dataset=c.source_dataset
                GROUP BY c.source_dataset ORDER BY count DESC, name LIMIT ?""",
                (limit,)).fetchall()
        return [dict(row) for row in rows]

    def reindex_labels(self, label_catalog: dict, resolver, *, force: bool = False) -> None:
        """Recompute auto-map eligibility only when rules or concepts change."""
        fingerprint = hashlib.sha256(json.dumps(label_catalog, sort_keys=True).encode()).hexdigest()
        with self._connect() as db:
            previous = db.execute("SELECT value FROM catalog_meta WHERE key='label_fingerprint'").fetchone()
            if not force and previous and previous[0] == fingerprint:
                return
            last_id = ""
            last_version = ""
            while batch := db.execute("""SELECT candidate_id, version_id, commit_json,
                label_json FROM candidates WHERE (candidate_id, version_id) > (?, ?)
                ORDER BY candidate_id, version_id LIMIT 500""",
                (last_id, last_version)).fetchall():
                for row in batch:
                    saved = json.loads(row["label_json"]) if row["label_json"] else None
                    auto = None if saved else resolver(json.loads(row["commit_json"]), label_catalog)
                    db.execute("""UPDATE candidates SET effective_label_code=?
                        WHERE candidate_id=? AND version_id=?""",
                        ((saved or auto or {}).get("code"), row["candidate_id"], row["version_id"]))
                last_id, last_version = batch[-1]["candidate_id"], batch[-1]["version_id"]
            db.execute("""INSERT INTO catalog_meta VALUES ('label_fingerprint', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (fingerprint,))

    def claim_paused(self, source_dataset: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT paused FROM claim_pauses WHERE source_dataset=?",
                             (source_dataset,)).fetchone()
        return bool(row[0]) if row else False

    def set_claim_paused(self, source_dataset: str, paused: bool, actor: str) -> None:
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM candidates WHERE source_dataset=? LIMIT 1",
                              (source_dataset,)).fetchone():
                raise KeyError(source_dataset)
            db.execute("""INSERT INTO claim_pauses VALUES (?, ?, ?, ?)
                ON CONFLICT(source_dataset) DO UPDATE SET paused=excluded.paused,
                changed_by=excluded.changed_by, changed_at_utc=excluded.changed_at_utc""",
                (source_dataset, int(paused), actor,
                 time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))

    def set_review(self, candidate_id: str, version_id: str, pointer: dict,
                   *, reset_by: str | None = None) -> None:
        with self._connect() as db:
            db.execute("UPDATE candidates SET decision=?, revision=?, review_json=? "
                       "WHERE candidate_id=? AND version_id=?",
                       (pointer["decision"], pointer["revision"], json.dumps(pointer),
                        candidate_id, version_id))
            db.execute("DELETE FROM leases WHERE candidate_id=? AND version_id=?",
                       (candidate_id, version_id))
            if reset_by:
                db.execute("INSERT OR REPLACE INTO skipped VALUES (?, ?, ?, ?)",
                           (candidate_id, version_id, reset_by, time.time() + 3600))

    def claim_reviewed(self, actor: str, candidate_id: str, version_id: str,
                       expected_revision: int) -> str:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM leases WHERE expires_at<?", (now,))
            row = db.execute(
                "SELECT decision, revision FROM candidates WHERE candidate_id=? AND version_id=?",
                (candidate_id, version_id)).fetchone()
            if row is None or row["decision"] not in ("pass", "reject") \
                    or row["revision"] != expected_revision:
                raise ObjectConflictError("审核结果已变化，请刷新后重试")
            lease = db.execute(
                "SELECT actor, token FROM leases WHERE candidate_id=? AND version_id=?",
                (candidate_id, version_id)).fetchone()
            if lease:
                if lease["actor"] != actor:
                    raise ObjectConflictError("该复审任务已由其他人领取")
                db.execute("UPDATE leases SET expires_at=? WHERE candidate_id=? AND version_id=?",
                           (now + 900, candidate_id, version_id))
                return lease["token"]
            paused = db.execute("""SELECT 1 FROM candidates AS c JOIN claim_pauses AS p
                ON p.source_dataset=c.source_dataset WHERE c.candidate_id=?
                AND c.version_id=? AND p.paused=1""", (candidate_id, version_id)).fetchone()
            if paused:
                raise ObjectConflictError("该来源已暂停新领取")
            token = uuid4().hex
            db.execute("INSERT INTO leases VALUES (?, ?, ?, ?, ?)",
                       (candidate_id, version_id, actor, token, now + 900))
            return token

    def set_label(self, candidate_id: str, version_id: str, label: dict) -> None:
        with self._connect() as db:
            db.execute("""UPDATE candidates SET label_json=?, effective_label_code=?
                WHERE candidate_id=? AND version_id=?""",
                (json.dumps(label), label.get("code"), candidate_id, version_id))

    def claim(self, actor: str, *, batch_size: int = 5, search: str = "",
              high_risk: bool = False) -> list[dict]:
        now = time.time()
        extra = ""
        params: list[object] = [actor]
        filter_params: list[object] = []
        if search:
            extra += " AND c.candidate_id || '/' || c.version_id IN "
            extra += "(SELECT item_key FROM candidate_search WHERE candidate_search MATCH ?)"
            filter_params.append(self._fts_query(search))
            params.extend(filter_params)
        if high_risk:
            extra += " AND json_extract(c.commit_json, '$.risk_tier')='high'"
        pause_filter = (" AND NOT EXISTS (SELECT 1 FROM claim_pauses AS p "
                        "WHERE p.source_dataset=c.source_dataset AND p.paused=1)")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM leases WHERE expires_at<?", (now,))
            db.execute("DELETE FROM skipped WHERE expires_at<?", (now,))
            existing = db.execute("""
                SELECT l.candidate_id, l.version_id, l.token FROM leases AS l
                JOIN candidates AS c ON c.candidate_id=l.candidate_id
                    AND c.version_id=l.version_id
                WHERE l.actor=? AND c.decision='unreviewed'
            """ + extra + " ORDER BY CASE WHEN c.revision>0 THEN 0 ELSE 1 END, "
            "json_extract(c.review_json, '$.returned_at_utc') DESC, "
            "c.published_at_utc, c.candidate_id LIMIT ?",
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
            """ + extra + pause_filter + " ORDER BY CASE WHEN c.revision>0 THEN 0 ELSE 1 END, "
            "json_extract(c.review_json, '$.returned_at_utc') DESC, "
            "c.published_at_utc, c.candidate_id LIMIT ?",
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
