"""Paged bootstrap/daily reconciliation plus incremental immutable publication receipts."""

import json
import time

from .motion_annotations import project_motion, require, validate_motion
from .synthetic_temporal_contract import document_sha256


class MotionLabelDiscovery:
    def __init__(self, service):
        self.service, self.store, self.root = service, service.store, service.root
        with service.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS discovery_meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS discovery_errors(
                    key TEXT PRIMARY KEY,error TEXT,attempts INTEGER,updated REAL);
            """)

    def meta(self, key, default=""):
        with self.service.db() as db:
            row = db.execute("SELECT value FROM discovery_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        with self.service.db() as db:
            db.execute("INSERT OR REPLACE INTO discovery_meta VALUES(?,?)", (key, str(value)))

    def page(self, category, after, limit):
        prefix = f"{self.root}/{category}/"
        return self.store.list_page(prefix, after or None, limit)

    def index(self, key, expected=None, generation=None):
        with self.service.db() as db:
            old = db.execute("SELECT generation,value FROM objects WHERE key=?", (key,)).fetchone()
        if old and (generation is None or old["generation"] == generation):
            if expected:
                require(
                    document_sha256(json.loads(old["value"])) == expected,
                    "Discovery receipt hash differs",
                )
            return 0
        value, generation = self.store.read_json(key)
        if expected:
            require(document_sha256(value) == expected, "Discovery receipt hash differs")
        if old:
            require(json.loads(old["value"]) == value, "Immutable label manifest changed")
        if key.startswith(self.root + "/feature-manifests/"):
            require(value["schema"] == "imu.motion_features.v1", "Invalid feature manifest")
            validate_motion(value["template"])
            mid = value["binding"]["motion_sha256"]
            require(
                value["template"]["motion_sha256"] == mid
                and value["feature_id"] == document_sha256(value["binding"])
                and key == f"{self.root}/feature-manifests/{value['feature_id']}.json"
                and value["object_key"] == f"{self.root}/features/{value['feature_id']}.npz",
                "Feature binding differs",
            )
            with self.service.db() as db:
                prior = db.execute("SELECT feature FROM motions WHERE id=?", (mid,)).fetchone()
                require(
                    not prior or json.loads(prior[0])["feature_id"] == value["feature_id"],
                    "Motion feature recipe changed; migrate explicitly",
                )
                db.execute(
                    "INSERT OR IGNORE INTO motions(id,feature,dataset,member,updated) "
                    "VALUES(?,?,?,?,?)",
                    (
                        mid,
                        json.dumps(value),
                        value["source_dataset"],
                        value["source_member"],
                        str(time.time()),
                    ),
                )
        elif key.startswith(self.root + "/variants/"):
            require(value["schema"] == "imu.motion_variant.v1", "Invalid motion variant")
            c = value["candidate"]
            require(
                key == f"{self.root}/variants/{c['candidate_id']}-{c['version_id']}.json",
                "Invalid variant key",
            )
            commit = self.service.source._commit(c["candidate_id"], c["version_id"])
            require(
                c["commit_sha256"] == document_sha256(commit)
                and c["input_hashes"] == {o["role"]: o["sha256"] for o in commit["objects"]}
                and c["input_hashes"]["motion"] == value["motion_sha256"]
                and value["policy_sha256"] == commit["policy_sha256"]
                and value["configuration_id"] == document_sha256(value["configuration"]),
                "Variant is not bound to a published machine-QA candidate",
            )
            feature = self.service.feature(value["motion_sha256"])
            require(feature["feature_id"] == value["feature_id"], "Variant feature differs")
            project_motion(feature["template"], c, value["clock"])
            with self.service.db() as db:
                db.execute(
                    "INSERT OR IGNORE INTO variants VALUES(?,?,?,?)",
                    (
                        c["candidate_id"] + "/" + c["version_id"],
                        value["motion_sha256"],
                        value["configuration_id"],
                        json.dumps(value),
                    ),
                )
            self.service.refresh_summary(
                value["motion_sha256"], self.service.state(value["motion_sha256"])[0]
            )
        else:
            raise ValueError("Unexpected label discovery namespace")
        with self.service.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO objects VALUES(?,?,?)", (key, generation, json.dumps(value))
            )
        return 1

    def consume(self, key, generation=None):
        if not key.startswith(self.root + "/index-feed/"):
            return self.index(key, generation=generation)
        value, _ = self.store.read_json(key)
        require(
            value["schema"] == "imu.motion_label_feed.v1" and value["feed_key"] == key,
            "Invalid label receipt",
        )
        count = 0
        for category, field in (("feature-manifests", "feature"), ("variants", "variant")):
            ref = value[field]
            require(ref["key"].startswith(f"{self.root}/{category}/"), "Receipt escapes namespace")
            count += self.index(ref["key"], ref["sha256"])
        return count

    def attempt(self, key, generation=None):
        try:
            count = self.consume(key, generation)
        except Exception as error:
            if getattr(error, "code", None) in (401, 403):
                raise
            with self.service.db() as db:
                db.execute(
                    "INSERT INTO discovery_errors VALUES(?,?,1,?) ON CONFLICT(key) "
                    "DO UPDATE SET error=excluded.error,attempts=attempts+1,"
                    "updated=excluded.updated",
                    (key, str(error), time.time()),
                )
            return 0
        with self.service.db() as db:
            db.execute("DELETE FROM discovery_errors WHERE key=?", (key,))
        return count

    def refresh(self, limit):
        count = 0
        with self.service.db() as db:
            failed = db.execute(
                "SELECT key FROM discovery_errors WHERE attempts<5 "
                "AND updated<? ORDER BY updated LIMIT 8",
                (time.time() - 30,),
            ).fetchall()
        for row in failed:
            count += self.attempt(row["key"])
        feed = self.page("index-feed", self.meta("feed_cursor"), limit)
        for item in feed:
            count += self.attempt(item.key, item.generation)
            self.set_meta("feed_cursor", item.key)
        if time.time() - float(self.meta("reconciled_at", "0")) < 86400:
            return count
        # Separate cursors keep variant discovery fair even during a large bootstrap.
        for category in ("feature-manifests", "variants"):
            cursor = self.meta("reconcile_" + category)
            if cursor == "done":
                continue
            page = self.page(category, cursor, max(1, limit // 2))
            for item in page:
                count += self.attempt(item.key, item.generation)
                self.set_meta("reconcile_" + category, item.key)
            if len(page) < max(1, limit // 2):
                self.set_meta("reconcile_" + category, "done")
        if all(self.meta("reconcile_" + c) == "done" for c in ("feature-manifests", "variants")):
            self.set_meta("reconciled_at", time.time())
            for category in ("feature-manifests", "variants"):
                self.set_meta("reconcile_" + category, "")
        return count
