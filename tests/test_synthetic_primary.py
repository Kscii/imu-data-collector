from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from test_synthetic_temporal import fixture

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.config import load_settings
from imu_data_collector.synthetic_temporal import ReviewBody
from imu_data_collector.synthetic_temporal_contract import (
    PRIMARY_RUN_SCHEMA,
    document_sha256,
    run_root,
)


def primary_fixture(tmp_path, role="development"):
    store, source, service, old, entry, proposal = fixture(tmp_path)
    run = {
        **old,
        "schema": PRIMARY_RUN_SCHEMA,
        "run_id": "primary-test",
        "candidates": [{**entry, "reference_role": role}],
    }
    service.create_run(run)
    entry = run["candidates"][0]
    service.publish(run, entry, proposal)
    return store, source, service, run, entry, proposal


def client_for(tmp_path, store):
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.synthetic_target = "prod"
    settings.annotation.synthetic_run_id = None
    return TestClient(create_annotation_app(settings, store=store))


def correction(proposal):
    doc = deepcopy(proposal["document"])
    doc["segments"][0].update(primary_code="standing", resolution="identified", reason=None)
    return ReviewBody(
        decision="pass",
        proposal_sha256=document_sha256(proposal),
        corrected_document=doc,
        review_seconds=12,
    )


def test_correction_route_freezes_training_input_and_retries_failed_job(tmp_path):
    store, _, service, run, entry, proposal = primary_fixture(tmp_path)
    base = "/api/v1/synthetic/temporal/runs/primary-test"
    ident = entry["candidate"]
    url = f"{base}/cases/{ident['candidate_id']}/{ident['version_id']}/correction"
    with client_for(tmp_path, store) as client:
        assert client.post(base + "/training").status_code == 422
        body = correction(proposal)
        response = client.post(url, json=body.model_dump())
        assert response.status_code == 200, response.text
        assert client.post(url, json=body.model_dump()).status_code == 409
        assert (
            service.case(run, entry)["reviewed_document"]["segments"][0]["primary_code"]
            == "standing"
        )
        assert client.get(base + "/training").json()["eligible_reviews"] == 1
        first = client.post(base + "/training").json()
        assert first["reviews"][0]["review_sha256"] == response.json()["revision_sha256"]
        assert client.post(base + "/training").json() == first
        store.write_json(
            run_root(run) + "/training/results/" + first["job_id"] + ".json",
            {"request_sha256": document_sha256(first), "state": "failed", "error": "test failure"},
            if_generation_match=0,
        )
        second = client.post(base + "/training").json()
        assert second["job_id"] != first["job_id"] and second["attempt"] == 1


def test_validation_is_blind_and_never_eligible_for_training(tmp_path):
    store, _, service, run, entry, proposal = primary_fixture(tmp_path, "validation")
    assert service.case(run, entry)["proposal"] is None
    body = correction(proposal)
    with pytest.raises(ValueError, match="independent reference"):
        service.review(run, entry, body, "test-human")
    service.save_reference(run, entry, body.corrected_document, "test-human")
    service.review(run, entry, body, "test-human")
    with client_for(tmp_path, store) as client:
        base = "/api/v1/synthetic/temporal/runs/primary-test"
        assert client.get(base + "/training").json()["eligible_reviews"] == 0
        assert client.post(base + "/training").status_code == 422


def test_correction_preserves_clock_auxiliary_scope_and_manual_control(tmp_path):
    _, _, service, run, entry, proposal = primary_fixture(tmp_path)
    body = correction(proposal)
    body.corrected_document["clock"]["source_start_ns"] += 40_000_000
    with pytest.raises(ValueError, match="clock"):
        service.review(run, entry, body, "test-human")
    body = correction(proposal)
    body.corrected_document["segments"][0].update(
        auxiliary_code="waving", auxiliary_status="identified"
    )
    with pytest.raises(ValueError, match="auxiliary"):
        service.review(run, entry, body, "test-human")
    with pytest.raises(ValueError, match="Primary"):
        service.approve(run, "f" * 64, "temporal", "test-human")


@pytest.mark.parametrize("with_metadata", [True, False])
def test_model_download_binds_bytes_and_supports_ranges(tmp_path, with_metadata):
    import hashlib

    store, _, _, run, _, _ = primary_fixture(tmp_path)
    content = b"engineering fixture, not a trained model"
    digest = hashlib.sha256(content).hexdigest()
    model = tmp_path / "fixture.onnx"
    model.write_bytes(content)
    base = run_root(run) + "/models/test-model"
    store.put_file(
        model,
        base + "/model.onnx",
        content_type="application/octet-stream",
        metadata={"sha256": digest} if with_metadata else {},
    )
    descriptor = {
        "files": {
            "model.onnx": {
                "object_key": base + "/model.onnx",
                "sha256": digest,
                "byte_length": len(content),
            }
        }
    }
    store.write_json(base + "/descriptor.json", descriptor, if_generation_match=0)
    with client_for(tmp_path, store) as client:
        url = "/api/v1/synthetic/temporal/runs/primary-test/models/test-model/model.onnx"
        response = client.get(url, headers={"Range": "bytes=0-10"})
        assert response.status_code == 206 and response.content == content[:11]
        assert response.headers["x-content-sha256"] == digest
        assert client.get(url.replace("model.onnx", "model.pt")).status_code == 404
        if not with_metadata:
            store.resolve(base + "/model.onnx").write_bytes(b"x" * len(content))
            assert client.get(url).status_code == 404
