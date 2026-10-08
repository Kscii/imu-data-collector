"""Isolated temporal annotation runs over existing immutable motion candidates."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .http_download import object_download_response
from .storage import ObjectConflictError
from .synthetic_temporal_contract import (
    IDENTIFIER,
    SEMANTIC_SCHEMA,
    SHA256,
    confirmed_spans,
    document_sha256,
    empty_document,
    formal_labels,
    normalize_semantic,
    project_annotations,
    run_prefix,
    run_root,
    semantic_draft,
    validate_document,
    validate_run,
)

INDEX = "synthetic-motion/temporal/run-index/"
REASONS = {
    "primary_class",
    "boundary",
    "missing_activity",
    "auxiliary",
    "missing_impact",
    "insufficient_evidence",
    "motion_quality",
}


def now():
    return datetime.now(UTC).isoformat()


class ReviewBody(BaseModel):
    decision: str
    proposal_sha256: str
    expected_review_sha256: str | None = None
    expected_draft_sha256: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    failed_segment_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    acknowledge_exclusions: bool = False
    corrected_document: dict | None = None
    review_seconds: float = Field(default=0, ge=0, le=86400)


def primary_run(run):
    return run["schema"] in ("imu.temporal_run.v2", "imu.temporal_run.v3")


def semantic_run(run):
    return run["schema"] == "imu.temporal_run.v3"


def needs_reference(run, entry):
    return (
        entry["reference_role"] == "validation"
        if primary_run(run)
        else entry["reference_role"] != "none"
    )


class TemporalReviewService:
    def __init__(self, store, source_service):
        self.store = store
        self.source = source_service

    def read(self, key):
        return self.store.read_json(key)[0]

    def optional(self, key):
        try:
            return self.store.read_json(key)
        except FileNotFoundError:
            return None, 0

    def immutable(self, key, document):
        try:
            self.store.write_json(key, document, if_generation_match=0)
        except ObjectConflictError:
            if self.read(key) != document:
                raise

    def get_run(self, run_id):
        if not IDENTIFIER.fullmatch(run_id):
            raise ValueError("Invalid run ID")
        index = self.read(INDEX + run_id + ".json")
        run = validate_run(self.read(index["run_key"]))
        if (
            run["run_id"] != run_id
            or index["run_key"] != run_root(run) + "/run.json"
            or index["sha256"] != document_sha256(run)
        ):
            raise ValueError("Run manifest identity/hash mismatch")
        return run

    def list_runs(self):
        runs = [
            self.get_run(item.key.removeprefix(INDEX).removesuffix(".json"))
            for item in self.store.list(INDEX)
            if item.key.endswith(".json")
        ]
        return [
            {
                "run_id": r["run_id"],
                "schema": r["schema"],
                "phase": r["phase"],
                "count": len(r["candidates"]),
                "created_at_utc": r["created_at_utc"],
            }
            for r in sorted(runs, key=lambda r: r["created_at_utc"], reverse=True)
        ]

    def create_run(self, run):
        validate_run(run)
        if run["source_prefix"] != self.source.prefix:
            raise ValueError("Run must reference the configured synthetic source")
        if run["phase"] == "full":
            pilot = self.get_run(run["full_from"]["pilot_run_id"])
            approval = self.read(run_root(pilot) + "/approval.json")
            if document_sha256(approval) != run["full_from"]["approval_sha256"] or not approval.get(
                "reviewer"
            ):
                raise ValueError("Full run requires the approved pilot report")
        for entry in run["candidates"]:
            self.commit(run, entry)
        self.immutable(run_root(run) + "/run.json", run)
        self.immutable(
            INDEX + run["run_id"] + ".json",
            {"run_key": run_root(run) + "/run.json", "sha256": document_sha256(run)},
        )
        return {"run_id": run["run_id"], "sha256": document_sha256(run)}

    def entry(self, run, cid, vid):
        for entry in run["candidates"]:
            identity = entry["candidate"]
            if identity["candidate_id"] == cid and identity["version_id"] == vid:
                return entry
        raise FileNotFoundError("Candidate is not part of this frozen run")

    def commit(self, run, entry):
        identity = entry["candidate"]
        key = (
            f"{run['source_prefix']}/candidates/"
            f"{identity['candidate_id']}/{identity['version_id']}.json"
        )
        commit = self.read(key)
        if (
            document_sha256(commit) != identity["commit_sha256"]
            or commit.get("candidate_id") != identity["candidate_id"]
            or commit.get("version_id") != identity["version_id"]
            or {item["role"]: item["sha256"] for item in commit["objects"]}
            != identity["input_hashes"]
        ):
            raise ValueError("Frozen candidate/input hashes differ")
        return commit

    @staticmethod
    def case_root(run, entry):
        identity = entry["candidate"]
        return f"{run_root(run)}/cases/{identity['candidate_id']}/{identity['version_id']}"

    def reference(self, run, entry):
        return self.optional(self.case_root(run, entry) + "/reference.json")[0]

    def draft(self, run, entry):
        pointer, generation = self.optional(self.case_root(run, entry) + "/draft.json")
        if not pointer:
            return None, generation
        value = self.read(pointer["revision_key"])
        if document_sha256(value) != pointer["revision_sha256"]:
            raise ValueError("Draft revision hash differs")
        self.check_semantic_document(entry, value["document"])
        return {**pointer, "document": value["document"]}, generation

    @staticmethod
    def check_semantic_document(entry, document):
        validate_document(
            document,
            expected_candidate=entry["candidate"],
            expected_count=entry["clock"]["sample_count"],
        )
        if document["schema"] != SEMANTIC_SCHEMA or document["clock"] != entry["clock"]:
            raise ValueError("Semantic draft schema/clock differs")
        if any(
            s["auxiliary_code"] is not None or s["auxiliary_status"] != "unrecognized"
            for s in document["segments"]
        ):
            raise ValueError("Only primary activity is reviewed in this run")

    def save_draft(self, run, entry, body, actor):
        if not semantic_run(run):
            raise ValueError("Interval drafts require a v3 run")
        prior, generation = self.draft(run, entry)
        expected = prior["revision_sha256"] if prior else None
        if body.get("expected_draft_sha256") != expected:
            raise ObjectConflictError("草稿已在其他页面更新，请重新载入后核对；本地内容仍保留")
        doc = body["document"]
        self.check_semantic_document(entry, doc)
        doc = normalize_semantic(doc)
        if prior and prior["document"] == doc:
            return prior
        value = {
            "schema": "imu.temporal_draft.v1",
            "document": doc,
            "previous_revision_sha256": expected,
            "author": actor,
            "created_at_utc": now(),
        }
        digest = document_sha256(value)
        key = self.case_root(run, entry) + "/drafts/" + digest + ".json"
        self.immutable(key, value)
        pointer = {"revision_key": key, "revision_sha256": digest}
        self.store.write_json(
            self.case_root(run, entry) + "/draft.json", pointer, if_generation_match=generation
        )
        return {**pointer, "document": doc}

    def checked_draft(self, run, entry, digest, document):
        draft, _ = self.draft(run, entry)
        if not draft or draft["revision_sha256"] != digest or draft["document"] != document:
            raise ObjectConflictError("保存的草稿已变化，请先保存并核对最新区间")
        return draft

    def eligible_review(self, run, entry, pointer=None):
        pointer = pointer or self.pointer(run, entry)[0]
        if not pointer or pointer["decision"] != "pass":
            return False
        if not semantic_run(run):
            return True
        revision = self.read(pointer["revision_key"])
        draft, _ = self.draft(run, entry)
        return bool(
            draft
            and revision.get("draft_sha256") == draft["revision_sha256"]
            and confirmed_spans(revision["temporal_annotation"])
        )

    def save_reference(self, run, entry, document, actor):
        if not needs_reference(run, entry):
            raise ValueError("Manual precision editing is limited to the frozen reference subset")
        if semantic_run(run):
            body = document
            document = body["document"]
            self.check_semantic_document(entry, document)
            self.checked_draft(run, entry, body.get("expected_draft_sha256"), document)
            if any(s["review_state"] == "pending" for s in document["segments"]):
                raise ValueError("独立参考仍有待讨论区间，请先保存草稿")
        validate_document(
            document,
            expected_candidate=entry["candidate"],
            expected_count=entry["clock"]["sample_count"],
        )
        if document["clock"] != entry["clock"]:
            raise ValueError("Reference clock differs")
        project_annotations(document)
        value = {
            "schema": "imu.temporal_reference.v1",
            "document": document,
            "sha256": document_sha256(document),
            "reviewer": actor,
            "created_at_utc": now(),
            "reference_role": entry["reference_role"],
        }
        self.immutable(self.case_root(run, entry) + "/reference.json", value)
        return value

    def pointer(self, run, entry):
        pointer, generation = self.optional(self.case_root(run, entry) + "/review.json")
        if pointer:
            revision = self.read(pointer["revision_key"])
            if document_sha256(revision) != pointer["revision_sha256"]:
                raise ValueError("Review revision hash differs")
        return pointer, generation

    def proposals(self, run, entry):
        prefix = self.case_root(run, entry) + "/proposals/"
        values = []
        for item in self.store.list(prefix):
            if not item.key.endswith(".json"):
                continue
            value = self.read(item.key)
            validate_proposal(value, entry)
            if item.key != prefix + document_sha256(value) + ".json":
                raise ValueError("Proposal key/hash mismatch")
            values.append(value)
        return values

    def current_proposal(self, run, entry, pointer):
        proposals = self.proposals(run, entry)
        if pointer is not None and (primary_run(run) or pointer["decision"] in ("pass", "reject")):
            matching = [p for p in proposals if document_sha256(p) == pointer["proposal_sha256"]]
        else:
            base = pointer["revision_sha256"] if pointer else None
            attempt = min(2, pointer["attempt"] + 1) if pointer else 0
            matching = [
                p
                for p in proposals
                if p["based_on_review_sha256"] == base
                and p["attempt"] == attempt
                and (not pointer or pointer["attempt"] < 2 or p["kind"] == "exclusion")
            ]
        # A worker task is deterministic; multiple divergent replies require intervention.
        if len(matching) > 1:
            raise ValueError("Conflicting proposals for the same review task")
        return matching[0] if matching else None

    def publish(self, run, entry, proposal):
        validate_proposal(proposal, entry)
        self.commit(run, entry)
        pointer, _ = self.pointer(run, entry)
        expected = pointer["revision_sha256"] if pointer else None
        if proposal["based_on_review_sha256"] != expected or (
            pointer and pointer["decision"] in ("pass", "reject")
        ):
            raise ObjectConflictError("Review changed before proposal publication")
        expected_attempt = min(2, pointer["attempt"] + 1) if pointer else 0
        if proposal["attempt"] != expected_attempt or (
            pointer and pointer["attempt"] == 2 and proposal["kind"] != "exclusion"
        ):
            raise ValueError("Automatic retry limit exceeded")
        if proposal["kind"] == "exclusion":
            if not pointer or pointer["attempt"] != 2:
                raise ValueError("Exclusion fallback follows two automatic retries")
            previous = self.read(pointer["revision_key"])["temporal_annotation"]
            old = {s["segment_id"]: s for s in previous["segments"]}
            new = proposal["document"]
            if set(old) != {s["segment_id"] for s in new["segments"]}:
                raise ValueError("Exclusion fallback must preserve interval identities")
            for segment in new["segments"]:
                before = old[segment["segment_id"]]
                if (
                    any(segment[k] != before[k] for k in ("start_ns", "stop_ns"))
                    or segment["primary_code"] not in (None, before["primary_code"])
                    or segment["auxiliary_code"] not in (None, before["auxiliary_code"])
                ):
                    raise ValueError("Exclusion fallback may only remove unsupported labels")
            if any(event not in previous["fall_events"] for event in new["fall_events"]):
                raise ValueError("Exclusion fallback cannot introduce a fall event")
        current = self.current_proposal(run, entry, pointer)
        if current is not None and current != proposal:
            raise ObjectConflictError("Task already has a different proposal")
        digest = document_sha256(proposal)
        self.immutable(self.case_root(run, entry) + "/proposals/" + digest + ".json", proposal)
        return {"sha256": digest}

    def case(self, run, entry, *, summary=False):
        pointer, _ = self.pointer(run, entry)
        reference = self.reference(run, entry)
        hidden = needs_reference(run, entry) and reference is None
        proposal = self.current_proposal(run, entry, pointer)
        status = (
            "reference_needed"
            if hidden
            else "accepted"
            if pointer and pointer["decision"] == "pass"
            else "rejected"
            if pointer and pointer["decision"] == "reject"
            else "pending_review"
            if proposal
            else "retry_exhausted"
            if pointer and pointer["attempt"] == 2
            else "retry_pending"
            if pointer
            else "inference_pending"
        )
        result = {
            "entry": entry,
            "status": status,
            "review": pointer,
            "proposal_sha256": document_sha256(proposal) if proposal and not hidden else None,
            "has_reference": reference is not None,
        }
        if not summary:
            result.update(proposal=None if hidden else proposal, reference=reference)
            result["reviewed_document"] = (
                self.read(pointer["revision_key"])["temporal_annotation"]
                if pointer and not hidden
                else None
            )
            if hidden:
                result["reference_template"] = empty_document(
                    **entry["candidate"],
                    sample_count=entry["clock"]["sample_count"],
                    source_start_ns=entry["clock"]["source_start_ns"],
                    source_rate_hz=entry["clock"]["source_rate_hz"],
                    producer={"kind": "independent-human-reference"},
                )
            if semantic_run(run):
                result["draft"] = self.draft(run, entry)[0]
                if hidden:
                    result["reference_template"] = semantic_draft(result["reference_template"])
                elif proposal:
                    result["editor_template"] = semantic_draft(
                        result["reviewed_document"] or proposal["document"]
                    )
        if semantic_run(run) and pointer and pointer["decision"] == "pass":
            result["status"] = (
                "accepted" if self.eligible_review(run, entry, pointer) else "pending_review"
            )
        return result

    def review(self, run, entry, body, actor):
        pointer, generation = self.pointer(run, entry)
        expected = pointer["revision_sha256"] if pointer else None
        if body.expected_review_sha256 != expected:
            raise ObjectConflictError("Review has changed; reload this candidate")
        if needs_reference(run, entry) and self.reference(run, entry) is None:
            raise ValueError("Complete the independent reference before viewing model suggestions")
        proposal = self.current_proposal(run, entry, pointer)
        if proposal is None or document_sha256(proposal) != body.proposal_sha256:
            raise ObjectConflictError("Proposal is absent or has changed")
        if body.decision not in ("pass", "return", "reject") or any(
            x not in REASONS for x in body.reason_codes
        ):
            raise ValueError("Invalid review decision/reason")
        doc = proposal["document"]
        draft_sha = None
        if semantic_run(run) and body.decision == "pass":
            if body.corrected_document is None:
                raise ValueError("请提交明确确认的区间；保存草稿不会通过审核")
            self.check_semantic_document(entry, body.corrected_document)
            draft = self.checked_draft(
                run, entry, body.expected_draft_sha256, body.corrected_document
            )
            draft_sha = draft["revision_sha256"]
            if not confirmed_spans(body.corrected_document):
                raise ValueError("至少确认一个可用区间；其他语义可保存为待讨论草稿")
        if body.corrected_document is not None:
            if not primary_run(run) or body.decision != "pass":
                raise ValueError("Primary corrections must be explicitly accepted")
            doc = validate_document(
                body.corrected_document,
                expected_candidate=entry["candidate"],
                expected_count=entry["clock"]["sample_count"],
            )
            if doc["clock"] != entry["clock"]:
                raise ValueError("Correction clock differs")
            if any(
                s["auxiliary_code"] is not None or s["auxiliary_status"] != "unrecognized"
                for s in doc["segments"]
            ):
                raise ValueError("Primary-only corrections leave auxiliary labels unrecognized")
            doc = {
                **doc,
                "producer": {
                    "kind": "human-primary-correction",
                    "reviewer": actor,
                    "proposal_sha256": body.proposal_sha256,
                },
            }
            project_annotations(doc)
        if not set(body.failed_segment_ids) <= {s["segment_id"] for s in doc["segments"]}:
            raise ValueError("Unknown failed segment")
        if body.decision in ("return", "reject") and not body.reason_codes:
            raise ValueError("A returned proposal needs a reason")
        if (
            body.decision == "pass"
            and any(s["resolution"] != "identified" for s in doc["segments"])
            and not semantic_run(run)
            and not body.acknowledge_exclusions
        ):
            raise ValueError("Explicitly confirm the intervals excluded from training")
        revision = {
            "schema": "imu.temporal_review.v1",
            "run_id": run["run_id"],
            "candidate_id": entry["candidate"]["candidate_id"],
            "version_id": entry["candidate"]["version_id"],
            "candidate_commit_sha256": entry["candidate"]["commit_sha256"],
            "decision": body.decision,
            "reviewer": actor,
            "created_at_utc": now(),
            "proposal_sha256": body.proposal_sha256,
            "attempt": proposal["attempt"],
            "previous_revision_sha256": expected,
            "reason": body.reason,
            "reason_codes": sorted(set(body.reason_codes)),
            "failed_segment_ids": sorted(set(body.failed_segment_ids)),
            "acknowledge_exclusions": body.acknowledge_exclusions,
            "labels": formal_labels(doc),
            "temporal_annotation": doc,
            "temporal_annotation_sha256": document_sha256(doc),
            "review_seconds": body.review_seconds,
            "corrected": body.corrected_document is not None,
        }
        if semantic_run(run):
            revision["draft_sha256"] = draft_sha
        digest = document_sha256(revision)
        cid, vid = entry["candidate"]["candidate_id"], entry["candidate"]["version_id"]
        key = f"{run_prefix(run)}/reviews/{cid}/{vid}/revisions/temporal-{digest}.json"
        self.immutable(key, revision)
        current = {
            "revision_key": key,
            "revision_sha256": digest,
            "decision": body.decision,
            "proposal_sha256": body.proposal_sha256,
            "attempt": proposal["attempt"],
            "reviewer": actor,
        }
        self.store.write_json(
            self.case_root(run, entry) + "/review.json", current, if_generation_match=generation
        )
        if body.decision == "return" and not primary_run(run):
            self.immutable(
                run_root(run) + "/retry-jobs/" + digest + ".json",
                {
                    "entry": entry,
                    "review_key": key,
                    "review_sha256": digest,
                    "attempt": min(2, proposal["attempt"] + 1),
                    "kind": "exclusion" if proposal["attempt"] == 2 else "inference",
                },
            )
        return current

    def create_snapshot(self, run, actor):
        if run["phase"] == "full":
            completed = {
                tuple(e["candidate"][k] for k in ("candidate_id", "version_id"))
                for e in run["candidates"]
                if self.case(run, e, summary=True)["status"] in ("accepted", "rejected")
            }
            newest = {}
            for row in self.source.list_candidates():
                old = newest.get(row["candidate_id"])
                if old is None or (row["published_at_utc"], row["version_id"]) > (
                    old["published_at_utc"],
                    old["version_id"],
                ):
                    newest[row["candidate_id"]] = row
            if any(
                (r["candidate_id"], r["version_id"]) not in completed
                and r["decision"] not in ("pass", "reject")
                for r in newest.values()
            ):
                raise ValueError(
                    "All current source candidates require human quality review "
                    "before production export"
                )
        entries = []
        unresolved = 0
        for entry in run["candidates"]:
            pointer, _ = self.pointer(run, entry)
            if pointer and pointer["decision"] == "reject":
                continue
            if not self.eligible_review(run, entry, pointer):
                unresolved += 1
                continue
            revision = self.read(pointer["revision_key"])
            if document_sha256(revision) != pointer["revision_sha256"]:
                raise ValueError("Review hash mismatch")
            identity = entry["candidate"]
            entries.append(
                {
                    "candidate_id": identity["candidate_id"],
                    "version_id": identity["version_id"],
                    "commit_key": (
                        f"{run['source_prefix']}/candidates/"
                        f"{identity['candidate_id']}/{identity['version_id']}.json"
                    ),
                    "commit_sha256": identity["commit_sha256"],
                    "source_prefix": run["source_prefix"],
                    "review_key": pointer["revision_key"],
                    "review_sha256": pointer["revision_sha256"],
                }
            )
        if not entries or (run["phase"] == "full" and unresolved):
            raise ValueError(
                "Full production snapshot requires completed human review for every candidate"
            )
        fingerprint = document_sha256({"run_sha256": document_sha256(run), "entries": entries})
        snapshot_id = "synthetic-" + fingerprint[:32]
        intent = {
            "schema": "imu_motion_simulator.snapshot_intent.v1",
            "snapshot_id": snapshot_id,
            "entries": entries,
            "temporal_run_id": run["run_id"],
            "requested_by": actor,
            "created_at_utc": now(),
        }
        key = f"{run_prefix(run)}/snapshots/requests/{snapshot_id}.json"
        previous, _ = self.optional(key)
        self.immutable(key, previous or intent)
        return {"snapshot_id": snapshot_id, "request_key": key}

    def approve(self, run, report_sha256, auxiliary_mode, actor):
        if primary_run(run):
            raise ValueError("Primary learning is a local pilot; full production is not enabled")
        if (
            run["phase"] != "pilot"
            or auxiliary_mode not in ("temporal", "segment_summary")
            or not isinstance(report_sha256, str)
            or not SHA256.fullmatch(report_sha256)
        ):
            raise ValueError("Only a pilot report can unlock full inference")
        report = self.read(run_root(run) + "/reports/" + report_sha256 + ".json")
        if document_sha256(report) != report_sha256 or report.get("run_sha256") != document_sha256(
            run
        ):
            raise ValueError("Pilot report hash/identity mismatch")
        if report.get("status") != "ready":
            raise ValueError("Pilot accuracy evaluation is not complete")
        if (
            len(run["candidates"]) != 30
            or sum(e["reference_role"] != "none" for e in run["candidates"]) != 12
        ):
            raise ValueError("Full-run gate requires the agreed 30/12 pilot")
        if (
            sum(e["reference_role"] == "adjustment" for e in run["candidates"]) != 4
            or sum(e["reference_role"] == "validation" for e in run["candidates"]) != 8
        ):
            raise ValueError("Pilot requires four adjustment and eight held-out references")
        bindings = {
            row["candidate_id"]: row["reference_sha256"]
            for row in report.get("reference_bindings", [])
        }
        for entry in run["candidates"]:
            if entry["reference_role"] != "none":
                reference = self.reference(run, entry)
                if reference is None:
                    raise ValueError("Independent references are incomplete")
                if bindings.get(entry["candidate"]["candidate_id"]) != document_sha256(reference):
                    raise ValueError("Report differs from frozen independent references")
            if self.case(run, entry, summary=True)["status"] not in ("accepted", "rejected"):
                raise ValueError("Complete the pilot review before approving its report")
        value = {
            "schema": "imu.temporal_pilot_approval.v1",
            "run_sha256": document_sha256(run),
            "report_sha256": report_sha256,
            "auxiliary_mode": auxiliary_mode,
            "reviewer": actor,
            "created_at_utc": now(),
        }
        self.immutable(run_root(run) + "/approval.json", value)
        return {"sha256": document_sha256(value), "approval": value}

    def snapshot(self, run, snapshot_id):
        if not re.fullmatch(r"synthetic-[0-9a-f]{32}", snapshot_id):
            raise ValueError("Invalid snapshot ID")
        prefix = run_prefix(run) + "/snapshots/"
        request = self.read(prefix + "requests/" + snapshot_id + ".json")
        if request.get("temporal_run_id") != run["run_id"]:
            raise ValueError("Snapshot belongs to another run")
        result = self.optional(prefix + "results/" + snapshot_id + ".json")[0]
        if result and (
            result.get("request_sha256") != document_sha256(request)
            or result.get("snapshot_id") != snapshot_id
        ):
            raise ValueError("Snapshot result differs from frozen request")
        failures = self.store.list(prefix + "failures/" + snapshot_id + "/") if not result else []
        return {
            "snapshot_id": snapshot_id,
            "created_at_utc": request["created_at_utc"],
            "candidate_count": len(request["entries"]),
            "result": result,
            "state": result["state"] if result else "failed" if failures else "queued",
        }


def validate_proposal(value, entry):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "document", "attempt", "kind", "based_on_review_sha256"}
        or value["schema"] != "imu.temporal_proposal.v1"
        or type(value["attempt"]) is not int
        or not 0 <= value["attempt"] <= 2
        or value["kind"] not in ("inference", "exclusion")
    ):
        raise ValueError("Invalid temporal proposal")
    doc = validate_document(
        value["document"],
        expected_candidate=entry["candidate"],
        expected_count=entry["clock"]["sample_count"],
    )
    if doc["clock"] != entry["clock"]:
        raise ValueError("Proposal source clock differs")
    project_annotations(doc)
    return value


def register_temporal_routes(app: FastAPI, source, current_actor):
    service = TemporalReviewService(source.store, source)
    app.state.temporal_review_service = service
    from .synthetic_primary import register_primary_routes

    register_primary_routes(app, service, current_actor)
    root = "/api/v1/synthetic/temporal"

    def invoke(function, *args):
        try:
            return function(*args)
        except ObjectConflictError as error:
            raise HTTPException(409, str(error)) from error
        except FileNotFoundError as error:
            raise HTTPException(404, "找不到试验或标注记录") from error
        except (ValueError, KeyError, TypeError) as error:
            raise HTTPException(422, str(error)) from error

    def admin(request):
        actor = current_actor(request)
        if not actor.is_admin:
            raise HTTPException(403, "仅管理员可创建任务或批准全量")
        return actor

    def locate(run_id, cid, vid):
        run = service.get_run(run_id)
        return run, service.entry(run, cid, vid)

    @app.get(root + "/runs")
    def runs(request: Request):
        current_actor(request)
        return {"runs": invoke(service.list_runs)}

    @app.post(root + "/runs")
    def create_run(body: dict, request: Request):
        admin(request)
        return invoke(service.create_run, body)

    @app.get(root + "/runs/{run_id}")
    def run_detail(run_id: str, request: Request, offset: int = 0, limit: int = 30):
        current_actor(request)
        run = invoke(service.get_run, run_id)
        selected = run["candidates"][max(0, offset) : max(0, offset) + max(1, min(100, limit))]
        return {
            "run": {k: v for k, v in run.items() if k != "candidates"},
            "total": len(run["candidates"]),
            "cases": [invoke(service.case, run, e) for e in selected],
        }

    @app.get(root + "/runs/{run_id}/cases/{cid}/{vid}")
    def case(run_id: str, cid: str, vid: str, request: Request):
        current_actor(request)
        return invoke(service.case, *invoke(locate, run_id, cid, vid))

    @app.post(root + "/runs/{run_id}/cases/{cid}/{vid}/proposal")
    def proposal(run_id: str, cid: str, vid: str, body: dict, request: Request):
        admin(request)
        return invoke(service.publish, *invoke(locate, run_id, cid, vid), body)

    @app.post(root + "/runs/{run_id}/cases/{cid}/{vid}/reference")
    def reference(run_id: str, cid: str, vid: str, body: dict, request: Request):
        actor = current_actor(request)
        return invoke(service.save_reference, *invoke(locate, run_id, cid, vid), body, actor.unikey)

    @app.post(root + "/runs/{run_id}/cases/{cid}/{vid}/draft")
    def draft(run_id: str, cid: str, vid: str, body: dict, request: Request):
        actor = current_actor(request)
        return invoke(service.save_draft, *invoke(locate, run_id, cid, vid), body, actor.unikey)

    @app.post(root + "/runs/{run_id}/cases/{cid}/{vid}/review")
    def review(run_id: str, cid: str, vid: str, body: ReviewBody, request: Request):
        actor = current_actor(request)
        return invoke(service.review, *invoke(locate, run_id, cid, vid), body, actor.unikey)

    @app.post(root + "/runs/{run_id}/snapshots")
    def snapshot(run_id: str, request: Request):
        actor = admin(request)
        return invoke(service.create_snapshot, invoke(service.get_run, run_id), actor.unikey)

    @app.get(root + "/runs/{run_id}/snapshots")
    def snapshots(run_id: str, request: Request):
        current_actor(request)
        run = invoke(service.get_run, run_id)
        prefix = run_prefix(run) + "/snapshots/requests/"
        result = []
        for item in service.store.list(prefix):
            intent = service.read(item.key)
            if intent.get("temporal_run_id") == run_id:
                result.append(invoke(service.snapshot, run, intent["snapshot_id"]))
        return {"snapshots": sorted(result, key=lambda s: s["created_at_utc"], reverse=True)}

    @app.get(root + "/runs/{run_id}/snapshots/{snapshot_id}/manifest")
    def snapshot_manifest(run_id: str, snapshot_id: str, request: Request):
        current_actor(request)
        snapshot = invoke(service.snapshot, invoke(service.get_run, run_id), snapshot_id)
        if snapshot["result"] is None:
            raise HTTPException(409, "快照尚未生成")
        return snapshot["result"]

    @app.get(root + "/runs/{run_id}/snapshots/{snapshot_id}/shards/{filename}")
    def snapshot_shard(run_id: str, snapshot_id: str, filename: str, request: Request):
        current_actor(request)
        run = invoke(service.get_run, run_id)
        snapshot = invoke(service.snapshot, run, snapshot_id)
        result = snapshot["result"]
        if not result or not re.fullmatch(r"shard-[0-9]{4}\.h5", filename):
            raise HTTPException(404, "找不到已生成分片")
        expected_key = f"{run_prefix(run)}/snapshots/{snapshot_id}/shards/{filename}"
        shard = next((s for s in result["shards"] if s["object_key"] == expected_key), None)
        info = service.store.stat(expected_key) if shard else None
        if (
            info is None
            or info.size_bytes != shard["byte_length"]
            or info.metadata.get("sha256") not in (None, shard["sha256"])
        ):
            raise HTTPException(404, "分片缺失或已经变化")
        return object_download_response(
            store=service.store,
            info=info,
            filename=filename,
            media_type="application/x-hdf5",
            range_header=request.headers.get("range"),
            sha256=shard["sha256"],
            label="时间区间标注 HDF5 分片",
        )

    @app.post(root + "/runs/{run_id}/approve")
    def approve(run_id: str, body: dict, request: Request):
        actor = admin(request)
        return invoke(
            service.approve,
            invoke(service.get_run, run_id),
            body.get("report_sha256", ""),
            body.get("auxiliary_mode"),
            actor.unikey,
        )

    @app.get(root + "/runs/{run_id}/report")
    def report(run_id: str, request: Request):
        current_actor(request)
        run = invoke(service.get_run, run_id)
        reports = []
        prefix = run_root(run) + "/reports/"
        for item in service.store.list(prefix):
            digest = item.key.removeprefix(prefix).removesuffix(".json")
            if not SHA256.fullmatch(digest):
                continue
            value = service.read(item.key)
            if document_sha256(value) != digest or value.get("run_sha256") != document_sha256(run):
                raise HTTPException(422, "试验报告校验失败")
            reports.append((digest, value))
        if not reports:
            return {
                "sha256": None,
                "approval": None,
                "report": {
                    "status": "awaiting_references",
                    "reference_count": sum(
                        service.reference(run, e) is not None for e in run["candidates"]
                    ),
                    "metrics": [],
                    "notes": ["参考标注和模型试跑完成后，工作进程会发布比较报告。"],
                },
            }
        digest, value = max(reports, key=lambda pair: pair[1].get("created_at_utc", ""))
        return {
            "sha256": digest,
            "report": value,
            "approval": service.optional(run_root(run) + "/approval.json")[0],
        }
