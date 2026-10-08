"""Import human-reviewed experimental material as production DRAFTS only.

No annotation writes unless --apply; the local directory cache may be refreshed.
Existing production edits and acceptances always win.
"""

import argparse
import json
import time
from copy import deepcopy
from pathlib import Path

from imu_data_collector.motion_annotations import apply_projection
from imu_data_collector.motion_label_service import MotionLabelService, now
from imu_data_collector.storage import LocalFilesystemStore
from imu_data_collector.synthetic_motion import SyntheticReviewService
from imu_data_collector.synthetic_temporal import TemporalReviewService
from imu_data_collector.synthetic_temporal_contract import document_sha256, semantic_draft


def migrate(old, run, destination, apply=False):
    results = []
    for entry in run["candidates"]:
        if entry["reference_role"] == "validation":
            continue
        pointer, _ = old.pointer(run, entry)
        if not pointer:
            continue
        c = entry["candidate"]
        mid = c["input_hashes"]["motion"]
        record = {"candidate_id": c["candidate_id"], "motion_sha256": mid}
        try:
            destination.check_holdout(mid)
            state, generation = destination.state(mid)
            if state["lease"] and state["lease"]["expires_at"] > time.time():
                results.append({**record, "status": "active-editor-kept"})
                continue
            if state["accepted"] or state["draft"]:
                results.append({**record, "status": "existing-production-version-kept"})
                continue
            variant = destination.variant(mid, c["candidate_id"], c["version_id"])
            if variant is None or variant["candidate"] != c or variant["clock"] != entry["clock"]:
                raise ValueError("Exact candidate/clock not available in production")
            case = old.case(run, entry)
            source = (
                case.get("draft", {}).get("document")
                if case.get("draft")
                else case.get("reviewed_document")
            )
            if not source:
                raise ValueError("No human review material")
            document = semantic_draft(deepcopy(source))
            for segment in document["segments"]:
                if segment["review_state"] != "confirmed" and segment["primary_code"]:
                    code = segment["primary_code"]
                    segment.update(
                        primary_code=None,
                        resolution="unrecognized",
                        fall_event_id=None,
                        reason="Prior unconfirmed suggestion: " + code,
                        semantic_text=(segment["semantic_text"] + "；待核对建议：" + code).strip(
                            "；"
                        ),
                    )
            allowed_events = {s["fall_event_id"] for s in document["segments"]}
            document["fall_events"] = [
                e for e in document["fall_events"] if e["event_id"] in allowed_events
            ]
            document["producer"] = {
                "kind": "experimental-review-migration",
                "source_run": run["run_id"],
            }
            document["source_evidence"].append(
                {
                    "kind": "production-draft-migration",
                    "source_run": run["run_id"],
                    "source_review": pointer,
                    "source_document_sha256": document_sha256(source),
                }
            )
            canonical = apply_projection(destination.feature(mid)["template"], document)
            revision = {
                "schema": "imu.motion_draft.v1",
                "motion_sha256": mid,
                "document": canonical,
                "author": "migration",
                "created_at_utc": now(),
                "previous": None,
            }
            if apply:
                state["draft"] = destination.reference("drafts", revision)
                destination.write_state(mid, state, generation)
            results.append(
                {**record, "status": "draft-imported" if apply else "would-import-draft"}
            )
        except (FileNotFoundError, ValueError) as error:
            results.append({**record, "status": "skipped", "reason": str(error)})
    return {"human_acceptances_created": 0, "records": results}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-store", type=Path, required=True)
    p.add_argument("--destination-store", type=Path, required=True)
    p.add_argument("--catalog-path", type=Path, required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--report", type=Path)
    args = p.parse_args()
    if not args.source_store.is_dir() or not args.destination_store.is_dir():
        p.error("Existing source and destination stores are required")
    source_store = LocalFilesystemStore(args.source_store)
    old = TemporalReviewService(
        source_store, SyntheticReviewService(source_store, None, target="prod")
    )
    store = LocalFilesystemStore(args.destination_store)
    destination = MotionLabelService(
        SyntheticReviewService(store, None, target="prod"), args.catalog_path
    )
    while destination.refresh(force=True) == 256:
        pass
    result = migrate(old, old.get_run(args.run_id), destination, args.apply)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
