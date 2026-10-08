from __future__ import annotations

import copy
import csv
import io
import json
import sqlite3
import zipfile
from datetime import timedelta

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.auth import Actor
from imu_data_collector.config import (
    AnnotationSettings,
    AuthSettings,
    ExternalDeviceSettings,
    Settings,
    StorageSettings,
    _construct_settings,
)
from imu_data_collector.external_device_api import register_external_devices
from imu_data_collector.external_device_domain import (
    PREFIX,
    device_id,
    json_bytes,
    metrics,
    now,
    timestamp,
    utc,
)
from imu_data_collector.external_device_runtime import ExternalDeviceRuntime
from imu_data_collector.external_device_service import (
    CredentialsError,
    ExternalDeviceService,
    HistoryClient,
    ResponseTooLarge,
    UpstreamError,
    UpstreamTimeout,
)
from imu_data_collector.external_device_worker import ExternalDeviceWorker
from imu_data_collector.storage import LocalFilesystemStore

START = "2026-10-01T16:00:00.000Z"
END = "2026-10-01T16:15:00.000Z"


@pytest.fixture
def service(tmp_path):
    settings = Settings(
        annotation=AnnotationSettings(catalog_path=tmp_path / "annotation.sqlite3"),
        storage=StorageSettings(root=tmp_path / "objects", cache_root=tmp_path / "cache"),
        external_devices=ExternalDeviceSettings(enabled=True),
    )
    return ExternalDeviceService(settings, LocalFilesystemStore(settings.storage.root))


def install_device(service, kind="mattress", product="product-a", number="123", at=START):
    history = f"mattress:{product}/{number}" if kind == "mattress" else number
    item = {"deviceNo": number, "historyDeviceNo": history, "displayName": "Fixture"}
    if kind == "mattress":
        item["productKey"] = product
    service.catalog.discover(kind, [item], at)
    return service.catalog.device(device_id(kind, history))


def record(device, source_id=1, received=START, message=None, payload=None):
    payload = (
        payload
        if payload is not None
        else {"params": {"HeartRate": 69, "RespiratoryRate": 14, "People_flag": 87, "D": 681}}
    )
    row = {
        "id": source_id,
        "receivedAt": received,
        "payload": payload,
        "rawPayload": json.dumps(payload),
    }
    if device["kind"] == "mattress":
        row.update(
            messageType=message or "REALTIME",
            deviceName=device["device_no"],
            productKey=device["product_key"],
            topic=None,
        )
    else:
        row.update(
            operation=message or "UHR", deviceNo=device["device_no"], dataTime=None, vid=None
        )
    return row


def envelope(device, records, start=START, end=END):
    data = {
        "deviceType": "MATTRESS_DEVICE" if device["kind"] == "mattress" else "RADAR_WATCH",
        "deviceNo": device["device_no"],
        "startTime": start,
        "endTime": end,
        "timeField": "receivedAt",
        "total": len(records),
        "records": records,
    }
    if device["kind"] == "mattress":
        data["productKey"] = device["product_key"]
    return {"code": 200, "msg": "success", "data": data}


def archive(service, device, records, start=START, end=END):
    obj = envelope(device, records, start, end)
    return service.archive(device, start, end, obj, json_bytes(obj))


def client_for(service, *, admin=True, enabled=True, access="members"):
    app = FastAPI()
    service.settings.external_devices.enabled = enabled
    service.settings.external_devices.access = access

    def actor(request: Request):
        if request.headers.get("x-test-login") == "no":
            raise HTTPException(401)
        return Actor("tester", None, None, admin, "local")

    register_external_devices(app, service.settings, service.store, actor)
    return TestClient(app)


def test_configuration_is_opt_in_and_has_no_secret_field():
    assert not Settings().external_devices.enabled
    assert Settings().external_devices.access == "members"
    settings = _construct_settings({"external_devices": {"enabled": True, "access": "members"}})
    assert settings.external_devices.enabled and settings.external_devices.sync_interval_s == 900
    assert not hasattr(settings.external_devices, "api_key")
    with pytest.raises(ValueError):
        ExternalDeviceSettings(access="public")


def test_member_navigation_and_management_use_verified_identity(service):
    settings = service.settings
    settings.auth = AuthSettings(mode="iap", iap_audience="test-audience")
    settings.identity.email_to_unikey = {"member@example.com": "rkim6933"}
    app = create_annotation_app(
        settings, service.store,
        token_verifier=lambda token, audience: {"email": "member@example.com", "sub": token},
    )
    client = TestClient(app)
    headers = {"X-Goog-IAP-JWT-Assertion": "verified-member"}
    assert client.get("/api/v1/config", headers=headers).json()["can_view_external_devices"]
    prefix = "/api/v1/external-devices"
    assert client.get(prefix + "/devices", headers=headers).status_code == 200
    assert client.get(prefix + "/devices").status_code == 401
    assert client.post(prefix + "/sync", headers=headers).status_code == 403
    assert client.post(
        prefix + "/rebuild", headers=headers,
        json={"confirmed": True, "history_start": START},
    ).status_code == 403
    assert client.put(
        prefix + "/settings", headers=headers, json={"history_start": START},
    ).status_code == 403
    settings.external_devices.access = "admins"
    assert not client.get("/api/v1/config", headers=headers).json()["can_view_external_devices"]
    assert client.get(prefix + "/devices", headers=headers).status_code == 403


def test_identity_dedup_versions_and_product_isolation(service):
    first = install_device(service)
    second = install_device(service, product="product-b")
    rows = [record(first), record(first, message="WARNING")]
    archive(service, first, rows)
    archive(service, first, rows)
    archive(service, second, [record(second)])
    assert len(service.catalog.records(first["id"], START, END)) == 2
    assert len(service.catalog.rows("SELECT * FROM records")) == 3
    assert len(service.catalog.rows("SELECT * FROM versions")) == 3
    totals = {item["id"]: item["total"] for item in service.devices()}
    assert totals == {first["id"]: 2, second["id"]: 1}
    changed = copy.deepcopy(rows)
    changed[0]["payload"]["params"]["HeartRate"] = 70
    changed[0]["rawPayload"] = json.dumps(changed[0]["payload"])
    # Deterministic newer fetch, even on clocks with coarse resolution.
    previous = service.catalog.rows("SELECT MAX(fetched_at) AS latest FROM records")[0]["latest"]
    service.catalog.execute("UPDATE records SET fetched_at='2000-01-01T00:00:00.000Z'")
    archive(service, first, changed)
    current = service.catalog.records(first["id"], START, END)[0]
    assert service.original(current)["payload"]["params"]["HeartRate"] == 70
    assert len(service.catalog.rows("SELECT * FROM versions")) == 4
    assert previous


def test_watch_flags_and_ids_are_preserved(service):
    device = install_device(service, kind="radar-watch")
    source = record(device, source_id=2**60, payload={"HR": 76, "_synthetic": True})
    archive(service, device, [source])
    row = service.catalog.records(device["id"], START, END)[0]
    assert row["source_id"] == str(2**60) and row["synthetic"] == 1
    assert service.devices()[0]["synthetic_records"] == 1
    client = client_for(service)
    detail = client.get(
        f"/api/v1/external-devices/devices/{device['id']}/records/{row['id']}"
    ).json()
    assert json.loads(detail["record_json"]) == source


@pytest.mark.parametrize(
    "damage", ["count", "identity", "product", "time", "duplicate", "sort", "query", "type"]
)
def test_invalid_responses_never_publish_coverage(service, damage):
    device = install_device(service)
    rows = [record(device), record(device, 2, "2026-10-01T16:00:02.000Z")]
    obj = envelope(device, rows)
    if damage == "count":
        obj["data"]["total"] = 1
    if damage == "identity":
        rows[0]["deviceName"] = "other"
    if damage == "product":
        obj["data"]["productKey"] = "other"
    if damage == "time":
        rows[1]["receivedAt"] = END
    if damage == "duplicate":
        rows[1]["id"] = 1
    if damage == "sort":
        rows.reverse()
    if damage == "query":
        obj["data"]["startTime"] = END
    if damage == "type":
        rows[0]["id"] = True
    with pytest.raises(ValueError):
        service.archive(device, START, END, obj, json_bytes(obj))
    assert not service.catalog.coverage(device["id"], START, END)["complete"]
    assert service.store.list(PREFIX + "/archives/") == []


def test_empty_coverage_is_distinct_from_unknown_and_daily_utc_boundary(service):
    device = install_device(service)
    archive(service, device, [])
    assert service.catalog.coverage(device["id"], START, END)["complete"]
    day_end = "2026-10-02T16:00:00.000Z"
    assert not service.catalog.coverage(device["id"], START, day_end)["complete"]
    archive(service, device, [], END, day_end)
    days = {d["day"]: d for d in service.calendar(device["id"], 2026, 10)["days"]}
    assert days["2026-10-02"]["state"] == "complete"
    assert days["2026-10-02"]["total"] == 0
    assert days["2026-10-01"]["state"] == "unqueried"


def test_metrics_never_fill_missing_or_guess_unknown_states():
    values = metrics(
        {"payload": {"params": {"HeartRate": 0, "People_flag": -1, "D": 681}}}, "mattress"
    )
    assert values["HeartRate"] == 0 and values["RespiratoryRate"] is None
    assert values["in_bed"] is None and values["sleep_stage"] == 1
    assert metrics({"operation": "UHR", "payload": {"HR": "72"}}, "radar-watch") == {"HR": 72}
    assert metrics({"operation": "OTHER", "payload": {"HR": 99}}, "radar-watch") == {}


def test_decimation_does_not_invent_gaps_in_dense_data(service):
    device = install_device(service)
    rows = [
        record(device, index, utc(timestamp(START) + timedelta(seconds=index * 2)))
        for index in range(400)
    ]
    archive(service, device, rows)
    curve = next(
        item
        for item in service.series(device["id"], START, END, bins=4)["series"]
        if item["key"] == "HeartRate"
    )
    assert curve["sampled"]
    assert all(point[1] is not None for point in curve["points"])


def test_same_time_null_and_numeric_records_can_be_plotted(service):
    device = install_device(service)
    rows = [record(device, 1), record(device, 2, payload={"params": {"HeartRate": None}})]
    archive(service, device, rows)
    curve = next(
        item
        for item in service.series(device["id"], START, END)["series"]
        if item["key"] == "HeartRate"
    )
    assert {point[1] for point in curve["points"]} == {69, None}


def test_export_coverage_matches_record_snapshot_during_concurrent_sync(service, monkeypatch):
    device = install_device(service)
    archive(service, device, [record(device)])
    stop = "2026-10-01T16:30:00.000Z"
    job = service.catalog.export_job(device["id"], START, stop)
    original = service.original
    inserted = False

    def read_and_sync(row, cache=None):
        nonlocal inserted
        if not inserted:
            inserted = True
            archive(service, device, [record(device, 2, END)], END, stop)
        return original(row, cache)

    monkeypatch.setattr(service, "original", read_and_sync)
    service.build_export(job)
    output = service.catalog.rows("SELECT * FROM exports WHERE id=?", (job["id"],))[0]
    with zipfile.ZipFile(io.BytesIO(service.store.read_bytes(output["object_key"]))) as package:
        manifest = json.loads(package.read("manifest.json"))
        assert len(json.loads(package.read("records.json"))) == manifest["total"] == 1
        assert manifest["coverage"]["gaps"] == [[END, stop]]
    assert service.catalog.coverage(device["id"], START, stop)["complete"]


def test_spikes_missing_values_and_synthetic_flags_survive_chart_sampling(service):
    device = install_device(service)
    rows = []
    for index in range(400):
        payload = {"params": {"HeartRate": 300 if index == 123 else 60}}
        if index == 200:
            payload["params"]["HeartRate"] = None
        if index == 123:
            payload["_synthetic"] = True
        rows.append(
            record(
                device, index, utc(timestamp(START) + timedelta(seconds=index * 2)), payload=payload
            )
        )
    archive(service, device, rows)
    points = next(
        s
        for s in service.series(device["id"], START, END, bins=4)["series"]
        if s["key"] == "HeartRate"
    )["points"]
    assert any(point[1] == 300 and point[2] for point in points)
    assert any(point[1] is None for point in points)
    assert len(points) < len(rows)


def test_archive_failure_does_not_advance_coverage(service, monkeypatch):
    device = install_device(service)
    monkeypatch.setattr(
        service.store, "write_json", lambda *a, **k: (_ for _ in ()).throw(OSError("failure"))
    )
    with pytest.raises(OSError):
        archive(service, device, [record(device)])
    assert service.catalog.rows("SELECT * FROM records") == []
    assert not service.catalog.coverage(device["id"], START, END)["complete"]


def test_index_failure_recovers_from_manifest_and_rebuild_is_idempotent(service, monkeypatch):
    device = install_device(service)
    service.persist_control()
    with monkeypatch.context() as patch:
        patch.setattr(
            service.catalog,
            "index_archive",
            lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError()),
        )
        with pytest.raises(sqlite3.OperationalError):
            archive(service, device, [record(device)])
    assert service.rebuild()["indexed_archives"] == 1
    assert service.rebuild()["indexed_archives"] == 0
    assert len(service.catalog.rows("SELECT * FROM records")) == 1
    # Full loss of the derived DB must not lose the source evidence or initial sync boundary.
    service.catalog.path.unlink()
    restored = ExternalDeviceService(service.settings, service.store)
    restored.rebuild()
    assert len(restored.catalog.rows("SELECT * FROM records")) == 1
    assert restored.catalog.device(device["id"])["first_seen"] == START
    assert restored.catalog.device(device["id"])["scheduled_until"] == END


def test_corrupt_archive_is_rejected(service):
    device = install_device(service)
    manifest = archive(service, device, [record(device)])
    service.store.resolve(manifest["object_key"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        service.load_archive(manifest)


def test_no_history_before_confirmation_and_live_priority(service):
    device = install_device(service, at=END)
    service.catalog.schedule_history(900)
    assert service.catalog.next_window() is None
    service.catalog.confirm_history(START)
    service.catalog.schedule_history(900)
    later = "2026-10-01T16:30:00.000Z"
    service.catalog.schedule_live(later, 900, 1800)
    first = service.catalog.next_window()
    assert first["lane"] == "live" and first["start"] == END
    service.catalog.recover_running()
    assert service.catalog.next_window()["id"] == first["id"]
    with pytest.raises(ValueError):
        service.catalog.confirm_history(END)
    new = install_device(service, number="456", at=later)
    assert new["history_start"] == START
    service.catalog.discover("mattress", [], later)
    assert not service.catalog.device(device["id"])["listed"]


class FixtureClient:
    def __init__(self, service, error=None):
        self.service, self.error = service, error

    def request(self, kind, payload):
        if self.error:
            raise self.error
        device = self.service.catalog.rows(
            "SELECT * FROM devices WHERE history_no=?", (payload["deviceNo"],)
        )[0]
        obj = envelope(device, [], payload["startTime"], payload["endTime"])
        return obj, json_bytes(obj)


@pytest.mark.parametrize("exception", [ResponseTooLarge("large"), UpstreamTimeout("timeout")])
def test_large_windows_split_without_claiming_success(service, exception):
    device = install_device(service)
    service.catalog.enqueue_window(device["id"], START, END, "live")
    worker = ExternalDeviceWorker(service, FixtureClient(service, exception))
    worker.synchronize(service.catalog.next_window())
    states = service.catalog.rows("SELECT state,start,end FROM windows ORDER BY start,end")
    assert sorted(row["state"] for row in states) == ["pending", "pending", "split"]
    child = [row for row in states if row["state"] == "pending"]
    assert (
        child[0]["start"] == START
        and child[0]["end"] == child[1]["start"]
        and child[1]["end"] == END
    )
    assert not service.catalog.coverage(device["id"], START, END)["complete"]


def test_worker_retries_then_commits_and_pauses_bad_credentials(service):
    device = install_device(service)
    service.catalog.enqueue_window(device["id"], START, END, "live")
    worker = ExternalDeviceWorker(service, FixtureClient(service, UpstreamError("HTTP 503")))
    worker.synchronize(service.catalog.next_window())
    assert service.catalog.rows("SELECT * FROM windows")[0]["state"] == "failed"
    assert service.catalog.next_window() is None
    service.catalog.execute("UPDATE windows SET retry_at=''")
    worker.client = FixtureClient(service)
    worker.synchronize(service.catalog.next_window())
    assert service.catalog.coverage(device["id"], START, END)["complete"]
    worker.discover = lambda at: (_ for _ in ()).throw(CredentialsError("key rejected"))
    worker.step()
    assert service.catalog.get_meta("paused") is True


def test_overlap_refresh_requeues_completed_split_children(service):
    device = install_device(service)
    service.catalog.enqueue_window(device["id"], START, END, "live")
    worker = ExternalDeviceWorker(service, FixtureClient(service, ResponseTooLarge("large")))
    worker.synchronize(service.catalog.next_window())
    worker.client = FixtureClient(service)
    worker.synchronize(service.catalog.next_window())
    worker.synchronize(service.catalog.next_window())
    assert service.catalog.coverage(device["id"], START, END)["complete"]
    service.catalog.enqueue_window(device["id"], START, END, "live", refresh=True)
    assert len(service.catalog.rows("SELECT * FROM windows WHERE state='pending'")) == 2


def test_json_csv_download_preserves_data_flags_and_coverage(service):
    device = install_device(service)
    raw = record(
        device, payload={"params": {"HeartRate": 0}, "_synthetic": True, "nested": [1, None]}
    )
    raw["rawPayload"] = "=UNTRUSTED()"
    archive(service, device, [raw])
    job = service.catalog.export_job(device["id"], START, "2026-10-01T16:30:00.000Z")
    service.build_export(job)
    client = client_for(service)
    url = f"/api/v1/external-devices/exports/{job['id']}/download"
    response = client.get(url)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as package:
        assert json.loads(package.read("records.json")) == [raw]
        manifest = json.loads(package.read("manifest.json"))
        assert manifest["total"] == manifest["synthetic_records"] == 1
        assert not manifest["coverage"]["complete"]
        cells = list(csv.DictReader(io.StringIO(package.read("records.csv").decode("utf-8-sig"))))
        assert cells[0]["RespiratoryRate"] == "" and cells[0]["HeartRate"] == "0.0"
        assert cells[0]["rawPayload"] == "'=UNTRUSTED()"
    partial = client.get(url, headers={"Range": "bytes=0-9"})
    assert partial.status_code == 206 and partial.content == response.content[:10]


def test_api_pagination_timezone_filter_and_detail_scope(service):
    device = install_device(service)
    other = install_device(service, product="other")
    archive(service, device, [record(device, 1), record(device, 2, "2026-10-01T16:00:02.000Z")])
    client = client_for(service)
    prefix = f"/api/v1/external-devices/devices/{device['id']}"
    query = {"start": "2026-10-02T00:00:00+08:00", "end": "2026-10-02T00:15:00+08:00", "limit": 1}
    first = client.get(prefix + "/records", params=query).json()
    assert first["total"] == 2 and len(first["records"]) == 1
    second = client.get(
        prefix + "/records", params={**query, "cursor": first["next_cursor"]}
    ).json()
    assert second["records"][0]["id"] != first["records"][0]["id"]
    assert second["next_cursor"] is None
    assert (
        client.get(prefix + "/records", params={**query, "start": "2026-10-02"}).status_code == 422
    )
    assert client.get(prefix + "/records", params={**query, "cursor": "!!!"}).status_code == 422
    rid = first["records"][0]["id"]
    assert (
        client.get(f"/api/v1/external-devices/devices/{other['id']}/records/{rid}").status_code
        == 404
    )


def test_permissions_apply_to_every_read_mutation_and_download(service):
    denied = client_for(service, admin=False, access="admins")
    for path in ("/status", "/devices", "/exports", "/exports/unknown/download"):
        assert denied.get("/api/v1/external-devices" + path).status_code == 403
    assert denied.post("/api/v1/external-devices/sync").status_code == 403
    member = client_for(service, admin=False)
    assert member.get("/api/v1/external-devices/devices").status_code == 200
    assert member.post("/api/v1/external-devices/sync").status_code == 403
    assert (
        member.get("/api/v1/external-devices/devices", headers={"x-test-login": "no"}).status_code
        == 401
    )
    disabled = client_for(service, enabled=False)
    assert disabled.get("/api/v1/external-devices/devices").status_code == 404


def test_history_needs_provider_confirmation_and_manual_sync_is_coalesced(service):
    install_device(service)
    client = client_for(service)
    body = {"start": START, "confirmed_by_provider": False}
    assert client.post("/api/v1/external-devices/history", json=body).status_code == 422
    body["confirmed_by_provider"] = True
    assert client.post("/api/v1/external-devices/history", json=body).status_code == 202
    assert client.post("/api/v1/external-devices/sync").status_code == 503
    runtime = client.app.state.external_device_service
    runtime.control.set_meta("worker_heartbeat", now())
    runtime.control.set_meta("credentials_configured", True)
    first = client.post("/api/v1/external-devices/sync")
    assert first.status_code == 202
    second = client.post("/api/v1/external-devices/sync")
    assert second.json()["id"] == first.json()["id"]


class Response:
    def __init__(self, status=200, raw=b"{}"):
        self.status_code, self.raw = status, raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        yield self.raw


class Session:
    def __init__(self, response):
        self.response, self.kwargs = response, None

    def request(self, method, url, **kwargs):
        self.kwargs = kwargs
        assert url.startswith("https://api.synerglobal.com.au/")
        return self.response


def test_client_tls_no_redirects_body_limit_and_secret_errors(service):
    session = Session(Response(302))
    client = HistoryClient(service.settings, key="private-key", session=session)
    with pytest.raises(UpstreamError):
        client.request("mattress")
    assert session.kwargs["allow_redirects"] is False
    assert session.kwargs.get("verify", True) is True
    session.response = Response(401)
    with pytest.raises(CredentialsError) as error:
        client.request("mattress")
    assert "private-key" not in str(error.value)
    service.settings.external_devices.max_response_bytes = 1024
    session.response = Response(raw=b" " * 1025)
    with pytest.raises(ResponseTooLarge):
        client.request("mattress")
    session.response = Response(raw=b'{"invalid": NaN}')
    with pytest.raises(UpstreamError):
        client.request("mattress")


def test_aggregates_preserve_zero_extrema_null_and_synthetic_groups(service):
    device = install_device(service)
    rows = [
        record(device, 1, "2026-10-01T16:00:10.000Z", payload={"params": {"HeartRate": 0}}),
        record(device, 2, "2026-10-01T16:00:20.000Z", payload={"params": {"HeartRate": 180}}),
        record(device, 3, "2026-10-01T16:00:30.000Z", payload={"params": {"HeartRate": None}}),
        record(
            device,
            4,
            "2026-10-01T16:00:40.000Z",
            payload={"params": {"HeartRate": 500}, "_synthetic": True},
        ),
    ]
    archive(service, device, rows)
    result = service.aggregate_series(
        device["id"], "HeartRate", START, "2026-10-01T17:00:00.000Z", 100
    )
    ordinary = next(p for p in result["points"] if p["count"] and not p["synthetic"])
    synthetic = next(p for p in result["points"] if p["count"] and p["synthetic"])
    assert result["interval_ms"] == result["source_level_ms"] == 60000
    assert (
        ordinary["value"],
        ordinary["min"],
        ordinary["max"],
        ordinary["count"],
        ordinary["missing"],
    ) == (90, 0, 180, 2, 1)
    assert synthetic["value"] == 500 and synthetic["count"] == 1
    assert result["count"] == 3
    assert any(p["value"] is None and p["count"] == 0 for p in result["points"])
    # Half-open, partial buckets must not include neighbouring records from a cached minute.
    narrow = service.aggregate_series(
        device["id"], "HeartRate", "2026-10-01T16:00:15.000Z", "2026-10-01T16:00:40.000Z", 100
    )
    assert narrow["count"] == 1
    assert next(p for p in narrow["points"] if p["count"])["value"] == 180


def test_state_aggregation_ties_use_latest_and_preserve_sample_counts(service):
    device = install_device(service)
    rows = [
        record(device, 1, "2026-10-01T16:00:10.000Z", payload={"params": {"People_flag": 0}}),
        record(device, 2, "2026-10-01T16:01:10.000Z", payload={"params": {"People_flag": 1}}),
    ]
    archive(service, device, rows)
    result = service.aggregate_series(
        device["id"], "in_bed", START, "2026-10-02T16:00:00.000Z", 100
    )
    point = next(p for p in result["points"] if p["count"])
    assert result["method"] == "mode" and point["value"] == 1
    assert point["states"] == {"0.0": 1, "1.0": 1}


def test_cumulative_steps_last_and_interval_steps_sum(service):
    device = install_device(service, kind="radar-watch")
    rows = [
        record(device, i, f"2026-10-01T16:00:0{i}.000Z", "LK", {"ST": 100 + i, "CST": i})
        for i in (1, 2)
    ]
    archive(service, device, rows)
    for metric, expected, method in (("ST", 102, "last"), ("CST", 3, "sum")):
        result = service.aggregate_series(
            device["id"], metric, START, "2026-10-01T17:00:00.000Z", 100
        )
        assert result["method"] == method
        assert next(p for p in result["points"] if p["count"])["value"] == expected


def test_year_queries_use_day_rollups_and_bounded_results(service, monkeypatch):
    from imu_data_collector import external_device_aggregation as aggregation

    device = install_device(service)
    archive(service, device, [record(device)])

    def no_raw_scan(*args, **kwargs):
        raise AssertionError("Aligned year query scanned raw history")

    monkeypatch.setattr(aggregation, "raw_statistics", no_raw_scan)
    result = service.aggregate_series(
        device["id"], "HeartRate", "2026-01-01T00:00:00.000Z", "2027-01-01T00:00:00.000Z", 100
    )
    assert result["source_level_ms"] == 86400000
    assert result["count"] == 1 and len(result["points"]) <= 200
    plan = service.catalog.rows(
        "EXPLAIN QUERY PLAN SELECT stats FROM metric_rollups "
        "WHERE device_id=? AND level=? AND metric=? AND bucket>=? AND bucket<?",
        (device["id"], 86400000, "HeartRate", 0, 9999999999999),
    )
    assert any("external_metric_range" in row["detail"] for row in plan)


def test_rollups_follow_latest_record_version_and_time_movement(service, monkeypatch):
    import imu_data_collector.external_device_service as source

    device = install_device(service)
    row = record(device, payload={"params": {"HeartRate": 99}})
    archive(service, device, [row])
    first = service.catalog.rows("SELECT fetched_at FROM records")[0]["fetched_at"]
    monkeypatch.setattr(source, "now", lambda: utc(timestamp(first) + timedelta(seconds=1)))
    newer = copy.deepcopy(row)
    newer["receivedAt"] = "2026-10-01T16:02:10.000Z"
    newer["payload"] = {"params": {"HeartRate": 50}, "_synthetic": True}
    archive(service, device, [newer])
    archive(service, device, [newer])  # Overlapping pulls must not double-count.
    result = service.aggregate_series(device["id"], "HeartRate", START, END, 100)
    populated = [p for p in result["points"] if p["count"]]
    assert len(populated) == result["count"] == 1
    assert populated[0]["synthetic"] and populated[0]["value"] == 50
    assert service.metric_metadata(device["id"])["metrics"][0]["last_record"] == newer["receivedAt"]


def test_existing_database_gets_rebuildable_aggregation_migration(service):
    device = install_device(service)
    archive(service, device, [record(device)])
    baseline = service.aggregate_series(device["id"], "HeartRate", START, END, 100)
    service.catalog.execute("DROP TABLE metric_rollups")
    service.catalog.execute("DROP TABLE metric_metadata")
    service.catalog.execute("DELETE FROM meta WHERE key='aggregation_version'")
    migrated = ExternalDeviceService(service.settings, service.store)
    assert not migrated.metric_metadata(device["id"])["ready"]
    migrated.rebuild()
    assert migrated.aggregate_series(device["id"], "HeartRate", START, END, 100) == baseline
    migrated.rebuild()
    assert migrated.metric_metadata(device["id"])["metrics"][0]["count"] == 1


def test_full_category_download_freezes_devices_and_uses_one_snapshot(service, monkeypatch):
    first = install_device(service, product="same-number-product-a")
    second = install_device(service, product="same-number-product-b")
    for item in (first, second):
        archive(service, item, [record(item)])
    service.catalog.execute("UPDATE devices SET listed=0 WHERE id=?", (first["id"],))
    job = service.catalog.full_export_job(kind="mattress")
    assert service.catalog.full_export_job(kind="mattress")["id"] == job["id"]
    future = install_device(service, number="new-after-request")
    archive(service, future, [record(future)])
    original = service.original
    synced = False

    def concurrent_sync(row, cache=None):
        nonlocal synced
        if not synced:
            synced = True
            for item in (first, second):
                archive(service, item, [record(item, 2, END)], END, "2026-10-01T16:30:00.000Z")
        return original(row, cache)

    monkeypatch.setattr(service, "original", concurrent_sync)
    service.build_export(job)
    output = service.catalog.rows("SELECT * FROM exports WHERE id=?", (job["id"],))[0]
    with zipfile.ZipFile(io.BytesIO(service.store.read_bytes(output["object_key"]))) as package:
        manifest = json.loads(package.read("manifest.json"))
        assert manifest["total"] == 2 and len(manifest["devices"]) == 2
        assert {d["id"] for d in manifest["devices"]} == {first["id"], second["id"]}
        for item in (first, second):
            assert len(json.loads(package.read(f"{item['id']}/records.json"))) == 1
            child = json.loads(package.read(f"{item['id']}/manifest.json"))
            assert child["coverage"]["covered"] == [[START, END]]


def test_full_exports_and_metric_api_enforce_scope_permissions(service):
    device = install_device(service)
    archive(service, device, [record(device)])
    client = client_for(service, admin=False)
    prefix = "/api/v1/external-devices"
    metadata = client.get(f"{prefix}/devices/{device['id']}/metrics").json()
    assert metadata["ready"] and metadata["metrics"][0]["key"] == "HeartRate"
    query = {"start": START, "end": END, "metric": "HeartRate", "point_budget": 100}
    assert client.get(f"{prefix}/devices/{device['id']}/series", params=query).json()["count"] == 1
    assert (
        client.get(
            f"{prefix}/devices/{device['id']}/series", params={**query, "point_budget": 10000}
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"{prefix}/devices/{device['id']}/series", params={**query, "metric": "other"}
        ).status_code
        == 422
    )
    for body in (
        {"scope": "kind"},
        {"scope": "device", "device_id": device["id"], "start": START},
        {"scope": "kind", "kind": "mattress", "device_id": device["id"]},
    ):
        assert client.post(prefix + "/exports", json=body).status_code == 422
    job = client.post(
        prefix + "/exports", json={"scope": "device", "device_id": device["id"]}
    ).json()
    service.build_export(service.catalog.rows("SELECT * FROM exports WHERE id=?", (job["id"],))[0])
    assert client.get(f"{prefix}/exports/{job['id']}").json()["state"] == "ready"
    response = client.get(f"{prefix}/exports/{job['id']}/download")
    with zipfile.ZipFile(io.BytesIO(response.content)) as package:
        assert len(json.loads(package.read("records.json"))) == 1
    restricted = client_for(service, admin=False, access="admins")
    for path in (
        f"/devices/{device['id']}/metrics",
        f"/exports/{job['id']}",
        f"/exports/{job['id']}/download",
    ):
        assert restricted.get(prefix + path).status_code == 403
    assert (
        restricted.post(prefix + "/exports", json={"scope": "kind", "kind": "mattress"}).status_code
        == 403
    )


def test_rollup_values_match_raw_records_across_tiers_and_clipped_edges(service):
    device = install_device(service)
    rows = []
    for i in range(100):
        at = utc(timestamp(START) + timedelta(minutes=i * 49, seconds=i % 19, milliseconds=123))
        rows.append(
            record(
                device,
                i,
                at,
                payload={
                    "params": {"HeartRate": i % 13 if i % 7 else None},
                    "_synthetic": i % 3 == 0,
                },
            )
        )
    stop = utc(timestamp(START) + timedelta(days=4))
    archive(service, device, rows, START, stop)
    for start, end, budget in (
        (START, stop, 100),
        ("2026-10-01T16:49:01.124Z", "2026-10-02T12:05:12.321Z", 500),
    ):
        result = service.aggregate_series(device["id"], "HeartRate", start, end, budget)
        for point in result["points"]:
            expected = [
                r["payload"]["params"]["HeartRate"]
                for r in rows
                if point["start"]
                <= round(timestamp(r["receivedAt"]).timestamp() * 1000)
                < point["end"]
                and r["payload"]["_synthetic"] == point["synthetic"]
            ]
            values = [v for v in expected if v is not None]
            assert point["count"] == len(values)
            assert point["missing"] == len(expected) - len(values)
            if values:
                assert point["value"] == pytest.approx(sum(values) / len(values))
                assert point["min"] == min(values) and point["max"] == max(values)
            else:
                assert point["value"] is None


class RefreshClient:
    """Deterministic upstream with independent directory and mutable retained records."""

    key = "fixture"
    last_response_bytes = 0

    def __init__(self, device, records=()):
        self.directory = {device["id"]: device}
        self.records = list(records)
        self.calls = []
        self.fail_after = None
        self.split_above = None

    def devices(self, kind):
        self.last_response_bytes = 100
        return [
            {
                "deviceNo": d["device_no"],
                "historyDeviceNo": d["history_no"],
                "displayName": d["display_name"],
                "productKey": d["product_key"],
            }
            for d in self.directory.values()
            if d["kind"] == kind
        ]

    def request(self, kind, payload):
        self.calls.append(payload.copy())
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise UpstreamError("Fixture connection failure")
        if (
            self.split_above
            and (timestamp(payload["endTime"]) - timestamp(payload["startTime"])).total_seconds()
            > self.split_above
        ):
            raise ResponseTooLarge("Fixture large response")
        device = next(d for d in self.directory.values() if d["history_no"] == payload["deviceNo"])
        rows = [
            copy.deepcopy(r)
            for r in self.records
            if payload["startTime"] <= r["receivedAt"] < payload["endTime"]
        ]
        rows.sort(key=lambda r: (r["receivedAt"], r.get("messageType", ""), r["id"]))
        obj = envelope(device, rows, payload["startTime"], payload["endTime"])
        return obj, json_bytes(obj)


@pytest.fixture
def refreshing(service, monkeypatch):
    import imu_data_collector.external_device_runtime as runtime_module
    import imu_data_collector.external_device_worker as worker_module

    clock = [END]
    monkeypatch.setattr(runtime_module, "now", lambda: clock[0])
    monkeypatch.setattr(worker_module, "now", lambda: clock[0])
    device = install_device(service)
    upstream = RefreshClient(device)
    runtime = ExternalDeviceRuntime(service.settings, service.store, initial=service)
    runtime.set_history_start(START)
    worker = ExternalDeviceWorker(runtime, upstream)
    worker.initialize()
    return runtime, worker, upstream, device, clock


def finish_refresh(runtime, worker, mode="sync"):
    task = runtime.request_task(mode)
    for _ in range(50):
        worker.step()
        result = next(t for t in runtime.tasks() if t["id"] == task["id"])
        if result["state"] in ("done", "failed"):
            return result
    pytest.fail("Refresh did not terminate")


def test_incremental_empty_ranges_advance_and_boundaries_are_exact(refreshing):
    runtime, worker, upstream, device, clock = refreshing
    upstream.records = [record(device, received=END)]
    assert finish_refresh(runtime, worker)["state"] == "done"
    assert runtime.devices()[0]["total"] == 0
    assert runtime.devices()[0]["synced_until"] == END
    clock[0] = utc(timestamp(END) + timedelta(minutes=15))
    assert finish_refresh(runtime, worker)["new_records"] == 1
    assert upstream.calls[-1]["startTime"] == END
    assert runtime.devices()[0]["synced_until"] == clock[0]
    prior_calls = len(upstream.calls)
    assert finish_refresh(runtime, worker)["state"] == "done"
    assert len(upstream.calls) == prior_calls


def test_incremental_never_overwrites_and_rebuild_uses_current_upstream(refreshing):
    runtime, worker, upstream, device, clock = refreshing
    upstream.records = [record(device)]
    assert finish_refresh(runtime, worker)["state"] == "done"
    old = runtime.current
    old_export = old.catalog.full_export_job(device=device["id"])
    original = old.catalog.rows("SELECT * FROM records")[0]
    clock[0] = utc(timestamp(END) + timedelta(minutes=15))
    upstream.records = [record(device, received=END, payload={"params": {"HeartRate": 99}})]
    assert finish_refresh(runtime, worker)["new_records"] == 0
    assert runtime.catalog.rows("SELECT * FROM records")[0]["hash"] == original["hash"]
    token = runtime.bound.set(old)
    try:
        result = finish_refresh(runtime, worker, "rebuild")
        assert result["state"] == "done"
        assert (
            runtime.current is old
        )  # A request/export that began before publication stays pinned.
    finally:
        runtime.bound.reset(token)
    assert runtime.generation != "legacy"
    assert (
        json.loads(runtime.catalog.rows("SELECT metric_json FROM records")[0]["metric_json"])[
            "HeartRate"
        ]
        == 99
    )
    assert old.catalog.rows("SELECT * FROM records")[0]["hash"] == original["hash"]
    source, job = runtime.export_job(old_export["id"])
    source.build_export(job)
    client = client_for(runtime.current)
    assert client.get(f"/api/v1/external-devices/exports/{job['id']}").json()["state"] == "ready"
    response = client.get(f"/api/v1/external-devices/exports/{job['id']}/download")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as package:
        assert json.loads(package.read("records.json"))[0]["payload"]["params"]["HeartRate"] == 69


def test_failed_rebuild_keeps_old_generation_and_retry_is_safe(refreshing):
    runtime, worker, upstream, device, clock = refreshing
    upstream.records = [record(device)]
    assert finish_refresh(runtime, worker)["state"] == "done"
    upstream.fail_after = len(upstream.calls)
    failed = finish_refresh(runtime, worker, "rebuild")
    assert failed["state"] == "failed"
    assert runtime.generation == "legacy" and runtime.devices()[0]["total"] == 1
    upstream.fail_after = None
    upstream.records = []  # Removed upstream data disappears only after a successful rebuild.
    assert finish_refresh(runtime, worker, "rebuild")["state"] == "done"
    assert runtime.devices()[0]["total"] == 0
    assert runtime.service("legacy").devices()[0]["total"] == 1
    runtime.recover()
    assert runtime.devices()[0]["total"] == 0


def test_failed_incremental_never_skips_a_gap(refreshing):
    runtime, worker, upstream, device, clock = refreshing
    worker.config.window_s = 900
    clock[0] = utc(timestamp(END) + timedelta(minutes=30))
    upstream.fail_after = 1
    result = finish_refresh(runtime, worker)
    assert result["state"] == "failed"
    assert runtime.devices()[0]["synced_until"] == END
    upstream.fail_after = None
    before = len(upstream.calls)
    assert finish_refresh(runtime, worker)["state"] == "done"
    assert upstream.calls[before]["startTime"] == END
    assert runtime.devices()[0]["synced_until"] == clock[0]


def test_rebuild_resume_and_generation_recovery_do_not_mix_archives(refreshing):
    runtime, worker, upstream, device, clock = refreshing
    upstream.records = [record(device)]
    assert finish_refresh(runtime, worker)["state"] == "done"
    upstream.records = []
    worker.config.window_s = 900
    clock[0] = utc(timestamp(END) + timedelta(minutes=30))
    task = runtime.request_task("rebuild")
    worker.step()
    assert runtime.active_task()["windows_done"] == 1
    resumed = ExternalDeviceRuntime(runtime.settings, runtime.store)
    next_worker = ExternalDeviceWorker(resumed, upstream)
    next_worker.initialize()
    assert finish_refresh(resumed, next_worker, "rebuild")["id"] == task["id"]
    generation = resumed.generation
    path = resumed.current.catalog.path
    path.unlink()
    restored = ExternalDeviceRuntime(runtime.settings, runtime.store)
    restored.recover()
    assert restored.generation == generation
    assert restored.devices()[0]["total"] == 0
    assert restored.devices()[0]["synced_until"] == clock[0]
    assert restored.service("legacy").devices()[0]["total"] == 1


def test_adaptive_split_preserves_task_progress(refreshing):
    runtime, worker, upstream, device, clock = refreshing
    upstream.split_above = 450
    result = finish_refresh(runtime, worker)
    assert result["state"] == "done"
    assert result["windows_done"] == result["windows_total"] == 2
    assert result["request_count"] == 5  # Two directories, failed wide query, two children.
    assert runtime.devices()[0]["synced_until"] == END


def test_new_device_history_does_not_starve_existing_device_updates(refreshing):
    runtime, worker, upstream, device, clock = refreshing
    assert finish_refresh(runtime, worker)["state"] == "done"
    new = {
        **device,
        "id": device_id("mattress", "mattress:product-a/456"),
        "device_no": "456",
        "history_no": "mattress:product-a/456",
    }
    upstream.directory[new["id"]] = new
    clock[0] = utc(timestamp(END) + timedelta(minutes=15))
    before = len(upstream.calls)
    runtime.request_task("sync")
    worker.step()
    assert upstream.calls[before]["deviceNo"] == device["history_no"]
    assert next(d for d in runtime.devices() if d["id"] == new["id"])["initial_sync"]
    assert finish_refresh(runtime, worker)["state"] == "done"
    assert not next(d for d in runtime.devices() if d["id"] == new["id"])["initial_sync"]
    del upstream.directory[new["id"]]
    assert finish_refresh(runtime, worker)["state"] == "done"
    assert not next(d for d in runtime.devices() if d["id"] == new["id"])["listed"]


def test_rebuild_confirmation_settings_and_permissions(refreshing):
    runtime, worker, upstream, device, clock = refreshing
    client = client_for(runtime.current)
    prefix = "/api/v1/external-devices"
    runtime.control.set_meta("worker_heartbeat", now())
    assert (
        client.post(
            prefix + "/rebuild", json={"confirmed": False, "history_start": START}
        ).status_code
        == 422
    )
    assert (
        client.post(
            prefix + "/rebuild", json={"confirmed": True, "history_start": "2026-09-01T00:00:00Z"}
        ).status_code
        == 409
    )
    response = client.post(prefix + "/rebuild", json={"confirmed": True, "history_start": START})
    assert response.status_code == 202
    assert (
        client.post(prefix + "/rebuild", json={"confirmed": True, "history_start": START}).json()[
            "id"
        ]
        == response.json()["id"]
    )
    assert client.post(prefix + "/sync").status_code == 409
    assert client.put(prefix + "/settings", json={"history_start": START}).status_code == 409
    assert finish_refresh(runtime, worker, "rebuild")["state"] == "done"
    updated = client.put(prefix + "/settings", json={"history_start": "2026-09-01T00:00:00Z"})
    assert updated.status_code == 200 and updated.json()["history_start_source"] == "configured"
    member = client_for(runtime.current, admin=False, access="members")
    assert (
        member.post(
            prefix + "/rebuild", json={"confirmed": True, "history_start": START}
        ).status_code
        == 403
    )
    assert member.put(prefix + "/settings", json={"history_start": START}).status_code == 403
    assert client.put(prefix + "/settings", json={"history_start": "invalid"}).status_code == 422


def test_append_archive_replay_retains_first_version_even_with_identical_timestamps(
    service, monkeypatch
):
    import imu_data_collector.external_device_service as service_module

    monkeypatch.setattr(service_module, "now", lambda: END)
    device = install_device(service)
    for value in (60, 90):
        obj = envelope(device, [record(device, payload={"params": {"HeartRate": value}})])
        service.archive(device, START, END, obj, json_bytes(obj), append_only=True)
    service.persist_control()
    service.catalog.path.unlink()
    restored = ExternalDeviceService(service.settings, service.store)
    restored.rebuild()
    assert (
        json.loads(restored.catalog.rows("SELECT metric_json FROM records")[0]["metric_json"])[
            "HeartRate"
        ]
        == 60
    )


def test_publication_commit_is_reconciled_before_accepting_another_job(refreshing, monkeypatch):
    runtime, worker, upstream, device, clock = refreshing
    assert finish_refresh(runtime, worker)["state"] == "done"
    activate = runtime.activate
    attempts = []

    def interrupted(task):
        attempts.append(task["id"])
        if len(attempts) == 1:
            raise OSError("Local pointer temporarily unavailable")
        activate(task)

    monkeypatch.setattr(runtime, "activate", interrupted)
    task = runtime.request_task("rebuild")
    worker.step()
    worker.step()
    assert runtime.generation == "legacy"
    assert runtime.active_task()["phase"] == "publishing"
    saved, _ = runtime.store.read_json(f"{PREFIX}/runtime.json")
    assert saved["active_generation"] == task["generation"]
    with pytest.raises(ValueError):
        runtime.request_task("sync")
    worker.last_control = -1e9  # Periodic persistence must not overwrite the committed pointer.
    worker.step()
    assert runtime.generation == task["generation"]
    assert runtime.active_task() is None
    saved, _ = runtime.store.read_json(f"{PREFIX}/runtime.json")
    assert saved["active_generation"] == task["generation"]


def test_process_death_between_object_and_local_pointer_recovers(refreshing, monkeypatch):
    runtime, worker, upstream, device, clock = refreshing
    task = runtime.request_task("rebuild")
    worker.step()
    monkeypatch.setattr(
        runtime, "activate", lambda task: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        worker.step()
    restored = ExternalDeviceRuntime(runtime.settings, runtime.store)
    restored.recover()
    assert restored.generation == task["generation"]
    assert restored.active_task() is None
    assert restored.tasks()[0]["state"] == "done"


def test_http_request_keeps_its_generation_during_publication(refreshing, monkeypatch):
    runtime, worker, upstream, device, clock = refreshing
    upstream.records = [record(device)]
    assert finish_refresh(runtime, worker)["state"] == "done"
    rid = runtime.catalog.rows("SELECT id FROM records")[0]["id"]
    client = client_for(runtime.current)
    api = client.app.state.external_device_service
    old = api.current
    generation = "1" * 32
    api.control.set_meta("generations", ["legacy", generation])
    rows = old.catalog.rows

    def query_and_switch(sql, params=()):
        result = rows(sql, params)
        if sql.startswith("SELECT * FROM records WHERE device_id=? AND id=?"):
            api.publish({"id": "fixture-publication", "generation": generation})
        return result

    monkeypatch.setattr(old.catalog, "rows", query_and_switch)
    response = client.get(f"/api/v1/external-devices/devices/{device['id']}/records/{rid}")
    assert response.status_code == 200
    assert json.loads(response.json()["record_json"])["id"] == 1
    assert client.get("/api/v1/external-devices/devices").json()["devices"] == []
