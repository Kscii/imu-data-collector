import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.config import load_settings
from imu_data_collector.storage import LocalFilesystemStore, ObjectConflictError
from imu_data_collector.synthetic_motion import ReviewInput, SyntheticReviewService


def _seed(store, prefix):
    candidate_id, version_id = "clip-1", "a" * 64
    objects = []
    for role in ("motion", "sensors", "selection"):
        content = role.encode()
        digest = hashlib.sha256(content).hexdigest()
        source = store.root / (role + ".source")
        source.write_bytes(content)
        key = f"{prefix}/objects/{digest}/{role}"
        store.put_file(source, key, content_type="application/octet-stream",
                       metadata={"sha256": digest})
        objects.append({"role": role, "key": key, "sha256": digest,
                        "byte_length": len(content)})
    preview = b"<html><head></head><body>review</body></html>"
    digest = hashlib.sha256(preview).hexdigest()
    source = store.root / "review.source"
    source.write_bytes(preview)
    preview_key = f"{prefix}/previews/{candidate_id}/{version_id}/index.html"
    store.put_file(source, preview_key, content_type="text/html",
                   metadata={"sha256": digest})
    commit = {
        "schema": "imu_motion_simulator.candidate_commit.v1",
        "candidate_id": candidate_id, "version_id": version_id,
        "source_dataset": "ACCAD", "source_member": "ACCAD/clip-1.npz",
        "policy_sha256": "b" * 64,
        "label_candidates": [{"code": "walk", "name": "Walk", "is_fall": False}],
        "warning_flags": [], "objects": objects,
        "bundle_files": [{"name": "index.html", "key": preview_key,
                          "sha256": digest, "byte_length": len(preview)}],
        "published_at_utc": "2026-09-16T00:00:00Z",
    }
    store.write_json(f"{prefix}/candidates/{candidate_id}/{version_id}.json",
                     commit, if_generation_match=0)
    return candidate_id, version_id, preview_key


def test_synthetic_reviews_freeze_exact_pass_and_keep_old_snapshot(tmp_path):
    store = LocalFilesystemStore(tmp_path)
    service = SyntheticReviewService(store, "pilot")
    candidate_id, version_id, _ = _seed(store, service.prefix)
    assert service.list_candidates()[0]["decision"] == "unreviewed"
    assert service.file(candidate_id, version_id, "index.html")[0] == (
        b"<html><head></head><body>review</body></html>")
    passed = service.review(candidate_id, version_id, ReviewInput(
        decision="pass", labels=[{"code": "walk", "name": "Walk", "is_fall": False}],
        expected_revision=0), "reviewer")
    assert passed["revision"] == 1
    with pytest.raises(ObjectConflictError):
        service.review(candidate_id, version_id, ReviewInput(
            decision="reject", reason="bad", labels=[], expected_revision=0), "other")
    snapshot = service.create_snapshot("reviewer")
    frozen = service.snapshot(snapshot["snapshot_id"])["request"]
    assert len(frozen["entries"]) == 1
    service.review(candidate_id, version_id, ReviewInput(
        decision="reject", reason="changed my mind", labels=[], expected_revision=1),
        "reviewer")
    assert service.list_candidates()[0]["decision"] == "reject"
    assert service.snapshot(snapshot["snapshot_id"])["request"] == frozen
    with pytest.raises(ValueError, match="No accepted"):
        service.create_snapshot("reviewer")


def test_objects_without_a_commit_are_not_listed(tmp_path):
    store = LocalFilesystemStore(tmp_path)
    service = SyntheticReviewService(store, "pilot")
    candidate_id, version_id, _ = _seed(store, service.prefix)
    store.delete(service.commit_key(candidate_id, version_id), if_generation_match=None)
    assert service.list_candidates() == []


def test_missing_artifact_blocks_review_and_snapshot_freeze(tmp_path):
    store = LocalFilesystemStore(tmp_path)
    service = SyntheticReviewService(store, "pilot")
    candidate_id, version_id, preview_key = _seed(store, service.prefix)
    store.delete(preview_key, if_generation_match=None)
    with pytest.raises(ValueError, match="missing or truncated"):
        service.review(candidate_id, version_id, ReviewInput(
            decision="pass", labels=[{"code": "walk", "name": "Walk",
                                      "is_fall": False}], expected_revision=0),
            "reviewer")
    store.put_file(store.root / "review.source", preview_key,
                   content_type="text/html",
                   metadata={"sha256": hashlib.sha256(
                       b"<html><head></head><body>review</body></html>").hexdigest()})
    service.review(candidate_id, version_id, ReviewInput(
        decision="pass", labels=[{"code": "walk", "name": "Walk",
                                  "is_fall": False}], expected_revision=0),
        "reviewer")
    store.delete(preview_key, if_generation_match=None)
    with pytest.raises(ValueError, match="missing or truncated"):
        service.create_snapshot("reviewer")


def test_annotation_api_exposes_separate_synthetic_review_flow(tmp_path):
    store = LocalFilesystemStore(tmp_path / "objects")
    service = SyntheticReviewService(store, "pilot")
    candidate_id, version_id, _ = _seed(store, service.prefix)
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.catalog_refresh_interval_s = 0
    settings.annotation.synthetic_run_id = "pilot"
    with TestClient(create_annotation_app(settings, store=store)) as client:
        response = client.get("/api/v1/synthetic/candidates")
        assert response.status_code == 200
        assert len(response.json()["candidates"]) == 1
        base = f"/api/v1/synthetic/candidates/{candidate_id}/{version_id}"
        response = client.get(base + "/files/index.html")
        assert response.status_code == 200
        assert b"imu-synthetic-review-key" in response.content
        assert b"<body>review" in response.content
        assert client.get(base + "/files/api/capabilities").json() == {
            "review_write": False}
        response = client.post(base + "/reviews", json={
            "decision": "pass", "labels": [{"code": "walk", "name": "Walk",
                                             "is_fall": False}],
            "reason": None, "expected_revision": 0,
        })
        assert response.status_code == 200
        assert response.json()["revision"] == 1
        assert client.post(base + "/reviews", json={
            "decision": "pass", "labels": [{"code": "walk", "name": "Walk",
                                             "is_fall": False}],
            "reason": None, "expected_revision": 0,
        }).status_code == 409
        snapshot = client.post("/api/v1/synthetic/snapshots")
        assert snapshot.status_code == 200
        snapshot_id = snapshot.json()["snapshot_id"]
        assert client.get(f"/api/v1/synthetic/snapshots/{snapshot_id}").json()[
            "request"]["entries"][0]["candidate_id"] == candidate_id


def test_quality_pass_requires_separate_formal_label_for_snapshot(tmp_path):
    store = LocalFilesystemStore(tmp_path / "objects")
    service = SyntheticReviewService(store, "pilot")
    candidate_id, version_id, _ = _seed(store, service.prefix)
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.catalog_refresh_interval_s = 0
    settings.annotation.synthetic_run_id = "pilot"
    with TestClient(create_annotation_app(settings, store=store)) as client:
        claim = client.post("/api/v1/synthetic/queue/claim").json()["candidates"]
        assert len(claim) == 1
        assert client.post("/api/v1/synthetic/queue/claim").json()["candidates"] == claim
        token = claim[0]["lease_token"]
        assert client.post("/api/v1/synthetic/queue/renew",
                           json={"lease_token": token}).status_code == 200
        base = f"/api/v1/synthetic/candidates/{candidate_id}/{version_id}"
        passed = client.post(base + "/reviews", json={
            "decision": "pass", "expected_revision": 0, "lease_token": token})
        assert passed.status_code == 200
        assert client.post("/api/v1/synthetic/queue/renew",
                           json={"lease_token": token}).status_code == 409
        assert client.get("/api/v1/synthetic/summary").json()["label_pending"] == 1
        assert len(client.get("/api/v1/synthetic/candidates?decision=pass&label_state=pending")
                   .json()["candidates"]) == 1
        assert client.post("/api/v1/synthetic/snapshots").status_code == 422
        concepts = client.get("/api/v1/synthetic/labels").json()["concepts"]
        code = next(item["code"] for item in concepts if item["active"] and not item["is_fall"])
        labeled = client.post(base + "/labels", json={"code": code,
                                                        "expected_revision": 0})
        assert labeled.status_code == 200
        assert client.get("/api/v1/synthetic/summary").json()["snapshot_eligible"] == 1
        assert (client.get("/api/v1/synthetic/candidates?decision=pass&label_state=pending")
                .json()["candidates"]) == []
        snapshot = client.post("/api/v1/synthetic/snapshots").json()
        entry = client.get(f"/api/v1/synthetic/snapshots/{snapshot['snapshot_id']}").json()[
            "request"]["entries"][0]
        assert entry["label_key"].startswith(service.label_root(candidate_id, version_id))
        assert "label_sha256" in entry
        review = store.read_json(passed.json()["revision_key"])[0]
        assert review["labels"] == []


def test_queue_skip_cools_down_candidate_for_same_actor(tmp_path):
    store = LocalFilesystemStore(tmp_path)
    service = SyntheticReviewService(store, "pilot", catalog_path=tmp_path / "queue.sqlite3")
    _seed(store, service.prefix)
    first = service.claim("reviewer")
    assert len(first) == 1
    assert service.claim("reviewer") == first
    assert service.claim("other") == []
    service.catalog.release("reviewer", first[0]["lease_token"], skip=True)
    assert service.claim("reviewer") == []
    assert len(service.claim("other")) == 1


def test_parallel_queue_refresh_imports_one_commit_once(tmp_path):
    store = LocalFilesystemStore(tmp_path)
    service = SyntheticReviewService(store, "pilot", catalog_path=tmp_path / "queue.sqlite3")
    _seed(store, service.prefix)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda _: service.catalog.refresh(
                store, service.prefix, service._commit, minimum_interval_s=0),
            range(4)))
    assert sum(result["new"] for result in results) == 1
    assert service.catalog.counts()["published"] == 1


def test_activated_exact_mapping_freezes_label_without_human_label_edit(tmp_path):
    store = LocalFilesystemStore(tmp_path / "objects")
    seed = SyntheticReviewService(store, "pilot")
    candidate_id, version_id, _ = _seed(store, seed.prefix)
    key = seed.commit_key(candidate_id, version_id)
    commit, generation = store.read_json(key)
    commit["label_candidates"] = [{
        "origin": "babel-1.0", "kind": "recording-candidate",
        "categories": ["walking"], "raw_label": "walk"}]
    store.write_json(key, commit, if_generation_match=generation)
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.catalog_refresh_interval_s = 0
    settings.annotation.synthetic_run_id = "pilot"
    with TestClient(create_annotation_app(settings, store=store)) as client:
        concepts = client.get("/api/v1/synthetic/labels").json()["concepts"]
        code = next(item["code"] for item in concepts if item["active"] and not item["is_fall"])
        created = client.post("/api/v1/synthetic/labels/mappings", json={
            "origin": "babel-1.0", "source_value": "walking",
            "source_dataset": "ACCAD", "target_code": code})
        assert created.status_code == 200
        rule_id = created.json()["rule_id"]
        preview = client.get(f"/api/v1/synthetic/labels/mappings/{rule_id}/preview").json()
        assert preview["matched_count"] == 1
        assert client.post(f"/api/v1/synthetic/labels/mappings/{rule_id}/state", json={
            "state": "active", "checked_candidate_ids": [candidate_id]}).status_code == 200
        base = f"/api/v1/synthetic/candidates/{candidate_id}/{version_id}"
        assert client.post(base + "/reviews", json={
            "decision": "pass", "expected_revision": 0}).status_code == 200
        snapshot = client.post("/api/v1/synthetic/snapshots").json()
        entry = client.get(f"/api/v1/synthetic/snapshots/{snapshot['snapshot_id']}").json()[
            "request"]["entries"][0]
        resolution = store.read_json(entry["label_key"])[0]
        assert resolution["label"]["code"] == code
        assert resolution["label"]["verification"] == "rule"
        assert resolution["label"]["mapping_rule_id"] == rule_id
        assert client.post(f"/api/v1/synthetic/labels/mappings/{rule_id}/state", json={
            "state": "retired"}).status_code == 200
        assert store.read_json(entry["label_key"])[0] == resolution
