"""Durable refresh jobs and atomic selection of isolated external-data generations."""

from __future__ import annotations

import json
import threading
import uuid
from contextvars import ContextVar

from imu_data_collector.external_device_catalog import ExternalDeviceCatalog
from imu_data_collector.external_device_domain import PREFIX, now, timestamp, utc
from imu_data_collector.external_device_service import ExternalDeviceService


class ExternalDeviceRuntime:
    def __init__(self, settings, store, initial: ExternalDeviceService | None = None):
        self.settings, self.store = settings, store
        self.control = ExternalDeviceCatalog(
            settings.annotation.catalog_path.with_name("external-device-control.sqlite3")
        )
        self.control.execute(
            "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY,state TEXT NOT NULL,"
            "created_at TEXT NOT NULL,body TEXT NOT NULL)"
        )
        self._services = {initial.generation: initial} if initial else {}
        self._lock = threading.RLock()
        self.bound: ContextVar[ExternalDeviceService | None] = ContextVar(
            "external_device_generation", default=None
        )

    def __getattr__(self, name):
        return getattr(self.current, name)

    @property
    def generation(self):
        return self.control.get_meta("active_generation", "legacy")

    @property
    def current(self):
        return self.bound.get() or self.service(self.generation)

    def service(self, generation):
        with self._lock:
            if generation not in self._services:
                self._services[generation] = ExternalDeviceService(
                    self.settings, self.store, generation=generation
                )
            return self._services[generation]

    def generations(self):
        return self.control.get_meta("generations", ["legacy"])

    def history_start(self):
        return self.control.get_meta("history_start", self.settings.external_devices.history_start)

    def set_history_start(self, start, source="configured"):
        start = utc(timestamp(start))
        if start >= now():
            raise ValueError("历史起点必须早于当前时间")
        with self.control.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM tasks WHERE state IN ('pending','running')").fetchone():
                raise ValueError("请等待当前更新或重建完成后修改起点")
            for key, value in (("history_start", start), ("history_start_source", source)):
                db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))
        return {"history_start": start, "history_start_source": source}

    def tasks(self):
        return [
            json.loads(row["body"])
            for row in self.control.rows(
                "SELECT body FROM tasks ORDER BY rowid DESC LIMIT 30"
            )
        ]

    def active_task(self):
        rows = self.control.rows("SELECT body FROM tasks WHERE state IN ('pending','running')")
        return json.loads(rows[0]["body"]) if rows else None

    def save_task(self, task):
        self.control.execute(
            "UPDATE tasks SET state=?,body=? WHERE id=?",
            (task["state"], json.dumps(task), task["id"]),
        )

    def request_task(self, mode, *, require_online=False, expected_start=None):
        if mode not in ("sync", "rebuild"):
            raise ValueError("Unknown external task")
        if require_online and not self.status()["worker_online"]:
            raise RuntimeError("未连接同步服务，请先启动已配置密钥的同步进程")
        with self.control.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if expected_start is not None and expected_start != self.history_start():
                raise ValueError("历史起点已改变，请重新确认范围")
            active = db.execute(
                "SELECT body FROM tasks WHERE state IN ('pending','running')"
            ).fetchone()
            if active:
                task = json.loads(active["body"])
                if task["mode"] != mode:
                    raise ValueError("另一项更新或重建正在执行，请等待完成")
                return task
            cutoff = now()
            if self.history_start() >= cutoff:
                raise ValueError("历史起点必须早于当前时间")
            generation = self.generation if mode == "sync" else uuid.uuid4().hex
            task = {
                "id": uuid.uuid4().hex,
                "mode": mode,
                "state": "pending",
                "phase": "queued",
                "generation": generation,
                "previous_generation": self.generation,
                "history_start": self.history_start(),
                "cutoff": cutoff,
                "created_at": cutoff,
                "started_at": None,
                "finished_at": None,
                "devices": [],
                "devices_done": 0,
                "devices_total": 0,
                "windows_done": 0,
                "windows_total": 0,
                "request_count": 0,
                "response_bytes": 0,
                "fetched_records": 0,
                "new_records": 0,
                "error": None,
            }
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?)",
                (task["id"], task["state"], cutoff, json.dumps(task)),
            )
            generations = list(dict.fromkeys([*self.generations(), generation]))
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('generations',?)", (json.dumps(generations),)
            )
        return task

    def persist(self, *, active_generation=None, published_task=None):
        key = f"{PREFIX}/runtime.json"
        previous = self.store.stat(key)
        data = {
            "schema_version": 1,
            "active_generation": active_generation or self.generation,
            "generations": self.generations(),
            "history_start": self.history_start(),
            "history_start_source": self.control.get_meta("history_start_source", "configured"),
            "published_task": published_task or self.control.get_meta("published_task"),
        }
        self.store.write_json(key, data, if_generation_match=previous.generation if previous else 0)

    def recover(self):
        key = f"{PREFIX}/runtime.json"
        if self.store.stat(key):
            saved, _ = self.store.read_json(key)
            # The durable object pointer is written before the local commit. Reconcile a
            # crash between those two writes without exposing an incomplete generation.
            self.control.set_meta("active_generation", saved["active_generation"])
            self.control.set_meta("published_task", saved.get("published_task"))
            generations = list(dict.fromkeys([*saved["generations"], *self.generations()]))
            self.control.set_meta("generations", generations)
            for field in ("history_start", "history_start_source"):
                if self.control.get_meta(field) is None:
                    self.control.set_meta(field, saved[field])
        task = next(
            (
                task
                for task in self.tasks()
                if task["id"] == self.control.get_meta("published_task")
            ),
            None,
        )
        if task and task["state"] != "done":
            task.update(state="done", phase="complete", finished_at=now())
            self.save_task(task)
        for generation in self.generations():
            service = self.service(generation)
            service.rebuild()
            service.catalog.recover_running()

    def publish(self, task):
        target = self.service(task["generation"])
        if target.catalog.rows("PRAGMA integrity_check")[0]["integrity_check"] != "ok":
            raise RuntimeError("新数据库完整性校验失败")
        if target.catalog.get_meta("aggregation_version") != 1:
            raise RuntimeError("新数据库统计索引尚未就绪")
        target.persist_control()
        # Never replace a live SQLite/WAL file. Readers retain their pinned old path.
        self.persist(active_generation=task["generation"], published_task=task["id"])
        self.activate(task)

    def activate(self, task):
        with self.control.connect() as db:
            for key, value in (
                ("active_generation", task["generation"]),
                ("published_task", task["id"]),
            ):
                db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def public_task(self, task):
        if not task:
            return None
        result = {key: value for key, value in task.items() if key != "devices"}
        end = task["finished_at"] or now()
        result["elapsed_s"] = max(
            0,
            (timestamp(end) - timestamp(task["started_at"] or task["created_at"])).total_seconds(),
        )
        return result

    def status(self):
        tasks = self.tasks()
        heartbeat = self.control.get_meta("worker_heartbeat")
        online = bool(heartbeat) and (timestamp(now()) - timestamp(heartbeat)).total_seconds() < 180
        return {
            **self.current.status(),
            "generation": self.generation,
            "history_start": self.history_start(),
            "history_start_source": self.control.get_meta("history_start_source", "configured"),
            "worker_heartbeat": heartbeat,
            "worker_online": online and self.control.get_meta("credentials_configured", False),
            "paused": self.control.get_meta("paused", False),
            "error": self.control.get_meta("worker_error"),
            "task": self.public_task(self.active_task() or (tasks[0] if tasks else None)),
        }

    def export_rows(self):
        return [
            (self.service(generation), row)
            for generation in self.generations()
            for row in self.service(generation).catalog.rows("SELECT * FROM exports")
        ]

    def export_job(self, identifier):
        for source, row in self.export_rows():
            if row["id"] == identifier:
                return source, row
        raise KeyError(identifier)
