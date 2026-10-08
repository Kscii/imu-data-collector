"""Additive, resumable projections of the currently accepted immutable records.

No upstream client is used. Checkpoints, field counts and projections commit together;
new views are published per device only after its minute rollups are complete.
"""

from __future__ import annotations

import json
from collections import OrderedDict

from imu_data_collector import external_device_aggregation as aggregation
from imu_data_collector.external_device_domain import digest, json_bytes, metrics, numeric

VERSION = 1
NEW_METRICS = {
    "Amp_value": ("信号幅度", "原值", "设备状态"),
    "CO": ("充电状态", "状态编号", "设备状态"),
    "distance_km": ("累计距离", "km", "活动"),
    "STTIME": ("运动时长", "分钟", "活动"),
    "energy_kcal": ("累计热量", "kcal", "活动"),
}
QUICK_METRICS = {
    "mattress": ("HeartRate", "RespiratoryRate", "in_bed", "sleep_stage"),
    "radar-watch": ("HR", "SPO", "ST", "energy_kcal"),
}
SCHEMA = """
CREATE INDEX IF NOT EXISTS records_archive_position ON records(device_id,archive_id,position);
CREATE TABLE IF NOT EXISTS insight_progress (
 device_id TEXT PRIMARY KEY, version INTEGER NOT NULL, stage TEXT NOT NULL,
 archive_id TEXT NOT NULL DEFAULT '', position INTEGER NOT NULL DEFAULT -1,
 processed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS insight_signatures (id TEXT PRIMARY KEY, fields TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS record_insights (
 id TEXT PRIMARY KEY, hash TEXT NOT NULL, signature TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS insight_field_counts (
 device_id TEXT NOT NULL, signature TEXT NOT NULL, count INTEGER NOT NULL,
 PRIMARY KEY(device_id,signature)
);
CREATE TABLE IF NOT EXISTS insight_dirty_minutes (
 device_id TEXT NOT NULL, minute INTEGER NOT NULL, PRIMARY KEY(device_id,minute)
);
CREATE TABLE IF NOT EXISTS insight_reports (
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL, received_at TEXT NOT NULL,
 synthetic INTEGER NOT NULL, valid INTEGER NOT NULL, summary TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS insight_reports_time ON insight_reports(device_id,received_at,id);
CREATE TABLE IF NOT EXISTS insight_events (
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL, received_at TEXT NOT NULL,
 time_ms INTEGER NOT NULL, synthetic INTEGER NOT NULL, message_type TEXT NOT NULL,
 code TEXT, summary TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS insight_events_time ON insight_events(device_id,received_at,id);
"""

REPORT_LABELS = {
    "Onbed_valid": "在床有效标志",
    "sleep_valid": "睡眠有效标志",
    "Gmt_create": "报告创建时间",
    "Monitor_begin_time": "监测开始时间",
    "gotobed_time": "上床时间",
    "getoffbed_time": "离床时间",
    "sleep_time": "入睡时间",
    "wake_time": "醒来时间",
    "bed_duration": "在床时长",
    "sleep_duration": "睡眠时长",
    "wake_duration": "清醒时长",
    "wake_num": "醒来次数",
    "light_duration": "浅睡时长",
    "deep_duration": "深睡时长",
    "ambulation_num": "离床次数",
    "apnea_num": "呼吸暂停次数",
    "apnea_duration_max": "最长呼吸暂停时长",
    "apnea_duration_average": "平均呼吸暂停时长",
    "motion_num": "体动次数",
    "motion_index": "体动指数",
    "sleep_latency": "入睡潜伏期",
    "sleep_efficiency": "睡眠效率",
    "max_hr": "最高心率",
    "min_hr": "最低心率",
    "ave_hr": "平均心率",
    "max_br": "最高呼吸率",
    "min_br": "最低呼吸率",
    "ave_br": "平均呼吸率",
    "sleep_score": "睡眠评分",
    "respiration_score": "呼吸评分",
    "Apn_begins": "呼吸暂停起点",
    "Apn_lengths": "呼吸暂停长度",
    "off_periods": "离床区间",
    "hrs": "心率序列",
    "brs": "呼吸率序列",
    "hr_br_time_split_point": "序列时间分段",
    "stages": "睡眠阶段序列",
    "moving_array": "体动序列",
}


def ensure_device(db, device: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO insight_progress(device_id,version,stage) "
        "VALUES (?,?,CASE WHEN EXISTS(SELECT 1 FROM records WHERE device_id=?) "
        "THEN 'records' ELSE 'ready' END)",
        (device, VERSION, device),
    )


def preparation(catalog, device: str) -> dict:
    rows = catalog.rows("SELECT stage,processed FROM insight_progress WHERE device_id=?", (device,))
    if rows:
        return {"ready": rows[0]["stage"] == "ready", **rows[0]}
    total = catalog.rows("SELECT total FROM device_stats WHERE device_id=?", (device,))
    ready = not total or total[0]["total"] == 0
    return {"ready": ready, "stage": "ready" if ready else "records", "processed": 0}


def values(record: dict) -> dict:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return {}
    return payload["params"] if isinstance(payload.get("params"), dict) else payload


def report_summary(record: dict) -> dict:
    source = values(record)
    flags_valid = (
        numeric(source.get("Onbed_valid")) == 1 and numeric(source.get("sleep_valid")) == 1
    )
    complete = flags_valid and all(
        source.get(key) is not None for key in ("sleep_time", "wake_time", "sleep_duration")
    )
    return {
        "valid": complete,
        "flags_valid": flags_valid,
        "reason": None
        if complete
        else "有效标志为零或缺失"
        if not flags_valid
        else "报告字段不完整",
        "fields": [
            {
                "key": key,
                "label": REPORT_LABELS.get(key, key),
                "value": value,
                "unit": "原值 · 单位/时区/枚举未确认",
            }
            for key, value in source.items()
        ],
    }


def field_description(kind: str, message: str, path: str) -> dict:
    key = path.rsplit(".", 1)[-1]
    projected = False
    label, unit = key, "未确认"
    if (
        kind == "mattress"
        and message == "REALTIME"
        and path in {f"payload.{key}", f"payload.params.{key}"}
    ):
        known = {
            "HeartRate": ("心率", "bpm"),
            "RespiratoryRate": ("呼吸率", "次/分钟"),
            "People_flag": ("在床原值；非负整数 % 2", "原值"),
            "D": ("睡眠状态原值；非负整数 % 8", "原值"),
            "E": ("呼吸暂停持续时间", "秒"),
            "moving": ("体动状态", "状态编号"),
            "RSSI": ("Wi-Fi 信号", "dBm"),
            "Amp_value": ("信号幅度", "原值"),
        }
        projected = key in known
        label, unit = known.get(key, (key, "未确认"))
    elif kind == "radar-watch" and path == f"payload.{key}":
        known = {
            "LK": {
                "BP": ("电池电量", "%"),
                "ST": ("总步数", "步"),
                "CST": ("区间步数", "步"),
                "KCAL": ("热量原值；除以 10 得 kcal", "0.1 kcal"),
                "DTC": ("距离原值；除以 100 得 km", "0.01 km"),
                "STTIME": ("运动时长", "分钟"),
                "CO": ("充电状态；0 未充电 / 1 充电 / 2 完成", "状态编号"),
            },
            "UHR": {"HR": ("心率", "原值")},
            "USPO": {"HR": ("心率", "原值"), "SPO": ("血氧", "原值")},
            "UHT": {"HT": ("体温", "原值"), "AT": ("环境温度", "原值")},
            "UBP": {"SBP": ("收缩压", "原值"), "DBP": ("舒张压", "原值")},
            "UBRR": {"BRR": ("呼吸率", "原值")},
        }.get(message, {})
        projected = key in known
        label, unit = known.get(key, (key, "未确认"))
    if kind == "mattress" and message == "SLEEP_REPORT":
        label = REPORT_LABELS.get(key, key)
    return {"label": label, "unit": unit, "numeric": projected}


def field_signature(record: dict, kind: str, message: str) -> list:
    fields = []

    def walk(value, path):
        if isinstance(value, dict) and value:
            for key, child in sorted(value.items()):
                walk(child, f"{path}.{key}" if path else key)
        else:
            definition = field_description(kind, message, path)
            status = (
                "missing"
                if value is None
                else "invalid"
                if definition["numeric"] and numeric(value) is None
                else "projected"
                if definition["numeric"]
                else "report"
                if (kind, message) == ("mattress", "SLEEP_REPORT")
                else "event"
                if (kind, message) in {("radar-watch", "UALM"), ("mattress", "WARNING")}
                else "raw"
            )
            fields.append([path, status])

    walk(record, "")
    return [kind, message, fields]


def index_record(db, device: dict, row_id: str, sha: str, record: dict) -> bool:
    previous = db.execute(
        "SELECT hash,signature FROM record_insights WHERE id=?", (row_id,)
    ).fetchone()
    if previous and previous["hash"] == sha:
        return False
    message = record.get("messageType" if device["kind"] == "mattress" else "operation", "")
    fields = field_signature(record, device["kind"], message)
    encoded = json_bytes(fields)
    signature = digest(encoded)
    db.execute(
        "INSERT OR IGNORE INTO insight_signatures VALUES (?,?)", (signature, encoded.decode())
    )
    if previous and previous["signature"] != signature:
        db.execute(
            "UPDATE insight_field_counts SET count=count-1 WHERE device_id=? AND signature=?",
            (device["id"], previous["signature"]),
        )
    if previous is None or previous["signature"] != signature:
        db.execute(
            "INSERT INTO insight_field_counts VALUES (?,?,1) ON CONFLICT(device_id,signature) DO "
            "UPDATE SET count=count+1",
            (device["id"], signature),
        )
    db.execute("INSERT OR REPLACE INTO record_insights VALUES (?,?,?)", (row_id, sha, signature))
    db.execute("DELETE FROM insight_reports WHERE id=?", (row_id,))
    db.execute("DELETE FROM insight_events WHERE id=?", (row_id,))
    time_ms = aggregation.millis(record["receivedAt"])
    received_at = aggregation.instant(time_ms)
    synthetic = int(
        isinstance(record.get("payload"), dict) and record["payload"].get("_synthetic") is True
    )
    if device["kind"] == "mattress" and message == "SLEEP_REPORT":
        summary = report_summary(record)
        db.execute(
            "INSERT INTO insight_reports VALUES (?,?,?,?,?,?)",
            (
                row_id,
                device["id"],
                received_at,
                synthetic,
                int(summary["valid"]),
                json.dumps(summary, ensure_ascii=False),
            ),
        )
    if (device["kind"], message) in {("radar-watch", "UALM"), ("mattress", "WARNING")}:
        source = values(record)
        code = source.get("TY") if message == "UALM" else source.get("code", source.get("type"))
        code = None if code is None else str(code)
        summary = {"label": "报警上报", "code": code, "note": "上游原始代码；事件含义尚未确认"}
        db.execute(
            "INSERT INTO insight_events VALUES (?,?,?,?,?,?,?,?)",
            (
                row_id,
                device["id"],
                received_at,
                time_ms,
                synthetic,
                message,
                code,
                json.dumps(summary, ensure_ascii=False),
            ),
        )
    return True


def backfill_step(service, *, limit: int = 2000, cache: OrderedDict | None = None) -> bool:
    """One checkpointed batch. Caller serializes this with ingestion via the worker lock."""
    catalog = service.catalog
    with catalog.connect() as db:
        for device in db.execute("SELECT id FROM devices").fetchall():
            ensure_device(db, device["id"])
    pending = catalog.rows(
        "SELECT * FROM insight_progress WHERE stage!='ready' ORDER BY device_id LIMIT 1"
    )
    if not pending:
        return False
    progress = pending[0]
    device = catalog.device(progress["device_id"])
    identifier = device["id"]
    if progress["stage"] == "records":
        rows = catalog.rows(
            "SELECT * FROM records WHERE device_id=? AND (archive_id,position)>(?,?) ORDER BY "
            "archive_id,position LIMIT ?",
            (identifier, progress["archive_id"], progress["position"], limit),
        )
        # Verify original hash before the write transaction. A racing replacement is skipped;
        # its accepted-version ingest has already indexed that replacement atomically.
        cache = cache if cache is not None else OrderedDict()
        originals = [(row, service.original(row, cache)) for row in rows]
        with catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row, record in originals:
                current = db.execute("SELECT hash FROM records WHERE id=?", (row["id"],)).fetchone()
                if not current or current["hash"] != row["hash"]:
                    continue
                if index_record(db, device, row["id"], row["hash"], record):
                    projected = json.loads(row["metric_json"])
                    projected.update(
                        {
                            key: value
                            for key, value in metrics(record, device["kind"]).items()
                            if key in NEW_METRICS
                        }
                    )
                    db.execute(
                        "UPDATE records SET metric_json=? WHERE id=?",
                        (json.dumps(projected), row["id"]),
                    )
                    minute = aggregation.millis(row["received_at"]) // 60000 * 60000
                    db.execute(
                        "INSERT OR IGNORE INTO insight_dirty_minutes VALUES (?,?)",
                        (identifier, minute),
                    )
            if rows:
                db.execute(
                    "UPDATE insight_progress SET archive_id=?,position=?,processed=processed+? "
                    "WHERE device_id=?",
                    (rows[-1]["archive_id"], rows[-1]["position"], len(rows), identifier),
                )
            else:
                db.execute(
                    "UPDATE insight_progress SET stage='rollups' WHERE device_id=?", (identifier,)
                )
    else:
        with catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            minutes = {
                row["minute"]
                for row in db.execute(
                    "SELECT minute FROM insight_dirty_minutes WHERE device_id=? ORDER BY minute "
                    "LIMIT 250",
                    (identifier,),
                )
            }
            aggregation.refresh_minutes(db, identifier, minutes, parents=False)
            db.executemany(
                "DELETE FROM insight_dirty_minutes WHERE device_id=? AND minute=?",
                [(identifier, minute) for minute in minutes],
            )
            if not minutes:
                all_minutes = {
                    row["bucket"]
                    for row in db.execute(
                        "SELECT DISTINCT bucket FROM metric_rollups WHERE device_id=? AND "
                        "level=60000",
                        (identifier,),
                    )
                }
                aggregation.refresh_parents(db, identifier, all_minutes)
                db.execute(
                    "UPDATE insight_progress SET stage='ready' WHERE device_id=?", (identifier,)
                )
    return True


def fields(catalog, identifier: str) -> dict:
    state = preparation(catalog, identifier)
    result = {}
    if state["ready"]:
        for row in catalog.rows(
            "SELECT s.fields,c.count FROM insight_field_counts c JOIN insight_signatures s ON "
            "s.id=c.signature WHERE c.device_id=? AND c.count>0",
            (identifier,),
        ):
            kind, message, entries = json.loads(row["fields"])
            for path, status in entries:
                key = (message, path)
                item = result.setdefault(
                    key,
                    {
                        "path": path,
                        "message": message,
                        **field_description(kind, message, path),
                        "count": 0,
                        "statuses": {},
                    },
                )
                item["count"] += row["count"]
                item["statuses"][status] = item["statuses"].get(status, 0) + row["count"]
    return {
        "preparation": state,
        "fields": sorted(result.values(), key=lambda item: (item["message"], item["path"])),
    }


def entries(
    catalog,
    identifier: str,
    category: str,
    start: str,
    end: str,
    limit: int,
    before=None,
    valid=None,
) -> dict:
    state = preparation(catalog, identifier)
    if not state["ready"]:
        return {"preparation": state, "items": [], "total": 0, "next": None, "incomplete": 0}
    table = {"reports": "insight_reports", "events": "insight_events"}[category]
    where = " WHERE device_id=? AND received_at>=? AND received_at<?"
    params = [identifier, start, end]
    if valid is not None and category == "reports":
        where += " AND valid=?"
        params.append(int(valid))
    with catalog.connect() as db:
        db.execute("BEGIN")
        total = db.execute(f"SELECT COUNT(*) FROM {table}" + where, params).fetchone()[0]
        incomplete = (
            db.execute(
                "SELECT COUNT(*) FROM insight_reports WHERE device_id=? AND received_at>=? AND "
                "received_at<? AND valid=0",
                (identifier, start, end),
            ).fetchone()[0]
            if category == "reports"
            else 0
        )
        if before:
            where += " AND (received_at,id)<(?,?)"
            params.extend(before)
        rows = [
            dict(row)
            for row in db.execute(
                f"SELECT * FROM {table}" + where + " ORDER BY received_at DESC,id DESC LIMIT ?",
                (*params, limit + 1),
            )
        ]
    more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        row["summary"] = json.loads(row["summary"])
        if category == "reports":
            row["summary"].pop("fields", None)  # Large arrays belong to the detail response.
    return {
        "preparation": state,
        "items": rows,
        "total": total,
        "incomplete": incomplete,
        "next": [rows[-1]["received_at"], rows[-1]["id"]] if more else None,
    }


def events(catalog, identifier: str, start: str, end: str, budget: int) -> dict:
    state = preparation(catalog, identifier)
    lower, upper = aggregation.millis(start), aggregation.millis(end)
    width = aggregation.interval_ms(lower, upper, budget)
    points = []
    if state["ready"]:
        rows = catalog.rows(
            "SELECT (time_ms / ?)*? AS bucket,synthetic,COUNT(*) AS count FROM insight_events "
            "WHERE device_id=? AND received_at>=? AND received_at<? GROUP BY bucket,synthetic "
            "ORDER BY bucket,synthetic",
            (width, width, identifier, start, end),
        )
        points = [
            {
                "start": max(row["bucket"], lower),
                "end": min(row["bucket"] + width, upper),
                "time": (max(row["bucket"], lower) + min(row["bucket"] + width, upper)) / 2,
                "synthetic": bool(row["synthetic"]),
                "count": row["count"],
            }
            for row in rows
        ]
    return {
        "preparation": state,
        "interval_ms": width,
        "points": points,
        "total": sum(row["count"] for row in points),
    }
