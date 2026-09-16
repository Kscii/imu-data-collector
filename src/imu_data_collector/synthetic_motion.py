"""Private synthetic-motion review domain, separate from device recordings."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from imu_data_collector.storage import ObjectConflictError, ObjectStore

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPES = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
               "json": "application/json", "bin": "application/octet-stream"}


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode()


class ReviewInput(BaseModel):
    decision: str
    reason: str | None = None
    labels: list[dict]
    expected_revision: int


class SyntheticReviewService:
    def __init__(self, store: ObjectStore, run_id: str):
        if not SAFE_ID.fullmatch(run_id):
            raise ValueError("Invalid synthetic run ID")
        self.store = store
        self.prefix = f"synthetic-motion/dev/{run_id}/v1"

    def _identity(self, candidate_id: str, version_id: str) -> None:
        if not SAFE_ID.fullmatch(candidate_id) or not SHA256.fullmatch(version_id):
            raise ValueError("Invalid candidate identity")

    def commit_key(self, candidate_id: str, version_id: str) -> str:
        self._identity(candidate_id, version_id)
        return f"{self.prefix}/candidates/{candidate_id}/{version_id}.json"

    def review_root(self, candidate_id: str, version_id: str) -> str:
        self._identity(candidate_id, version_id)
        return f"{self.prefix}/reviews/{candidate_id}/{version_id}"

    def _commit(self, candidate_id: str, version_id: str) -> dict:
        commit, _ = self.store.read_json(self.commit_key(candidate_id, version_id))
        if commit.get("schema") != "imu_motion_simulator.candidate_commit.v1" \
                or commit.get("candidate_id") != candidate_id \
                or commit.get("version_id") != version_id \
                or not SHA256.fullmatch(str(commit.get("policy_sha256", ""))):
            raise ValueError("Invalid synthetic candidate commit")
        files = commit.get("bundle_files")
        objects = commit.get("objects")
        if not isinstance(files, list) or not isinstance(objects, list) or not files:
            raise ValueError("Candidate commit has no artifacts")
        if {item.get("role") for item in objects} != {"motion", "sensors", "selection"}:
            raise ValueError("Candidate commit has invalid object roles")
        for item in files + objects:
            if not isinstance(item, dict) or not str(item.get("key", "")).startswith(
                    self.prefix + "/") or not SHA256.fullmatch(str(item.get("sha256", ""))) \
                    or type(item.get("byte_length")) is not int or item["byte_length"] < 0:
                raise ValueError("Candidate commit references invalid object")
            if item in files and not SAFE_ID.fullmatch(str(item.get("name", ""))):
                raise ValueError("Candidate commit has invalid file name")
        return commit

    def latest(self, candidate_id: str, version_id: str) -> dict | None:
        key = self.review_root(candidate_id, version_id) + "/latest.json"
        try:
            value, _ = self.store.read_json(key)
            return value
        except FileNotFoundError:
            return None

    def list_candidates(self) -> list[dict]:
        result = []
        prefix = self.prefix + "/candidates/"
        for info in self.store.list(prefix):
            if not info.key.endswith(".json"):
                continue
            parts = info.key[len(prefix):].split("/")
            if len(parts) != 2 or not parts[1].endswith(".json"):
                continue
            candidate_id, version_id = parts[0], parts[1][:-5]
            try:
                # The publisher writes this immutable commit only after every
                # referenced object is verified. Avoid N object HEADs per queue
                # refresh; detail/file access verifies current availability.
                commit = self._commit(candidate_id, version_id)
                review = self.latest(candidate_id, version_id)
            except (FileNotFoundError, ValueError):
                continue
            result.append({
                "candidate_id": candidate_id, "version_id": version_id,
                "source_dataset": commit["source_dataset"],
                "source_member": commit["source_member"],
                "published_at_utc": commit["published_at_utc"],
                "label_candidates": commit["label_candidates"],
                "warning_flags": commit["warning_flags"],
                "decision": review["decision"] if review else "unreviewed",
                "revision": review["revision"] if review else 0,
            })
        return sorted(result, key=lambda row: (row["source_dataset"], row["candidate_id"]))

    def detail(self, candidate_id: str, version_id: str) -> dict:
        commit = self._commit(candidate_id, version_id)
        for item in commit["objects"] + commit["bundle_files"]:
            info = self.store.stat(item["key"])
            if info is None or info.size_bytes != item["byte_length"]:
                raise ValueError("Candidate artifact is missing or truncated")
            declared = info.metadata.get("sha256")
            if declared is not None and declared != item["sha256"]:
                raise ValueError("Candidate artifact hash metadata differs")
        return {"commit": commit, "review": self.latest(candidate_id, version_id)}

    def file(self, candidate_id: str, version_id: str, filename: str) -> tuple[bytes, str]:
        commit = self._commit(candidate_id, version_id)
        file = next((item for item in commit["bundle_files"] if item["name"] == filename), None)
        if file is None:
            raise FileNotFoundError(filename)
        payload = self.store.read_bytes(file["key"])
        if (len(payload) != file["byte_length"]
                or hashlib.sha256(payload).hexdigest() != file["sha256"]):
            raise ValueError("Candidate preview file hash differs")
        return payload, MEDIA_TYPES.get(filename.rsplit(".", 1)[-1], "application/octet-stream")

    def review(self, candidate_id: str, version_id: str, body: ReviewInput, reviewer: str) -> dict:
        commit = self.detail(candidate_id, version_id)["commit"]
        if body.decision not in {"pass", "reject"} or body.expected_revision < 0:
            raise ValueError("Review decision or expected revision is invalid")
        if body.decision == "pass" and not body.labels:
            raise ValueError("Passing review requires labels")
        if body.decision == "pass" and (
            len(body.labels) != 1
            or not isinstance(body.labels[0].get("code"), str)
            or not body.labels[0]["code"].strip()
            or not isinstance(body.labels[0].get("name"), str)
            or not body.labels[0]["name"].strip()
            or type(body.labels[0].get("is_fall")) is not bool
        ):
            raise ValueError("Passing review requires one complete activity label")
        if body.decision == "reject" and not (body.reason or "").strip():
            raise ValueError("Rejected review requires a reason")
        root = self.review_root(candidate_id, version_id)
        pointer_key = root + "/latest.json"
        try:
            previous, generation = self.store.read_json(pointer_key)
        except FileNotFoundError:
            previous, generation = None, 0
        current_revision = previous["revision"] if previous else 0
        if current_revision != body.expected_revision:
            raise ObjectConflictError("审核已被更新，请刷新后重试")
        revision = {
            "schema": "imu_motion_simulator.synthetic_review_revision.v1",
            "candidate_id": candidate_id, "version_id": version_id,
            "candidate_commit_sha256": hashlib.sha256(_canonical(commit)).hexdigest(),
            "policy_sha256": commit["policy_sha256"],
            "revision": current_revision + 1, "decision": body.decision,
            "reviewer": reviewer, "reason": body.reason, "labels": body.labels,
            "previous_revision_sha256": previous["revision_sha256"] if previous else None,
            "created_at_utc": datetime.now(UTC).isoformat(),
        }
        digest = hashlib.sha256(_canonical(revision)).hexdigest()
        revision_key = root + "/revisions/" + uuid4().hex + ".json"
        self.store.write_json(revision_key, revision, if_generation_match=0)
        pointer = {"revision": current_revision + 1, "decision": body.decision,
                   "revision_key": revision_key, "revision_sha256": digest,
                   "candidate_id": candidate_id, "version_id": version_id}
        self.store.write_json(pointer_key, pointer, if_generation_match=generation)
        return pointer

    def create_snapshot(self, actor: str) -> dict:
        entries = []
        newest = {}
        for row in self.list_candidates():
            identity = row["candidate_id"]
            prior = newest.get(identity)
            if prior is None or (row["published_at_utc"], row["version_id"]) > (
                    prior["published_at_utc"], prior["version_id"]):
                newest[identity] = row
        for row in newest.values():
            if row["decision"] != "pass":
                continue
            candidate_id, version_id = row["candidate_id"], row["version_id"]
            commit = self.detail(candidate_id, version_id)["commit"]
            pointer = self.latest(candidate_id, version_id)
            if pointer is None:
                continue
            revision, _ = self.store.read_json(pointer["revision_key"])
            digest = hashlib.sha256(_canonical(revision)).hexdigest()
            if digest != pointer["revision_sha256"] or revision["decision"] != "pass":
                raise ValueError("Review revision changed during snapshot freeze")
            entries.append({
                "candidate_id": candidate_id, "version_id": version_id,
                "commit_key": self.commit_key(candidate_id, version_id),
                "commit_sha256": hashlib.sha256(_canonical(commit)).hexdigest(),
                "review_key": pointer["revision_key"],
                "review_sha256": pointer["revision_sha256"],
            })
        if not entries:
            raise ValueError("No accepted synthetic candidate is available")
        snapshot_id = "synthetic-" + uuid4().hex
        intent = {
            "schema": "imu_motion_simulator.snapshot_intent.v1",
            "snapshot_id": snapshot_id, "created_by": actor,
            "created_at_utc": datetime.now(UTC).isoformat(),
            "entries": entries,
        }
        key = f"{self.prefix}/snapshots/requests/{snapshot_id}.json"
        self.store.write_json(key, intent, if_generation_match=0)
        return {"snapshot_id": snapshot_id, "state": "queued", "candidate_count": len(entries)}

    def snapshot(self, snapshot_id: str) -> dict:
        if not SAFE_ID.fullmatch(snapshot_id):
            raise ValueError("Invalid snapshot ID")
        intent, _ = self.store.read_json(
            f"{self.prefix}/snapshots/requests/{snapshot_id}.json")
        try:
            result, _ = self.store.read_json(
                f"{self.prefix}/snapshots/results/{snapshot_id}.json")
        except FileNotFoundError:
            failures = self.store.list(
                f"{self.prefix}/snapshots/failures/{snapshot_id}/")
            result = (self.store.read_json(sorted(failures, key=lambda item: item.key)[-1].key)[0]
                      if failures else {"state": "queued"})
        return {"request": intent, "result": result}


def register_synthetic_motion(app: FastAPI, store: ObjectStore, run_id: str | None,
                              current_actor) -> None:
    if not run_id:
        return
    service = SyntheticReviewService(store, run_id)
    app.state.synthetic_review_service = service

    @app.get("/api/v1/synthetic/candidates")
    def synthetic_candidates(request: Request):
        current_actor(request)
        return {"candidates": service.list_candidates()}

    @app.get("/api/v1/synthetic/candidates/{candidate_id}/{version_id}")
    def synthetic_detail(candidate_id: str, version_id: str, request: Request):
        current_actor(request)
        try:
            return service.detail(candidate_id, version_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="找不到合成候选") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/synthetic/candidates/{candidate_id}/{version_id}/files/{filename}")
    def synthetic_file(candidate_id: str, version_id: str, filename: str, request: Request):
        current_actor(request)
        try:
            payload, media_type = service.file(candidate_id, version_id, filename)
            if filename == "index.html":
                payload = payload.replace(
                    b"</head>",
                    b"<style>#panel > .row:nth-of-type(n+3){display:none}</style></head>",
                )
            return Response(content=payload, media_type=media_type,
                            headers={"Cache-Control": "private, max-age=3600"})
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="找不到审核载荷") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/synthetic/candidates/{candidate_id}/{version_id}/files/api/capabilities")
    def synthetic_preview_capabilities(candidate_id: str, version_id: str, request: Request):
        current_actor(request)
        try:
            service._commit(candidate_id, version_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="找不到合成候选") from error
        return {"review_write": False}

    @app.post("/api/v1/synthetic/candidates/{candidate_id}/{version_id}/reviews")
    def synthetic_review(candidate_id: str, version_id: str, body: ReviewInput,
                         request: Request):
        actor = current_actor(request)
        try:
            return service.review(candidate_id, version_id, body, actor.unikey)
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/synthetic/snapshots")
    def synthetic_snapshot_create(request: Request):
        actor = current_actor(request)
        try:
            return service.create_snapshot(actor.unikey)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/synthetic/snapshots/{snapshot_id}")
    def synthetic_snapshot_get(snapshot_id: str, request: Request):
        current_actor(request)
        try:
            return service.snapshot(snapshot_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="找不到合成快照") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
