"""Separate unreviewed synthetic exports and weak rules for the dataset page."""
from __future__ import annotations

import hashlib
import json
import math
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from imu_data_collector.http_download import object_download_response
from imu_data_collector.storage import ObjectConflictError, ObjectStore


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode()

RULE_SCHEMA = "imu_motion_simulator.provisional_rules.v1"
EXPORT_SCHEMA = "imu_motion_simulator.provisional_export.v1"
HEX = re.compile(r"^[0-9a-f]{64}$")
SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CODE = re.compile(r"^[a-z][a-z0-9_]*$")


class ProvisionalRule(BaseModel):
    rule_id: str
    origin: str
    source_value: str
    source_dataset: str | None = None
    target_code: str
    is_fall: bool = False


class ProvisionalRulesInput(BaseModel):
    expected_revision: int
    rules: list[ProvisionalRule]


class ProvisionalPreviewInput(BaseModel):
    candidate_ids: list[str] = Field(default_factory=list)
    rules: list[ProvisionalRule]


def default_rules() -> dict:
    mapping = {
        "walk": "walking", "run": "running", "jog": "jogging",
        "stand": "standing", "sit": "sitting", "jump": "jumping",
        "turn": "turning", "dance": "dancing", "throw": "throwing",
        "kick": "kicking", "wave": "waving", "stretch": "stretching",
    }
    return {"schema": RULE_SCHEMA, "revision": 1, "rules": [
        {"rule_id": f"babel-{source}-v1", "origin": "babel-1.0",
         "source_value": source, "source_dataset": None,
         "target_code": target, "is_fall": False}
        for source, target in sorted(mapping.items())]}


def validate_rules(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema", "revision", "rules"} \
            or value["schema"] != RULE_SCHEMA or type(value["revision"]) is not int \
            or value["revision"] < 1 or not isinstance(value["rules"], list):
        raise ValueError("无效的未审核数据规则版本")
    ids, sources = set(), set()
    for row in value["rules"]:
        if not isinstance(row, dict) or set(row) != {"rule_id", "origin", "source_value",
                                                    "source_dataset", "target_code", "is_fall"} \
                or not isinstance(row["rule_id"], str) or not row["rule_id"] \
                or row["origin"] not in {"babel-1.0", "stageii-source-member"} \
                or not isinstance(row["source_value"], str) or not row["source_value"] \
                or row["source_value"] != row["source_value"].strip().lower() \
                or not CODE.fullmatch(str(row["target_code"])) \
                or row["is_fall"] is not False \
                or (row["source_dataset"] is not None and
                    (not isinstance(row["source_dataset"], str) or not row["source_dataset"])):
            raise ValueError("规则无效；未审核数据不能自动确认跌倒")
        source = (row["origin"], row["source_value"], row["source_dataset"])
        if row["rule_id"] in ids or source in sources:
            raise ValueError("规则 ID 或来源条件重复")
        ids.add(row["rule_id"])
        sources.add(source)
    return value


def preview_label(commit: dict, rules: dict) -> dict:
    labels = commit.get("label_candidates") or []
    recording = [row for row in labels if row.get("kind") == "recording-candidate"
                 and row.get("code")]
    temporal = [row for row in labels if row.get("kind") == "temporal-candidate"
                and row.get("code")]
    matched = []
    for rule in rules["rules"]:
        if rule["source_dataset"] not in (None, commit["source_dataset"]):
            continue
        own = [row for row in recording if row.get("origin") == rule["origin"]]
        if rule["origin"] == "babel-1.0":
            values = {str(value).lower() for row in own
                      for value in row.get("categories") or []}
            temporal_values = {str(value).lower() for row in temporal
                               if row.get("origin") == rule["origin"]
                               for value in row.get("categories") or []}
            if values == {rule["source_value"]} and (
                    not temporal_values or temporal_values == values):
                matched.append(rule)
        elif len(own) == 1 and str(own[0]["code"]).lower() == rule["source_value"]:
            matched.append(rule)
    specific = [rule for rule in matched if rule["source_dataset"] == commit["source_dataset"]]
    if specific:
        matched = specific
    return ({"state": "weak", "code": matched[0]["target_code"],
             "rule_id": matched[0]["rule_id"]} if len(matched) == 1 else
            {"state": "unresolved", "code": None, "rule_id": None})


def register_provisional_routes(app: FastAPI, service, current_actor) -> None:
    store: ObjectStore = service.store
    prefix = service.prefix + "/datasets/provisional"
    rules_key = prefix + "/rules/current.json"

    def rules_now():
        try:
            value, generation = store.read_json(rules_key)
        except FileNotFoundError:
            value, generation = default_rules(), 0
        return validate_rules(value), generation

    def manifest(export_id: str):
        if not re.fullmatch(r"[0-9a-f]{32}", export_id):
            raise HTTPException(status_code=404, detail="找不到导出版本")
        try:
            value, _ = store.read_json(f"{prefix}/exports/{export_id}/manifest.json")
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="找不到导出版本") from error
        h5 = value.get("h5") or {}
        key = f"{prefix}/exports/{export_id}/{value.get('dataset_id')}.h5"
        if value.get("schema") != EXPORT_SCHEMA or value.get("export_id") != export_id \
                or value.get("artifact_profile") != "imu_dataset_provisional" \
                or value.get("evaluation_role") != "unverified_synthetic" \
                or h5.get("object_key") != key \
                or h5.get("filename") != key.rsplit("/", 1)[-1] \
                or not HEX.fullmatch(str(h5.get("sha256", ""))) \
                or type(h5.get("byte_length")) is not int or h5["byte_length"] < 1:
            raise HTTPException(status_code=422, detail="未审核数据 manifest 无效")
        duration = value.get("duration_s")
        if duration is not None and (isinstance(duration, bool)
                                     or not isinstance(duration, (int, float))
                                     or not math.isfinite(duration) or duration <= 0):
            raise HTTPException(status_code=422, detail="未审核数据时长无效")
        info = store.stat(key)
        if info is None or info.size_bytes != h5["byte_length"] \
                or info.metadata.get("sha256") not in (None, h5["sha256"]):
            raise HTTPException(status_code=422, detail="未审核数据 H5 缺失或已变更")
        return value, info

    @app.get("/api/v1/synthetic/datasets/provisional")
    def list_exports(request: Request):
        current_actor(request)
        rows = []
        for info in store.list(prefix + "/exports/"):
            if not info.key.endswith("/manifest.json"):
                continue
            export_id = info.key.split("/")[-2]
            try:
                value, _ = manifest(export_id)
            except HTTPException:
                continue
            rows.append(value)
        return {"exports": sorted(rows, key=lambda row: row["created_at_utc"],
                                  reverse=True)}

    @app.get("/api/v1/synthetic/datasets/provisional/{export_id}/manifest")
    def download_manifest(export_id: str, request: Request):
        current_actor(request)
        value, _ = manifest(export_id)
        return Response(content=_canonical(value), media_type="application/json",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{export_id}-manifest.json"'})

    @app.get("/api/v1/synthetic/datasets/provisional/{export_id}/download")
    def download_export(export_id: str, request: Request):
        current_actor(request)
        value, info = manifest(export_id)
        return object_download_response(
            store=store, info=info, filename=value["h5"]["filename"],
            media_type="application/x-hdf5", range_header=request.headers.get("range"),
            sha256=value["h5"]["sha256"], label="未审核合成数据")

    @app.get("/api/v1/synthetic/provisional-rules")
    def get_rules(request: Request):
        current_actor(request)
        value, _ = rules_now()
        return {**value, "sha256": hashlib.sha256(_canonical(value)).hexdigest()}

    @app.post("/api/v1/synthetic/provisional-rules/preview")
    def preview_rules(body: ProvisionalPreviewInput, request: Request):
        actor = current_actor(request)
        if not actor.is_admin:
            raise HTTPException(status_code=403, detail="只有管理员可以预览规则")
        if len(body.candidate_ids) > 30 or any(not SAFE.fullmatch(item)
                                                for item in body.candidate_ids):
            raise HTTPException(status_code=422, detail="最多预览 30 个候选")
        current, _ = rules_now()
        candidate_rules = validate_rules({"schema": RULE_SCHEMA,
                                          "revision": current["revision"] + 1,
                                          "rules": [row.model_dump() for row in body.rules]})
        rows = []
        for candidate_id in body.candidate_ids:
            matches = [item for item in service.list_candidates(search=candidate_id,
                       limit=200, offset=0, decision="all", high_risk=False)
                       if item["candidate_id"] == candidate_id]
            for item in matches:
                commit = service._commit(item["candidate_id"], item["version_id"])
                rows.append({"candidate_id": item["candidate_id"],
                             "version_id": item["version_id"],
                             **preview_label(commit, candidate_rules)})
        return {"preview": rows}

    @app.put("/api/v1/synthetic/provisional-rules")
    def update_rules(body: ProvisionalRulesInput, request: Request):
        actor = current_actor(request)
        if not actor.is_admin:
            raise HTTPException(status_code=403, detail="只有管理员可以发布规则")
        current, generation = rules_now()
        if current["revision"] != body.expected_revision:
            raise HTTPException(status_code=409, detail="规则版本已变化，请刷新")
        value = validate_rules({"schema": RULE_SCHEMA,
                                "revision": current["revision"] + 1,
                                "rules": [row.model_dump() for row in body.rules]})
        digest = hashlib.sha256(_canonical(value)).hexdigest()
        try:
            store.write_json(f"{prefix}/rules/versions/{digest}.json", value,
                             if_generation_match=0)
        except ObjectConflictError:
            pass
        try:
            store.write_json(rules_key, value, if_generation_match=generation)
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail="规则已被其他管理员修改") from error
        return {**value, "sha256": digest}
