"""Private synthetic-motion review domain, separate from device recordings."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from imu_data_collector.http_download import object_download_response
from imu_data_collector.storage import ObjectConflictError, ObjectStore
from imu_data_collector.synthetic_catalog import SyntheticCatalog
from imu_data_collector.synthetic_labels import SyntheticLabelRegistry

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
    reason_codes: list[str] = Field(default_factory=list)
    labels: list[dict] = Field(default_factory=list)
    expected_revision: int
    lease_token: str | None = None


class LabelInput(BaseModel):
    code: str
    expected_revision: int


class RuleInput(BaseModel):
    origin: str
    source_value: str
    target_code: str
    source_dataset: str | None = None


class RuleStateInput(BaseModel):
    state: str
    checked_candidate_ids: list[str] = Field(default_factory=list)


class MotionConceptUpdateInput(BaseModel):
    expected_revision: int
    name: str | None = None
    active: bool | None = None


class LeaseInput(BaseModel):
    lease_token: str
    skip: bool = False


class ReviewClaimInput(BaseModel):
    candidate_id: str
    version_id: str
    expected_revision: int


REJECTION_CODES = {
    "motion_corruption": "人体姿态或动作损坏",
    "motion_discontinuity": "动作不自然跳变",
    "imu_artifact": "IMU 曲线异常",
    "motion_imu_mismatch": "画面与 IMU 不一致",
    "clip_boundary": "片段边界或截断问题",
    "other_quality": "其他质量问题",
}

VIEWER_BRIDGE = b"""<script>
(() => {
  if (parent === window) return;
  const version = 2;
  const report = () => {
    const slider = document.querySelector('#time');
    parent.postMessage({type: 'imu-synthetic-review-ready', version,
      frame: slider ? Number(slider.value) : 0,
      maxFrame: slider ? Number(slider.max) : 0}, location.origin);
  };
  report();
  addEventListener('load', report);
  const keys = new Set([' ', 'r', 'R', 'n', 'N', 'p', 'P', 'x', 'X', 's', 'S']);
  document.addEventListener('wheel', event => {
    if (event.target.closest('#view')) event.preventDefault();
  }, {capture: true, passive: false});
  document.addEventListener('keydown', event => {
    if (parent === window || event.altKey || event.ctrlKey || event.metaKey
        || event.target.closest('input,textarea,select,[contenteditable="true"]')) return;
    if (keys.has(event.key)) {
      event.preventDefault();
      parent.postMessage({type: 'imu-synthetic-review-key', key: event.key}, location.origin);
    }
  }, true);
  addEventListener('message', event => {
    if (event.source !== parent || event.origin !== location.origin
        || event.data?.type !== 'imu-synthetic-review-control') return;
    const play = document.querySelector('#play');
    if (event.data.action === 'toggle') play?.click();
    if (event.data.action === 'replay') {
      const slider = document.querySelector('#time');
      if (slider) { slider.value = '0'; slider.dispatchEvent(new Event('input')); }
      play?.click();
    }
    if (event.data.action === 'seek' && Number.isInteger(event.data.frame)) {
      const slider = document.querySelector('#time');
      if (slider) {
        slider.value = String(Math.max(0, Math.min(Number(slider.max), event.data.frame)));
        slider.dispatchEvent(new Event('input'));
      }
    }
    if (event.data.action === 'chart-ready') {
      const plot = document.querySelector('#plot');
      if (plot) plot.style.display = event.data.ready ? 'none' : '';
    }
  });
  let sentFrame = -1;
  setInterval(() => {
    const slider = document.querySelector('#time');
    if (!slider || parent === window) return;
    const frame = Number(slider.value);
    if (frame !== sentFrame) {
      sentFrame = frame;
      parent.postMessage({type: 'imu-synthetic-review-frame', frame}, location.origin);
    }
  }, 80);
  setInterval(report, 1000);
  setTimeout(() => {
    if (window.__imuReviewViewerVersion) return;
    document.querySelector('#view canvas')?.dispatchEvent(new WheelEvent('wheel', {
      deltaY: -420, bubbles: true, cancelable: true,
    }));
  }, 1200);
})();
</script>"""


class SyntheticReviewService:
    def __init__(self, store: ObjectStore, run_id: str,
                 labels: SyntheticLabelRegistry | None = None,
                 catalog_path: Path | None = None):
        if not SAFE_ID.fullmatch(run_id):
            raise ValueError("Invalid synthetic run ID")
        self.store = store
        self.prefix = f"synthetic-motion/dev/{run_id}/v1"
        self.labels = labels
        self.catalog = SyntheticCatalog(catalog_path) if catalog_path else None

    def _identity(self, candidate_id: str, version_id: str) -> None:
        if not SAFE_ID.fullmatch(candidate_id) or not SHA256.fullmatch(version_id):
            raise ValueError("Invalid candidate identity")

    def commit_key(self, candidate_id: str, version_id: str) -> str:
        self._identity(candidate_id, version_id)
        return f"{self.prefix}/candidates/{candidate_id}/{version_id}.json"

    def review_root(self, candidate_id: str, version_id: str) -> str:
        self._identity(candidate_id, version_id)
        return f"{self.prefix}/reviews/{candidate_id}/{version_id}"

    def label_root(self, candidate_id: str, version_id: str) -> str:
        self._identity(candidate_id, version_id)
        return f"{self.prefix}/labels/{candidate_id}/{version_id}"

    def latest_label(self, candidate_id: str, version_id: str) -> dict | None:
        try:
            return self.store.read_json(
                self.label_root(candidate_id, version_id) + "/latest.json")[0]
        except FileNotFoundError:
            return None

    def resolved_label(self, commit: dict, review: dict | None = None,
                       *, cached_label: dict | None = None,
                       use_remote: bool = True,
                       label_catalog: dict | None = None) -> dict | None:
        if cached_label:
            return cached_label
        candidate_id, version_id = commit["candidate_id"], commit["version_id"]
        pointer = self.latest_label(candidate_id, version_id) if use_remote else None
        if pointer:
            return self.store.read_json(pointer["revision_key"])[0]["label"]
        if review and use_remote:
            revision = self.store.read_json(review["revision_key"])[0]
            if revision.get("labels"):
                label = revision["labels"][0]
                return {**label, "origin": "legacy-human", "verification": "human",
                        "taxonomy_id": "legacy-synthetic",
                        "taxonomy_version": "1.0.0"}
        return self.labels.active_label(commit, label_catalog) if self.labels else None

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

    def refresh_index(self, label_catalog: dict | None = None) -> None:
        if self.catalog is None:
            return
        refresh = self.catalog.refresh(self.store, self.prefix, self._commit,
                                       minimum_interval_s=10)
        if self.labels:
            self.catalog.reindex_labels(label_catalog or self.labels.catalog(),
                                        self.labels.active_label,
                                        force=refresh["new"] > 0)

    def list_candidates(self, *, decision: str = "all", search: str = "",
                        high_risk: bool = False, limit: int | None = None,
                        offset: int = 0, label_state: str | None = None) -> list[dict]:
        if label_state not in (None, "pending", "labeled"):
            raise ValueError("Invalid label state")
        result = []
        label_catalog = self.labels.catalog() if self.labels else None
        if self.catalog:
            self.refresh_index(label_catalog)
            source = [(row["commit"], row["review"], row["label"], False,
                       row["decision"], row["revision"])
                      for row in self.catalog.rows(
                          decision=decision, search=search, high_risk=high_risk,
                          label_state=label_state,
                          limit=limit or 1_000_000_000, offset=offset)]
        else:
            source = []
            prefix = self.prefix + "/candidates/"
            for info in self.store.list(prefix):
                if not info.key.endswith(".json"):
                    continue
                parts = info.key[len(prefix):].split("/")
                if len(parts) != 2 or not parts[1].endswith(".json"):
                    continue
                candidate_id, version_id = parts[0], parts[1][:-5]
                try:
                    commit = self._commit(candidate_id, version_id)
                    review = self.latest(candidate_id, version_id)
                except (FileNotFoundError, ValueError):
                    continue
                source.append((commit, review, None, True,
                               review["decision"] if review else "unreviewed",
                               review["revision"] if review else 0))
        for commit, review, cached_label, use_remote, current_decision, current_revision in source:
            candidate_id, version_id = commit["candidate_id"], commit["version_id"]
            if decision != "all" and current_decision != decision:
                continue
            if high_risk and commit.get("risk_tier") != "high":
                continue
            if search and not self.catalog and search.lower() not in (
                    f"{candidate_id} {commit['source_dataset']} {commit['source_member']}".lower()):
                continue
            result.append({
                "candidate_id": candidate_id, "version_id": version_id,
                "source_dataset": commit["source_dataset"],
                "source_member": commit["source_member"],
                "published_at_utc": commit["published_at_utc"],
                "label_candidates": commit["label_candidates"],
                "warning_flags": commit["warning_flags"],
                "risk_score": commit.get("risk_score"),
                "risk_tier": commit.get("risk_tier", "unknown"),
                "label": self.resolved_label(
                    commit, review, cached_label=cached_label,
                    use_remote=use_remote, label_catalog=label_catalog),
                "decision": current_decision,
                "revision": current_revision,
            })
        if label_state:
            result = [row for row in result if (row["label"] is None)
                      == (label_state == "pending")]
        if self.catalog:
            return result
        return sorted(result, key=lambda row: (row["source_dataset"], row["candidate_id"]))[
            offset:offset + limit if limit is not None else None]

    def claim(self, actor: str, batch_size: int = 5, *, search: str = "",
              high_risk: bool = False) -> list[dict]:
        if self.catalog is None:
            raise ValueError("Synthetic queue is unavailable")
        self.refresh_index()
        label_catalog = self.labels.catalog() if self.labels else None
        claimed = self.catalog.claim(actor, batch_size=batch_size,
                                     search=search, high_risk=high_risk)
        result = []
        for item in claimed:
            row = self.catalog.get(item["candidate_id"], item["version_id"])
            commit = row["commit"]
            result.append({
                "candidate_id": item["candidate_id"], "version_id": item["version_id"],
                "lease_token": item["lease_token"],
                "source_dataset": commit["source_dataset"],
                "source_member": commit["source_member"],
                "published_at_utc": commit["published_at_utc"],
                "label_candidates": commit["label_candidates"],
                "warning_flags": commit["warning_flags"],
                "risk_score": commit.get("risk_score"),
                "risk_tier": commit.get("risk_tier", "unknown"),
                "label": self.resolved_label(
                    commit, row["review"], cached_label=row["label"],
                    use_remote=False, label_catalog=label_catalog),
                "decision": row["decision"], "revision": row["revision"],
            })
        return result

    def claim_reviewed(self, actor: str, body: ReviewClaimInput) -> dict:
        if self.catalog is None:
            raise ValueError("Synthetic queue is unavailable")
        self._identity(body.candidate_id, body.version_id)
        self.refresh_index()
        pointer = self.latest(body.candidate_id, body.version_id)
        if pointer is None or pointer["decision"] not in ("pass", "reject") \
                or pointer["revision"] != body.expected_revision:
            raise ObjectConflictError("审核结果已变化，请刷新后重试")
        cached = self.catalog.get(body.candidate_id, body.version_id)
        if cached["revision"] != pointer["revision"] \
                or cached["decision"] != pointer["decision"]:
            self.catalog.set_review(body.candidate_id, body.version_id, pointer)
        token = self.catalog.claim_reviewed(
            actor, body.candidate_id, body.version_id, body.expected_revision)
        return {"lease_token": token, "revision": pointer["revision"]}

    def detail(self, candidate_id: str, version_id: str) -> dict:
        commit = self._commit(candidate_id, version_id)
        for item in commit["objects"] + commit["bundle_files"]:
            info = self.store.stat(item["key"])
            if info is None or info.size_bytes != item["byte_length"]:
                raise ValueError("Candidate artifact is missing or truncated")
            declared = info.metadata.get("sha256")
            if declared is not None and declared != item["sha256"]:
                raise ValueError("Candidate artifact hash metadata differs")
        review = self.latest(candidate_id, version_id)
        return {"commit": commit, "review": review,
                "label": self.resolved_label(commit, review),
                "label_revision": self.latest_label(candidate_id, version_id)}

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
        if self.catalog and body.lease_token:
            self.catalog.check_lease(candidate_id, version_id, reviewer, body.lease_token)
        commit = self.detail(candidate_id, version_id)["commit"]
        if body.decision not in {"pass", "reject", "unreviewed"} or body.expected_revision < 0:
            raise ValueError("Review decision or expected revision is invalid")
        if body.labels and (
            body.decision != "pass" or len(body.labels) != 1
            or not isinstance(body.labels[0].get("code"), str)
            or not body.labels[0]["code"].strip()
            or not isinstance(body.labels[0].get("name"), str)
            or not body.labels[0]["name"].strip()
            or type(body.labels[0].get("is_fall")) is not bool
        ):
            raise ValueError("Legacy review label is invalid")
        if any(code not in REJECTION_CODES for code in body.reason_codes):
            raise ValueError("Unknown rejection reason code")
        if body.decision == "reject" and not (
            (body.reason or "").strip() or body.reason_codes):
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
        if body.decision == "unreviewed" and (
                previous is None or previous["decision"] == "unreviewed"):
            raise ValueError("只有已审核条目才能撤回为未审核")
        if body.decision == "unreviewed" and (body.labels or body.reason_codes):
            raise ValueError("撤回审核不能附带标签或拒绝原因")
        if self.catalog and previous and previous["decision"] in ("pass", "reject") \
                and not body.lease_token:
            raise ObjectConflictError("请先领取复审任务")
        revision = {
            "schema": "imu_motion_simulator.synthetic_review_revision.v1",
            "candidate_id": candidate_id, "version_id": version_id,
            "candidate_commit_sha256": hashlib.sha256(_canonical(commit)).hexdigest(),
            "policy_sha256": commit["policy_sha256"],
            "revision": current_revision + 1, "decision": body.decision,
            "reviewer": reviewer, "reason": body.reason,
            "reason_codes": sorted(set(body.reason_codes)), "labels": body.labels,
            "previous_revision_sha256": previous["revision_sha256"] if previous else None,
            "created_at_utc": datetime.now(UTC).isoformat(),
        }
        digest = hashlib.sha256(_canonical(revision)).hexdigest()
        revision_key = root + "/revisions/" + uuid4().hex + ".json"
        self.store.write_json(revision_key, revision, if_generation_match=0)
        pointer = {"revision": current_revision + 1, "decision": body.decision,
                   "revision_key": revision_key, "revision_sha256": digest,
                   "candidate_id": candidate_id, "version_id": version_id}
        if body.decision == "unreviewed":
            pointer["returned_at_utc"] = revision["created_at_utc"]
        self.store.write_json(pointer_key, pointer, if_generation_match=generation)
        if self.catalog:
            self.catalog.set_review(
                candidate_id, version_id, pointer,
                reset_by=reviewer if body.decision == "unreviewed" else None)
        return pointer

    def set_label(self, candidate_id: str, version_id: str,
                  body: LabelInput, actor: str) -> dict:
        if self.labels is None:
            raise ValueError("Synthetic label registry is unavailable")
        commit = self.detail(candidate_id, version_id)["commit"]
        review = self.latest(candidate_id, version_id)
        if review is None or review["decision"] != "pass":
            raise ValueError("只有质量审核通过的动作才能设置正式标签")
        catalog = self.labels.catalog()
        concept = self.labels.concept(body.code)
        root = self.label_root(candidate_id, version_id)
        pointer_key = root + "/latest.json"
        try:
            previous, generation = self.store.read_json(pointer_key)
        except FileNotFoundError:
            previous, generation = None, 0
        revision_number = previous["revision"] if previous else 0
        if body.expected_revision != revision_number:
            raise ObjectConflictError("标签已被更新，请刷新后重试")
        suggested = self.labels.active_label(commit)
        unchanged = suggested is not None and suggested["code"] == body.code
        label = {
            "code": concept["code"], "name": concept["name"],
            "is_fall": concept["is_fall"],
            "taxonomy_id": catalog["taxonomy_id"],
            "taxonomy_version": catalog["version"],
            "origin": "auto" if unchanged else "human",
            "verification": "human",
            "mapping_rule_id": suggested["mapping_rule_id"] if unchanged else None,
        }
        revision = {
            "schema": "imu_annotation.synthetic_label_revision.v1",
            "candidate_id": candidate_id, "version_id": version_id,
            "candidate_commit_sha256": hashlib.sha256(_canonical(commit)).hexdigest(),
            "revision": revision_number + 1, "label": label,
            "actor": actor, "created_at_utc": datetime.now(UTC).isoformat(),
            "previous_revision_sha256": previous["revision_sha256"] if previous else None,
        }
        digest = hashlib.sha256(_canonical(revision)).hexdigest()
        key = root + "/revisions/" + uuid4().hex + ".json"
        self.store.write_json(key, revision, if_generation_match=0)
        pointer = {"revision": revision_number + 1, "revision_key": key,
                   "revision_sha256": digest, "candidate_id": candidate_id,
                   "version_id": version_id}
        self.store.write_json(pointer_key, pointer, if_generation_match=generation)
        if self.catalog:
            self.catalog.set_label(candidate_id, version_id, label)
        return pointer

    def _mapping_commits(self):
        if self.catalog:
            self.refresh_index()
            return (row["commit"] for row in self.catalog.rows(limit=1_000_000_000))
        return (self._commit(row["candidate_id"], row["version_id"])
                for row in self.list_candidates())

    def mapping_options(self, origin: str, source_dataset: str = "",
                        search: str = "", limit: int = 50) -> dict:
        if origin not in {"babel-1.0", "stageii-source-member"}:
            raise ValueError("Invalid mapping source")
        datasets: Counter[str] = Counter()
        values: Counter[str] = Counter()
        for commit in self._mapping_commits():
            dataset = commit["source_dataset"]
            datasets[dataset] += 1
            if source_dataset and dataset != source_dataset:
                continue
            for label in commit.get("label_candidates") or []:
                if label.get("origin") != origin or label.get("kind") != "recording-candidate":
                    continue
                candidates = (label.get("categories") or []) if origin == "babel-1.0" \
                    else [label.get("code")]
                for value in {str(item).strip().lower() for item in candidates if item}:
                    if value and search.lower() in value:
                        values[value] += 1
        ordered = sorted(values.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return {"datasets": [{"name": name, "count": count}
                             for name, count in sorted(datasets.items())],
                "values": [{"value": value, "count": count}
                           for value, count in ordered]}

    def _matching_candidates(self, rule: dict) -> list[dict]:
        matched_by_id = {}
        for commit in self._mapping_commits():
            if self.labels.matches(rule, commit):
                candidate_id = commit["candidate_id"]
                previous = matched_by_id.get(candidate_id)
                if previous is None or (commit["published_at_utc"], commit["version_id"]) > (
                        previous["published_at_utc"], previous["version_id"]):
                    matched_by_id[candidate_id] = {
                        "candidate_id": candidate_id, "version_id": commit["version_id"],
                        "published_at_utc": commit["published_at_utc"]}
        return sorted(matched_by_id.values(), key=lambda row: row["candidate_id"])

    def mapping_estimate(self, body: RuleInput) -> dict:
        if self.labels is None or body.origin not in {"babel-1.0", "stageii-source-member"} \
                or not body.source_value.strip():
            raise ValueError("Invalid mapping source")
        concept = self.labels.concept(body.target_code)
        if concept["is_fall"]:
            raise ValueError("跌倒标签必须逐条人工确认，不能由来源词自动赋值")
        rule = {"origin": body.origin, "source_value": body.source_value.strip().lower(),
                "source_dataset": body.source_dataset or None}
        matched = self._matching_candidates(rule)
        return {"matched_count": len(matched), "sample_candidates": matched[:20]}

    def mapping_preview(self, rule_id: str) -> dict:
        if self.labels is None:
            raise ValueError("Synthetic label registry is unavailable")
        rule = next((item for item in self.labels.catalog()["rules"]
                     if item["rule_id"] == rule_id), None)
        if rule is None:
            raise FileNotFoundError(rule_id)
        matched = self._matching_candidates(rule)
        return {"rule_id": rule_id, "matched_count": len(matched),
                "sample_candidates": matched[:min(20, len(matched))],
                "sample_candidate_ids": [row["candidate_id"] for row in matched[:20]]}

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
            label_pointer = self.latest_label(candidate_id, version_id)
            if label_pointer:
                label_revision, _ = self.store.read_json(label_pointer["revision_key"])
                if hashlib.sha256(_canonical(label_revision)).hexdigest() != (
                        label_pointer["revision_sha256"]) \
                        or label_revision.get("candidate_id") != candidate_id \
                        or label_revision.get("version_id") != version_id \
                        or label_revision["candidate_commit_sha256"] != (
                            hashlib.sha256(_canonical(commit)).hexdigest()):
                    raise ValueError("Label revision changed during snapshot freeze")
                label = label_revision["label"]
            elif revision.get("labels"):
                label = revision["labels"][0]
            else:
                label = self.labels.active_label(commit) if self.labels else None
            if label is None:
                continue
            entry = {
                "candidate_id": candidate_id, "version_id": version_id,
                "commit_key": self.commit_key(candidate_id, version_id),
                "commit_sha256": hashlib.sha256(_canonical(commit)).hexdigest(),
                "review_key": pointer["revision_key"],
                "review_sha256": pointer["revision_sha256"],
            }
            if label_pointer:
                entry["label_key"] = label_pointer["revision_key"]
                entry["label_sha256"] = label_pointer["revision_sha256"]
            elif not revision.get("labels"):
                # Freeze an automatic resolution before writing the snapshot
                # request; later mapping edits cannot change this snapshot.
                resolution = {
                    "schema": "imu_annotation.synthetic_label_resolution.v1",
                    "candidate_id": candidate_id, "version_id": version_id,
                    "candidate_commit_sha256": entry["commit_sha256"],
                    "label": label,
                }
                label_digest = hashlib.sha256(_canonical(resolution)).hexdigest()
                label_key = (self.label_root(candidate_id, version_id)
                             + "/resolutions/" + label_digest + ".json")
                try:
                    self.store.write_json(label_key, resolution, if_generation_match=0)
                except ObjectConflictError:
                    existing, _ = self.store.read_json(label_key)
                    if existing != resolution:
                        raise
                entry["label_key"] = label_key
                entry["label_sha256"] = label_digest
            entries.append(entry)
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
                              current_actor,
                              label_registry: SyntheticLabelRegistry | None = None,
                              catalog_path: Path | None = None) -> None:
    if not run_id:
        return
    service = SyntheticReviewService(store, run_id, label_registry, catalog_path)
    app.state.synthetic_review_service = service

    @app.get("/api/v1/synthetic/candidates")
    def synthetic_candidates(request: Request):
        current_actor(request)
        params = request.query_params
        try:
            limit = max(1, min(200, int(params.get("limit", "100"))))
            offset = max(0, int(params.get("offset", "0")))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="分页参数无效") from error
        decision = params.get("decision", "all")
        if decision not in {"all", "unreviewed", "pass", "reject"}:
            raise HTTPException(status_code=422, detail="审核状态无效")
        label_state = params.get("label_state")
        if label_state not in (None, "pending", "labeled"):
            raise HTTPException(status_code=422, detail="标签状态无效")
        return {"candidates": service.list_candidates(
            decision=decision, search=params.get("search", ""),
            high_risk=params.get("high_risk") == "true",
            limit=limit, offset=offset, label_state=label_state),
            "limit": limit, "offset": offset}

    @app.post("/api/v1/synthetic/queue/claim")
    def synthetic_queue_claim(request: Request):
        actor = current_actor(request)
        try:
            return {"candidates": service.claim(
                actor.unikey, search=request.query_params.get("search", ""),
                high_risk=request.query_params.get("high_risk") == "true")}
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/synthetic/queue/claim-reviewed")
    def synthetic_queue_claim_reviewed(body: ReviewClaimInput, request: Request):
        actor = current_actor(request)
        try:
            return service.claim_reviewed(actor.unikey, body)
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="找不到合成候选") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/synthetic/queue/renew")
    def synthetic_queue_renew(body: LeaseInput, request: Request):
        actor = current_actor(request)
        try:
            service.catalog.renew(actor.unikey, body.lease_token)
            return {"ok": True}
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/synthetic/queue/release")
    def synthetic_queue_release(body: LeaseInput, request: Request):
        actor = current_actor(request)
        try:
            service.catalog.release(actor.unikey, body.lease_token, skip=body.skip)
            return {"ok": True}
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/v1/synthetic/summary")
    def synthetic_summary(request: Request):
        current_actor(request)
        if service.catalog:
            service.refresh_index()
            result = service.catalog.summary()
        else:
            rows = service.list_candidates()
            result = {
                "published": len(rows),
                "unreviewed": sum(row["decision"] == "unreviewed" for row in rows),
                "passed": sum(row["decision"] == "pass" for row in rows),
                "rejected": sum(row["decision"] == "reject" for row in rows),
                "snapshot_eligible": sum(row["decision"] == "pass" and row["label"] is not None
                                         for row in rows),
                "label_pending": sum(row["decision"] == "pass" and row["label"] is None
                                     for row in rows),
                "latest_published_at_utc": max(
                    (row["published_at_utc"] for row in rows), default=None),
                "indexed_at_utc": None,
            }
        return {**result,
                "read_only": bool(getattr(service.store, "read_only", False)),
                "preview_mode": getattr(service.store, "preview_mode", None)}

    @app.get("/api/v1/synthetic/rejection-reasons")
    def synthetic_rejection_reasons(request: Request):
        current_actor(request)
        return {"reasons": [{"code": code, "name": name}
                            for code, name in REJECTION_CODES.items()]}

    @app.get("/api/v1/synthetic/labels")
    def synthetic_labels(request: Request):
        current_actor(request)
        return label_registry.catalog() if label_registry else {
            "taxonomy_id": "unavailable", "version": "0", "concepts": [], "rules": []}

    @app.post("/api/v1/synthetic/labels/concepts")
    def synthetic_concept_create(body: dict, request: Request):
        actor = current_actor(request)
        if not actor.is_admin:
            raise HTTPException(status_code=403, detail="该操作仅限管理员")
        try:
            return label_registry.add_concept(
                code=body["code"], name=body["name"],
                is_fall=body["is_fall"], actor=actor.unikey)
        except (KeyError, ValueError, ObjectConflictError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.patch("/api/v1/synthetic/labels/concepts/{code}")
    def synthetic_concept_update(code: str, body: MotionConceptUpdateInput,
                                 request: Request):
        actor = current_actor(request)
        if not actor.is_admin:
            raise HTTPException(status_code=403, detail="该操作仅限管理员")
        try:
            return label_registry.update_concept(
                code, expected_revision=body.expected_revision,
                name=body.name, active=body.active, actor=actor.unikey)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="找不到动捕专用概念") from error
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/synthetic/labels/mapping-options")
    def synthetic_mapping_options(request: Request):
        current_actor(request)
        params = request.query_params
        try:
            return service.mapping_options(
                params.get("origin", "babel-1.0"),
                params.get("source_dataset", ""), params.get("search", "")[:120])
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/synthetic/labels/mappings/estimate")
    def synthetic_mapping_estimate(body: RuleInput, request: Request):
        current_actor(request)
        try:
            return service.mapping_estimate(body)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/synthetic/labels/mappings")
    def synthetic_mapping_create(body: RuleInput, request: Request):
        actor = current_actor(request)
        if not actor.is_admin:
            raise HTTPException(status_code=403, detail="该操作仅限管理员")
        try:
            return label_registry.add_rule(
                origin=body.origin, source_value=body.source_value,
                target_code=body.target_code, source_dataset=body.source_dataset,
                actor=actor.unikey)
        except (ValueError, ObjectConflictError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/synthetic/labels/mappings/{rule_id}/preview")
    def synthetic_mapping_preview(rule_id: str, request: Request):
        current_actor(request)
        try:
            return service.mapping_preview(rule_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="找不到映射规则") from error

    @app.post("/api/v1/synthetic/labels/mappings/{rule_id}/state")
    def synthetic_mapping_state(rule_id: str, body: RuleStateInput,
                                request: Request):
        actor = current_actor(request)
        if not actor.is_admin:
            raise HTTPException(status_code=403, detail="该操作仅限管理员")
        try:
            preview = service.mapping_preview(rule_id)
            expected = min(20, preview["matched_count"])
            if body.state == "active" and (
                preview["matched_count"] == 0
                or not set(body.checked_candidate_ids).issubset(
                    set(preview["sample_candidate_ids"]))):
                raise ValueError("请先检查预览中的代表样本")
            return label_registry.set_rule_state(
                rule_id, body.state, checked_candidate_ids=body.checked_candidate_ids,
                required_count=expected, actor=actor.unikey)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="找不到映射规则") from error
        except (ValueError, ObjectConflictError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

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
            if filename == "index.html" and request.query_params.get("bridge") == "2":
                payload = payload.replace(
                    b"</head>",
                    b"<style>#panel{display:none!important}</style></head>",
                )
                payload = payload.replace(b"</body>", VIEWER_BRIDGE + b"</body>")
            return Response(content=payload, media_type=media_type,
                            headers={"Cache-Control": "private, no-store" if filename == "index.html"
                                     else "private, max-age=3600"})
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
            if service.catalog and not actor.is_admin and not body.lease_token:
                raise ObjectConflictError("请先领取审核任务")
            return service.review(candidate_id, version_id, body, actor.unikey)
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/synthetic/candidates/{candidate_id}/{version_id}/labels")
    def synthetic_set_label(candidate_id: str, version_id: str,
                            body: LabelInput, request: Request):
        actor = current_actor(request)
        try:
            return service.set_label(candidate_id, version_id, body, actor.unikey)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="找不到合成候选") from error
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/synthetic/snapshots")
    def synthetic_snapshot_list(request: Request):
        current_actor(request)
        prefix = service.prefix + "/snapshots/requests/"
        result = []
        for info in service.store.list(prefix):
            if not info.key.endswith(".json"):
                continue
            snapshot_id = info.key.rsplit("/", 1)[-1][:-5]
            snapshot = service.snapshot(snapshot_id)
            result.append({
                "snapshot_id": snapshot_id,
                "created_at_utc": snapshot["request"]["created_at_utc"],
                "candidate_count": len(snapshot["request"]["entries"]),
                "result": snapshot["result"],
            })
        return {"snapshots": sorted(result, key=lambda row:
                                     row["created_at_utc"], reverse=True)}

    @app.post("/api/v1/synthetic/snapshots")
    def synthetic_snapshot_create(request: Request):
        actor = current_actor(request)
        if getattr(service.store, "preview_mode", None):
            raise HTTPException(status_code=403, detail="本地预览不构建云端快照")
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

    @app.get("/api/v1/synthetic/snapshots/{snapshot_id}/shards/{filename}")
    def synthetic_snapshot_shard(snapshot_id: str, filename: str, request: Request):
        current_actor(request)
        try:
            snapshot = service.snapshot(snapshot_id)
            result = snapshot["result"]
            shard = next((item for item in result.get("shards", [])
                          if item["object_key"].rsplit("/", 1)[-1] == filename), None)
            if result.get("state") != "complete" or shard is None \
                    or not re.fullmatch(r"shard-[0-9]{4}\.h5", filename):
                raise FileNotFoundError(filename)
            info = service.store.stat(shard["object_key"])
            if info is None or info.size_bytes != shard["byte_length"] \
                    or (info.metadata.get("sha256") not in (None, shard["sha256"])):
                raise ValueError("Snapshot shard is missing or changed")
            return object_download_response(
                store=service.store, info=info, filename=filename,
                media_type="application/x-hdf5",
                range_header=request.headers.get("range"), sha256=shard["sha256"],
                label="合成快照分片")
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="找不到快照分片") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
