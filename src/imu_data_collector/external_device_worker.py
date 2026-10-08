"""Single upstream reader; incremental work takes priority over historical backfill."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta

from imu_data_collector.external_device_domain import DEVICE_TYPES, PREFIX, now, timestamp, utc
from imu_data_collector.external_device_insights import backfill_step
from imu_data_collector.external_device_runtime import ExternalDeviceRuntime
from imu_data_collector.external_device_service import (
    CredentialsError,
    ExternalDeviceService,
    HistoryClient,
    ResponseTooLarge,
    UpstreamError,
    UpstreamTimeout,
)
from imu_data_collector.file_lock import exclusive_file_lock

logger = logging.getLogger(__name__)


class ExternalDeviceWorker:
    def __init__(self, service: ExternalDeviceService, client: HistoryClient | None = None):
        self.runtime = (
            service
            if isinstance(service, ExternalDeviceRuntime)
            else ExternalDeviceRuntime(service.settings, service.store, initial=service)
        )
        self.service = self.runtime.current
        self.catalog = self.service.catalog
        self.client = client or HistoryClient(service.settings)
        self.config = service.settings.external_devices
        self.next_discovery = 0.0
        self.last_control = 0.0
        self.task = None
        self.response_bytes = 0
        self.fetched_records = 0

    def initialize(self) -> None:
        self.runtime.recover()
        self.runtime.control.set_meta("paused", False)
        self.runtime.control.set_meta(
            "credentials_configured", bool(getattr(self.client, "key", True))
        )

    def discover(self, at: str) -> None:
        failures = []
        for kind in DEVICE_TYPES:
            try:
                if self.task:
                    self.task["request_count"] += 1
                    self.runtime.save_task(self.task)
                devices = self.client.devices(kind)
                if self.task:
                    self.task["response_bytes"] += getattr(self.client, "last_response_bytes", 0)
                self.catalog.discover(kind, devices, at)
            except CredentialsError:
                raise
            except Exception:
                failures.append(kind)
        if failures:
            raise UpstreamError("Device discovery failed: " + ", ".join(failures))
        self.catalog.set_meta("last_discovery", at)

    def synchronize(self, job: dict) -> None:
        try:
            device = self.catalog.device(job["device_id"])
            try:
                obj, raw = self.client.request(
                    device["kind"],
                    {
                        "deviceNo": device["history_no"],
                        "startTime": job["start"],
                        "endTime": job["end"],
                    },
                )
            finally:
                self.response_bytes = getattr(self.client, "last_response_bytes", 0)
            self.response_bytes = len(raw)
            self.fetched_records = obj.get("data", {}).get("total", 0)
            self.service.archive(device, job["start"], job["end"], obj, raw, append_only=True)
            self.catalog.execute(
                "UPDATE windows SET state='done',error=NULL,retry_at='',updated_at=? WHERE id=?",
                (now(), job["id"]),
            )
        except CredentialsError as error:
            self.catalog.fail_window(job, str(error))
            raise
        except (ResponseTooLarge, UpstreamTimeout) as error:
            start, end = timestamp(job["start"]), timestamp(job["end"])
            if (end - start).total_seconds() > 60:
                midpoint = utc(start + (end - start) / 2)
                self.catalog.enqueue_window(job["device_id"], job["start"], midpoint, job["lane"])
                self.catalog.enqueue_window(job["device_id"], midpoint, job["end"], job["lane"])
                if job.get("task_id"):
                    self.catalog.execute(
                        "UPDATE windows SET task_id=? WHERE device_id=? AND start>=? AND end<=?",
                        (job["task_id"], job["device_id"], job["start"], job["end"]),
                    )
                self.catalog.execute(
                    "UPDATE windows SET state='split',error=?,updated_at=? WHERE id=?",
                    (str(error), now(), job["id"]),
                )
            else:
                self.catalog.fail_window(job, str(error))
        except UpstreamError as error:
            self.catalog.fail_window(job, str(error))
        except Exception as error:
            logger.error("External window failed (%s)", type(error).__name__)
            self.catalog.fail_window(job, "Response validation, archival or indexing failed")

    def plan_windows(self, task):
        # Completed coverage is evidence. Queue positions and last-record timestamps are not.
        self.catalog.execute("DELETE FROM windows WHERE state!='done'")
        plans = []
        for device in self.catalog.rows("SELECT * FROM devices WHERE listed=1"):
            origin = device["history_start"] or task["history_start"]
            initial = not self.catalog.coverage(device["id"], origin, device["first_seen"])[
                "complete"
            ]
            self.catalog.execute(
                "UPDATE devices SET history_start=?,scheduled_until=? WHERE id=?",
                (origin, task["cutoff"], device["id"]),
            )
            plans.append({"id": device["id"], "start": origin, "end": task["cutoff"]})
            for lower, upper in self.catalog.coverage(device["id"], origin, task["cutoff"])["gaps"]:
                start = timestamp(lower)
                while start < timestamp(upper):
                    end = min(start + timedelta(seconds=self.config.window_s), timestamp(upper))
                    self.catalog.enqueue_window(
                        device["id"], utc(start), utc(end), "history" if initial else "live"
                    )
                    self.catalog.execute(
                        "UPDATE windows SET task_id=? WHERE device_id=? AND start=? AND end=?",
                        (task["id"], device["id"], utc(start), utc(end)),
                    )
                    start = end
        task.update(devices=plans, devices_total=len(plans), phase="fetching")
        task["records_before"] = self.catalog.rows("SELECT COUNT(*) AS n FROM records")[0]["n"]
        self.service.persist_control()
        self.runtime.save_task(task)

    def progress(self, task):
        task["devices_done"] = sum(
            self.catalog.coverage(item["id"], item["start"], item["end"])["complete"]
            for item in task["devices"]
        )
        rows = self.catalog.rows(
            "SELECT state,COUNT(*) AS n FROM windows WHERE task_id=? "
            "AND state!='split' GROUP BY state",
            (task["id"],),
        )
        task["windows_total"] = sum(row["n"] for row in rows)
        task["windows_done"] = sum(row["n"] for row in rows if row["state"] == "done")
        task["new_records"] = self.catalog.rows("SELECT COUNT(*) AS n FROM records")[0][
            "n"
        ] - task.get("records_before", 0)
        self.runtime.save_task(task)

    def step(self) -> bool:
        control = self.runtime.control
        control.set_meta("worker_heartbeat", now())
        control.set_meta("credentials_configured", bool(getattr(self.client, "key", True)))
        task = self.runtime.active_task()
        if (
            not task
            and not control.get_meta("paused", False)
            and time.monotonic() >= self.next_discovery
        ):
            task = self.runtime.request_task("sync")
        if time.monotonic() - self.last_control > 15 and not (
            task and task["phase"] == "publishing"
        ):
            self.runtime.persist()
            self.last_control = time.monotonic()
        if not task:
            return False
        self.task = task
        self.service = self.runtime.service(task["generation"])
        self.catalog = self.service.catalog
        try:
            if task["state"] == "pending" or task["phase"] == "discovering":
                task.update(
                    state="running", phase="discovering", started_at=task["started_at"] or now()
                )
                control.set_meta("paused", False)
                control.set_meta("worker_error", None)
                self.runtime.save_task(task)
                self.discover(task["cutoff"])
                self.plan_windows(task)
            # Retry/recover only this task's uncommitted windows; completed coverage survives.
            job = self.catalog.next_window()
            if job:
                task["request_count"] += 1
                self.runtime.save_task(task)
                self.response_bytes = self.fetched_records = 0
                try:
                    self.synchronize(job)
                finally:
                    task["response_bytes"] += self.response_bytes
                    task["fetched_records"] += self.fetched_records
                    self.progress(task)
                failed = self.catalog.rows(
                    "SELECT error FROM windows WHERE id=? AND state='failed'", (job["id"],)
                )
                if failed:
                    raise UpstreamError(failed[0]["error"])
                return True
            self.progress(task)
            if task["devices_done"] != task["devices_total"]:
                raise UpstreamError("仍有未完成的查询区间，请重试")
            self.service.persist_control()
            if task["mode"] == "rebuild":
                task["phase"] = "publishing"
                self.runtime.save_task(task)
                self.runtime.publish(task)
            task.update(state="done", phase="complete", finished_at=now(), error=None)
            self.runtime.save_task(task)
            self.next_discovery = time.monotonic() + (
                0 if task["mode"] == "rebuild" else self.config.sync_interval_s
            )
            if task["mode"] != "rebuild":
                self.runtime.persist()
            return True
        except Exception as error:
            if task["phase"] == "publishing":
                try:
                    key = f"{PREFIX}/runtime.json"
                    saved = (
                        self.runtime.store.read_json(key)[0] if self.runtime.store.stat(key) else {}
                    )
                    committed = saved.get("published_task") == task["id"]
                except Exception:
                    committed = True  # An ambiguous commit must be reconciled before retry/failure.
                if committed:
                    task["error"] = "正在确认数据切换结果，将自动重试"
                    self.runtime.save_task(task)
                    return False
            message = (
                str(error)
                if isinstance(error, UpstreamError)
                else "归档、索引或切换失败；原数据仍可使用"
            )
            logger.error("External task failed (%s)", type(error).__name__)
            task.update(state="failed", phase="failed", error=message, finished_at=now())
            self.runtime.save_task(task)
            control.set_meta("worker_error", message)
            if isinstance(error, CredentialsError):
                control.set_meta("paused", True)
                self.catalog.set_meta("paused", True)
            self.next_discovery = time.monotonic() + self.config.sync_interval_s
            return False
        finally:
            self.task = None

    def export_one(self, source, job: dict) -> None:
        try:
            source.build_export(job)
        except Exception as error:
            logger.error("External export failed (%s)", type(error).__name__)
            source.catalog.execute(
                "UPDATE exports SET state='failed',error=?,updated_at=? WHERE id=?",
                ("Export failed; archive or index verification did not complete", now(), job["id"]),
            )

    @contextmanager
    def heartbeat(self):
        stopped = threading.Event()

        def pulse():
            while not stopped.is_set():
                try:
                    self.runtime.control.set_meta("worker_heartbeat", now())
                except Exception as error:
                    logger.error("External heartbeat failed (%s)", type(error).__name__)
                stopped.wait(10)

        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)

    def run(self, stop: threading.Event | None = None, *, once: bool = False) -> None:
        if not self.config.enabled:
            raise ValueError("external_devices.enabled must be true")
        stop = stop or threading.Event()
        lock = self.runtime.control.path.with_suffix(".worker.lock")
        with exclusive_file_lock(lock), self.heartbeat(), ThreadPoolExecutor(max_workers=1) as pool:
            self.initialize()
            export = None
            projection_cache = OrderedDict()
            while not stop.is_set():
                try:
                    worked = self.step()
                    worked = backfill_step(self.runtime.current, cache=projection_cache) or worked
                    if export is None or export.done():
                        jobs = [
                            (source, job)
                            for source, job in self.runtime.export_rows()
                            if job["state"] == "pending"
                        ]
                        if jobs:
                            source, job = min(jobs, key=lambda pair: pair[1]["created_at"])
                            source.catalog.execute(
                                "UPDATE exports SET state='running' WHERE id=?", (job["id"],)
                            )
                            export = pool.submit(self.export_one, source, job)
                    if once:
                        break
                    stop.wait(1 if worked else 5)
                except Exception as error:
                    logger.error("External worker iteration failed (%s)", type(error).__name__)
                    self.runtime.control.set_meta(
                        "worker_error", "Worker storage or index operation failed"
                    )
                    if once:
                        raise
                    stop.wait(30)
            self.service.persist_control()
