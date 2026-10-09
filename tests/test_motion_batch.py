from copy import deepcopy

import pytest

from imu_data_collector.motion_label_batch import batch_policy
from imu_data_collector.production_gate import production_gate
from imu_data_collector.synthetic_temporal_contract import document_sha256


def valid_report():
    metric = {
        "identified_precision": 0.96,
        "known_reference_coverage": 0.5,
        "boundary_mae_s": 0.2,
        "boundary_p90_s": 0.4,
        "matched_segments": 10,
        "out_of_vocabulary_identified": 0,
    }
    return {
        "schema": "imu.motion_model_validation.v2",
        "reference_completed": 20,
        "frozen_before_validation": True,
        "group_disjoint": True,
        "baseline": metric,
        "candidate": {
            **metric,
            "known_reference_coverage": 0.6,
            "ordinary_precision": 0.96,
            "ordinary_assessed_seconds": 20,
            "full_candidate": {"coverage": 1},
            "per_class": {"standing": {"precision": 0.96}},
            "fall": {
                "reference_events": 20,
                "source_groups": 5,
                "hard_negative_intervals": 40,
                "event_precision": 0.95,
                "event_recall": 0.95,
                "onset_p90_s": 0.4,
                "stop_p90_s": 0.4,
                "impact_p90_s": 0.16,
                "missing_impacts": 0,
                "false_events": 1,
                "negative_hours": 1,
            },
        },
    }


class Service:
    root = "test"

    def __init__(self, control):
        self.control = control

    def optional(self, key):
        return self.control, 0


def test_no_control_and_expired_budget_do_not_admit_jobs():
    release = {"model": {"release_kind": "existing-baseline"}}
    assert batch_policy(Service(None), release) is None
    policy = {
        "schema": "imu.motion_batch.v1",
        "mode": "sample",
        "started_unix": 100,
        "deadline_unix": 200,
        "release_sha256": document_sha256(release),
        "motion_sha256": ["a" * 64],
    }
    assert batch_policy(Service(policy), release, at=200) is None
    assert batch_policy(Service(policy), release, at=150) == policy
    policy["mode"] = "full"
    with pytest.raises(ValueError, match="新版独立"):
        batch_policy(Service(policy), release, at=150)


def test_fall_support_and_timing_gate_cannot_be_bypassed_by_good_overall_score():
    assert production_gate(valid_report())
    for field, value in (
        ("reference_events", 19),
        ("source_groups", 4),
        ("hard_negative_intervals", 39),
        ("event_recall", 0.89),
        ("impact_p90_s", 0.21),
        ("missing_impacts", 1),
    ):
        report = deepcopy(valid_report())
        report["candidate"]["fall"][field] = value
        with pytest.raises(ValueError):
            production_gate(report)
    report = valid_report()
    report["candidate"]["ordinary_precision"] = 0.949
    with pytest.raises(ValueError, match="95%"):
        production_gate(report)


def test_full_batch_binds_final_release_forecast_and_total_budget():
    release = {"model": {"validation": valid_report()}}
    digest = document_sha256(release)
    policy = {
        "schema": "imu.motion_batch.v1",
        "mode": "full",
        "started_unix": 100,
        "deadline_unix": 7300,
        "release_sha256": digest,
        "motion_sha256": ["a" * 64],
        "forecast": {
            "sample_count": 100,
            "cloud_measured": True,
            "release_sha256": digest,
            "total_seconds_with_margin": 7000,
            "margin_fraction": 0.3,
        },
    }
    assert batch_policy(Service(policy), release, at=300) == policy
    policy["forecast"]["total_seconds_with_margin"] = 7201
    with pytest.raises(ValueError, match="two-hour"):
        batch_policy(Service(policy), release, at=300)
    policy["deadline_unix"] += 1
    with pytest.raises(ValueError, match="two hours"):
        batch_policy(Service(policy), release, at=300)


def test_expired_or_outside_sample_jobs_remain_resumable(tmp_path):
    import json

    from test_motion_labels import seed

    from imu_data_collector.motion_label_worker import MotionLabelWorker

    _, _, service, mid, _ = seed(tmp_path)
    worker = MotionLabelWorker(service, tmp_path / "cache")
    with service.db() as db:
        db.execute(
            "INSERT INTO inference_jobs(id,motion,release_json,feature_json,state,created) "
            "VALUES(?,?,?,?,?,?)",
            ("job", mid, "{}", json.dumps(service.feature(mid)), "queued", 0),
        )
    assert worker.step(policy={"deadline_unix": 1, "motion_sha256": [mid]}) is None
    assert worker.step(policy={"motion_sha256": ["a" * 64]}) is None
    with service.db() as db:
        row = db.execute("SELECT state,attempts FROM inference_jobs").fetchone()
        assert tuple(row) == ("queued", 0)


@pytest.mark.parametrize("current", [False, True])
def test_decoder_rebuild_reuses_network_and_persists_teacher_masks(tmp_path, monkeypatch, current):
    import hashlib
    import json

    import numpy as np
    from test_motion_labels import seed

    import imu_data_collector.motion_label_worker as module
    from imu_data_collector.motion_label_runtime import default_sequence_decoder
    from imu_data_collector.synthetic_temporal_contract import CURRENT_PRIMARY, PRODUCTION_PRIMARY

    _, _, service, mid, _ = seed(tmp_path)
    feature = service.feature(mid)
    path = tmp_path / "features.npz"
    np.savez(
        path,
        joints=np.zeros((100, 22, 3), np.float32),
        valid=np.ones(100, bool),
        binding=json.dumps(feature["binding"]),
    )

    def put(path, key):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        service.store.put_file(
            path, key, content_type="application/octet-stream", metadata={"sha256": digest}
        )
        return {"object_key": key, "sha256": digest, "byte_length": path.stat().st_size}

    feature.update(put(path, feature["object_key"]))
    onnx = tmp_path / "model.onnx"
    onnx.write_bytes(b"engineering-network-stub")
    labels = list(CURRENT_PRIMARY if current else PRODUCTION_PRIMARY)
    predicted_index = labels.index("fall") if current else 0
    metadata = {
        "labels": labels,
        "supported_indices": list(range(len(labels))),
        "event_heads": True,
        "decoder": default_sequence_decoder(labels),
    }
    if current:
        metadata["decoder"]["calibration"] = {
            "status": "calibrated",
            "high_thresholds": {"fall": 0.9},
        }
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(json.dumps(metadata))
    model = {
        "model_id": "cache-test",
        "onnx": put(onnx, service.root + "/model-artifacts/test/model.onnx"),
        "metadata": put(metadata_path, service.root + "/model-artifacts/test/metadata.json"),
    }
    calls = []

    class Session:
        def run(self, _, inputs):
            calls.append(1)
            shape = inputs["valid"].shape
            scores = np.zeros((*shape, len(labels)), np.float32)
            scores[..., predicted_index] = 1
            return [scores, np.zeros((*shape, 1), np.float32), np.zeros((*shape, 1), np.float32)]

    monkeypatch.setattr(module, "cpu_session", lambda _: Session())
    worker = module.MotionLabelWorker(service, tmp_path / "cache")
    first = worker.infer(feature, model)
    count = len(calls)
    second = worker.infer(feature, model)
    assert first == second and len(calls) == count
    metadata["decoder"]["transition_weight"] = 0.15
    metadata_path.write_text(json.dumps(metadata))
    model["metadata"] = put(metadata_path, service.root + "/model-artifacts/test/changed.json")
    third = worker.infer(feature, model)
    assert len(calls) == count and third["scores_key"] == first["scores_key"]
    assert third["quality"]["sequence_scores_key"] != first["quality"]["sequence_scores_key"]
    descriptor = service.store.read_json(third["quality"]["sequence_scores_key"])[0]
    saved = module.checked_download(service, descriptor, tmp_path / "teacher.npz")
    with np.load(saved, allow_pickle=False) as archive:
        assert archive["sequence_scores"].shape == (100, len(labels))
        assert archive["valid"].all()
        assert (archive["high_confidence_indices"] == (predicted_index if current else -1)).all()
    assert third["document"]["taxonomy"]["version"] == ("3.0.0" if current else "2.0.0")
    if current:
        assert third["document"]["fall_events"][0]["impact_ns"] is None
        assert third["quality"]["intervals"][0]["reason"] == "experimental_fall"
        assert not third["quality"]["capabilities"]["fall"]["passed"]
        assert all(
            s["primary_code"] is None
            for s in third["quality"]["high_confidence_document"]["segments"]
        )


def test_staged_gate_recomputes_capabilities_and_preserves_legacy_gate():
    from imu_data_collector.production_gate import validated_capabilities

    report = valid_report()
    report["candidate"]["fall"]["reference_events"] = 2
    with pytest.raises(ValueError):
        production_gate(report)
    report.update(schema="imu.motion_model_validation.v3", taxonomy_version="3.0.0")
    assert production_gate(report)
    assert validated_capabilities(report)["daily_activity"]["passed"]
    assert not validated_capabilities(report)["fall"]["passed"]
    report["capabilities"] = {"fall": {"passed": True}}
    with pytest.raises(ValueError):
        production_gate(report, capability="fall")
    report["candidate"]["ordinary_precision"] = 0.94
    with pytest.raises(ValueError):
        production_gate(report)
