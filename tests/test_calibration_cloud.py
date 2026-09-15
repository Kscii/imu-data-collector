import copy
import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_desktop_cloud import _broker_settings, _FakeBucket, _patch_broker_storage

from imu_data_collector import upload_broker
from imu_data_collector.calibration_cloud import (
    CalibrationUpload,
    check_uploaded_report,
    register_calibration_catalog,
)
from imu_data_collector.calibration_experiments import SCHEMA, json_bytes
from imu_data_collector.storage import LocalFilesystemStore


def upload_fixture():
    raw = b"raw evidence bytes"
    raw_sha = hashlib.sha256(raw).hexdigest()
    report = {
        "schema_version": SCHEMA,
        "experiment_id": "cal-" + "1" * 32,
        "sensor_sn": "IMU-0002-R01",
        "operator_id": "xfan0282",
        "created_at_utc": "2026-09-15T10:00:00+00:00",
        "training_eligible": False,
        "data_tier": "test",
        "candidate": {"verified": False},
        "sources": [{"recording_id": "recording-1", "sha256": raw_sha, "size_bytes": len(raw)}],
    }
    report_bytes = json_bytes(report)
    report_sha = hashlib.sha256(report_bytes).hexdigest()
    request = CalibrationUpload(
        experiment_id=report["experiment_id"],
        sensor_sn=report["sensor_sn"],
        report_sha256=report_sha,
        artifacts=[
            {"role": "raw_h5", "sha256": raw_sha, "size_bytes": len(raw)},
            {"role": "report", "sha256": report_sha, "size_bytes": len(report_bytes)},
        ],
    )
    return request, report, {"report.json": report_bytes, f"raw-{raw_sha}.h5": raw}


def broker_client(monkeypatch, tmp_path):
    bucket = _FakeBucket()
    _patch_broker_storage(monkeypatch, bucket)
    monkeypatch.setattr(
        upload_broker.google_id_token,
        "verify_oauth2_token",
        lambda *_args: {"email": "member@example.com", "email_verified": True},
    )
    app = upload_broker.create_upload_broker_app(_broker_settings(tmp_path))
    return TestClient(app), bucket


def test_upload_auth_hash_checks_manifest_last_retry_and_cloud_download(monkeypatch, tmp_path):
    client, bucket = broker_client(monkeypatch, tmp_path)
    request, _report, files = upload_fixture()
    body = request.model_dump(mode="json")
    assert client.post("/v1/calibration-uploads", json=body).status_code == 401
    headers = {"Authorization": "Bearer test"}
    response = client.post("/v1/calibration-uploads", json=body, headers=headers)
    assert response.status_code == 200, response.text
    upload_id = response.json()["upload_id"]

    def complete():
        return client.post(
            "/v1/calibration-uploads/complete", json={"upload_id": upload_id}, headers=headers
        )

    assert complete().status_code == 409
    marker = bucket.blob(f"{request.prefix}/manifest.json")
    assert not marker.exists(None)
    for artifact in request.files():
        bucket.blob(artifact["object_key"]).upload_from_string(files[artifact["file_id"]])
    response = complete()
    assert response.status_code == 200, response.text
    assert response.json()["verified_sha256"] is True
    first_manifest = marker.content
    assert complete().status_code == 200
    assert marker.content == first_manifest
    retry = client.post("/v1/calibration-uploads", json=body, headers=headers)
    assert all(item["already_present"] for item in retry.json()["sessions"])

    # A ready manifest is the only catalog entry; no SQL migration is needed.
    store = LocalFilesystemStore(tmp_path / "objects")
    for artifact in request.files():
        path = tmp_path / artifact["file_id"]
        path.write_bytes(files[artifact["file_id"]])
        store.put_file(
            path,
            artifact["object_key"],
            content_type=artifact["content_type"],
            metadata={"sha256": artifact["sha256"]},
        )
    app = FastAPI()
    register_calibration_catalog(app, store)
    browser = TestClient(app)
    assert browser.get("/api/v1/calibration-experiments").json() == []
    store.write_json(
        f"{request.prefix}/manifest.json", json.loads(first_manifest), if_generation_match=0
    )
    assert len(browser.get("/api/v1/calibration-experiments").json()) == 1
    root = (
        f"/api/v1/calibration-experiments/{request.experiment_id}/reports/{request.report_sha256}"
    )
    assert browser.get(root).json()["candidate"]["verified"] is False
    for file_id, content in files.items():
        response = browser.get(f"{root}/files/{file_id}")
        assert response.status_code == 200, response.text
        assert response.content == content
    assert browser.get(f"{root}/files/not-in-manifest").status_code == 404


def test_wrong_bytes_and_foreign_upload_owner_never_publish(monkeypatch, tmp_path):
    client, bucket = broker_client(monkeypatch, tmp_path)
    request, _, files = upload_fixture()
    headers = {"Authorization": "Bearer test"}
    started = client.post(
        "/v1/calibration-uploads", json=request.model_dump(mode="json"), headers=headers
    ).json()
    for artifact in request.files():
        payload = files[artifact["file_id"]]
        bucket.blob(artifact["object_key"]).upload_from_string(b"x" * len(payload))
    response = client.post(
        "/v1/calibration-uploads/complete",
        json={"upload_id": started["upload_id"]},
        headers=headers,
    )
    assert response.status_code == 409
    assert not bucket.blob(f"{request.prefix}/manifest.json").exists(None)
    plan_blob = bucket.blob(f"_upload_sessions/calibration/{started['upload_id']}.json")
    plan = json.loads(plan_blob.content)
    plan["actor"]["unikey"] = "another-user"
    plan_blob.upload_from_string(json_bytes(plan))
    assert (
        client.post(
            "/v1/calibration-uploads/complete",
            json={"upload_id": started["upload_id"]},
            headers=headers,
        ).status_code
        == 403
    )


def test_report_must_reference_exact_raw_files_and_remain_unverified():
    request, report, _ = upload_fixture()
    check_uploaded_report(report, request)
    changed = copy.deepcopy(report)
    changed["sources"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="raw sources"):
        check_uploaded_report(changed, request)
    changed = copy.deepcopy(report)
    changed["candidate"]["verified"] = True
    with pytest.raises(ValueError, match="status"):
        check_uploaded_report(changed, request)
    with pytest.raises(ValueError):
        CalibrationUpload.model_validate({**request.model_dump(), "experiment_id": "../outside"})
