"""Portable motion-level semantics and exact projections to HDF5 3.3 clocks.

Authority lives in the simulator; collector/benchmark receive byte-identical
copies except for the temporal-contract import. No storage or ML dependencies.
"""

from copy import deepcopy

from .synthetic_temporal_contract import (
    FALLS,
    PERIOD_NS,
    SEMANTIC_SCHEMA,
    SHA256,
    document_sha256,
    empty_document,
    semantic_draft,
    validate_document,
)

SCHEMA = "imu.motion_annotation.v1"
REVIEW_SCHEMA = "imu.motion_review.v1"
POLICY = "motion-quality-inheritance-20261009-v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def identity(motion_sha):
    require(isinstance(motion_sha, str) and SHA256.fullmatch(motion_sha), "Invalid motion hash")
    return {
        "candidate_id": "motion-" + motion_sha[:32],
        "version_id": motion_sha,
        "commit_sha256": motion_sha,
        "input_hashes": dict.fromkeys(("motion", "sensors", "selection"), motion_sha),
    }


def empty_motion(motion_sha, sample_count, source_rate_hz):
    doc = semantic_draft(
        empty_document(
            **identity(motion_sha), sample_count=sample_count, source_rate_hz=source_rate_hz
        )
    )
    doc.pop("candidate")
    doc["schema"] = SCHEMA
    doc["motion_sha256"] = motion_sha
    doc["clock"]["origin"] = "motion-start"
    return validate_motion(doc)


def validate_motion(document):
    require(document.get("schema") == SCHEMA, "Unsupported motion annotation")
    require(
        document["clock"]["origin"] == "motion-start" and document["clock"]["source_start_ns"] == 0,
        "Motion clock must start at zero",
    )
    temporary = deepcopy(document)
    temporary["candidate"] = identity(temporary.pop("motion_sha256"))
    temporary["schema"] = SEMANTIC_SCHEMA
    temporary["clock"]["origin"] = "selection_first_imu_sample"
    validate_document(temporary)
    require(
        all(
            s["auxiliary_code"] is None and s["auxiliary_status"] == "unrecognized"
            for s in document["segments"]
        ),
        "Production annotation is primary-only",
    )
    return document


def mask(segment, reason):
    return dict(
        segment,
        primary_code=None,
        resolution=segment["resolution"]
        if segment["resolution"] != "identified"
        else "unrecognized",
        reason=reason,
        review_state="excluded",
        pending_reasons=[],
        fall_event_id=None,
    )


def _pieces(document, start, stop, *, offset=0, clip_notes=True):
    events = {e["event_id"]: e for e in document["fall_events"]}
    segments, selected_events = [], {}
    for source in document["segments"]:
        a, b = max(start, source["start_ns"]), min(stop, source["stop_ns"])
        if a >= b:
            continue
        segment = deepcopy(source)
        segment.update(start_ns=a - offset, stop_ns=b - offset)
        # Notes keep their original anchors in the motion evidence; the projected
        # copy clips anchors to its visible clock, as required by annotation v2.
        segment["notes"] = (
            [
                dict(
                    n,
                    start_ns=max(start, n["start_ns"]) - offset,
                    stop_ns=min(stop, n["stop_ns"]) - offset,
                )
                for n in segment["notes"]
                if max(start, n["start_ns"]) < min(stop, n["stop_ns"])
            ]
            if clip_notes
            else segment["notes"]
        )
        event = events.get(segment["fall_event_id"])
        if event and not (start <= event["start_ns"] < event["stop_ns"] <= stop):
            segment = mask(segment, "Cropped fall event; onset/impact is incomplete")
        elif event:
            selected_events[event["event_id"]] = {
                **event,
                **{k: event[k] - offset for k in ("start_ns", "impact_ns", "stop_ns")},
            }
        segments.append(segment)
    return segments, list(selected_events.values())


def project_motion(document, candidate, clock):
    validate_motion(document)
    require(
        candidate["input_hashes"]["motion"] == document["motion_sha256"],
        "Projection motion differs",
    )
    start, duration = clock["source_start_ns"], clock["duration_ns"]
    require(
        0 <= start < start + duration <= document["clock"]["duration_ns"],
        "Projection outside motion coverage",
    )
    segments, events = _pieces(document, start, start + duration, offset=start)
    result = {k: deepcopy(v) for k, v in document.items() if k != "motion_sha256"}
    result.update(
        schema=SEMANTIC_SCHEMA,
        candidate=deepcopy(candidate),
        clock=deepcopy(clock),
        segments=segments,
        fall_events=events,
    )
    return validate_document(
        result, expected_candidate=candidate, expected_count=clock["sample_count"]
    )


def normalize_recording(document):
    """One recording acceptance confirms known tags, never an unknown or absent impact."""
    result = deepcopy(document)
    require(result.get("schema") == SEMANTIC_SCHEMA, "Production editor requires annotation v2")
    events = {e["event_id"]: e for e in result["fall_events"]}
    used = {}
    for index, segment in enumerate(result["segments"]):
        require(
            segment["auxiliary_code"] is None and segment["auxiliary_status"] == "unrecognized",
            "Production annotation is primary-only",
        )
        if segment["resolution"] != "identified":
            result["segments"][index] = mask(
                segment, segment.get("reason") or "Human retained unknown"
            )
            continue
        if segment["primary_code"] in FALLS:
            event = events.get(segment["fall_event_id"])
            if not event:
                result["segments"][index] = mask(segment, "Fall event needs onset and impact")
                continue
            used[event["event_id"]] = event
        segment.update(review_state="confirmed", pending_reasons=[])
    result["fall_events"] = list(used.values())
    result["segments"] = coalesce(result["segments"])
    return validate_document(result)


def coalesce(segments):
    def add_note(notes, note):
        nid = note["note_id"]
        if nid in notes and notes[nid] != note:
            nid += "-" + document_sha256(note)[:12]
        notes[nid] = dict(note, note_id=nid)

    result = []
    for item in deepcopy(segments):
        prior = result[-1] if result else None
        if (
            prior
            and prior["stop_ns"] == item["start_ns"]
            and prior["primary_code"] is not None
            and all(
                prior[k] == item[k]
                for k in ("primary_code", "review_state", "resolution", "fall_event_id")
            )
        ):
            notes = {n["note_id"]: n for n in prior["notes"]}
            for s in (prior, item):
                if s["semantic_text"].strip():
                    add_note(
                        notes,
                        {
                            "note_id": "semantic-" + s["segment_id"],
                            "start_ns": s["start_ns"],
                            "stop_ns": s["stop_ns"],
                            "text": s["semantic_text"],
                        },
                    )
            for note in item["notes"]:
                add_note(notes, note)
            prior.update(stop_ns=item["stop_ns"], semantic_text="", notes=list(notes.values()))
        else:
            result.append(item)
    return result


def apply_projection(document, edit):
    """Replace only the displayed interval, retaining unseen canonical motion."""
    validate_motion(document)
    validate_document(edit)
    require(
        edit["candidate"]["input_hashes"]["motion"] == document["motion_sha256"],
        "Edited projection motion differs",
    )
    start, stop = (
        edit["clock"]["source_start_ns"],
        (edit["clock"]["source_start_ns"] + edit["clock"]["duration_ns"]),
    )
    require(0 <= start < stop <= document["clock"]["duration_ns"], "Edit outside motion")
    result = deepcopy(document)
    prefix, before = _pieces(document, 0, start, clip_notes=False)
    suffix, after = _pieces(document, stop, document["clock"]["duration_ns"], clip_notes=False)
    middle, events = _pieces(edit, 0, edit["clock"]["duration_ns"], offset=-start)
    # Saving a projection must not shrink unchanged notes to the visible crop.
    # Changed/new text retains the explicit range visible in the editor.
    originals = {(n["note_id"], n["text"]): n for s in document["segments"] for n in s["notes"]}
    for segment in middle:
        segment["notes"] = [
            deepcopy(originals.get((n["note_id"], n["text"]), n)) for n in segment["notes"]
        ]
    result["segments"] = coalesce(prefix + middle + suffix)
    # Splits across the projected range must not create duplicate identities.
    for index, segment in enumerate(result["segments"]):
        segment["segment_id"] = f"segment-{index:05d}"
    result["fall_events"] = before + events + after
    result["producer"] = deepcopy(edit["producer"])
    result["source_evidence"] = deepcopy(edit["source_evidence"])
    return validate_motion(result)


def reviewed_ranges(ranges, start, stop):
    result = []
    for a, b in sorted([*ranges, [start, stop]]):
        require(type(a) is int and type(b) is int and 0 <= a < b, "Invalid reviewed range")
        if result and a <= result[-1][1]:
            result[-1][1] = max(b, result[-1][1])
        else:
            result.append([a, b])
    return result


def covered(review, clock):
    a = clock["source_start_ns"]
    b = a + clock["duration_ns"]
    return (
        review
        and review["decision"] == "pass"
        and any(x <= a < b <= y for x, y in review["reviewed_ranges"])
    )


def usable_ns(document):
    return sum(
        s["stop_ns"] - s["start_ns"]
        for s in document["segments"]
        if s["resolution"] == "identified" and s.get("review_state") == "confirmed"
    )


def primary_prediction(template, labels, indices, model_id):
    """Same segmentation/masking for production inference and model evaluation."""
    values = [int(i) for i in indices]
    require(len(values) == template["clock"]["sample_count"], "Prediction clock differs")
    result = deepcopy(template)
    result.update(
        producer={"kind": "cpu-motion-model", "model_id": model_id}, segments=[], fall_events=[]
    )
    edges = [0] + [i for i in range(1, len(values)) if values[i] != values[i - 1]] + [len(values)]
    for i, (a, b) in enumerate(zip(edges, edges[1:], strict=False)):
        code = labels[values[a]] if values[a] >= 0 else None
        segment = deepcopy(template["segments"][0])
        segment.update(
            segment_id=f"segment-{i:05d}",
            start_ns=a * PERIOD_NS,
            stop_ns=b * PERIOD_NS,
            primary_code=code,
            resolution="identified" if code else "unrecognized",
            reason=None if code else "Model abstained",
            review_state="confirmed" if code else "pending",
            pending_reasons=[] if code else ["class"],
        )
        if code in FALLS:
            segment.update(
                primary_code=None,
                resolution="unrecognized",
                reason="Fall event requires onset and impact",
                semantic_text="自动建议：" + code,
                review_state="pending",
                pending_reasons=["boundary"],
            )
        result["segments"].append(segment)
    return validate_motion(result)


def accepted_projection(review, candidate, clock):
    require(
        review.get("schema") == REVIEW_SCHEMA and review.get("reviewer") and covered(review, clock),
        "No human-reviewed motion coverage",
    )
    result = project_motion(review["document"], candidate, clock)
    result["source_evidence"] = [
        e for e in result["source_evidence"] if e.get("kind") != "motion-acceptance"
    ]
    result["source_evidence"].append(
        {
            "kind": "motion-acceptance",
            "policy": POLICY,
            "sha256": document_sha256(review),
            "review": deepcopy(review),
        }
    )
    result["producer"] = {
        "kind": "inherited-motion-review",
        "review_sha256": document_sha256(review),
    }
    return validate_document(result)


def validate_inheritance(document, reviewer):
    evidence = [e for e in document["source_evidence"] if e.get("kind") == "motion-acceptance"]
    if document["producer"].get("kind") != "inherited-motion-review":
        require(not evidence, "Unexpected motion inheritance evidence")
        return
    require(len(evidence) == 1, "Missing motion acceptance evidence")
    item = evidence[0]
    review = item["review"]
    require(
        item["policy"] == POLICY
        and document_sha256(review) == item["sha256"]
        and review["reviewer"] == reviewer,
        "Inherited human review differs",
    )
    expected = accepted_projection(review, document["candidate"], document["clock"])
    require(document == expected, "Motion projection differs from human acceptance")
