"""Read-only upstream client, immutable archives, browser projections and exports."""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
import re
import tempfile
import time
import uuid
import zipfile
from collections import OrderedDict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import requests

from imu_data_collector import external_device_aggregation as aggregation
from imu_data_collector import external_device_insights as insights
from imu_data_collector.config import Settings
from imu_data_collector.external_device_catalog import ExternalDeviceCatalog
from imu_data_collector.external_device_domain import (
    BASE_URL,
    DEVICE_TYPES,
    PREFIX,
    digest,
    json_bytes,
    metrics,
    now,
    timestamp,
    utc,
    validate_history,
)
from imu_data_collector.external_device_labels import bilingual
from imu_data_collector.storage import ObjectStore


class UpstreamError(RuntimeError):
    pass


class CredentialsError(UpstreamError):
    pass


class ResponseTooLarge(UpstreamError):
    pass


class UpstreamTimeout(UpstreamError):
    pass


class HistoryClient:
    def __init__(self, settings: Settings, *, key: str | None = None, session=None):
        self.settings = settings.external_devices
        self.key = key if key is not None else os.environ.get("IMU_SYNERGLOBAL_API_KEY", "")
        self.session = session or requests.Session()

    def request(self, kind: str, payload: dict | None = None) -> tuple[dict, bytes]:
        if kind not in DEVICE_TYPES:
            raise ValueError("Unsupported device type")
        if not self.key or len(self.key) > 512:
            raise CredentialsError("External API key is not configured")
        url = f"{BASE_URL}/{kind}" + ("/devices" if payload is None else "")
        started = time.monotonic()
        self.last_response_bytes = 0
        try:
            with self.session.request(
                "GET" if payload is None else "POST",
                url,
                json=payload,
                headers={"X-API-Key": self.key, "Accept": "application/json"},
                timeout=(10, self.settings.request_timeout_s),
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code in (401, 403):
                    raise CredentialsError(
                        "External API rejected its credential; synchronization paused"
                    )
                if response.status_code != 200:
                    raise UpstreamError(f"External API returned HTTP {response.status_code}")
                raw = bytearray()
                for chunk in response.iter_content(65536):
                    raw.extend(chunk)
                    self.last_response_bytes = len(raw)
                    if len(raw) > self.settings.max_response_bytes:
                        raise ResponseTooLarge("External response exceeds configured size limit")
                    if time.monotonic() - started > self.settings.request_timeout_s:
                        raise UpstreamTimeout("External request exceeded its time budget")
        except requests.Timeout:
            raise UpstreamTimeout("External request timed out") from None
        except requests.RequestException:
            # Requests exception strings can contain request details; never persist them.
            raise UpstreamError("External connection failed") from None
        try:
            obj = json.loads(raw)
            json_bytes(obj)  # Reject non-standard NaN/Infinity before publishing evidence.
            if not isinstance(obj, dict):
                raise ValueError
        except (ValueError, TypeError):
            raise UpstreamError("External response is not a valid JSON object") from None
        self.last_response_bytes = len(raw)
        return obj, bytes(raw)

    def devices(self, kind: str) -> list[dict]:
        obj, _ = self.request(kind)
        data = obj.get("data")
        if (
            obj.get("code") != 200
            or not isinstance(data, dict)
            or data.get("deviceType") != DEVICE_TYPES[kind]
            or not isinstance(data.get("devices"), list)
            or type(data.get("total")) is not int
            or data["total"] != len(data["devices"])
        ):
            raise UpstreamError("Invalid external device list")
        seen = set()
        for item in data["devices"]:
            if not isinstance(item, dict):
                raise UpstreamError("Invalid external device identity")
            history = item.get("historyDeviceNo")
            number = item.get("deviceNo")
            if (
                not isinstance(history, str)
                or len(history) > 255
                or not re.fullmatch(r"[A-Za-z0-9_:/-]+", history)
                or not isinstance(number, str)
                or not number
                or not isinstance(item.get("displayName"), str)
                or history in seen
            ):
                raise UpstreamError("Invalid external device identity")
            if kind == "mattress":
                product = item.get("productKey")
                if (
                    not isinstance(product, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", product)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", number)
                    or history != f"mattress:{product}/{number}"
                ):
                    raise UpstreamError("Invalid external mattress identity")
            elif history != number:
                raise UpstreamError("Invalid external watch identity")
            seen.add(history)
        return data["devices"]


METRIC_LABELS = {
    "HeartRate": ("心率", "bpm"),
    "RespiratoryRate": ("呼吸率", "次/分钟"),
    "People_flag": ("在床标志原值", "原值"),
    "in_bed": ("在床状态", "0/1"),
    "D": ("睡眠状态原值", "原值"),
    "sleep_stage": ("睡眠状态", "状态编号"),
    "E": ("呼吸暂停持续时间", "秒"),
    "moving": ("体动", "状态编号"),
    "RSSI": ("Wi-Fi 信号", "dBm"),
    "HR": ("心率", "原值"),
    "SPO": ("血氧", "原值"),
    "HT": ("体温", "原值"),
    "AT": ("环境温度", "原值"),
    "SBP": ("收缩压", "原值"),
    "DBP": ("舒张压", "原值"),
    "BRR": ("呼吸率", "原值"),
    "BP": ("电池电量", "%"),
    "ST": ("总步数", "步"),
    "CST": ("区间步数", "步"),
    "KCAL": ("热量原值", "原值 ×0.1 kcal"),
}

# Keep the original CSV columns in their original positions; new columns are appended.
LEGACY_METRIC_KEYS = tuple(METRIC_LABELS)

METRIC_LABELS.update({key: value[:2] for key, value in insights.NEW_METRICS.items()})


class ExternalDeviceService:
    def __init__(self, settings: Settings, store: ObjectStore, generation: str = "legacy"):
        self.settings = settings
        self.store = store
        if generation != "legacy" and not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise ValueError("Invalid external generation")
        self.generation = generation
        self.prefix = PREFIX if generation == "legacy" else f"{PREFIX}/generations/{generation}"
        path = settings.annotation.catalog_path.with_name("external-devices.sqlite3")
        if generation != "legacy":
            path = path.parent / "external-device-generations" / f"{generation}.sqlite3"
        self.catalog = ExternalDeviceCatalog(path)
        self.cache_root = settings.storage.cache_root / "external-devices"

    def archive(
        self, device: dict, start: str, end: str, obj: dict, raw: bytes, *, append_only=False
    ) -> dict:
        records = validate_history(obj, device, start, end)
        if json.loads(raw) != obj:
            raise ValueError("Response bytes do not match parsed response")
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM meta WHERE key='archive_sequence'").fetchone()
            sequence = (json.loads(row["value"]) if row else 0) + 1
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('archive_sequence',?)", (json.dumps(sequence),)
            )
        fetched = now()
        identifier = uuid.uuid4().hex
        base = f"{self.prefix}/archives/{device['id']}/{identifier}"
        compressed = gzip.compress(raw, mtime=0)
        manifest = {
            "schema_version": 1,
            "append_only": append_only,
            "sequence": sequence,
            "id": identifier,
            "device": device,
            "start": start,
            "end": end,
            "fetched_at": fetched,
            "total": len(records),
            "object_key": base + ".json.gz",
            "manifest_key": base + ".manifest.json",
            "sha256": digest(compressed),
            "response_sha256": digest(raw),
            "size_bytes": len(compressed),
            "response_bytes": len(raw),
        }
        self.put_bytes(compressed, manifest["object_key"], "application/gzip")
        self.store.write_json(manifest["manifest_key"], manifest, if_generation_match=0)
        self.catalog.index_archive(manifest, records)
        return manifest

    def put_bytes(self, content: bytes, key: str, content_type: str) -> None:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.cache_root) as folder:
            file = Path(folder) / "upload"
            file.write_bytes(content)
            info = self.store.put_file(
                file, key, content_type=content_type, metadata={"sha256": digest(content)}
            )
            if info.size_bytes != len(content) or info.metadata.get("sha256") != digest(content):
                raise OSError("Archive upload verification failed")

    def load_archive(self, manifest: dict) -> dict:
        key = manifest["object_key"]
        expected_base = f"{self.prefix}/archives/{manifest['device']['id']}/{manifest['id']}"
        if (
            key != expected_base + ".json.gz"
            or manifest["manifest_key"] != expected_base + ".manifest.json"
        ):
            raise ValueError("Archive location mismatch")
        info = self.store.stat(key)
        if info is None or info.size_bytes != manifest["size_bytes"]:
            raise ValueError("Missing or truncated external archive")
        compressed = self.store.read_bytes(key)
        if digest(compressed) != manifest["sha256"]:
            raise ValueError("External archive checksum mismatch")
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(manifest["response_bytes"] + 1)
        if len(raw) != manifest["response_bytes"] or digest(raw) != manifest["response_sha256"]:
            raise ValueError("External response checksum mismatch")
        obj = json.loads(raw)
        validate_history(obj, manifest["device"], manifest["start"], manifest["end"])
        return obj

    def original(self, row: dict, cache: OrderedDict | None = None) -> dict:
        cache = cache if cache is not None else OrderedDict()
        aid = row["archive_id"]
        if aid not in cache:
            archives = self.catalog.rows("SELECT * FROM archives WHERE id=?", (aid,))
            if not archives:
                raise KeyError("Archive not found")
            manifest, _ = self.store.read_json(archives[0]["manifest_key"])
            cache[aid] = self.load_archive(manifest)["data"]["records"]
            while len(cache) > 4:
                cache.popitem(last=False)
        record = cache[aid][row["position"]]
        if digest(json_bytes(record)) != row["hash"]:
            raise ValueError("External index does not match its archive")
        return record

    def persist_control(self) -> None:
        control = {
            "schema_version": 1,
            "devices": self.catalog.rows("SELECT * FROM devices"),
            "confirmed_history_start": self.catalog.get_meta("confirmed_history_start"),
            "updated_at": now(),
        }
        key = f"{self.prefix}/control.json"
        current = self.store.stat(key)
        self.store.write_json(
            key, control, if_generation_match=current.generation if current else 0
        )

    def rebuild(self) -> dict:
        """Replay immutable manifests; safe to repeat after a DB or index-commit failure."""
        control_info = self.store.stat(f"{self.prefix}/control.json")
        control = self.store.read_json(control_info.key)[0] if control_info else {}
        for device in control.get("devices", []):
            with self.catalog.connect() as db:
                db.execute(
                    """INSERT INTO devices
                    (id,kind,history_no,device_no,product_key,display_name,listed,first_seen,scheduled_until,history_start)
                    VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
                    tuple(
                        device[k]
                        for k in (
                            "id",
                            "kind",
                            "history_no",
                            "device_no",
                            "product_key",
                            "display_name",
                            "listed",
                            "first_seen",
                            "first_seen",
                        )
                    )
                    + (device.get("history_start"),),
                )
        indexed = {row["id"] for row in self.catalog.rows("SELECT id FROM archives")}
        count = 0
        manifests = []
        for info in self.store.list(f"{self.prefix}/archives/"):
            if not info.key.endswith(".manifest.json"):
                continue
            manifest, _ = self.store.read_json(info.key)
            if manifest["manifest_key"] != info.key:
                raise ValueError("Invalid archive manifest location")
            if manifest["id"] in indexed:
                continue
            manifests.append(manifest)
        # Append-only replay must choose the same first version after DB loss.
        for manifest in sorted(
            manifests, key=lambda item: (item.get("sequence", 0), item["fetched_at"], item["id"])
        ):
            obj = self.load_archive(manifest)
            self.catalog.index_archive(manifest, obj["data"]["records"])
            count += 1
        if control.get("confirmed_history_start") and not self.catalog.get_meta(
            "confirmed_history_start"
        ):
            self.catalog.confirm_history(control["confirmed_history_start"])
        for device in self.catalog.rows("SELECT * FROM devices"):
            cursor = self.catalog.contiguous_until(device["id"], device["first_seen"], now())
            self.catalog.execute(
                "UPDATE devices SET scheduled_until=MAX(scheduled_until,?) WHERE id=?",
                (cursor, device["id"]),
            )
        self.catalog.set_meta("initialized", True)
        aggregation.rebuild(self.catalog)
        return {"indexed_archives": count}

    def metric_metadata(self, identifier: str) -> dict:
        rows = self.catalog.rows("SELECT * FROM metric_metadata WHERE device_id=?", (identifier,))
        preparation = insights.preparation(self.catalog, identifier)
        kind = self.catalog.device(identifier)["kind"]
        preferred = ("HeartRate", "HR", "RespiratoryRate", "BRR")
        rows.sort(
            key=lambda row: (
                preferred.index(row["metric"]) if row["metric"] in preferred else len(preferred),
                row["metric"],
            )
        )
        return {
            "ready": self.catalog.get_meta("aggregation_version") == 1,
            "preparation": preparation,
            "metrics": [
                bilingual(
                    {
                        "key": row["metric"],
                        "label": METRIC_LABELS.get(row["metric"], (row["metric"], "原值"))[0],
                        "unit": METRIC_LABELS.get(row["metric"], (row["metric"], "原值"))[1],
                        "method": aggregation.method(row["metric"]),
                        "quick": row["metric"] in insights.QUICK_METRICS[kind],
                        "group": insights.NEW_METRICS.get(
                            row["metric"],
                            (
                                None,
                                None,
                                "常用"
                                if row["metric"] in insights.QUICK_METRICS[kind]
                                else "更多指标",
                            ),
                        )[2],
                        "count": row["count"],
                        "first_record": row["first_record"],
                        "last_record": row["last_record"],
                    },
                    "label",
                    "unit",
                    "group",
                )
                for row in rows
                if preparation["ready"] or row["metric"] not in insights.NEW_METRICS
            ],
        }

    def aggregate_series(
        self, identifier: str, metric: str, start: str, end: str, budget: int
    ) -> dict:
        result = aggregation.project(self.catalog, identifier, metric, start, end, budget)
        result["label"], result["unit"] = METRIC_LABELS.get(metric, (metric, "原值"))
        return bilingual(result, "label", "unit")

    def status(self) -> dict:
        states = self.catalog.rows(
            "SELECT lane,state,COUNT(*) AS count FROM windows GROUP BY lane,state"
        )
        return {
            "sync_interval_s": self.settings.external_devices.sync_interval_s,
            "history_start": self.catalog.get_meta("confirmed_history_start"),
            "last_discovery": self.catalog.get_meta("last_discovery"),
            "worker_heartbeat": self.catalog.get_meta("worker_heartbeat"),
            "paused": self.catalog.get_meta("paused", False),
            "error": self.catalog.get_meta("worker_error"),
            "windows": states,
        }

    def devices(self) -> list[dict]:
        result = self.catalog.rows("SELECT * FROM devices ORDER BY kind,history_no")
        for device in result:
            stats = self.catalog.rows(
                "SELECT * FROM device_stats WHERE device_id=?", (device["id"],)
            )
            device.update(
                stats[0]
                if stats
                else {
                    "total": 0,
                    "synthetic_records": 0,
                    "first_record": None,
                    "last_record": None,
                    "last_success": None,
                }
            )
            errors = self.catalog.rows(
                """SELECT start,end,error FROM windows WHERE device_id=?
                AND state='failed' ORDER BY updated_at DESC LIMIT 1""",
                (device["id"],),
            )
            device["last_error"] = errors[0] if errors else None
            device["synced_until"] = self.catalog.contiguous_until(
                device["id"],
                device["history_start"] or device["first_seen"],
                device["scheduled_until"],
            )
            device["history_complete"] = (
                bool(device["history_start"])
                and self.catalog.coverage(
                    device["id"], device["history_start"], device["first_seen"]
                )["complete"]
            )
            device["initial_sync"] = (
                bool(device["history_start"]) and not device["history_complete"]
            )
        return result

    def calendar(self, identifier: str, year: int, month: int) -> dict:
        device = self.catalog.device(identifier)
        begin = datetime(year, month, 1, tzinfo=UTC) - timedelta(hours=8)
        following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        end = datetime.combine(following, datetime.min.time(), UTC) - timedelta(hours=8)
        counts = {
            row["day"]: row
            for row in self.catalog.rows(
                """SELECT date(received_at,'+8 hours') AS day,
            COUNT(*) AS total,SUM(synthetic) AS synthetic_records FROM records
            WHERE device_id=? AND received_at>=? AND received_at<? GROUP BY day""",
                (identifier, utc(begin), utc(end)),
            )
        }
        days = []
        cursor = begin
        current = now()
        while cursor < end:
            start, stop = utc(cursor), utc(cursor + timedelta(days=1))
            day = (cursor + timedelta(hours=8)).date().isoformat()
            info = {"day": day, "total": 0, "synthetic_records": 0, **counts.get(day, {})}
            coverage = self.catalog.coverage(identifier, start, stop)
            failed = self.catalog.rows(
                """SELECT 1 FROM windows WHERE device_id=?
                AND state='failed' AND end>? AND start<? LIMIT 1""",
                (identifier, start, stop),
            )
            info["state"] = (
                "future"
                if start > current
                else "failed"
                if failed
                else "complete"
                if coverage["complete"]
                else "partial"
                if coverage["covered"]
                else "unqueried"
            )
            days.append(info)
            cursor += timedelta(days=1)
        origin = device["history_start"] or device["first_seen"]
        return {
            "years": list(range(timestamp(origin).year, datetime.now(UTC).year + 1)),
            "days": days,
        }

    @staticmethod
    def public_record(row: dict, *, additions_ready: bool = True) -> dict:
        return {
            "id": row["id"],
            "source_id": row["source_id"],
            "message_type": row["message_type"],
            "received_at": row["received_at"],
            "metrics": {
                key: value
                for key, value in json.loads(row["metric_json"]).items()
                if additions_ready or key not in insights.NEW_METRICS
            },
            "synthetic": bool(row["synthetic"]),
        }

    def series(self, identifier: str, start: str, end: str, bins: int = 500) -> dict:
        lower = timestamp(start).timestamp() * 1000
        span = (timestamp(end).timestamp() * 1000 - lower) / bins
        buckets: dict = {}
        counts: dict = {}
        previous: dict = {}
        kind = self.catalog.device(identifier)["kind"]
        additions_ready = insights.preparation(self.catalog, identifier)["ready"]
        gap_ms = 10000 if kind == "mattress" else 1800000
        with self.catalog.connect() as db:
            rows = db.execute(
                """SELECT received_at,metric_json,synthetic FROM records
                WHERE device_id=? AND received_at>=? AND received_at<? ORDER BY received_at,id""",
                (identifier, start, end),
            )
            for row in rows:
                t = timestamp(row["received_at"]).timestamp() * 1000
                index = min(bins - 1, int((t - lower) / span))
                for field, value in json.loads(row["metric_json"]).items():
                    if not additions_ready and field in insights.NEW_METRICS:
                        continue
                    if field in previous and t - previous[field] > gap_ms:
                        gap_time = previous[field] + 1
                        gap_index = min(bins - 1, int((gap_time - lower) / span))
                        buckets.setdefault(field, {}).setdefault(gap_index, {})["gap"] = [
                            gap_time,
                            None,
                            False,
                        ]
                    previous[field] = t
                    counts[field] = counts.get(field, 0) + 1
                    point = [t, value, bool(row["synthetic"])]
                    bucket = buckets.setdefault(field, {}).setdefault(index, {})
                    bucket.setdefault("first", point)
                    bucket["last"] = point
                    if value is None:
                        bucket["gap"] = point
                    else:
                        if "min" not in bucket or value < bucket["min"][1]:
                            bucket["min"] = point
                        if "max" not in bucket or value > bucket["max"][1]:
                            bucket["max"] = point
        result = []
        for field, values in buckets.items():
            points = sorted(
                {tuple(point) for bucket in values.values() for point in bucket.values()},
                key=lambda point: (point[0], point[1] is None, point[1] or 0, point[2]),
            )
            label, unit = METRIC_LABELS.get(field, (field, "原值"))
            result.append(
                {
                    "key": field,
                    "label": label,
                    "unit": unit,
                    "points": [list(point) for point in points],
                    "source_points": counts[field],
                    "sampled": counts[field] > len(points),
                }
            )
        return {"series": result, "coverage": self.catalog.coverage(identifier, start, end)}

    def write_export_device(
        self, db, device: dict, start: str, end: str, folder: Path, snapshot_at: str
    ) -> dict:
        folder.mkdir(parents=True, exist_ok=True)
        records_path, csv_path = folder / "records.json", folder / "records.csv"
        cache: OrderedDict = OrderedDict()
        count, synthetic = 0, 0
        coverage = self.catalog.coverage(device["id"], start, end, db=db)
        with (
            records_path.open("w", encoding="utf-8") as output,
            csv_path.open("w", newline="", encoding="utf-8-sig") as sheet,
        ):
            output.write("[")
            writer = csv.writer(sheet)
            writer.writerow(
                [
                    "record_id",
                    "source_id",
                    "received_at",
                    "message_type",
                    "synthetic",
                    *LEGACY_METRIC_KEYS,
                    "payload_json",
                    "rawPayload",
                    *insights.NEW_METRICS,
                ]
            )
            rows = db.execute(
                "SELECT * FROM records WHERE device_id=? AND received_at>=? "
                "AND received_at<? ORDER BY received_at,id",
                (device["id"], start, end),
            )
            for raw_row in rows:
                row = dict(raw_row)
                record = self.original(row, cache)
                if count:
                    output.write(",\n")
                json.dump(record, output, ensure_ascii=False, allow_nan=False)
                values = json.loads(row["metric_json"])
                additions = metrics(record, device["kind"])
                cells = [
                    row["id"],
                    row["source_id"],
                    row["received_at"],
                    row["message_type"],
                    bool(row["synthetic"]),
                    *(values.get(key) for key in LEGACY_METRIC_KEYS),
                    json.dumps(record.get("payload"), ensure_ascii=False),
                    record.get("rawPayload"),
                    *(additions.get(key) for key in insights.NEW_METRICS),
                ]
                writer.writerow(
                    [
                        "'" + cell
                        if isinstance(cell, str) and cell.lstrip().startswith(("=", "+", "-", "@"))
                        else cell
                        for cell in cells
                    ]
                )
                count += 1
                synthetic += row["synthetic"]
            output.write("]\n")
        manifest = {
            "schema_version": 1,
            "source": BASE_URL,
            "device": device,
            "start": start,
            "end": end,
            "time_field": "receivedAt",
            "exported_at": snapshot_at,
            "total": count,
            "synthetic_records": synthetic,
            "coverage": coverage,
            "history_start": device["history_start"],
            "history_start_confirmed": self.catalog.get_meta("confirmed_history_start"),
            "csv_notes": "Missing values are empty; formula-like source strings are prefixed "
            "with an apostrophe. JSON preserves source types and strings.",
            "csv_additions": {
                key: {"label": label, "unit": unit}
                for key, (label, unit, _) in insights.NEW_METRICS.items()
            },
            "files": {
                p.name: {"sha256": self.file_hash(p), "size_bytes": p.stat().st_size}
                for p in (records_path, csv_path)
            },
        }
        (folder / "manifest.json").write_bytes(json_bytes(manifest))
        return manifest

    def build_export(self, job: dict) -> None:
        scope = json.loads(job["scope_json"]) if job.get("scope_json") else None
        identifiers = scope["device_ids"] if scope else [job["device_id"]]
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.catalog.execute(
            "UPDATE exports SET state='running',updated_at=? WHERE id=?", (now(), job["id"])
        )
        with tempfile.TemporaryDirectory(dir=self.cache_root) as temporary:
            folder = Path(temporary)
            content = folder / "content"
            manifests = []
            with self.catalog.connect() as db:
                db.execute("BEGIN")
                # The first read fixes one snapshot for every device, range and coverage.
                devices = [
                    dict(db.execute("SELECT * FROM devices WHERE id=?", (item,)).fetchone())
                    for item in identifiers
                ]
                snapshot_at = now()
                for device in devices:
                    start, end = job["start"], job["end"]
                    if scope:
                        stats = db.execute(
                            "SELECT first_record,last_record FROM device_stats WHERE device_id=?",
                            (device["id"],),
                        ).fetchone()
                        origin = device["history_start"] or device["first_seen"]
                        start = (
                            min(origin, stats["first_record"])
                            if stats and stats["first_record"]
                            else origin
                        )
                        end = (
                            max(
                                snapshot_at,
                                utc(timestamp(stats["last_record"]) + timedelta(milliseconds=1)),
                            )
                            if stats and stats["last_record"]
                            else snapshot_at
                        )
                    destination = (
                        content / device["id"] if scope and scope["scope"] == "kind" else content
                    )
                    manifests.append(
                        self.write_export_device(db, device, start, end, destination, snapshot_at)
                    )
            if scope and scope["scope"] == "kind":
                (content / "manifest.json").write_bytes(
                    json_bytes(
                        {
                            "schema_version": 1,
                            "source": BASE_URL,
                            "scope": scope,
                            "exported_at": snapshot_at,
                            "total": sum(m["total"] for m in manifests),
                            "devices": [
                                {
                                    "id": m["device"]["id"],
                                    "device_no": m["device"]["device_no"],
                                    "product_key": m["device"]["product_key"],
                                    "total": m["total"],
                                    "manifest": f"{m['device']['id']}/manifest.json",
                                }
                                for m in manifests
                            ],
                        }
                    )
                )
            package = folder / "data.zip"
            with zipfile.ZipFile(
                package, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                for file in sorted(content.rglob("*")):
                    if file.is_file():
                        archive.write(file, file.relative_to(content).as_posix())
            key = f"{PREFIX}/exports/{job['id']}.zip"
            sha = self.file_hash(package)
            info = self.store.put_file(
                package,
                key,
                content_type="application/zip",
                metadata={"sha256": sha},
                if_absent=False,
            )
            self.catalog.execute(
                "UPDATE exports SET state='ready',object_key=?,sha256=?,"
                "size_bytes=?,error=NULL,updated_at=? WHERE id=?",
                (key, sha, info.size_bytes, now(), job["id"]),
            )

    @staticmethod
    def file_hash(path: Path) -> str:
        import hashlib

        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
