from copy import deepcopy

import pytest
from test_synthetic_primary import client_for, primary_fixture

from imu_data_collector.synthetic_temporal_contract import (
    SEMANTIC_RUN_SCHEMA,
    confirmed_spans,
    document_sha256,
    semantic_draft,
)


def fixture(tmp_path, role="development"):
    store, _, service, old, entry, proposal = primary_fixture(tmp_path, role)
    run = {**old, "schema": SEMANTIC_RUN_SCHEMA, "run_id": "semantic-test"}
    service.create_run(run)
    service.publish(run, entry, proposal)
    base = "/api/v1/synthetic/temporal/runs/semantic-test"
    identity = entry["candidate"]
    url = f"{base}/cases/{identity['candidate_id']}/{identity['version_id']}"
    return store, service, run, entry, proposal, base, url


def test_interval_draft_conflict_partial_acceptance_and_training_export_mask(tmp_path):
    store, service, run, entry, proposal, base, url = fixture(tmp_path)
    doc = semantic_draft(proposal["document"])
    first = doc["segments"][0]
    doc["segments"] = [
        dict(
            first,
            stop_ns=1_000_000_000,
            primary_code="standing",
            resolution="identified",
            reason=None,
            review_state="confirmed",
            pending_reasons=[],
            semantic_text="站着伸手",
        ),
        dict(
            first,
            segment_id="pending-roll",
            start_ns=1_000_000_000,
            semantic_text="翻滚？",
            notes=[
                {
                    "note_id": "my-note",
                    "start_ns": 1_000_000_000,
                    "stop_ns": 2_000_000_000,
                    "text": "不知道如何切分",
                }
            ],
        ),
    ]
    with client_for(tmp_path, store) as client:
        saved = client.post(url + "/draft", json={"document": doc}).json()
        assert saved["document"] == doc
        assert client.get(url).json()["draft"] == saved
        assert client.get(base + "/training").json()["eligible_reviews"] == 0
        changed = deepcopy(doc)
        changed["segments"][1]["semantic_text"] = "从侧面翻滚"
        assert client.post(url + "/draft", json={"document": changed}).status_code == 409
        response = client.post(
            url + "/review",
            json={
                "decision": "pass",
                "proposal_sha256": document_sha256(proposal),
                "expected_draft_sha256": saved["revision_sha256"],
                "corrected_document": saved["document"],
            },
        )
        assert response.status_code == 200, response.text
        assert client.get(base + "/training").json()["eligible_reviews"] == 1
        review = service.read(response.json()["revision_key"])
        assert confirmed_spans(review["temporal_annotation"]) == [(0, 25, "standing")]
        snapshot = service.create_snapshot(run, "engineering-test")
        assert snapshot["snapshot_id"]
        next_draft = client.post(
            url + "/draft",
            json={"document": changed, "expected_draft_sha256": saved["revision_sha256"]},
        )
        assert next_draft.status_code == 200
        assert client.get(base + "/training").json()["eligible_reviews"] == 0
        assert client.post(base + "/training").status_code == 422
        assert client.get(url).json()["status"] == "pending_review"
        with pytest.raises(ValueError):
            service.create_snapshot(run, "engineering-test")


def test_blind_reference_drafts_keep_notes_without_revealing_model(tmp_path):
    store, _, _, _, proposal, base, url = fixture(tmp_path, "validation")
    doc = semantic_draft(proposal["document"])
    doc["segments"][0]["semantic_text"] = "静止站立，上身摆动"
    with client_for(tmp_path, store) as client:
        draft = client.post(url + "/draft", json={"document": doc}).json()
        assert client.get(url).json()["proposal"] is None
        body = {"document": doc, "expected_draft_sha256": draft["revision_sha256"]}
        assert client.post(url + "/reference", json=body).status_code == 422
        doc["segments"][0].update(
            review_state="confirmed",
            pending_reasons=[],
            primary_code="standing",
            resolution="identified",
            reason=None,
        )
        draft = client.post(
            url + "/draft",
            json={"document": doc, "expected_draft_sha256": draft["revision_sha256"]},
        ).json()
        body = {"document": draft["document"], "expected_draft_sha256": draft["revision_sha256"]}
        reference = client.post(url + "/reference", json=body)
        assert reference.status_code == 200, reference.text
        assert reference.json()["document"]["segments"][0]["semantic_text"] == "静止站立，上身摆动"
        assert client.get(url).json()["proposal"] is not None
        assert client.get(base + "/training").json()["eligible_reviews"] == 0
