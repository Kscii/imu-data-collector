"""Rebuildable minute/hour/day statistics and bounded, single-metric projections."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from itertools import groupby

from imu_data_collector.external_device_domain import timestamp, utc

LEVELS = (60_000, 3_600_000, 86_400_000)
STATE_FIELDS = {"in_bed", "sleep_stage", "moving", "People_flag", "D", "CO"}


def millis(value: str) -> int:
    return round(timestamp(value).timestamp() * 1000)


def instant(value: int) -> str:
    return utc(datetime.fromtimestamp(value / 1000, UTC))


def method(key: str) -> str:
    return (
        "mode"
        if key in STATE_FIELDS
        else "last"
        if key in {"ST", "distance_km", "STTIME", "energy_kcal"}
        else "sum"
        if key == "CST"
        else "mean"
    )


def empty() -> dict:
    return {
        "count": 0,
        "missing": 0,
        "sum": 0,
        "min": None,
        "max": None,
        "first": None,
        "last": None,
        "states": {},
    }


def add(stats: dict, value, time: int, identity: str, key: str) -> None:
    if value is None:
        stats["missing"] += 1
        return
    stats["count"] += 1
    stats["sum"] += value
    stats["min"] = value if stats["min"] is None else min(stats["min"], value)
    stats["max"] = value if stats["max"] is None else max(stats["max"], value)
    point = [time, identity, value]
    if stats["first"] is None or point[:2] < stats["first"][:2]:
        stats["first"] = point
    if stats["last"] is None or point[:2] > stats["last"][:2]:
        stats["last"] = point
    if key in STATE_FIELDS:
        state = stats["states"].setdefault(str(value), [0, time, identity])
        state[0] += 1
        state[1:] = max(state[1:], [time, identity])


def merge(target: dict, source: dict) -> None:
    target["missing"] += source["missing"]
    if not source["count"]:
        return
    target["count"] += source["count"]
    target["sum"] += source["sum"]
    for name, fn in (("min", min), ("max", max)):
        target[name] = source[name] if target[name] is None else fn(target[name], source[name])
    for name, fn in (("first", min), ("last", max)):
        target[name] = (
            source[name]
            if target[name] is None
            else fn(target[name], source[name], key=lambda p: p[:2])
        )
    for value, entry in source["states"].items():
        state = target["states"].setdefault(value, [0, entry[1], entry[2]])
        state[0] += entry[0]
        state[1:] = max(state[1:], entry[1:])


def raw_statistics(db, device: str, start: int, end: int, key: str | None = None) -> dict:
    groups: dict = {}
    for row in db.execute(
        "SELECT id,received_at,metric_json,synthetic FROM records "
        "WHERE device_id=? AND received_at>=? AND received_at<? ORDER BY received_at,id",
        (device, instant(start), instant(end)),
    ):
        for field, value in json.loads(row["metric_json"]).items():
            if key is None or field == key:
                stats = groups.setdefault((field, row["synthetic"]), empty())
                add(stats, value, millis(row["received_at"]), row["id"], field)
    return groups


def write_bucket(db, device: str, level: int, bucket: int, groups: dict) -> None:
    db.execute(
        "DELETE FROM metric_rollups WHERE device_id=? AND level=? AND bucket=?",
        (device, level, bucket),
    )
    db.executemany(
        "INSERT INTO metric_rollups VALUES (?,?,?,?,?,?)",
        [
            (device, level, bucket, field, synthetic, json.dumps(stats))
            for (field, synthetic), stats in groups.items()
        ],
    )


def refresh_parents(db, device: str, minutes: set[int]) -> None:
    for level, child in ((LEVELS[1], LEVELS[0]), (LEVELS[2], LEVELS[1])):
        for bucket in sorted({value // level * level for value in minutes}):
            groups: dict = {}
            for row in db.execute(
                "SELECT metric,synthetic,stats FROM metric_rollups "
                "WHERE device_id=? AND level=? AND bucket>=? AND bucket<?",
                (device, child, bucket, bucket + level),
            ):
                merge(
                    groups.setdefault((row["metric"], row["synthetic"]), empty()),
                    json.loads(row["stats"]),
                )
            write_bucket(db, device, level, bucket, groups)
    # Only one row per metric/day is examined for menu metadata, never the raw history.
    summaries: dict = {}
    for row in db.execute(
        "SELECT metric,stats FROM metric_rollups WHERE device_id=? AND level=?", (device, LEVELS[2])
    ):
        merge(summaries.setdefault(row["metric"], empty()), json.loads(row["stats"]))
    db.execute("DELETE FROM metric_metadata WHERE device_id=?", (device,))
    db.executemany(
        "INSERT INTO metric_metadata VALUES (?,?,?,?,?)",
        [
            (device, field, stats["count"], instant(stats["first"][0]), instant(stats["last"][0]))
            for field, stats in summaries.items()
            if stats["count"]
        ],
    )


def refresh_minutes(db, device: str, minutes: set[int], *, parents: bool = True) -> None:
    if not minutes:
        return
    for bucket in sorted(minutes):
        write_bucket(
            db, device, LEVELS[0], bucket, raw_statistics(db, device, bucket, bucket + LEVELS[0])
        )
    if parents:
        refresh_parents(db, device, minutes)


def rebuild(catalog) -> None:
    if catalog.get_meta("aggregation_version") == 1:
        return
    with catalog.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM metric_rollups")
        db.execute("DELETE FROM metric_metadata")
        for device in db.execute("SELECT id FROM devices").fetchall():
            rows = db.execute(
                "SELECT * FROM records WHERE device_id=? ORDER BY received_at,id", (device["id"],)
            )
            minutes = set()
            for bucket, records in groupby(
                rows, lambda row: millis(row["received_at"]) // LEVELS[0] * LEVELS[0]
            ):
                groups: dict = {}
                for row in records:
                    for field, value in json.loads(row["metric_json"]).items():
                        add(
                            groups.setdefault((field, row["synthetic"]), empty()),
                            value,
                            millis(row["received_at"]),
                            row["id"],
                            field,
                        )
                write_bucket(db, device["id"], LEVELS[0], bucket, groups)
                minutes.add(bucket)
            refresh_parents(db, device["id"], minutes)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('aggregation_version','1')")


def interval_ms(start: int, end: int, budget: int) -> int:
    required = max(1, math.ceil((end - start) / (budget - 1)))
    choices = (
        1,
        10,
        100,
        1000,
        5000,
        10000,
        30000,
        60000,
        120000,
        300000,
        600000,
        900000,
        1800000,
        3600000,
        7200000,
        21600000,
        43200000,
        86400000,
        172800000,
        604800000,
    )
    return next(
        (value for value in choices if value >= required),
        math.ceil(required / LEVELS[2]) * LEVELS[2],
    )


def range_statistics(
    db, device: str, key: str, start: int, end: int, max_level: int
) -> tuple[dict, int]:
    """Cover the exact range with coarse complete buckets and finer boundary pieces."""
    result: dict = {}
    used = 0
    cursor = start
    while cursor < end:
        level = next(
            (
                size
                for size in reversed(LEVELS)
                if size <= max_level and cursor % size == 0 and cursor + size <= end
            ),
            0,
        )
        if level:
            stop = end // level * level
            # Stop at the next coarser boundary so long ranges use the coarsest tier.
            larger = next((size for size in LEVELS if size > level and size <= max_level), None)
            if larger:
                stop = min(stop, (cursor // larger + 1) * larger)
            for row in db.execute(
                "SELECT synthetic,stats FROM metric_rollups WHERE device_id=? AND level=? "
                "AND metric=? AND bucket>=? AND bucket<?",
                (device, level, key, cursor, stop),
            ):
                merge(result.setdefault(row["synthetic"], empty()), json.loads(row["stats"]))
            used = max(used, level)
        else:
            stop = min(end, (cursor // LEVELS[0] + 1) * LEVELS[0])
            for (_, synthetic), stats in raw_statistics(db, device, cursor, stop, key).items():
                merge(result.setdefault(synthetic, empty()), stats)
        cursor = stop
    return result, used


def project(catalog, device: str, key: str, start: str, end: str, budget: int) -> dict:
    if catalog.get_meta("aggregation_version") != 1:
        raise RuntimeError("Aggregation index is being prepared")
    lower, upper = millis(start), millis(end)
    width = interval_ms(lower, upper, budget)
    first = lower // width * width
    points = []
    used = 0
    with catalog.connect() as db:
        db.execute("BEGIN")
        coverage = catalog.coverage(device, start, end, db=db)
        for bucket in range(first, upper, width):
            begin, stop = max(lower, bucket), min(upper, bucket + width)
            groups, level = range_statistics(db, device, key, begin, stop, width)
            used = max(used, level)
            for synthetic in (0, 1):
                stats = groups.get(synthetic, empty())
                count = stats["count"]
                value = None
                if count:
                    value = (
                        float(max(stats["states"], key=lambda v: stats["states"][v]))
                        if method(key) == "mode"
                        else stats["last"][2]
                        if method(key) == "last"
                        else stats["sum"]
                        if method(key) == "sum"
                        else stats["sum"] / count
                    )
                points.append(
                    {
                        "start": begin,
                        "end": stop,
                        "time": (begin + stop) / 2,
                        "synthetic": bool(synthetic),
                        "value": value,
                        "count": count,
                        "missing": stats["missing"],
                        "min": stats["min"],
                        "max": stats["max"],
                        "mean": stats["sum"] / count if count else None,
                        "states": {v: s[0] for v, s in stats["states"].items()},
                    }
                )
    return {
        "metric": key,
        "method": method(key),
        "interval_ms": width,
        "source_level_ms": used,
        "points": points,
        "coverage": coverage,
        "count": sum(p["count"] for p in points),
    }
