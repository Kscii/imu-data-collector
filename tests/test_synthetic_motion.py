import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.config import load_settings
from imu_data_collector.storage import LocalFilesystemStore, ObjectConflictError
from imu_data_collector.synthetic_catalog import SyntheticCatalog
from imu_data_collector.synthetic_motion import (
    LabelInput,
    ReviewClaimInput,
    ReviewInput,
    SyntheticReviewService,
)


def _seed(store, prefix, candidate_id="clip-1"):
    version_id = "a" * 64
    objects = []
    for role in ("motion", "sensors", "selection"):
        content = role.encode() if candidate_id == "clip-1" else f"{role}:{candidate_id}".encode()
        digest = hashlib.sha256(content).hexdigest()
        source = store.root / (role + ".source" if candidate_id == "clip-1"
                               else candidate_id + "." + role + ".source")
        source.write_bytes(content)
        key = f"{prefix}/objects/{digest}/{role}"
        store.put_file(source, key, content_type="application/octet-stream",
                       metadata={"sha256": digest})
        objects.append({"role": role, "key": key, "sha256": digest,
                        "byte_length": len(content)})
    preview = b"<html><head></head><body>review</body></html>"
    digest = hashlib.sha256(preview).hexdigest()
    source = store.root / ("review.source" if candidate_id == "clip-1"
                           else candidate_id + ".review.source")
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


def test_production_target_only_reads_production_namespace(tmp_path):
    store = LocalFilesystemStore(tmp_path / "objects")
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.catalog_refresh_interval_s = 0
    settings.annotation.synthetic_target = "prod"
    settings.annotation.synthetic_run_id = None
    _seed(store, SyntheticReviewService(store, "old-pilot").prefix, "old-clip")
    production = SyntheticReviewService(store, None, target="prod")
    _seed(store, production.prefix, "new-clip")
    with TestClient(create_annotation_app(settings, store=store)) as client:
        config = client.get("/api/v1/config").json()
        assert config["synthetic_target"] == "prod"
        assert config["synthetic_enabled"] is True
        response = client.get("/api/v1/synthetic/candidates")
        assert response.status_code == 200
        assert [row["candidate_id"] for row in response.json()["candidates"]] == ["new-clip"]
    assert (tmp_path / "synthetic-catalog-prod.sqlite3").is_file()


def test_synthetic_run_id_is_validated_before_catalog_path(tmp_path):
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.synthetic_run_id = "../unsafe"
    with pytest.raises(ValueError, match="Invalid synthetic run ID"):
        create_annotation_app(settings)


def test_incremental_feed_indexes_new_commit_and_keeps_cursor_on_bad_digest(tmp_path):
    store = LocalFilesystemStore(tmp_path / "objects")
    catalog = SyntheticCatalog(tmp_path / "catalog.sqlite3")
    service = SyntheticReviewService(store, "pilot")
    first_id, version_id, _ = _seed(store, service.prefix)
    assert catalog.refresh(store, service.prefix, service._commit,
                           minimum_interval_s=0)["new"] == 1
    second_id, _, _ = _seed(store, service.prefix, "clip-2")
    commit_key = service.commit_key(second_id, version_id)
    commit = store.read_json(commit_key)[0]
    payload = (json.dumps(commit, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode()
    feed_key = f"{service.prefix}/index-feed/2026091701/20260917T010000000000Z-{second_id}.json"
    feed = {"schema": "imu_motion_simulator.candidate_index_feed.v1",
            "feed_key": feed_key, "candidate_id": second_id,
            "version_id": version_id, "commit_key": commit_key,
            "commit_sha256": "0" * 64}
    store.write_json(feed_key, feed, if_generation_match=0)
    assert catalog.refresh(store, service.prefix, service._commit,
                           minimum_interval_s=0)["new"] == 0
    with catalog._connect() as db:
        assert db.execute(
            "SELECT value FROM catalog_meta WHERE key='feed_last_key'").fetchone() is None
    store.delete(feed_key, if_generation_match=None)
    store.write_json(feed_key, {**feed, "commit_sha256": hashlib.sha256(payload).hexdigest()},
                     if_generation_match=0)
    assert catalog.refresh(store, service.prefix, service._commit,
                           minimum_interval_s=0)["new"] == 1
    assert catalog.get(first_id, version_id)["decision"] == "unreviewed"
    assert catalog.get(second_id, version_id)["decision"] == "unreviewed"
    with catalog._connect() as db:
        assert db.execute(
            "SELECT value FROM catalog_meta WHERE key='feed_last_key'").fetchone()[0] == feed_key


def test_unified_synthetic_work_queue_filters_paginates_and_pauses_claims(tmp_path):
    store = LocalFilesystemStore(tmp_path / "objects")
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.catalog_refresh_interval_s = 0
    settings.annotation.synthetic_run_id = "pilot"
    prefix = SyntheticReviewService(store, "pilot").prefix
    _seed(store, prefix, "clip-1")
    _seed(store, prefix, "clip-2")
    with TestClient(create_annotation_app(settings, store=store)) as client:
        base = "/api/v1/work-items?domain=synthetic&view=claimable"
        first = client.get(base + "&limit=1")
        assert first.status_code == 200
        assert first.json()["total"] == 2
        assert len(first.json()["items"]) == 1
        cursor = first.json()["next_cursor"]
        assert cursor
        second = client.get(base + "&limit=1&cursor=" + cursor)
        assert second.status_code == 200
        assert second.json()["total"] == 2
        assert second.json()["items"][0]["item_id"] != first.json()["items"][0]["item_id"]
        assert second.json()["next_cursor"] is None
        assert client.get(base + "&risk=ordinary").json()["total"] == 0
        assert client.get(base + "&search=clip-2").json()["total"] == 1
        pause = client.post("/api/v1/work-items/claim-pause", json={
            "domain": "synthetic", "group": "ACCAD", "paused": True})
        assert pause.status_code == 200
        assert client.get(base).json()["total"] == 0
        assert client.post("/api/v1/synthetic/queue/claim").json()["candidates"] == []
        assert client.get("/api/v1/work-items?domain=synthetic&view=all").json()["total"] == 2


def test_actionable_catalog_hides_superseded_candidate_versions(tmp_path):
    store = LocalFilesystemStore(tmp_path / "objects")
    service = SyntheticReviewService(store, "pilot")
    candidate_id, old_version, _ = _seed(store, service.prefix)
    newer = store.read_json(service.commit_key(candidate_id, old_version))[0]
    newer["version_id"] = "b" * 64
    newer["published_at_utc"] = "2026-09-17T00:00:00Z"
    store.write_json(service.commit_key(candidate_id, newer["version_id"]), newer,
                     if_generation_match=0)
    catalog = SyntheticCatalog(tmp_path / "catalog.sqlite3")
    assert catalog.refresh(store, service.prefix, service._commit,
                           minimum_interval_s=0)["new"] == 2
    actionable, total = catalog.work_items(actor="reviewer", view="claimable")
    assert total == 1
    assert actionable[0]["version_id"] == newer["version_id"]
    assert catalog.work_items(actor="reviewer", view="all")[1] == 2


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
        exact = client.get(base + "/entry")
        assert exact.status_code == 200
        assert exact.json()["candidate_id"] == candidate_id
        assert exact.json()["version_id"] == version_id
        assert exact.json()["decision"] == "unreviewed"
        assert client.get(base.replace(candidate_id, "missing") + "/entry").status_code == 404
        response = client.get(base + "/files/index.html?bridge=2")
        assert response.status_code == 200
        assert b"imu-synthetic-review-key" in response.content
        assert b"#panel{display:none!important}" in response.content
        assert b"<body>review" in response.content
        assert b"imu-synthetic-review-key" not in client.get(
            base + "/files/index.html").content
        assert client.get(base + "/files/api/capabilities").json() == {
            "review_write": False}
        response = client.post(base + "/reviews", json={
            "decision": "pass", "labels": [{"code": "walk", "name": "Walk",
                                             "is_fall": False}],
            "reason": None, "expected_revision": 0,
        })
        assert response.status_code == 200
        assert response.json()["revision"] == 1
        assert client.get(base + "/entry").json()["decision"] == "pass"
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


def test_reviewed_results_can_be_claimed_revised_and_reset_without_changing_snapshot(tmp_path):
    store = LocalFilesystemStore(tmp_path / "objects")
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.catalog_refresh_interval_s = 0
    settings.annotation.synthetic_run_id = "pilot"
    service = create_annotation_app(settings, store=store).state.synthetic_review_service
    candidate_id, version_id, _ = _seed(store, service.prefix)
    first = service.claim("reviewer-a")[0]
    base = dict(candidate_id=candidate_id, version_id=version_id)
    service.review(candidate_id, version_id, ReviewInput(
        decision="pass", expected_revision=0, lease_token=first["lease_token"]),
        "reviewer-a")
    concept = next(item for item in service.labels.catalog()["concepts"]
                   if item["active"] and not item["is_fall"])
    service.set_label(candidate_id, version_id, LabelInput(
        code=concept["code"], expected_revision=0), "reviewer-a")
    frozen = service.create_snapshot("reviewer-a")
    request = service.snapshot(frozen["snapshot_id"])["request"]

    claim = service.claim_reviewed("reviewer-b", ReviewClaimInput(
        **base, expected_revision=1))
    with pytest.raises(ObjectConflictError, match="其他人"):
        service.claim_reviewed("reviewer-a", ReviewClaimInput(**base, expected_revision=1))
    with pytest.raises(ObjectConflictError, match="领取复审"):
        service.review(candidate_id, version_id, ReviewInput(
            decision="reject", reason_codes=["imu_artifact"],
            expected_revision=1), "reviewer-a")
    rejected = service.review(candidate_id, version_id, ReviewInput(
        decision="reject", reason_codes=["imu_artifact"],
        expected_revision=1, lease_token=claim["lease_token"]), "reviewer-b")
    assert rejected["revision"] == 2
    assert service.list_candidates()[0]["decision"] == "reject"
    with pytest.raises(ObjectConflictError, match="变化"):
        service.claim_reviewed("reviewer-a", ReviewClaimInput(**base, expected_revision=1))
    with pytest.raises(ObjectConflictError, match="审核已被更新"):
        service.review(candidate_id, version_id, ReviewInput(
            decision="pass", expected_revision=1), "reviewer-a")

    claim = service.claim_reviewed("reviewer-a", ReviewClaimInput(
        **base, expected_revision=2))
    reset = service.review(candidate_id, version_id, ReviewInput(
        decision="unreviewed", expected_revision=2,
        lease_token=claim["lease_token"]), "reviewer-a")
    assert reset["revision"] == 3
    assert store.read_json(reset["revision_key"])[0]["reason"] is None
    row = service.list_candidates()[0]
    assert row["decision"] == "unreviewed" and row["label"]["code"] == concept["code"]
    assert service.claim("reviewer-a") == []
    assert service.snapshot(frozen["snapshot_id"])["request"] == request
    with pytest.raises(ValueError, match="No accepted"):
        service.create_snapshot("reviewer-b")
    next_claim = service.claim("reviewer-b")[0]
    service.review(candidate_id, version_id, ReviewInput(
        decision="pass", expected_revision=3,
        lease_token=next_claim["lease_token"]), "reviewer-b")
    assert service.list_candidates()[0]["label"]["code"] == concept["code"]
    assert service.create_snapshot("reviewer-b")["candidate_count"] == 1


def test_returned_candidate_precedes_new_unreviewed_and_keeps_formal_label(tmp_path):
    store = LocalFilesystemStore(tmp_path / "objects")
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.catalog_refresh_interval_s = 0
    settings.annotation.synthetic_run_id = "pilot"
    service = create_annotation_app(settings, store=store).state.synthetic_review_service
    candidate_id, version_id, _ = _seed(store, service.prefix, "returned")
    _seed(store, service.prefix, "new")
    initial = service.claim("reviewer-a", batch_size=1)[0]
    assert initial["candidate_id"] == "new"  # same publish time, lexical order
    service.catalog.release("reviewer-a", initial["lease_token"], skip=True)
    initial = service.claim("reviewer-a", batch_size=1)[0]
    assert initial["candidate_id"] == candidate_id
    service.review(candidate_id, version_id, ReviewInput(
        decision="pass", expected_revision=0, lease_token=initial["lease_token"]),
        "reviewer-a")
    concept = next(item for item in service.labels.catalog()["concepts"]
                   if item["active"] and not item["is_fall"])
    service.set_label(candidate_id, version_id, LabelInput(
        code=concept["code"], expected_revision=0), "reviewer-a")
    claim = service.claim_reviewed("reviewer-b", ReviewClaimInput(
        candidate_id=candidate_id, version_id=version_id, expected_revision=1))
    service.review(candidate_id, version_id, ReviewInput(
        decision="unreviewed", expected_revision=1,
        lease_token=claim["lease_token"]), "reviewer-b")
    rows = service.list_candidates(decision="unreviewed")
    assert rows[0]["candidate_id"] == candidate_id
    assert rows[0]["label"]["code"] == concept["code"]
    assert service.claim("reviewer-c", batch_size=1)[0]["candidate_id"] == candidate_id


def test_reviewed_claim_api_reuses_own_lease_and_releases_expired_lease(tmp_path):
    store = LocalFilesystemStore(tmp_path / "objects")
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.catalog_refresh_interval_s = 0
    settings.annotation.synthetic_run_id = "pilot"
    app = create_annotation_app(settings, store=store)
    service = app.state.synthetic_review_service
    candidate_id, version_id, _ = _seed(store, service.prefix)
    first = service.claim("reviewer-a")[0]
    service.review(candidate_id, version_id, ReviewInput(
        decision="pass", expected_revision=0,
        lease_token=first["lease_token"]), "reviewer-a")
    request = {"candidate_id": candidate_id, "version_id": version_id,
               "expected_revision": 1}
    with TestClient(app) as client:
        claimed = client.post("/api/v1/synthetic/queue/claim-reviewed", json=request)
        assert claimed.status_code == 200
        token = claimed.json()["lease_token"]
        assert client.post("/api/v1/synthetic/queue/claim-reviewed",
                           json=request).json()["lease_token"] == token
        with pytest.raises(ObjectConflictError, match="其他人"):
            service.claim_reviewed("another-reviewer", ReviewClaimInput(**request))
        with sqlite3.connect(service.catalog.path) as db:
            db.execute("UPDATE leases SET expires_at=? WHERE token=?",
                       (time.time() - 1, token))
        replacement = service.claim_reviewed(
            "another-reviewer", ReviewClaimInput(**request))
        assert replacement["lease_token"] != token
        with pytest.raises(ObjectConflictError, match="过期"):
            service.review(candidate_id, version_id, ReviewInput(
                decision="reject", reason="test", expected_revision=1,
                lease_token=token), "reviewer-a")


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
        options = client.get("/api/v1/synthetic/labels/mapping-options",
                             params={"origin": "babel-1.0"}).json()
        assert {item["value"] for item in options["values"]} == {"walking"}
        assert {item["name"] for item in options["datasets"]} == {"ACCAD"}
        estimate = client.post("/api/v1/synthetic/labels/mappings/estimate", json={
            "origin": "babel-1.0", "source_value": "walking",
            "source_dataset": "ACCAD", "target_code": code})
        assert estimate.status_code == 200
        assert estimate.json()["matched_count"] == 1
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


def test_motion_concept_edit_keeps_shared_concepts_unchanged(tmp_path):
    store = LocalFilesystemStore(tmp_path / "objects")
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = tmp_path / "objects"
    settings.storage.cache_root = tmp_path / "cache"
    settings.annotation.catalog_path = tmp_path / "catalog.sqlite3"
    settings.annotation.synthetic_run_id = "pilot"
    with TestClient(create_annotation_app(settings, store=store)) as client:
        catalog = client.get("/api/v1/synthetic/labels").json()
        shared = next(item["code"] for item in catalog["concepts"]
                      if item["scope"] == "shared")
        assert client.patch(f"/api/v1/synthetic/labels/concepts/{shared}", json={
            "expected_revision": catalog["motion_revision"], "name": "Changed"}).status_code == 404
        created = client.post("/api/v1/synthetic/labels/concepts", json={
            "code": "custom_motion", "name": "Custom motion", "is_fall": False})
        assert created.status_code == 200
        revision = client.get("/api/v1/synthetic/labels").json()["motion_revision"]
        edited = client.patch("/api/v1/synthetic/labels/concepts/custom_motion", json={
            "expected_revision": revision, "name": "Updated motion", "active": False})
        assert edited.status_code == 200
        catalog = client.get("/api/v1/synthetic/labels").json()
        motion = next(item for item in catalog["concepts"]
                      if item["code"] == "custom_motion")
        assert motion["name"] == "Updated motion" and not motion["active"]
        assert client.patch("/api/v1/synthetic/labels/concepts/custom_motion", json={
            "expected_revision": revision, "active": True}).status_code == 409
