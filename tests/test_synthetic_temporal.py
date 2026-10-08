from copy import deepcopy

import pytest
from test_synthetic_motion import _seed

from imu_data_collector.storage import LocalFilesystemStore, ObjectConflictError
from imu_data_collector.synthetic_motion import SyntheticReviewService
from imu_data_collector.synthetic_temporal import ReviewBody, TemporalReviewService
from imu_data_collector.synthetic_temporal_contract import (
    RUN_SCHEMA,
    document_sha256,
    empty_document,
    run_root,
)


def fixture(tmp_path, reference_role="none"):
    store = LocalFilesystemStore(tmp_path / "objects")
    source = SyntheticReviewService(store, None, target="prod")
    cid, vid, _ = _seed(store, source.prefix)
    commit = store.read_json(source.commit_key(cid, vid))[0]
    doc = empty_document(
        candidate_id=cid,
        version_id=vid,
        commit_sha256=document_sha256(commit),
        sample_count=50,
        input_hashes={x["role"]: x["sha256"] for x in commit["objects"]},
    )
    entry = {
        "candidate": doc["candidate"],
        "clock": doc["clock"],
        "source_dataset": "ACCAD",
        "source_member": "clip.npz",
        "group_key": "ACCAD/person1",
        "stratum": "basic",
        "reference_role": reference_role,
    }
    run = {
        "schema": RUN_SCHEMA,
        "run_id": "pilot-30",
        "phase": "pilot",
        "source_prefix": source.prefix,
        "created_at_utc": "2026-10-08T00:00:00Z",
        "seed": 20261008,
        "candidates": [entry],
        "full_from": None,
    }
    service = TemporalReviewService(store, source)
    service.create_run(run)
    proposal = {
        "schema": "imu.temporal_proposal.v1",
        "document": doc,
        "attempt": 0,
        "kind": "inference",
        "based_on_review_sha256": None,
    }
    return store, source, service, run, entry, proposal


def test_reference_is_blind_and_original_production_review_is_unchanged(tmp_path):
    store, source, service, run, entry, proposal = fixture(tmp_path, "validation")
    service.publish(run, entry, proposal)
    assert service.case(run, entry)["proposal"] is None
    assert service.case(run, entry)["status"] == "reference_needed"
    body = ReviewBody(
        decision="pass", proposal_sha256=document_sha256(proposal), acknowledge_exclusions=True
    )
    with pytest.raises(ValueError, match="independent reference"):
        service.review(run, entry, body, "reviewer")
    service.save_reference(run, entry, proposal["document"], "reference-author")
    assert service.case(run, entry)["proposal"] == proposal
    accepted = service.review(run, entry, body, "reviewer")
    assert accepted["decision"] == "pass"
    assert (
        source.latest(entry["candidate"]["candidate_id"], entry["candidate"]["version_id"]) is None
    )
    with pytest.raises(ObjectConflictError, match="changed"):
        service.review(run, entry, body, "second-reviewer")
    snapshot = service.create_snapshot(run, "admin")
    intent = store.read_json(snapshot["request_key"])[0]
    assert intent["entries"][0]["source_prefix"] == source.prefix
    assert intent["entries"][0]["review_key"].startswith(
        "synthetic-motion/dev/pilot-30/v1/reviews/"
    )


def test_exclusion_needs_confirmation_and_retries_are_bounded(tmp_path):
    _, _, service, run, entry, proposal = fixture(tmp_path)
    previous = None
    for attempt in range(3):
        proposal = dict(proposal, attempt=attempt, based_on_review_sha256=previous)
        service.publish(run, entry, proposal)
        with pytest.raises(ValueError, match="confirm"):
            service.review(
                run,
                entry,
                ReviewBody(
                    decision="pass",
                    proposal_sha256=document_sha256(proposal),
                    expected_review_sha256=previous,
                ),
                "reviewer",
            )
        pointer = service.review(
            run,
            entry,
            ReviewBody(
                decision="return",
                proposal_sha256=document_sha256(proposal),
                expected_review_sha256=previous,
                reason_codes=["primary_class"],
            ),
            "reviewer",
        )
        previous = pointer["revision_sha256"]
    with pytest.raises(ValueError, match="retry limit"):
        service.publish(run, entry, dict(proposal, based_on_review_sha256=previous))
    fallback = dict(proposal, kind="exclusion", based_on_review_sha256=previous)
    service.publish(run, entry, fallback)
    assert service.case(run, entry)["status"] == "pending_review"
    pointer = service.review(
        run,
        entry,
        ReviewBody(
            decision="pass",
            proposal_sha256=document_sha256(fallback),
            expected_review_sha256=previous,
            acknowledge_exclusions=True,
        ),
        "reviewer",
    )
    assert pointer["decision"] == "pass"


def test_lineage_stale_proposals_and_full_gate(tmp_path):
    _, _, service, run, entry, proposal = fixture(tmp_path)
    broken = deepcopy(proposal)
    broken["document"]["candidate"]["input_hashes"]["motion"] = "f" * 64
    with pytest.raises(ValueError, match="candidate differs"):
        service.publish(run, entry, broken)
    service.publish(run, entry, proposal)
    service.review(
        run,
        entry,
        ReviewBody(
            decision="return", proposal_sha256=document_sha256(proposal), reason_codes=["boundary"]
        ),
        "reviewer",
    )
    with pytest.raises(ObjectConflictError, match="changed"):
        service.publish(run, entry, proposal)
    with pytest.raises(ValueError, match="Full run needs"):
        service.create_run(dict(run, phase="full"))
    with pytest.raises(ValueError, match="Only a pilot"):
        service.approve(run, "../../bad", "temporal", "admin")


def test_quality_rejection_is_terminal_and_does_not_trigger_model_retry(tmp_path):
    store, _, service, run, entry, proposal = fixture(tmp_path)
    service.publish(run, entry, proposal)
    service.review(
        run,
        entry,
        ReviewBody(
            decision="reject",
            proposal_sha256=document_sha256(proposal),
            reason_codes=["motion_quality"],
        ),
        "reviewer",
    )
    assert service.case(run, entry)["status"] == "rejected"
    assert store.list(run_root(run) + "/retry-jobs/") == []
    with pytest.raises(ValueError, match="requires"):
        service.create_snapshot(run, "admin")


def test_snapshot_download_is_bound_to_run_request_and_shard(tmp_path):
    import hashlib

    from fastapi.testclient import TestClient

    from imu_data_collector.annotation_api import create_annotation_app
    from imu_data_collector.config import load_settings
    from imu_data_collector.synthetic_temporal_contract import run_prefix

    store, _, service, run, entry, proposal = fixture(tmp_path)
    service.publish(run, entry, proposal)
    service.review(
        run,
        entry,
        ReviewBody(
            decision="pass", proposal_sha256=document_sha256(proposal), acknowledge_exclusions=True
        ),
        "engineering-only",
    )
    snapshot = service.create_snapshot(run, "admin")
    snapshot_id = snapshot["snapshot_id"]
    intent = store.read_json(snapshot["request_key"])[0]
    base = run_prefix(run) + "/snapshots/"
    content = b"engineering-test-only-h5-placeholder"
    digest = hashlib.sha256(content).hexdigest()
    path = tmp_path / "shard-0001.h5"
    path.write_bytes(content)
    key = base + snapshot_id + "/shards/" + path.name
    store.put_file(path, key, content_type="application/x-hdf5", metadata={"sha256": digest})
    result = {
        "snapshot_id": snapshot_id,
        "request_sha256": document_sha256(intent),
        "state": "complete",
        "shards": [{"object_key": key, "byte_length": len(content), "sha256": digest}],
    }
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.synthetic_target = "prod"
    settings.annotation.synthetic_run_id = None
    route = "/api/v1/synthetic/temporal/runs/pilot-30/snapshots"
    with TestClient(create_annotation_app(settings, store=store)) as client:
        assert client.get(route).json()["snapshots"][0]["state"] == "queued"
        assert client.get(f"{route}/{snapshot_id}/manifest").status_code == 409
        store.write_json(
            base + f"failures/{snapshot_id}/failure.json",
            {"state": "failed"},
            if_generation_match=0,
        )
        assert client.get(route).json()["snapshots"][0]["state"] == "failed"
        store.write_json(base + f"results/{snapshot_id}.json", result, if_generation_match=0)
        assert client.get(f"{route}/{snapshot_id}/manifest").json() == result
        response = client.get(
            f"{route}/{snapshot_id}/shards/shard-0001.h5", headers={"Range": "bytes=0-10"}
        )
        assert response.status_code == 206
        assert response.content == content[:11]
        assert response.headers["x-content-sha256"] == digest
        assert client.get(f"{route}/{snapshot_id}/shards/shard-0002.h5").status_code == 404
        other_id = "synthetic-" + "f" * 32
        store.write_json(
            base + f"requests/{other_id}.json",
            dict(intent, temporal_run_id="another-run"),
            if_generation_match=0,
        )
        assert client.get(f"{route}/{other_id}/manifest").status_code == 422


def test_local_preview_cannot_write_production_or_another_pilot(tmp_path):
    import runpy
    from pathlib import Path

    module = runpy.run_path(str(Path(__file__).parents[1] / "scripts/serve-temporal-preview.py"))
    store = LocalFilesystemStore(tmp_path / "objects")
    preview = module["PilotStore"](store, "pilot-30")
    for key in (
        "synthetic-motion/prod/v1/reviews/test.json",
        "synthetic-motion/dev/another/v1/reviews/test.json",
    ):
        with pytest.raises(ValueError, match="isolated"):
            preview.write_json(key, {}, if_generation_match=0)
    preview.write_json(
        "synthetic-motion/dev/pilot-30/v1/reviews/test.json", {}, if_generation_match=0
    )
    assert len(store.list("synthetic-motion/dev/pilot-30/")) == 1
