"""Versioned, controlled activity labels for synthetic motion.

Real IMU concepts remain owned by the existing taxonomy. Motion-only concepts
and exact source mappings live beside them, so a shared code has one authority
without exposing motion-only actions in the real IMU annotation form.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from imu_data_collector.storage import ObjectConflictError, ObjectStore

CODE = re.compile(r"^[a-z][a-z0-9_]*$")
RULE_ORIGINS = {"babel-1.0", "stageii-source-member"}


class SyntheticLabelRegistry:
    KEY = "taxonomies/motion-actions/current.json"

    def __init__(self, store: ObjectStore, real_taxonomies) -> None:
        self.store = store
        self.real_taxonomies = real_taxonomies
        try:
            store.read_json(self.KEY)
        except FileNotFoundError:
            try:
                store.write_json(self.KEY, {
                    "schema": "imu_annotation.motion_label_registry.v1",
                    "revision": 1, "concepts": [], "rules": [],
                }, if_generation_match=0)
            except ObjectConflictError:
                pass

    def _state(self) -> tuple[dict, int]:
        return self.store.read_json(self.KEY)

    def _update(self, mutate) -> dict:
        current, generation = self._state()
        changed = mutate({**current, "concepts": list(current["concepts"]),
                          "rules": list(current["rules"])})
        changed["revision"] = current["revision"] + 1
        self.store.write_json(self.KEY, changed, if_generation_match=generation)
        return changed

    def catalog(self) -> dict:
        real = self.real_taxonomies.current()[0]
        motion, _ = self._state()
        shared = [
            {"code": item.code, "name": item.name, "is_fall": group == "fall",
             "active": item.active, "scope": "shared"}
            for group in ("fall", "non_fall") for item in getattr(real, group)
        ]
        return {
            "taxonomy_id": "motion-actions",
            "version": f"{real.version}.motion-r{motion['revision']}",
            "concepts": shared + motion["concepts"],
            "rules": motion["rules"],
        }

    def concept(self, code: str) -> dict:
        for item in self.catalog()["concepts"]:
            if item["code"] == code and item["active"]:
                return item
        raise ValueError("正式标签不在当前受控列表中")

    def add_concept(self, *, code: str, name: str, is_fall: bool,
                    actor: str) -> dict:
        if not CODE.fullmatch(code) or not name.strip() or type(is_fall) is not bool:
            raise ValueError("Invalid activity concept")
        if any(item["code"] == code for item in self.catalog()["concepts"]):
            raise ValueError("Activity code already exists")

        def change(state):
            if any(item["code"] == code for item in state["concepts"]):
                raise ValueError("Activity code already exists")
            state["concepts"].append({
                "code": code, "name": name.strip(), "is_fall": is_fall,
                "active": True, "scope": "motion", "created_by": actor,
                "created_at_utc": datetime.now(UTC).isoformat(),
            })
            return state

        return self._update(change)

    def add_rule(self, *, origin: str, source_value: str,
                 target_code: str, source_dataset: str | None, actor: str) -> dict:
        if origin not in RULE_ORIGINS or not source_value.strip():
            raise ValueError("Invalid mapping source")
        if self.concept(target_code)["is_fall"]:
            raise ValueError("跌倒标签必须逐条人工确认，不能由来源词自动赋值")
        rule = {
            "rule_id": uuid4().hex, "origin": origin,
            "source_value": source_value.strip().lower(),
            "source_dataset": source_dataset or None,
            "target_code": target_code, "state": "draft",
            "created_by": actor, "created_at_utc": datetime.now(UTC).isoformat(),
        }
        self._update(lambda state: {**state, "rules": state["rules"] + [rule]})
        return rule

    @staticmethod
    def matches(rule: dict, commit: dict) -> bool:
        if rule["source_dataset"] and rule["source_dataset"] != commit["source_dataset"]:
            return False
        labels = commit.get("label_candidates") or []
        if rule["origin"] == "babel-1.0":
            recording = [item for item in labels if item.get("origin") == "babel-1.0"
                         and item.get("kind") == "recording-candidate"]
            temporal = [item for item in labels if item.get("origin") == "babel-1.0"
                        and item.get("kind") == "temporal-candidate"]
            categories = {category.lower() for item in recording
                          for category in item.get("categories") or []}
            temporal_categories = {category.lower() for item in temporal
                                   for category in item.get("categories") or []}
            # Missing or conflicting labels must remain unresolved. A sequence
            # with several activities is not given a single recording label.
            return categories == {rule["source_value"]} and (
                not temporal_categories or temporal_categories == categories)
        recording = [item for item in labels if item.get("origin") == "stageii-source-member"
                     and item.get("kind") == "recording-candidate"]
        return len(recording) == 1 and recording[0].get("code", "").lower() == rule["source_value"]

    def active_label(self, commit: dict, catalog: dict | None = None) -> dict | None:
        catalog = catalog or self.catalog()
        matched = [rule for rule in catalog["rules"] if rule["state"] == "active"
                   and self.matches(rule, commit)]
        if len(matched) != 1:
            return None
        rule = matched[0]
        concept = next((item for item in catalog["concepts"]
                        if item["code"] == rule["target_code"] and item["active"]), None)
        if concept is None:
            return None
        return {
            "code": concept["code"], "name": concept["name"],
            "is_fall": concept["is_fall"],
            "taxonomy_id": catalog["taxonomy_id"], "taxonomy_version": catalog["version"],
            "origin": "auto", "verification": "rule",
            "mapping_rule_id": rule["rule_id"],
        }

    def set_rule_state(self, rule_id: str, state: str, *,
                       checked_candidate_ids: list[str], required_count: int,
                       actor: str) -> dict:
        if state not in {"active", "retired"}:
            raise ValueError("Invalid mapping state")
        if state == "active" and len(set(checked_candidate_ids)) < required_count:
            raise ValueError("映射抽检数量不足")

        def change(value):
            found = False
            rules = []
            for rule in value["rules"]:
                if rule["rule_id"] == rule_id:
                    if rule["state"] == "retired":
                        raise ValueError("Retired mapping cannot be reactivated")
                    rule = {**rule, "state": state,
                            "reviewed_candidate_ids": checked_candidate_ids,
                            "activated_by": actor,
                            "updated_at_utc": datetime.now(UTC).isoformat()}
                    found = True
                rules.append(rule)
            if not found:
                raise FileNotFoundError(rule_id)
            value["rules"] = rules
            return value

        return self._update(change)
