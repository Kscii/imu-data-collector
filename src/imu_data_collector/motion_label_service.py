"""Production motion review: one human revision, many independently checked IMUs."""

from __future__ import annotations

import fcntl
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .motion_annotations import (
    POLICY,
    REVIEW_SCHEMA,
    accepted_projection,
    apply_projection,
    covered,
    normalize_recording,
    project_motion,
    require,
    reviewed_ranges,
    usable_ns,
    validate_motion,
)
from .storage import ObjectConflictError
from .synthetic_temporal_contract import (
    ACTIVITY_RULES_VERSION,
    SHA256,
    activity_rules_evidence,
    document_sha256,
    formal_labels,
    validate_document,
)


def now():
    return datetime.now(UTC).isoformat()


class MotionLabelService:
    def __init__(self, source, catalog_path=None):
        self.source, self.store = source, source.store
        self.root = source.prefix + "/motion-labels"
        if catalog_path is None:
            catalog_path = (
                source.catalog.path.with_name("motion-labels.sqlite3")
                if source.catalog
                else Path(getattr(source.store, "root", "/tmp")) / "motion-labels.sqlite3"
            )
        self.path = Path(catalog_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock, self.last_scan = threading.Lock(), 0.0
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS objects(
                    key TEXT PRIMARY KEY, generation INTEGER, value TEXT);
                CREATE TABLE IF NOT EXISTS motions(
                    id TEXT PRIMARY KEY, feature TEXT, status TEXT DEFAULT 'inference_pending',
                    dataset TEXT, member TEXT, updated TEXT);
                CREATE TABLE IF NOT EXISTS variants(
                    id TEXT PRIMARY KEY, motion TEXT, config TEXT, value TEXT);
                CREATE INDEX IF NOT EXISTS variants_motion ON variants(motion);
                CREATE INDEX IF NOT EXISTS motion_queue ON motions(status, updated);
            """)
            if "accepted_usable_ns" not in {r[1] for r in db.execute("PRAGMA table_info(motions)")}:
                db.execute(
                    "ALTER TABLE motions ADD COLUMN accepted_usable_ns INTEGER NOT NULL DEFAULT 0"
                )

    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def publication_lock(self):
        """Serialize activation/publication across API and the single local worker."""
        with self.path.with_suffix(".publication.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def optional(self, key):
        try:
            return self.store.read_json(key)
        except FileNotFoundError:
            return None, 0

    def immutable(self, key, value):
        try:
            self.store.write_json(key, value, if_generation_match=0)
        except ObjectConflictError:
            require(self.store.read_json(key)[0] == value, "Immutable motion object differs")

    def reference(self, category, value):
        digest = document_sha256(value)
        key = f"{self.root}/{category}/{digest}.json"
        self.immutable(key, value)
        return {"revision_key": key, "revision_sha256": digest}

    def resolve(self, reference):
        if not reference:
            return None
        require(
            reference["revision_key"].startswith(self.root + "/"),
            "Reference escapes motion namespace",
        )
        value = self.store.read_json(reference["revision_key"])[0]
        require(
            document_sha256(value) == reference["revision_sha256"], "Motion revision hash differs"
        )
        return value

    def refresh(self, *, force=False, limit=256):
        """Bounded discovery; large backfills run in the worker, not HTTP requests."""
        if not self.lock.acquire(blocking=False):
            return 0
        try:
            if not force and time.monotonic() - self.last_scan < 30:
                return 0
            count = 0
            changed = set()
            with self.db() as db:
                known = {
                    r["key"]: r["generation"]
                    for r in db.execute("SELECT key,generation FROM objects")
                }
                for category in ("feature-manifests", "variants"):
                    for item in self.store.list(f"{self.root}/{category}/"):
                        if known.get(item.key) == item.generation:
                            continue
                        value, generation = self.store.read_json(item.key)
                        if category == "feature-manifests":
                            require(
                                value["schema"] == "imu.motion_features.v1",
                                "Invalid feature manifest",
                            )
                            validate_motion(value["template"])
                            mid = value["binding"]["motion_sha256"]
                            require(
                                value["template"]["motion_sha256"] == mid
                                and value["feature_id"] == document_sha256(value["binding"])
                                and value["object_key"]
                                == f"{self.root}/features/{value['feature_id']}.npz",
                                "Feature binding differs",
                            )
                            old = db.execute(
                                "SELECT feature FROM motions WHERE id=?", (mid,)
                            ).fetchone()
                            if old:
                                prior = json.loads(old[0])
                                # A changed geometry recipe needs a new model-compatible cache;
                                # never silently let object-list order select one.
                                require(
                                    prior["feature_id"] == value["feature_id"],
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
                                    now(),
                                ),
                            )
                        else:
                            require(
                                value["schema"] == "imu.motion_variant.v1", "Invalid motion variant"
                            )
                            c = value["candidate"]
                            commit = self.source._commit(c["candidate_id"], c["version_id"])
                            require(
                                c["commit_sha256"] == document_sha256(commit)
                                and c["input_hashes"]
                                == {o["role"]: o["sha256"] for o in commit["objects"]}
                                and c["input_hashes"]["motion"] == value["motion_sha256"]
                                and value["policy_sha256"] == commit["policy_sha256"]
                                and value["configuration_id"]
                                == document_sha256(value["configuration"]),
                                "Variant is not bound to a published machine-QA candidate",
                            )
                            feature = db.execute(
                                "SELECT feature FROM motions WHERE id=?", (value["motion_sha256"],)
                            ).fetchone()
                            if not feature:
                                continue
                            project_motion(json.loads(feature[0])["template"], c, value["clock"])
                            db.execute(
                                "INSERT OR IGNORE INTO variants VALUES(?,?,?,?)",
                                (
                                    c["candidate_id"] + "/" + c["version_id"],
                                    value["motion_sha256"],
                                    value["configuration_id"],
                                    json.dumps(value),
                                ),
                            )
                            changed.add(value["motion_sha256"])
                        db.execute(
                            "INSERT OR REPLACE INTO objects VALUES(?,?,?)",
                            (item.key, generation, json.dumps(value)),
                        )
                        count += 1
                        if count >= limit:
                            break
                    if count >= limit:
                        break
            for mid in changed:
                self.refresh_summary(mid, self.state(mid)[0])
            self.last_scan = time.monotonic()
            return count
        finally:
            self.lock.release()

    def feature(self, mid):
        require(isinstance(mid, str) and SHA256.fullmatch(mid), "Invalid motion identity")
        with self.db() as db:
            row = db.execute("SELECT feature FROM motions WHERE id=?", (mid,)).fetchone()
        if row is None:
            raise FileNotFoundError(mid)
        return json.loads(row[0])

    def holdout_groups(self):
        groups = set()
        for item in self.store.list(self.root + "/holdouts/"):
            value = self.store.read_json(item.key)[0]
            release, _ = self.optional(self.root + "/holdout-releases/" + value["run_id"] + ".json")
            if not release:
                groups.update(value["group_keys"])
        return groups

    def check_holdout(self, mid):
        require(
            self.feature(mid).get("group_key") not in self.holdout_groups(),
            "这条动捕属于独立验证组；请先在管理员实验页完成盲标参考",
        )

    def variants(self, mid):
        with self.db() as db:
            rows = db.execute(
                "SELECT value FROM variants WHERE motion=? ORDER BY id", (mid,)
            ).fetchall()
        return sorted(
            (json.loads(r[0]) for r in rows),
            key=lambda v: (
                v["configuration"]["layout_id"] != "chest-1",
                v["configuration"]["variant"] != "ideal",
                -v["clock"]["duration_ns"],
                v["configuration_id"],
                -datetime.fromisoformat(
                    v.get("published_at_utc", "1970-01-01T00:00:00+00:00")
                ).timestamp(),
                v["candidate"]["version_id"],
            ),
        )

    def variant(self, mid, cid, vid):
        return next(
            (
                v
                for v in self.variants(mid)
                if v["candidate"]["candidate_id"] == cid and v["candidate"]["version_id"] == vid
            ),
            None,
        )

    def state(self, mid):
        self.feature(mid)
        value, generation = self.optional(f"{self.root}/states/{mid}.json")
        return value or {
            "draft": None,
            "accepted": None,
            "prediction": None,
            "lease": None,
            "variant_rejections": {},
        }, generation

    def write_state(self, mid, state, generation):
        self.store.write_json(
            f"{self.root}/states/{mid}.json", state, if_generation_match=generation
        )
        self.refresh_summary(mid, state)

    def refresh_summary(self, mid, state):
        review = self.resolve(state["accepted"])
        variants = self.variants(mid)
        variant = variants[0] if variants else None
        variant_rejected = (
            variant
            and (variant["candidate"]["candidate_id"] + "/" + variant["candidate"]["version_id"])
            in state["variant_rejections"]
        )
        status = (
            "rejected"
            if variant_rejected or (review and review["decision"] == "reject")
            else "accepted"
            if variant and covered(review, variant["clock"])
            else "pending_review"
            if state["prediction"] or state["draft"]
            else "inference_pending"
        )
        accepted_usable = (
            usable_ns(project_motion(review["document"], variant["candidate"], variant["clock"]))
            if status == "accepted"
            else 0
        )
        with self.db() as db:
            db.execute(
                "UPDATE motions SET status=?,updated=?,accepted_usable_ns=? WHERE id=?",
                (status, now(), accepted_usable, mid),
            )

    def claim(self, mid, actor, token=None):
        self.check_holdout(mid)
        state, generation = self.state(mid)
        lease = state["lease"]
        if lease and lease["expires_at"] > time.time():
            if lease["actor"] != actor or (token and token != lease["token"]):
                raise ObjectConflictError("这条动捕正在其他审核窗口中编辑")
            if token is None:
                raise ObjectConflictError("已有编辑会话，请回到原窗口或等待租约释放")
        else:
            self.clear_editor_protection(state)
        state["lease"] = {
            "actor": actor,
            "token": token or uuid4().hex,
            "expires_at": time.time() + 180,
        }
        self.write_state(mid, state, generation)
        return state["lease"]

    def release(self, mid, actor, token):
        state, generation = self.state(mid)
        if state["lease"] and state["lease"]["actor"] == actor and state["lease"]["token"] == token:
            state["lease"] = None
            self.clear_editor_protection(state)
            self.write_state(mid, state, generation)
        return {"released": True}

    def clear_editor_protection(self, state):
        if (
            state["draft"]
            and self.resolve(state["draft"]).get("author") == "system-edit-protection"
        ):
            state["draft"] = None

    def check_edit(self, state, body, actor):
        lease = state["lease"]
        if (
            not lease
            or lease["actor"] != actor
            or lease["token"] != body.get("lease_token")
            or lease["expires_at"] <= time.time()
        ):
            raise ObjectConflictError("编辑租约已失效；草稿保留，请重新领取")
        for field in ("draft", "accepted"):
            actual = state[field]["revision_sha256"] if state[field] else None
            expected = body.get(
                "expected_draft_sha256" if field == "draft" else "expected_review_sha256"
            )
            if (
                field == "draft"
                and expected is None
                and state["draft"]
                and self.resolve(state["draft"]).get("author") == "system-edit-protection"
            ):
                # This revision only freezes the document already shown to the
                # lease holder; it is not a competing human edit.
                continue
            if actual != expected:
                raise ObjectConflictError("标注已被修改；请保留当前草稿并重新加载")

    def base_document(self, mid, state):
        if state["draft"]:
            return self.resolve(state["draft"])["document"]
        if state["accepted"]:
            return self.resolve(state["accepted"])["document"]
        if state["prediction"]:
            return self.resolve(state["prediction"])["document"]
        return self.feature(mid)["template"]

    def edit(self, mid, cid, vid, body, actor, *, decision=None):
        if body.get("activity_rules_version") != ACTIVITY_RULES_VERSION:
            raise ObjectConflictError("分类规则已更新；请保存本地草稿并刷新页面后按新规则检查。")
        variant = self.variant(mid, cid, vid)
        require(variant is not None, "Unknown IMU variant")
        state, generation = self.state(mid)
        self.check_edit(state, body, actor)
        edited = body["document"]
        validate_document(edited, expected_candidate=variant["candidate"])
        require(edited["clock"] == variant["clock"], "Edited clock differs")
        # The server owns provenance and producer identity, not arbitrary editor input.
        edited = deepcopy(edited)
        base = self.base_document(mid, state)
        edited["source_evidence"] = deepcopy(base["source_evidence"])
        rules = activity_rules_evidence()
        if rules not in edited["source_evidence"]:
            edited["source_evidence"].append(rules)
        edited["producer"] = {"kind": "human-production-draft", "reviewer": actor}
        draft_base = base
        preserve_unseen_draft = bool(
            state["draft"]
            and self.resolve(state["draft"]).get("author") != "system-edit-protection"
        )
        if decision == "pass":
            edited = normalize_recording(edited)
            # Only the displayed range was reviewed. Unseen draft changes must
            # not replace previously accepted semantics outside that range.
            prior = self.resolve(state["accepted"])
            base = (
                prior["document"]
                if prior and prior["decision"] == "pass"
                else self.feature(mid)["template"]
            )
        document = apply_projection(base, edited)
        revision = {
            "schema": "imu.motion_draft.v1",
            "motion_sha256": mid,
            "document": document,
            "author": actor,
            "activity_rules": rules,
            "activity_rules_range": {
                "start_ns": variant["clock"]["source_start_ns"],
                "stop_ns": variant["clock"]["source_start_ns"] + variant["clock"]["duration_ns"],
            },
            "created_at_utc": now(),
            "previous": state["draft"],
        }
        if decision is None:
            state["draft"] = self.reference("drafts", revision)
        else:
            require(decision in ("pass", "reject"), "Invalid recording decision")
            scope = body.get("scope", "motion")
            require(scope in ("motion", "variant"), "Invalid rejection scope")
            reason = str(body.get("reason", "")).strip()
            require(decision != "reject" or reason, "拒绝时请填写质量问题")
            variant_key = cid + "/" + vid
            if decision == "reject" and scope == "variant":
                state["variant_rejections"][variant_key] = self.reference(
                    "variant-rejections",
                    {
                        "candidate": variant["candidate"],
                        "reviewer": actor,
                        "reason": reason,
                        "created_at_utc": now(),
                    },
                )
            else:
                prior = self.resolve(state["accepted"])
                ranges = prior["reviewed_ranges"] if prior and prior["decision"] == "pass" else []
                start = variant["clock"]["source_start_ns"]
                review = {
                    "schema": REVIEW_SCHEMA,
                    "motion_sha256": mid,
                    "document": document,
                    "activity_rules": rules,
                    "activity_rules_range": revision["activity_rules_range"],
                    "decision": decision,
                    "reviewer": actor,
                    "reason": reason,
                    "created_at_utc": now(),
                    "previous": state["accepted"],
                    "reviewed_variant": variant,
                    "inheritance_policy": POLICY,
                    "reviewed_ranges": reviewed_ranges(
                        ranges, start, start + variant["clock"]["duration_ns"]
                    ),
                }
                state["accepted"] = self.reference("reviews", review)
                state["draft"] = None
                if decision == "pass":
                    state["variant_rejections"].pop(variant_key, None)
                    remaining = apply_projection(draft_base, edited)
                    if preserve_unseen_draft and any(
                        remaining[k] != document[k] for k in ("segments", "fall_events")
                    ):
                        state["draft"] = self.reference(
                            "drafts", {**revision, "document": remaining}
                        )
        self.write_state(mid, state, generation)
        return self.case(mid, variant)

    def case(self, mid, variant=None, *, summary=False):
        self.check_holdout(mid)
        feature = self.feature(mid)
        variants = self.variants(mid)
        require(bool(variants), "Motion has no published IMU variant")
        variant = variant or variants[0]
        state, _ = self.state(mid)
        review, prediction = self.resolve(state["accepted"]), self.resolve(state["prediction"])
        rejected = (
            variant["candidate"]["candidate_id"] + "/" + variant["candidate"]["version_id"]
            in state["variant_rejections"]
        )
        accepted = covered(review, variant["clock"])
        status = (
            "rejected"
            if rejected or (review and review["decision"] == "reject")
            else "accepted"
            if accepted
            else "pending_review"
            if state["draft"] or prediction
            else "inference_pending"
        )
        base = self.base_document(mid, state)
        document = project_motion(base, variant["candidate"], variant["clock"])
        result = {
            "motion_sha256": mid,
            "variants": variants,
            "status": status,
            "entry": {
                "candidate": variant["candidate"],
                "clock": variant["clock"],
                "source_dataset": feature["source_dataset"],
                "source_member": feature["source_member"],
                "reference_role": "production",
            },
            "review": state["accepted"],
            "draft": ({**state["draft"], "document": document} if state["draft"] else None),
            "proposal": {"document": document},
            "proposal_sha256": document_sha256(document),
            "editor_template": document,
            "usable_ns": usable_ns(document),
            "total_ns": variant["clock"]["duration_ns"],
            "legacy_quality": self.source.latest(
                variant["candidate"]["candidate_id"], variant["candidate"]["version_id"]
            ),
            "has_suggestion": False,
            "suggestion": None,
        }
        if result["legacy_quality"]:
            pointer = result["legacy_quality"]
            legacy = self.store.read_json(pointer["revision_key"])[0]
            require(
                document_sha256(legacy) == pointer["revision_sha256"], "Legacy review hash differs"
            )
            result["legacy_quality"] = {
                k: legacy.get(k) for k in ("decision", "reason", "reviewer")
            }
        if prediction:
            suggested = project_motion(
                prediction["document"], variant["candidate"], variant["clock"]
            )

            def shape(d):
                return [
                    (s["start_ns"], s["stop_ns"], s["primary_code"], s["resolution"])
                    for s in d["segments"]
                ]

            result["has_suggestion"] = bool(
                (review or state["draft"]) and shape(suggested) != shape(document)
            )
            result["suggestion"] = suggested if result["has_suggestion"] else None
        return result

    def listing(self, *, offset=0, limit=30, status="all", search=""):
        require(0 <= offset and 1 <= limit <= 100, "Invalid pagination")
        require(
            status in ("all", "inference_pending", "pending_review", "accepted", "rejected"),
            "Invalid queue status",
        )
        clause, args = self.visible_clause()
        if status != "all":
            clause += " AND status=?"
            args.append(status)
        if search:
            clause += " AND (dataset LIKE ? OR member LIKE ?)"
            args += ["%" + search + "%"] * 2
        with self.db() as db:
            count = db.execute("SELECT count(*) FROM motions WHERE " + clause, args).fetchone()[0]
            rows = db.execute(
                "SELECT id,status FROM motions WHERE "
                + clause
                + " ORDER BY dataset,member,id LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
        cases = []
        for row in rows:
            variant = self.variants(row["id"])[0]
            cases.append(
                {
                    "motion_sha256": row["id"],
                    "status": row["status"],
                    "entry": {
                        "candidate": variant["candidate"],
                        "clock": variant["clock"],
                        "source_dataset": variant["source_dataset"],
                        "source_member": variant["source_member"],
                        "reference_role": "production",
                    },
                }
            )
        return {"cases": cases, "total": count, "offset": offset, "limit": limit}

    def visible_clause(self):
        clause, args = "EXISTS(SELECT 1 FROM variants v WHERE v.motion=motions.id)", []
        held_out = sorted(self.holdout_groups())
        if held_out:
            clause += (
                " AND COALESCE(json_extract(feature,'$.group_key'),'') NOT IN ("
                + ",".join("?" for _ in held_out)
                + ")"
            )
            args.extend(held_out)
        return clause, args

    def create_snapshot(self, actor, configuration_id=None):
        with self.db() as db:
            variants = [
                json.loads(r[0])
                for r in db.execute("SELECT value FROM variants ORDER BY motion,id")
            ]
        configurations = {v["configuration_id"]: v["configuration"] for v in variants}
        if configuration_id is None:
            defaults = [
                k
                for k, c in configurations.items()
                if c["layout_id"] == "chest-1" and c["variant"] == "ideal"
            ]
            require(len(defaults) == 1, "请明确选择一个 IMU 配置")
            configuration_id = defaults[0]
        require(configuration_id in configurations, "Unknown snapshot configuration")
        entries, seen_versions = [], set()
        holdouts = self.holdout_groups()
        for variant in sorted(
            variants,
            key=lambda v: (v.get("published_at_utc", ""), v["candidate"]["version_id"]),
            reverse=True,
        ):
            if variant["configuration_id"] != configuration_id:
                continue
            if self.feature(variant["motion_sha256"]).get("group_key") in holdouts:
                continue
            logical = (
                variant["motion_sha256"],
                variant["clock"]["source_start_ns"],
                variant["clock"]["duration_ns"],
            )
            if logical in seen_versions:
                continue
            seen_versions.add(logical)
            c = variant["candidate"]
            state, _ = self.state(variant["motion_sha256"])
            review = self.resolve(state["accepted"])
            if (
                not covered(review, variant["clock"])
                or c["candidate_id"] + "/" + c["version_id"] in state["variant_rejections"]
            ):
                continue
            doc = accepted_projection(review, c, variant["clock"])
            if not usable_ns(doc):
                continue
            commit = self.source._commit(c["candidate_id"], c["version_id"])
            require(document_sha256(commit) == c["commit_sha256"], "Frozen candidate changed")
            revision = {
                "schema": "imu.motion_projected_review.v1",
                "candidate_id": c["candidate_id"],
                "version_id": c["version_id"],
                "candidate_commit_sha256": c["commit_sha256"],
                "decision": "pass",
                "reviewer": review["reviewer"],
                "reason": "motion-review-inherited:" + state["accepted"]["revision_sha256"],
                "labels": formal_labels(doc),
                "temporal_annotation": doc,
                "temporal_annotation_sha256": document_sha256(doc),
                "motion_review": state["accepted"],
                "inheritance_policy": POLICY,
            }
            digest = document_sha256(revision)
            key = (
                f"{self.source.prefix}/reviews/{c['candidate_id']}/{c['version_id']}"
                f"/revisions/motion-{digest}.json"
            )
            self.immutable(key, revision)
            entries.append(
                {
                    "candidate_id": c["candidate_id"],
                    "version_id": c["version_id"],
                    "commit_key": self.source.commit_key(c["candidate_id"], c["version_id"]),
                    "commit_sha256": c["commit_sha256"],
                    "source_prefix": self.source.prefix,
                    "review_key": key,
                    "review_sha256": digest,
                }
            )
        require(bool(entries), "没有该配置下已通过且含可用区间的录制")
        snapshot_id = (
            "synthetic-"
            + document_sha256({"entries": entries, "configuration_id": configuration_id})[:32]
        )
        key = f"{self.source.prefix}/snapshots/requests/{snapshot_id}.json"
        intent = {
            "schema": "imu_motion_simulator.snapshot_intent.v1",
            "snapshot_id": snapshot_id,
            "entries": entries,
            "requested_by": actor,
            "created_at_utc": now(),
            "configuration_id": configuration_id,
            "motion_annotation_policy": POLICY,
        }
        old, _ = self.optional(key)
        self.immutable(key, old or intent)
        return {"snapshot_id": snapshot_id, "request_key": key, "candidate_count": len(entries)}
