"""Production motion review: one human revision, many independently checked IMUs."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import numpy as np

from .motion_annotations import (
    POLICY,
    REVIEW_SCHEMA,
    accepted_projection,
    apply_projection,
    covered,
    normalize_recording,
    primary_prediction,
    project_motion,
    require,
    reviewed_ranges,
    usable_ns,
)
from .storage import ObjectConflictError
from .synthetic_temporal_contract import (
    SHA256,
    AnnotationIssues,
    activity_rules_evidence,
    current_document,
    standing_document,
    STANDING_TAXONOMY_VERSION,
    POSTURE_TAXONOMIES,
    derive_transition_endpoints,
    document_sha256,
    duration_submission_issues,
    standing_editor_document,
    editor_document,
    formal_labels,
    partial_fall_events,
    validate_document,
    walking_bouts,
)


def now():
    return datetime.now(UTC).isoformat()


class MotionLabelService:
    def __init__(self, source, catalog_path=None, *, review_taxonomy_version=STANDING_TAXONOMY_VERSION):
        require(review_taxonomy_version in POSTURE_TAXONOMIES, "Unsupported review taxonomy")
        self.review_taxonomy_version = review_taxonomy_version
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

    def editor_document(self, document):
        return (standing_editor_document(document) if self.review_taxonomy_version == STANDING_TAXONOMY_VERSION else editor_document(document))

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
            from .motion_label_discovery import MotionLabelDiscovery

            count = MotionLabelDiscovery(self).refresh(limit)
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

    def project_prediction(self, prediction, variant):
        """Recheck duration after a candidate crop, using bound sequence evidence."""
        document = prediction["document"]
        quality = prediction.get("quality") or {}
        metadata = quality.get("duration_projection")
        clock = variant["clock"]
        if metadata and (
            clock["source_start_ns"] or clock["sample_count"] != document["clock"]["sample_count"]
        ):
            from .semantic_runtime import finalize_segments

            key = quality["sequence_scores_key"]
            require(
                key.startswith(self.root + "/sequence-manifests/"),
                "Sequence reference escapes namespace",
            )
            descriptor = self.store.read_json(key)[0]
            require(
                descriptor["object_key"].startswith(self.root + "/sequences/"),
                "Sequence payload escapes namespace",
            )
            payload = self.store.read_bytes(descriptor["object_key"])
            require(
                len(payload) == descriptor["byte_length"]
                and hashlib.sha256(payload).hexdigest() == descriptor["sha256"],
                "Sequence payload hash differs",
            )
            with np.load(io.BytesIO(payload), allow_pickle=False) as archive:
                decoded = {k: archive[k] for k in archive.files}
            start = clock["source_start_ns"] // 40_000_000
            stop = start + clock["sample_count"]
            final = finalize_segments(decoded, metadata, start=start, stop=stop)
            # Retain known impact timestamps if the reconstructed event contains them.
            values = final["class_indices"]
            edges = np.r_[0, np.flatnonzero(np.diff(values)) + 1, len(values)]
            impacts = {}
            for a, b in zip(edges[:-1], edges[1:], strict=True):
                if values[a] < 0 or metadata["labels"][values[a]] != "fall":
                    continue
                points = [
                    e["impact_ns"] / 40_000_000
                    for e in document["fall_events"]
                    if e["impact_ns"] is not None and a < e["impact_ns"] / 40_000_000 < b
                ]
                if len(points) == 1:
                    impacts[int(a)] = points[0]
            document = primary_prediction(
                document,
                metadata["labels"],
                values,
                document["producer"]["model_id"],
                impacts=impacts,
            )
        return project_motion(document, variant["candidate"], clock)

    def edit(self, mid, cid, vid, body, actor, *, decision=None):
        target = body.get("document", {}).get("taxonomy", {}).get("version")
        if target not in POSTURE_TAXONOMIES or body.get("activity_rules_version") != activity_rules_evidence(target)["version"]:
            raise ObjectConflictError("分类规则已更新；本机修改保持暂存，请刷新页面后按新规则检查。")
        variant = self.variant(mid, cid, vid)
        require(variant is not None, "Unknown IMU variant")
        state, generation = self.state(mid)
        self.check_edit(state, body, actor)
        edited = body["document"]
        validate_document(edited, expected_candidate=variant["candidate"])
        if decision != "reject" and (issues := duration_submission_issues(edited)):
            raise AnnotationIssues(issues)
        require(edited["clock"] == variant["clock"], "Edited clock differs")
        # The server owns provenance and producer identity, not arbitrary editor input.
        edited = derive_transition_endpoints(edited)
        base = self.base_document(mid, state)
        base = standing_document(base) if target == STANDING_TAXONOMY_VERSION else current_document(base)
        edited["source_evidence"] = deepcopy(base["source_evidence"])
        rules = activity_rules_evidence(edited["taxonomy"]["version"])
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
        if prediction and not state["draft"] and not state["accepted"]:
            document = self.project_prediction(prediction, variant)
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
            "editor_template": self.editor_document(document),
            "walking_bouts": walking_bouts(document),
            "prediction_quality": None,
            "suggestion_quality": None,
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
            if prediction.get("quality"):
                start = variant["clock"]["source_start_ns"]
                stop = start + variant["clock"]["duration_ns"]
                quality = prediction["quality"]
                projected_quality = {
                    **{
                        k: v
                        for k, v in quality.items()
                        if k not in ("intervals", "high_confidence_document")
                    },
                    "intervals": [
                        {
                            **s,
                            "start_ns": max(start, s["start_ns"]) - start,
                            "stop_ns": min(stop, s["stop_ns"]) - start,
                        }
                        for s in quality["intervals"]
                        if s["start_ns"] < stop and s["stop_ns"] > start
                    ],
                }
                result["suggestion_quality"] = projected_quality
                if not state["draft"] and not state["accepted"]:
                    result["prediction_quality"] = projected_quality
            suggested = self.project_prediction(prediction, variant)
            if prediction.get("quality", {}).get("duration_projection"):
                # Cropping may adjust boundaries: stale confidence/evidence must
                # not be attached to a different interval just by overlap.
                current_shapes = {
                    (s["start_ns"], s["stop_ns"], s["primary_code"]) for s in suggested["segments"]
                }
                for key in ("prediction_quality", "suggestion_quality"):
                    if result.get(key):
                        result[key]["intervals"] = [
                            s
                            for s in result[key]["intervals"]
                            if (s["start_ns"], s["stop_ns"], s["primary_code"]) in current_shapes
                        ]

            def shape(d):
                return [
                    (s["start_ns"], s["stop_ns"], s["primary_code"], s["resolution"])
                    for s in d["segments"]
                ]

            result["has_suggestion"] = bool(
                (review or state["draft"]) and shape(suggested) != shape(document)
            )
            result["suggestion"] = self.editor_document(suggested) if result["has_suggestion"] else None
        migrations = [
            e
            for e in result["editor_template"]["source_evidence"]
            if e.get("kind") == "posture-taxonomy-migration"
        ]
        result["migration"] = migrations[-1] if migrations else None
        result["needs_rule_review"] = bool(
            review
            and review["document"]["taxonomy"]["version"] not in POSTURE_TAXONOMIES
            and migrations
            and migrations[-1]["review_required"]
        )
        if accepted and review["document"]["taxonomy"]["version"] in POSTURE_TAXONOMIES:
            frozen = project_motion(review["document"], variant["candidate"], variant["clock"])
            result["needs_rule_review"] = any(
                old.get("transition") != new.get("transition")
                for old, new in zip(
                    frozen["segments"], derive_transition_endpoints(frozen)["segments"], strict=True
                )
            )
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
        entries, seen_versions, exclusions = [], set(), []
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
            doc = accepted_projection(
                review, c, variant["clock"],
                taxonomy_version=STANDING_TAXONOMY_VERSION
                if self.review_taxonomy_version == STANDING_TAXONOMY_VERSION
                and review["document"]["taxonomy"]["version"] in POSTURE_TAXONOMIES
                else None,
            )
            migrated = current_document(doc)
            pending = [
                e
                for e in migrated["source_evidence"]
                if e.get("kind") == "posture-taxonomy-migration"
            ]
            if partial_fall_events(doc) or (
                doc["taxonomy"]["version"] not in POSTURE_TAXONOMIES
                and pending
                and pending[-1]["review_required"]
            ):
                exclusions.append(
                    {
                        "motion_sha256": variant["motion_sha256"],
                        "reason": "partial_fall"
                        if partial_fall_events(doc)
                        else "taxonomy_review_required",
                    }
                )
                continue
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
            "supervision_exclusions": exclusions,
        }
        old, _ = self.optional(key)
        self.immutable(key, old or intent)
        return {"snapshot_id": snapshot_id, "request_key": key, "candidate_count": len(entries)}
