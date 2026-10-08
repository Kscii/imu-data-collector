"""Portable temporal-label contract; vendored byte-for-byte by its consumers.

Authority: imu-motion-simulator/contracts/temporal.py. No platform or ML imports.
Times are integer nanoseconds relative to the selected motion's first IMU sample.
The document is a proposal, never evidence of human acceptance on its own.
"""

from __future__ import annotations

import hashlib
import json
import re

SCHEMA = "imu.temporal_annotation.v1"
TAXONOMY_ID = "motion-temporal"
TAXONOMY_VERSION = "1.0.0"
PERIOD_NS = 40_000_000
PRIMARY = (
    "standing",
    "walking",
    "running",
    "sitting",
    "lying",
    "squatting",
    "sit_down",
    "stand_up",
    "lie_down",
    "get_up",
    "squatting_down",
    "stair_up",
    "stair_down",
    "bend_or_pick_up",
    "stumble_recovery",
    "jumping",
    "turning",
    "crawling",
    "kneeling",
    "forward_fall",
    "backward_fall",
    "left_lateral_fall",
    "right_lateral_fall",
    "fall_unspecified",
)
FALLS = frozenset(PRIMARY[-5:])
# Ordered, frozen tie-break for independently supported, simultaneous auxiliaries.
AUXILIARY = (
    "backward_movement",
    "sideways_movement",
    "turning",
    "kicking",
    "punching",
    "throwing",
    "catching",
    "lifting_object",
    "carrying_object",
    "placing_object",
    "stretching",
    "reaching",
    "waving",
    "clapping",
    "pointing",
)
RESOLUTIONS = {"identified", "unrecognized", "uncovered", "uncertain_boundary"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
RUN_SCHEMA = "imu.temporal_run.v1"
PRIMARY_RUN_SCHEMA = "imu.temporal_run.v2"
SOURCE_PREFIX = re.compile(r"^synthetic-motion/(?:prod|dev/[A-Za-z0-9][A-Za-z0-9._-]*)/v1$")


def canonical_bytes(value):
    return (
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode()


def document_sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def taxonomy():
    return {
        "id": TAXONOMY_ID,
        "version": TAXONOMY_VERSION,
        "primary": [
            {"code": code, "name": code.replace("_", " "), "is_fall": code in FALLS}
            for code in PRIMARY
        ],
        "auxiliary": [
            {"code": code, "name": code.replace("_", " "), "priority": i}
            for i, code in enumerate(AUXILIARY)
        ],
    }


def run_prefix(run):
    return (
        f"synthetic-motion/dev/{run['run_id']}/v1"
        if run["phase"] == "pilot"
        else "synthetic-motion/prod/v1"
    )


def run_root(run):
    return f"{run_prefix(run)}/temporal/runs/{run['run_id']}"


def validate_run(run):
    _require(
        isinstance(run, dict)
        and set(run)
        == {
            "schema",
            "run_id",
            "phase",
            "source_prefix",
            "created_at_utc",
            "seed",
            "candidates",
            "full_from",
        }
        and run.get("schema") in (RUN_SCHEMA, PRIMARY_RUN_SCHEMA),
        "Run fields/schema",
    )
    _require(isinstance(run["run_id"], str) and IDENTIFIER.fullmatch(run["run_id"]), "Run ID")
    _require(
        run["phase"] in ("pilot", "full")
        and isinstance(run["source_prefix"], str)
        and SOURCE_PREFIX.fullmatch(run["source_prefix"]),
        "Run scope",
    )
    _require(type(run["seed"]) is int and isinstance(run["created_at_utc"], str), "Run metadata")
    if run["phase"] == "pilot":
        _require(run["full_from"] is None, "Pilot must not impersonate full-run approval")
    else:
        _require(
            isinstance(run["full_from"], dict)
            and set(run["full_from"]) == {"pilot_run_id", "approval_sha256"}
            and IDENTIFIER.fullmatch(run["full_from"]["pilot_run_id"])
            and SHA256.fullmatch(run["full_from"]["approval_sha256"]),
            "Full run needs pilot approval",
        )
    entries = run["candidates"]
    primary_only = run["schema"] == PRIMARY_RUN_SCHEMA
    if primary_only:
        _require(run["phase"] == "pilot", "Primary learning runs are isolated pilots")
    _require(
        isinstance(entries, list)
        and entries
        and (run["phase"] == "full" or len(entries) <= (40 if primary_only else 30)),
        "Pilot candidate limit exceeded",
    )
    identities, reference_groups = set(), {}
    for entry in entries:
        _require(
            isinstance(entry, dict)
            and set(entry)
            == {
                "candidate",
                "clock",
                "source_dataset",
                "source_member",
                "group_key",
                "stratum",
                "reference_role",
            },
            "Run candidate fields",
        )
        identity, clock = entry["candidate"], entry["clock"]
        example = empty_document(
            **identity,
            sample_count=clock["sample_count"],
            source_start_ns=clock["source_start_ns"],
            source_rate_hz=clock["source_rate_hz"],
        )
        _require(example["clock"] == clock, "Run clock differs")
        key = (identity["candidate_id"], identity["version_id"])
        _require(key not in identities, "Duplicate candidate in run")
        identities.add(key)
        _require(
            all(
                isinstance(entry[k], str) and entry[k]
                for k in ("source_dataset", "source_member", "group_key", "stratum")
            ),
            "Source grouping",
        )
        role = entry["reference_role"]
        _require(
            role
            in (
                ("development", "validation")
                if primary_only
                else ("none", "adjustment", "validation")
            ),
            "Reference role",
        )
        if role != "none":
            group = entry["group_key"]
            _require(
                group not in reference_groups or reference_groups[group] == role,
                "Reference adjustment/validation source groups overlap",
            )
            reference_groups[group] = role
    canonical_bytes(run)
    return run


def empty_document(
    *,
    candidate_id,
    version_id,
    commit_sha256,
    sample_count,
    input_hashes,
    source_start_ns=0,
    source_rate_hz=25.0,
    reason="Awaiting temporal annotation",
    producer=None,
):
    """A fully covered, explicitly unresolved draft, not a negative recording."""
    document = {
        "schema": SCHEMA,
        "candidate": {
            "candidate_id": candidate_id,
            "version_id": version_id,
            "commit_sha256": commit_sha256,
            "input_hashes": input_hashes,
        },
        "clock": {
            "origin": "selection_first_imu_sample",
            "source_start_ns": source_start_ns,
            "source_rate_hz": source_rate_hz,
            "sample_count": sample_count,
            "duration_ns": sample_count * PERIOD_NS,
        },
        "taxonomy": taxonomy(),
        "auxiliary_mode": "temporal",
        "segments": [
            {
                "segment_id": "segment-0001",
                "start_ns": 0,
                "stop_ns": sample_count * PERIOD_NS,
                "primary_code": None,
                "auxiliary_code": None,
                "auxiliary_status": "unrecognized",
                "resolution": "unrecognized",
                "reason": reason,
                "fall_event_id": None,
            }
        ],
        "fall_events": [],
        "producer": producer or {"kind": "unresolved"},
        "source_evidence": [],
    }
    validate_document(document)
    return document


def validate_document(document, *, expected_candidate=None, expected_count=None):
    _require(
        isinstance(document, dict) and document.get("schema") == SCHEMA,
        "Unsupported temporal annotation schema",
    )
    _require(
        set(document)
        == {
            "schema",
            "candidate",
            "clock",
            "taxonomy",
            "auxiliary_mode",
            "segments",
            "fall_events",
            "producer",
            "source_evidence",
        },
        "Unexpected temporal annotation fields",
    )
    candidate = document["candidate"]
    _require(
        isinstance(candidate, dict)
        and set(candidate) == {"candidate_id", "version_id", "commit_sha256", "input_hashes"},
        "Candidate fields",
    )
    _require(
        isinstance(candidate["candidate_id"], str)
        and IDENTIFIER.fullmatch(candidate["candidate_id"]),
        "Invalid candidate ID",
    )
    for field in ("version_id", "commit_sha256"):
        _require(
            isinstance(candidate[field], str) and SHA256.fullmatch(candidate[field]),
            "Invalid candidate hash",
        )
    hashes = candidate["input_hashes"]
    _require(
        isinstance(hashes, dict)
        and set(hashes) == {"motion", "sensors", "selection"}
        and all(isinstance(x, str) and SHA256.fullmatch(x) for x in hashes.values()),
        "Invalid input hashes",
    )
    if expected_candidate is not None:
        _require(candidate == expected_candidate, "Temporal annotation candidate differs")
    clock = document["clock"]
    _require(
        isinstance(clock, dict)
        and set(clock)
        == {"origin", "source_start_ns", "source_rate_hz", "sample_count", "duration_ns"},
        "Invalid temporal clock fields",
    )
    _require(
        clock["origin"] == "selection_first_imu_sample"
        and type(clock["source_start_ns"]) is int
        and clock["source_start_ns"] >= 0,
        "Invalid temporal time origin",
    )
    count = clock["sample_count"]
    _require(
        type(count) is int
        and count >= 2
        and type(clock["duration_ns"]) is int
        and clock["duration_ns"] == count * PERIOD_NS,
        "Temporal duration must match the 25 Hz sample extent",
    )
    _require(
        type(clock["source_rate_hz"]) in (float, int) and 0 < clock["source_rate_hz"] < 100_000,
        "Invalid source rate",
    )
    if expected_count is not None:
        _require(count == expected_count, "Temporal annotation sample count differs")
    _require(document["taxonomy"] == taxonomy(), "Unfrozen or unknown temporal taxonomy")
    _require(document["auxiliary_mode"] in ("temporal", "segment_summary"), "Auxiliary mode")
    _require(
        isinstance(document["producer"], dict)
        and document["producer"].get("kind")
        and isinstance(document["source_evidence"], list),
        "Missing annotation evidence",
    )
    segments, events = document["segments"], document["fall_events"]
    _require(
        isinstance(segments, list) and segments and isinstance(events, list),
        "Temporal segments/events must be lists",
    )
    cursor, ids, event_segments = 0, set(), {}
    for segment in segments:
        _require(
            isinstance(segment, dict)
            and set(segment)
            == {
                "segment_id",
                "start_ns",
                "stop_ns",
                "primary_code",
                "auxiliary_code",
                "auxiliary_status",
                "resolution",
                "reason",
                "fall_event_id",
            },
            "Segment fields",
        )
        sid = segment["segment_id"]
        _require(
            isinstance(sid, str) and IDENTIFIER.fullmatch(sid) and sid not in ids,
            "Duplicate or invalid segment ID",
        )
        ids.add(sid)
        start, stop = segment["start_ns"], segment["stop_ns"]
        _require(
            type(start) is int
            and type(stop) is int
            and start == cursor
            and start < stop <= clock["duration_ns"],
            "Segment coverage gap/overlap",
        )
        cursor = stop
        resolution, code = segment["resolution"], segment["primary_code"]
        _require(resolution in RESOLUTIONS, "Unknown resolution")
        if resolution == "identified":
            _require(code in PRIMARY and segment["reason"] is None, "Unresolved primary code")
        else:
            _require(
                code is None and isinstance(segment["reason"], str) and segment["reason"].strip(),
                "Exclusion needs an explicit reason",
            )
        auxiliary, status = segment["auxiliary_code"], segment["auxiliary_status"]
        _require(status in ("identified", "none", "unrecognized"), "Auxiliary status")
        _require(
            (status == "identified" and auxiliary in AUXILIARY)
            or (status != "identified" and auxiliary is None),
            "Unresolved auxiliary code",
        )
        _require(
            resolution == "identified" or auxiliary is None,
            "Excluded segment cannot carry a formal auxiliary",
        )
        event_id = segment["fall_event_id"]
        if code in FALLS:
            _require(
                isinstance(event_id, str) and IDENTIFIER.fullmatch(event_id),
                "Fall segment needs an event ID",
            )
            event_segments.setdefault(event_id, []).append(segment)
        else:
            _require(event_id is None, "Non-fall segment cannot reference a fall event")
    _require(cursor == clock["duration_ns"], "Incomplete temporal coverage")
    seen = set()
    for event in events:
        _require(
            isinstance(event, dict)
            and set(event) == {"event_id", "code", "start_ns", "impact_ns", "stop_ns", "evidence"},
            "Fall event fields",
        )
        eid = event["event_id"]
        _require(eid in event_segments and eid not in seen, "Orphan or duplicate fall event")
        seen.add(eid)
        parts = event_segments[eid]
        _require(
            event["code"] in FALLS and all(p["primary_code"] == event["code"] for p in parts),
            "Inconsistent fall event class",
        )
        _require(
            type(event["start_ns"]) is int
            and type(event["stop_ns"]) is int
            and event["start_ns"] == parts[0]["start_ns"]
            and event["stop_ns"] == parts[-1]["stop_ns"]
            and all(a["stop_ns"] == b["start_ns"] for a, b in zip(parts, parts[1:], strict=False)),
            "Fall event must cover contiguous segments",
        )
        _require(
            type(event["impact_ns"]) is int
            and event["start_ns"] < event["impact_ns"] < event["stop_ns"]
            and isinstance(event["evidence"], str)
            and event["evidence"].strip(),
            "Fall needs an evidenced impact strictly inside the event",
        )
    _require(seen == set(event_segments), "Missing fall event/impact")
    canonical_bytes(document)  # Reject NaN and unserializable evidence.
    return document


def _ceil_sample(ns):
    return (ns + PERIOD_NS - 1) // PERIOD_NS


def project_annotations(document, *, sequence_index=0):
    """Project to the unchanged HDF5 core; merge auxiliary splits, not fall events.

    Uncertain ranges round outwards, so no unrecognized time leaks into a training
    sample. Recognized activity boundaries/onset use ceil; impact uses half-up.
    Empty quantized segments disappear, but an unrepresentable fall is rejected.
    """
    validate_document(document)
    count = document["clock"]["sample_count"]
    events = {e["event_id"]: e for e in document["fall_events"]}
    ranges = []
    for segment in document["segments"]:
        excluded = segment["resolution"] != "identified"
        start = segment["start_ns"] // PERIOD_NS if excluded else _ceil_sample(segment["start_ns"])
        stop = min(count, _ceil_sample(segment["stop_ns"]))
        ranges.append(
            [
                start,
                stop,
                "exclude" if excluded else "activity",
                "other" if excluded else segment["primary_code"],
                segment["fall_event_id"],
            ]
        )
    # Conservatively expand exclusions to overlapping grid cells, trimming neighbours.
    excluded_ranges = [(a, b) for a, b, kind, _, _ in ranges if kind == "exclude"]
    grid = []
    for a, b, kind, code, eid in ranges:
        if kind == "activity":
            pieces = [(a, b)]
            for left, right in excluded_ranges:
                pieces = [
                    (x, y)
                    for start, stop in pieces
                    for x, y in ((start, min(stop, left)), (max(start, right), stop))
                    if x < y
                ]
            if eid and pieces != [(a, b)]:
                raise ValueError(
                    "Exclusion overlaps a fall after quantization; exclude the entire event"
                )
        else:
            pieces = [(a, b)]
        grid.extend([x, y, kind, code, eid] for x, y in pieces if x < y)
    grid.sort(key=lambda r: (r[0], r[1]))
    merged = []
    for row in grid:
        if merged and row[2:] == merged[-1][2:] and row[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], row[1])
        else:
            merged.append(row)
    cursor, rows, used = 0, [], set()
    for start, stop, kind, code, eid in merged:
        _require(start == cursor, "Quantized coverage gap/overlap")
        cursor = stop
        rows.append((sequence_index, kind, start, stop, code))
        if eid:
            event = events[eid]
            impact = (event["impact_ns"] + PERIOD_NS // 2) // PERIOD_NS
            _require(
                eid not in used and start < impact < stop,
                "Fall event cannot be represented on the 25 Hz grid",
            )
            used.add(eid)
            rows.extend(
                (
                    (sequence_index, "onset", start, start, code),
                    (sequence_index, "impact", impact, impact, code),
                )
            )
    _require(cursor == count and used == set(events), "Quantized event/coverage mismatch")
    order = {"activity": 0, "onset": 1, "impact": 2, "exclude": 3}
    return sorted(rows, key=lambda r: (r[0], r[2], order[r[1]], r[3], r[4]))


def formal_labels(document):
    validate_document(document)
    # Freeze the full finite vocabulary, including an all-excluded recording.
    return [
        {
            **row,
            "taxonomy_id": TAXONOMY_ID,
            "taxonomy_version": TAXONOMY_VERSION,
            "origin": "temporal-review",
            "verification": "human",
        }
        for row in document["taxonomy"]["primary"]
    ]


def validate_embedded(refs, annotations, *, sequence_index, sample_count):
    """Offline verification of proposal, acceptance binding, and core projection."""
    embedded = refs.get("temporal_annotation")
    _require(
        isinstance(embedded, dict) and set(embedded) == {"sha256", "document"},
        "Missing embedded temporal annotation",
    )
    document = embedded["document"]
    validate_document(document, expected_count=sample_count)
    _require(document_sha256(document) == embedded["sha256"], "Temporal annotation hash mismatch")
    for role in ("motion", "sensors"):
        _require(
            document["candidate"]["input_hashes"][role] == refs[role]["sha256"],
            "Temporal input lineage mismatch",
        )
    review = refs.get("review", {})
    raw = review.get("document_json")
    _require(
        isinstance(raw, str) and hashlib.sha256(raw.encode()).hexdigest() == review.get("sha256"),
        "Missing or mismatched embedded human review",
    )
    accepted = json.loads(raw)
    _require(
        accepted.get("decision") == "accepted"
        and accepted.get("reviewer")
        and accepted.get("automatic_qa_passed") is True
        and accepted.get("temporal_annotation_sha256") == embedded["sha256"],
        "Temporal annotation is not bound to human acceptance",
    )
    expected = project_annotations(document, sequence_index=sequence_index)
    actual = []

    def decode(value):
        return value.decode() if isinstance(value, bytes) else str(value)

    for row in annotations:
        actual.append(
            (
                int(row["sequence_index"]),
                decode(row["kind"]),
                int(row["start_sample"]),
                int(row["stop_sample"]),
                decode(row["code"]),
            )
        )
    _require(actual == expected, "Embedded temporal annotation differs from HDF5 core")
    return document
