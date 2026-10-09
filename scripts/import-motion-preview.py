"""Import a frozen development comparison without model activation or review writes."""

import argparse
import json
from pathlib import Path

from imu_data_collector.motion_annotations import project_motion
from imu_data_collector.motion_label_previews import MotionLabelPreviews
from imu_data_collector.motion_label_service import MotionLabelService
from imu_data_collector.storage import create_object_store
from imu_data_collector.synthetic_motion import SyntheticReviewService
from imu_data_collector.synthetic_temporal_contract import IDENTIFIER, document_sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feedback", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--preview-id", required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--local-store", type=Path)
    destination.add_argument("--bucket")
    parser.add_argument("--project")
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not IDENTIFIER.fullmatch(args.preview_id):
        parser.error("Invalid preview ID")
    feedback = json.loads(args.feedback.read_text())
    diagnostics = json.loads(args.diagnostics.read_text())
    freeze = json.loads(args.freeze.read_text())
    predictions = {r["candidate_id"]: r["models"] for r in diagnostics["per_clip"]}
    records = []
    for row in feedback["records"]:
        entry, review = row["entry"], row["review"]
        if entry["group_key"] in feedback["validation_group_keys"]:
            raise ValueError("Independent validation cannot enter development preview")
        if document_sha256(review) != row["review_pointer"]["revision_sha256"]:
            raise ValueError("Human review hash differs")
        c = entry["candidate"]
        models = predictions[c["candidate_id"]]
        records.append(
            {
                "motion_sha256": review["motion_sha256"],
                "candidate": c,
                "human_revision": row["review_pointer"],
                "documents": {
                    "human": project_motion(review["document"], c, entry["clock"]),
                    "baseline": models["A"]["prediction"],
                    "candidate": models["C"]["prediction"],
                },
            }
        )
    manifest = {
        "schema": "imu.motion_preview.v1",
        "preview_id": args.preview_id,
        "title": "14 条重标后的候选 C 对照",
        "models": {"baseline": "skeleton-tcn-v1", "candidate": "retrained-c-20261009"},
        "model_hashes": {
            k: freeze[k]
            for k in (
                "baseline_model_sha256",
                "baseline_metadata_sha256",
                "candidate_model_sha256",
                "candidate_metadata_sha256",
            )
        },
        "activity_rules_version": feedback["activity_rules"]["version"],
        "created_at_utc": freeze["created_at_utc"],
        "records": records,
        "input_hashes": {
            "feedback": document_sha256(feedback),
            "diagnostics": document_sha256(diagnostics),
            "freeze": document_sha256(freeze),
        },
    }
    if args.apply:
        store = create_object_store(
            "gcs" if args.bucket else "local",
            args.local_store or Path("/tmp"),
            args.bucket,
            args.project,
        )
        source = SyntheticReviewService(store, None, target="prod")
        service = MotionLabelService(source, catalog_path=args.catalog)
        service.refresh(force=True)
        # Validate before publication, including current holdout visibility and bindings.
        for record in records:
            service.check_holdout(record["motion_sha256"])
            variant = service.variant(
                record["motion_sha256"],
                record["candidate"]["candidate_id"],
                record["candidate"]["version_id"],
            )
            if variant is None:
                raise ValueError("Preview requires published variants")
            from imu_data_collector.synthetic_temporal_contract import validate_document

            for document in record["documents"].values():
                validate_document(document, expected_candidate=variant["candidate"])
                if document["clock"] != variant["clock"]:
                    raise ValueError("Preview clock differs")
        service.immutable(service.root + "/previews/" + args.preview_id + ".json", manifest)
        preview = MotionLabelPreviews(service)
        for record in records:
            preview.case(args.preview_id, record["motion_sha256"])
    print(
        json.dumps(
            {
                "preview_id": args.preview_id,
                "records": len(records),
                "applied": args.apply,
                "sha256": document_sha256(manifest),
                "model_activated": False,
                "human_decisions_written": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
