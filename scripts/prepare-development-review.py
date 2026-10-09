"""Seed proposals in a fresh isolated development store; never approve or activate."""

import argparse
import hashlib
import json
from pathlib import Path

from imu_data_collector.motion_label_service import MotionLabelService
from imu_data_collector.motion_label_worker import MotionLabelWorker
from imu_data_collector.storage import LocalFilesystemStore
from imu_data_collector.synthetic_motion import SyntheticReviewService
from imu_data_collector.synthetic_temporal_contract import document_sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("local-store", "state-dir", "supplement", "model-bundle", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.supplement.read_text())
    assert manifest["selection_kind"].startswith("development-only")
    store = LocalFilesystemStore(args.local_store)
    service = MotionLabelService(
        SyntheticReviewService(store, None, target="prod"),
        catalog_path=args.state_dir / "motion-labels.sqlite3",
    )
    assert service.optional(service.root + "/active-model.json")[0] is None
    service.immutable(
        service.root + "/holdouts/targeted-development-protection.json",
        {
            "run_id": "targeted-development-protection",
            "group_keys": manifest["protected_groups"],
        },
    )
    while service.refresh(force=True) == 256:
        pass
    model = {
        "schema": "imu.production_model.v1",
        "model_id": "research-c-v3-development-20261010",
        "feature_version": "motion-body22-world-25hz-v1",
        "release_kind": "research-only",
        "validation": None,
    }
    for name, filename in (("onnx", "model.onnx"), ("metadata", "metadata.json")):
        path = args.model_bundle / filename
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        key = service.root + "/model-artifacts/" + digest + "/" + filename
        store.put_file(
            path, key, content_type="application/octet-stream", metadata={"sha256": digest}
        )
        model[name] = {"object_key": key, "sha256": digest, "byte_length": path.stat().st_size}
    worker = MotionLabelWorker(service, args.state_dir / "inference-cache")
    records = []
    for row in manifest["records"]:
        entry = row["entry"]
        mid = entry["candidate"]["input_hashes"]["motion"]
        assert entry["group_key"] not in set(manifest["protected_groups"])
        service.check_holdout(mid)
        state, generation = service.state(mid)
        if state["prediction"]:
            prediction = service.resolve(state["prediction"])
            assert prediction["model"] == model, "Do not replace a visible research suggestion"
        else:
            assert not state["draft"] and not state["accepted"] and not state["lease"], (
                "Review already started"
            )
            prediction = worker.infer(service.feature(mid), model)
            state["prediction"] = service.reference("predictions", prediction)
            service.write_state(mid, state, generation)
        variant = service.variant(
            mid, entry["candidate"]["candidate_id"], entry["candidate"]["version_id"]
        )
        projected = service.project_prediction(prediction, variant)
        assert all(
            s["stop_ns"] - s["start_ns"] >= 120000000
            for s in projected["segments"]
            if s["primary_code"] is not None
        )
        records.append(
            {
                "candidate_id": entry["candidate"]["candidate_id"],
                "motion_sha256": mid,
                "prediction_sha256": state["prediction"]["revision_sha256"],
                "projected_document_sha256": document_sha256(projected),
                "segments": len(projected["segments"]),
            }
        )
    result = {
        "records": records,
        "model": model,
        "model_activated": False,
        "human_decisions_written": 0,
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {"predictions": len(records), "model_activated": False, "human_decisions_written": 0}
        )
    )


if __name__ == "__main__":
    main()
