import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from test_synthetic_motion import _seed

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.config import load_settings
from imu_data_collector.motion_annotations import (
    apply_projection,
    empty_motion,
    project_motion,
    validate_inheritance,
    validate_motion,
)
from imu_data_collector.motion_label_models import gate, promote_model
from imu_data_collector.motion_label_service import MotionLabelService
from imu_data_collector.motion_label_worker import MotionLabelWorker
from imu_data_collector.storage import LocalFilesystemStore, ObjectConflictError
from imu_data_collector.synthetic_motion import SyntheticReviewService
from imu_data_collector.synthetic_temporal_contract import PERIOD_NS, document_sha256


def seed(tmp_path):
    store = LocalFilesystemStore(tmp_path / "objects")
    source = SyntheticReviewService(store, None, target="prod")
    cid, vid, _ = _seed(store, source.prefix)
    commit = store.read_json(source.commit_key(cid, vid))[0]
    hashes = {o["role"]: o["sha256"] for o in commit["objects"]}
    mid = hashes["motion"]
    doc = empty_motion(mid, 100, 25.0)
    binding = {
        "motion_sha256": mid,
        "model_sha256": "b" * 64,
        "feature_version": "motion-body22-world-25hz-v1",
    }
    feature_id = document_sha256(binding)
    root = source.prefix + "/motion-labels"
    feature = {
        "schema": "imu.motion_features.v1",
        "binding": binding,
        "feature_id": feature_id,
        "template": doc,
        "object_key": root + "/features/" + feature_id + ".npz",
        "sha256": "c" * 64,
        "byte_length": 1,
        "sample_count": 100,
        "source_dataset": "ACCAD",
        "source_member": "clip.npz",
    }
    store.write_json(
        root + "/feature-manifests/" + feature_id + ".json", feature, if_generation_match=0
    )
    config = {"layout_id": "chest-1", "profile_id": "ideal", "variant": "ideal"}
    variant = {
        "schema": "imu.motion_variant.v1",
        "motion_sha256": mid,
        "feature_id": feature_id,
        "candidate": {
            "candidate_id": cid,
            "version_id": vid,
            "commit_sha256": document_sha256(commit),
            "input_hashes": hashes,
        },
        "clock": {**doc["clock"], "origin": "selection_first_imu_sample"},
        "configuration": config,
        "configuration_id": document_sha256(config),
        "source_dataset": "ACCAD",
        "source_member": "clip.npz",
        "policy_sha256": commit["policy_sha256"],
    }
    store.write_json(
        root + "/variants/" + cid + "-" + vid + ".json", variant, if_generation_match=0
    )
    service = MotionLabelService(source)
    assert service.refresh(force=True) == 2
    return store, source, service, mid, variant


def edit_body(service, mid, variant):
    lease = service.claim(mid, "human-test")
    case = service.case(mid, variant)
    document = deepcopy(case["editor_template"])
    first = document["segments"][0]
    document["segments"] = [
        {
            **first,
            "stop_ns": 2_000_000_000,
            "primary_code": "standing",
            "resolution": "identified",
            "reason": None,
            "semantic_text": "静止站立",
        },
        {
            **first,
            "segment_id": "unknown",
            "start_ns": 2_000_000_000,
            "semantic_text": "难以归类的动作",
        },
    ]
    return {
        "document": document,
        "lease_token": lease["token"],
        "expected_review_sha256": None,
        "expected_draft_sha256": None,
    }


def test_whole_approval_unknown_mask_and_snapshot_frozen_inheritance(tmp_path):
    store, source, service, mid, variant = seed(tmp_path)
    body = edit_body(service, mid, variant)
    c = variant["candidate"]
    result = service.edit(
        mid, c["candidate_id"], c["version_id"], body, "human-test", decision="pass"
    )
    assert result["status"] == "accepted"
    assert result["usable_ns"] == 2_000_000_000
    assert source.latest(c["candidate_id"], c["version_id"]) is None
    snapshot = service.create_snapshot("admin")
    request = store.read_json(snapshot["request_key"])[0]
    revision = store.read_json(request["entries"][0]["review_key"])[0]
    doc = revision["temporal_annotation"]
    validate_inheritance(doc, "human-test")
    assert doc["segments"][1]["primary_code"] is None
    assert doc["segments"][1]["semantic_text"] == "难以归类的动作"
    tampered = deepcopy(doc)
    tampered["segments"][0]["primary_code"] = "walking"
    with pytest.raises(ValueError, match="differs"):
        validate_inheritance(tampered, "human-test")
    body.update(
        document=result["editor_template"],
        expected_review_sha256=result["review"]["revision_sha256"],
    )
    body["document"]["segments"][0]["primary_code"] = "walking"
    saved = service.edit(mid, c["candidate_id"], c["version_id"], body, "human-test")
    assert saved["review"] == result["review"]
    assert service.create_snapshot("admin")["snapshot_id"] == snapshot["snapshot_id"]


def test_all_unknown_can_be_reviewed_but_never_exported(tmp_path):
    _, _, service, mid, variant = seed(tmp_path)
    lease = service.claim(mid, "human-test")
    body = {"document": service.case(mid)["editor_template"], "lease_token": lease["token"]}
    c = variant["candidate"]
    result = service.edit(
        mid, c["candidate_id"], c["version_id"], body, "human-test", decision="pass"
    )
    assert result["status"] == "accepted" and result["usable_ns"] == 0
    with pytest.raises(ValueError, match="可用"):
        service.create_snapshot("admin")


def test_edit_conflicts_and_variant_rejection_do_not_reject_motion(tmp_path):
    _, _, service, mid, variant = seed(tmp_path)
    body = edit_body(service, mid, variant)
    c = variant["candidate"]
    with pytest.raises(ObjectConflictError):
        service.claim(mid, "second-human")
    result = service.edit(mid, c["candidate_id"], c["version_id"], body, "human-test")
    with pytest.raises(ObjectConflictError):
        service.edit(mid, c["candidate_id"], c["version_id"], body, "human-test")
    body.update(
        expected_draft_sha256=result["draft"]["revision_sha256"],
        reason="当前传感器波形不合理",
        scope="variant",
    )
    result = service.edit(
        mid, c["candidate_id"], c["version_id"], body, "human-test", decision="reject"
    )
    assert result["status"] == "rejected"
    assert service.state(mid)[0]["accepted"] is None


def test_reannotation_preserves_human_review_and_stale_job_cannot_publish(tmp_path):
    store, _, service, mid, variant = seed(tmp_path)
    body = edit_body(service, mid, variant)
    c = variant["candidate"]
    result = service.edit(
        mid, c["candidate_id"], c["version_id"], body, "human-test", decision="pass"
    )
    original = deepcopy(service.state(mid)[0]["accepted"])
    release = {"model": {"model_id": "next"}}
    store.write_json(service.root + "/active-model.json", release, if_generation_match=0)
    proposal = {"document": deepcopy(service.feature(mid)["template"])}
    s = proposal["document"]["segments"][0]
    s.update(
        primary_code="walking",
        resolution="identified",
        reason=None,
        review_state="confirmed",
        pending_reasons=[],
    )
    worker = MotionLabelWorker(service, tmp_path / "cache")
    assert worker.publish(mid, proposal, release) == "complete"
    assert service.state(mid)[0]["accepted"] == original
    case = service.case(mid)
    assert case["has_suggestion"] and case["editor_template"] == result["editor_template"]
    assert (
        worker.publish(
            mid, {"document": empty_motion(mid, 100, 25.0)}, {"model": {"model_id": "stale"}}
        )
        == "superseded"
    )
    assert service.state(mid)[0]["accepted"] == original


def test_active_editor_keeps_visible_draft_when_new_prediction_arrives(tmp_path):
    store, _, service, mid, _ = seed(tmp_path)
    service.claim(mid, "human-test")
    original = service.case(mid)["editor_template"]
    release = {"model": {"model_id": "next"}}
    store.write_json(service.root + "/active-model.json", release, if_generation_match=0)
    doc = deepcopy(service.feature(mid)["template"])
    doc["segments"][0].update(
        primary_code="standing",
        resolution="identified",
        reason=None,
        review_state="confirmed",
        pending_reasons=[],
    )
    MotionLabelWorker(service, tmp_path / "cache").publish(mid, {"document": doc}, release)
    assert service.case(mid)["editor_template"] == original
    assert service.case(mid)["has_suggestion"]
    service.release(mid, "human-test", service.state(mid)[0]["lease"]["token"])
    assert service.case(mid)["editor_template"]["segments"][0]["primary_code"] == "standing"
    assert service.state(mid)[0]["draft"] is None


def test_background_prediction_does_not_make_open_editor_save_conflict(tmp_path):
    store, _, service, mid, variant = seed(tmp_path)
    body = edit_body(service, mid, variant)
    release = {"model": {"model_id": "new"}}
    store.write_json(service.root + "/active-model.json", release, if_generation_match=0)
    MotionLabelWorker(service, tmp_path / "cache").publish(
        mid, {"document": service.feature(mid)["template"]}, release
    )
    c = variant["candidate"]
    saved = service.edit(mid, c["candidate_id"], c["version_id"], body, "human-test")
    assert saved["editor_template"]["segments"][0]["primary_code"] == "standing"
    service.release(mid, "human-test", body["lease_token"])
    assert service.resolve(service.state(mid)[0]["draft"])["author"] == "human-test"


def test_projection_is_independent_of_sensor_hash_and_rejects_wrong_motion(tmp_path):
    _, _, service, mid, variant = seed(tmp_path)
    doc = service.feature(mid)["template"]
    second = deepcopy(variant["candidate"])
    second["input_hashes"]["sensors"] = "f" * 64
    assert project_motion(doc, second, variant["clock"])["candidate"] == second
    second["input_hashes"]["motion"] = "e" * 64
    with pytest.raises(ValueError, match="motion differs"):
        project_motion(doc, second, variant["clock"])
    broken = deepcopy(doc)
    broken["clock"]["source_start_ns"] = PERIOD_NS
    with pytest.raises(ValueError, match="zero"):
        validate_motion(broken)


def test_model_promotion_gate_checks_coverage_precision_and_boundaries():
    metrics = {
        "identified_precision": 0.95,
        "known_reference_coverage": 0.5,
        "boundary_mae_s": 0.2,
        "boundary_p90_s": 0.4,
        "matched_segments": 10,
    }
    report = {
        "reference_completed": 20,
        "frozen_before_validation": True,
        "baseline": metrics,
        "candidate": {**metrics, "identified_precision": 0.94, "known_reference_coverage": 0.6},
        "out_of_vocabulary_regression": False,
    }
    gate(report)
    for key, value in (
        ("identified_precision", 0.939),
        ("known_reference_coverage", 0.599),
        ("boundary_mae_s", 0.201),
        ("matched_segments", 9),
        ("identified_precision", float("nan")),
        ("known_reference_coverage", 1.1),
        ("matched_segments", 10.5),
        ("boundary_mae_s", -1),
    ):
        wrong = deepcopy(report)
        wrong["candidate"][key] = value
        with pytest.raises(ValueError):
            gate(wrong)


def test_production_routes_and_experiment_access(tmp_path):
    store, _, _, _, _ = seed(tmp_path)
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.synthetic_target = "prod"
    settings.annotation.synthetic_run_id = None
    app = create_annotation_app(settings, store=store)
    app.state.motion_label_service.refresh(force=True)
    with TestClient(app) as client:
        response = client.get("/api/v1/synthetic/motions")
        assert response.status_code == 200 and response.json()["total"] == 1
        mid = response.json()["cases"][0]["motion_sha256"]
        assert (
            client.get("/api/v1/synthetic/motions/" + mid).json()["status"] == "inference_pending"
        )
        assert client.get("/api/v1/synthetic/motions/configurations").json()["configurations"]


def test_crop_round_trip_preserves_original_note_anchors(tmp_path):
    _, _, service, mid, variant = seed(tmp_path)
    doc = deepcopy(service.feature(mid)["template"])
    doc["segments"][0]["notes"] = [
        {"note_id": "whole-note", "start_ns": 0, "stop_ns": 4_000_000_000, "text": "完整动作说明"}
    ]
    clock = {
        **variant["clock"],
        "source_start_ns": 1_000_000_000,
        "duration_ns": 2_000_000_000,
        "sample_count": 50,
    }
    projected = project_motion(doc, variant["candidate"], clock)
    assert projected["segments"][0]["notes"][0]["stop_ns"] == 2_000_000_000
    restored = apply_projection(doc, projected)
    assert all(s["notes"] == doc["segments"][0]["notes"] for s in restored["segments"])


def test_crop_approval_cannot_accept_unseen_draft_changes(tmp_path, monkeypatch):
    _, _, service, mid, variant = seed(tmp_path)
    body = edit_body(service, mid, variant)
    body["document"]["segments"] = [{**body["document"]["segments"][0], "stop_ns": 4_000_000_000}]
    c = variant["candidate"]
    result = service.edit(
        mid, c["candidate_id"], c["version_id"], body, "human-test", decision="pass"
    )
    body.update(
        document=deepcopy(result["editor_template"]),
        expected_review_sha256=result["review"]["revision_sha256"],
    )
    body["document"]["segments"][0]["primary_code"] = "walking"
    draft = service.edit(mid, c["candidate_id"], c["version_id"], body, "human-test")
    crop = deepcopy(variant)
    crop["clock"].update(source_start_ns=1_000_000_000, duration_ns=2_000_000_000, sample_count=50)
    monkeypatch.setattr(service, "variant", lambda *args: crop)
    edited = service.case(mid, crop)["editor_template"]
    edited["segments"][0]["primary_code"] = "running"
    body.update(document=edited, expected_draft_sha256=draft["draft"]["revision_sha256"])
    service.edit(mid, c["candidate_id"], c["version_id"], body, "human-test", decision="pass")
    accepted = service.resolve(service.state(mid)[0]["accepted"])["document"]
    assert [(s["start_ns"], s["stop_ns"], s["primary_code"]) for s in accepted["segments"]] == [
        (0, 1_000_000_000, "standing"),
        (1_000_000_000, 3_000_000_000, "running"),
        (3_000_000_000, 4_000_000_000, "standing"),
    ]
    unsaved = service.resolve(service.state(mid)[0]["draft"])["document"]
    assert [s["primary_code"] for s in unsaved["segments"]] == ["walking", "running", "walking"]


def test_protected_groups_cannot_be_reviewed_or_exported(tmp_path):
    store, _, service, mid, variant = seed(tmp_path)
    body = edit_body(service, mid, variant)
    c = variant["candidate"]
    service.edit(mid, c["candidate_id"], c["version_id"], body, "human-test", decision="pass")
    feature = service.feature(mid)
    feature["group_key"] = "held-out-person"
    with service.db() as db:
        db.execute("UPDATE motions SET feature=? WHERE id=?", (json.dumps(feature), mid))
    store.write_json(
        service.root + "/holdouts/blind.json",
        {"run_id": "blind", "group_keys": ["held-out-person"]},
        if_generation_match=0,
    )
    assert service.listing()["total"] == 0
    with pytest.raises(ValueError, match="独立验证组"):
        service.case(mid)
    with pytest.raises(ValueError, match="可用"):
        service.create_snapshot("admin")


def test_new_sensor_variant_inherits_review_and_latest_snapshot_deduplicates(tmp_path):
    store, source, service, mid, variant = seed(tmp_path)
    body = edit_body(service, mid, variant)
    c = variant["candidate"]
    accepted = service.edit(
        mid, c["candidate_id"], c["version_id"], body, "human-test", decision="pass"
    )
    newer = deepcopy(variant)
    commit = deepcopy(source._commit(c["candidate_id"], c["version_id"]))
    commit["version_id"] = "d" * 64
    commit["published_at_utc"] = "2026-10-09T00:00:00Z"
    next(o for o in commit["objects"] if o["role"] == "sensors")["sha256"] = "e" * 64
    newer["candidate"].update(
        version_id=commit["version_id"], commit_sha256=document_sha256(commit)
    )
    newer["candidate"]["input_hashes"]["sensors"] = "e" * 64
    newer["published_at_utc"] = commit["published_at_utc"]
    store.write_json(
        source.commit_key(c["candidate_id"], commit["version_id"]), commit, if_generation_match=0
    )
    store.write_json(service.root + "/variants/new.json", newer, if_generation_match=0)
    service.refresh(force=True)
    case = service.case(mid)
    assert case["status"] == "accepted" and case["review"] == accepted["review"]
    assert case["entry"]["candidate"]["version_id"] == "d" * 64
    assert service.listing()["cases"][0]["status"] == "accepted"
    request = store.read_json(service.create_snapshot("admin")["request_key"])[0]
    assert [e["version_id"] for e in request["entries"]] == ["d" * 64]
    state, generation = service.state(mid)
    state["variant_rejections"][c["candidate_id"] + "/" + "d" * 64] = {"reason": "test"}
    service.write_state(mid, state, generation)
    with pytest.raises(ValueError, match="可用"):
        service.create_snapshot("admin")


def test_non_admin_can_review_but_cannot_use_experimental_or_model_routes(tmp_path):
    store, _, _, _, variant = seed(tmp_path)
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.synthetic_target = "prod"
    settings.annotation.synthetic_run_id = None
    settings.auth.local_actor_id = "test-member"
    settings.identity.allowed_unikeys += ("test-member",)
    app = create_annotation_app(settings, store=store)
    app.state.motion_label_service.refresh(force=True)
    c = variant["candidate"]
    with TestClient(app) as client:
        assert client.get("/api/v1/synthetic/motions").status_code == 200
        assert client.get("/api/v1/synthetic/motions/models").status_code == 403
        assert client.get("/api/v1/synthetic/temporal/runs").status_code == 403
        assert client.get("/api/v1/synthetic/temporal/runs/unknown/training").status_code == 403
        assert (
            client.post(
                f"/api/v1/synthetic/candidates/{c['candidate_id']}/{c['version_id']}/reviews",
                json={"decision": "pass", "expected_revision": 0},
            ).status_code
            == 410
        )


def test_worker_persistent_retries_and_crash_recovery(tmp_path, monkeypatch):
    store, _, service, mid, _ = seed(tmp_path)
    release = {"model": {"model_id": "test"}}
    store.write_json(service.root + "/active-model.json", release, if_generation_match=0)
    worker = MotionLabelWorker(service, tmp_path / "cache")
    with service.db() as db:
        db.execute(
            "INSERT INTO inference_jobs(id,motion,release_json,feature_json,state,created) "
            "VALUES(?,?,?,?,?,?)",
            ("test", mid, json.dumps(release), json.dumps(service.feature(mid)), "running", 0),
        )
    worker.recover()

    def fail(*args):
        raise ValueError("temporary artifact failure")

    monkeypatch.setattr(worker, "infer", fail)
    for attempt in range(5):
        result = worker.step()
        assert result["state"] == ("failed" if attempt == 4 else "queued")
        assert worker.step() is None
        with service.db() as db:
            db.execute("UPDATE inference_jobs SET not_before=0")
    assert service.state(mid)[0]["prediction"] is None


def test_model_bootstrap_cannot_bypass_gate_and_baseline_can_rollback(tmp_path):
    store, _, service, _, _ = seed(tmp_path)
    import hashlib

    def model(mid, payload):
        path = tmp_path / (mid + ".bin")
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        key = service.root + "/model-artifacts/" + digest + "/model.onnx"
        store.put_file(
            path, key, content_type="application/octet-stream", metadata={"sha256": digest}
        )
        artifact = {"object_key": key, "sha256": digest, "byte_length": len(payload)}
        return {
            "schema": "imu.production_model.v1",
            "model_id": mid,
            "feature_version": "motion-body22-world-25hz-v1",
            "onnx": artifact,
            "metadata": artifact,
            "release_kind": "existing-baseline",
            "validation": None,
        }

    baseline = model("baseline", b"a")
    other = model("unverified", b"b")
    for m in (baseline, other):
        store.write_json(
            service.root + "/models/" + m["model_id"] + ".json", m, if_generation_match=0
        )
    active = promote_model(service, "baseline", "admin", None)
    with pytest.raises(ValueError, match="基线"):
        promote_model(service, "unverified", "admin", document_sha256(active))
    metric = {
        "identified_precision": 0.95,
        "known_reference_coverage": 0.5,
        "boundary_mae_s": 0.2,
        "boundary_p90_s": 0.4,
        "matched_segments": 10,
    }
    candidate = model("validated", b"c")
    from pathlib import Path

    import imu_data_collector.motion_label_runtime as runtime

    candidate.update(
        release_kind="validated-candidate",
        validation={
            "runtime_sha256": hashlib.sha256(Path(runtime.__file__).read_bytes()).hexdigest(),
            "reference_completed": 20,
            "frozen_before_validation": True,
            "baseline": metric,
            "candidate": {**metric, "known_reference_coverage": 0.6},
            "out_of_vocabulary_regression": False,
            "baseline_model_sha256": baseline["onnx"]["sha256"],
            "baseline_metadata_sha256": baseline["metadata"]["sha256"],
            "candidate_model_sha256": candidate["onnx"]["sha256"],
            "candidate_metadata_sha256": candidate["metadata"]["sha256"],
        },
    )
    store.write_json(service.root + "/models/validated.json", candidate, if_generation_match=0)
    active = promote_model(service, "validated", "admin", document_sha256(active))
    rollback = promote_model(service, "baseline", "admin", document_sha256(active))
    assert rollback["model"] == baseline


def test_migration_preserves_semantics_and_never_creates_acceptance(tmp_path):
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    migrate = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts/migrate-motion-review-drafts.py")
    )["migrate"]
    _, _, service, mid, variant = seed(tmp_path)
    body = edit_body(service, mid, variant)
    document = body["document"]
    document["segments"][0].update(review_state="confirmed", pending_reasons=[])
    document["segments"][1]["notes"] = [
        {
            "note_id": "old-note",
            "start_ns": 2_000_000_000,
            "stop_ns": 4_000_000_000,
            "text": "原人工区间备注",
        }
    ]
    entry = {
        "candidate": variant["candidate"],
        "clock": variant["clock"],
        "reference_role": "development",
    }
    run = {"run_id": "old-experiment", "candidates": [entry]}
    pointer = {"revision_key": "old/review.json", "revision_sha256": "f" * 64}
    old = SimpleNamespace(
        pointer=lambda *a: (pointer, 1), case=lambda *a: {"draft": {"document": document}}
    )
    assert migrate(old, run, service, True)["records"][0]["status"] == "active-editor-kept"
    service.release(mid, "human-test", body["lease_token"])
    assert migrate(old, run, service)["records"][0]["status"] == "would-import-draft"
    assert service.state(mid)[0]["draft"] is None
    result = migrate(old, run, service, True)
    state = service.state(mid)[0]
    assert result["human_acceptances_created"] == 0 and state["accepted"] is None
    migrated = service.resolve(state["draft"])["document"]
    assert migrated["segments"][1]["semantic_text"] == document["segments"][1]["semantic_text"]
    assert migrated["segments"][1]["notes"] == document["segments"][1]["notes"]
    assert (
        migrate(old, run, service, True)["records"][0]["status"]
        == "existing-production-version-kept"
    )
