"""活动分类表的对象存储版本与并发控制。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from imu_data_collector.models import ActivityTaxonomyDefinition
from imu_data_collector.storage import ObjectConflictError, ObjectStore


class ActivityTaxonomyStore:
    """保存 current 指针和不可变历史快照。"""

    def __init__(self, store: ObjectStore, seed: dict[str, Any]) -> None:
        self.store = store
        self.taxonomy_id = str(seed["taxonomy_id"])
        self._lock = threading.RLock()
        self._seed = self._normalize(seed)
        self._ensure_initialized()

    @property
    def current_key(self) -> str:
        return f"taxonomies/{self.taxonomy_id}/current.json"

    def version_key(self, version: str) -> str:
        safe = version.replace("+", "_").replace("/", "_")
        return f"taxonomies/{self.taxonomy_id}/versions/{safe}.json"

    @staticmethod
    def _normalize(payload: dict[str, Any]) -> ActivityTaxonomyDefinition:
        normalized = dict(payload)
        normalized.setdefault("revision", 1)
        for label in ("fall", "non_fall"):
            normalized[label] = [
                {**item, "active": bool(item.get("active", True))}
                for item in normalized[label]
            ]
        return ActivityTaxonomyDefinition.model_validate(normalized)

    def _ensure_initialized(self) -> None:
        try:
            current, _generation = self.store.read_json(self.current_key)
            definition = self._normalize(current)
        except FileNotFoundError:
            definition = self._seed
            try:
                self.store.write_json(
                    self.current_key,
                    definition.model_dump(mode="json"),
                    if_generation_match=0,
                )
            except ObjectConflictError:
                current, _generation = self.store.read_json(self.current_key)
                definition = self._normalize(current)
        try:
            self.store.write_json(
                self.version_key(definition.version),
                definition.model_dump(mode="json"),
                if_generation_match=0,
            )
        except ObjectConflictError:
            pass

    def current(self) -> tuple[ActivityTaxonomyDefinition, int]:
        payload, generation = self.store.read_json(self.current_key)
        return self._normalize(payload), generation

    def version(self, version: str) -> ActivityTaxonomyDefinition:
        payload, _generation = self.store.read_json(self.version_key(version))
        return self._normalize(payload)

    def mutate(
        self,
        expected_version: str,
        update: Callable[[ActivityTaxonomyDefinition], ActivityTaxonomyDefinition],
    ) -> ActivityTaxonomyDefinition:
        with self._lock:
            current, generation = self.current()
            if current.version != expected_version:
                raise ObjectConflictError("活动标签已经更新，请刷新后重试")
            changed = ActivityTaxonomyDefinition.model_validate(
                update(current).model_dump(mode="json")
            )
            revision = current.revision + 1
            base_version = current.version.split("+", 1)[0]
            changed = ActivityTaxonomyDefinition.model_validate(
                changed.model_dump(mode="json")
                | {"revision": revision, "version": f"{base_version}+r{revision}"}
            )
            payload = changed.model_dump(mode="json")
            snapshot_key = self.version_key(changed.version)
            try:
                self.store.write_json(snapshot_key, payload, if_generation_match=0)
            except ObjectConflictError:
                existing, _existing_generation = self.store.read_json(snapshot_key)
                if self._normalize(existing) != changed:
                    raise
            self.store.write_json(
                self.current_key,
                payload,
                if_generation_match=generation,
            )
            return changed
