"""Projection migration and member-facing insight contracts."""

import copy
import csv
import io
import json
import zipfile
from collections import OrderedDict

import pytest
from test_external_devices import (  # noqa: F401
    END,
    START,
    archive,
    client_for,
    install_device,
    record,
)
from test_external_devices import service as service_fixture

from imu_data_collector import external_device_aggregation as aggregation
from imu_data_collector import external_device_insights as insights
from imu_data_collector.external_device_domain import metrics
from imu_data_collector.external_device_service import LEGACY_METRIC_KEYS

service = service_fixture


def test_additive_metrics_use_message_qualified_fields_and_units():
    payload = {"CO": 2, "DTC": "1234", "STTIME": 45, "KCAL": 234, "HRV": 31}
    result = metrics({"operation": "LK", "payload": payload}, "radar-watch")
    assert result["KCAL"] == 234
    assert result["energy_kcal"] == 23.4
    assert result["distance_km"] == 12.34
    assert result["STTIME"] == 45
    assert result["CO"] == 2
    assert metrics({"operation": "UHR", "payload": payload}, "radar-watch") == {"HR": None}
    for bad in (None, True, "oops", "NaN"):
        invalid = metrics({"operation": "LK", "payload": {"DTC": bad}}, "radar-watch")
        assert invalid["distance_km"] is None
    assert (
        metrics({"messageType": "REALTIME", "payload": {"params": {"Amp_value": 24}}}, "mattress")[
            "Amp_value"
        ]
        == 24
    )
    assert "Amp_value" not in metrics(
        {"messageType": "SLEEP_REPORT", "payload": {"Amp_value": 24}}, "mattress"
    )
    assert aggregation.method("KCAL") == "mean"
    assert all(
        aggregation.method(key) == "last" for key in ("energy_kcal", "distance_km", "STTIME")
    )
    assert aggregation.method("CO") == "mode"
    assert aggregation.method("Amp_value") == "mean"


def test_added_series_last_values_and_unknown_state_preserved(service):
    device = install_device(service, kind="radar-watch")
    archive(
        service,
        device,
        [
            record(device, 1, message="LK", payload={"KCAL": 100, "DTC": 101, "CO": 9}),
            record(
                device,
                2,
                received="2026-10-01T16:00:01.000Z",
                message="LK",
                payload={"KCAL": 200, "DTC": 102, "CO": 9},
            ),
        ],
    )
    energy = service.aggregate_series(device["id"], "energy_kcal", START, END, 100)
    assert [p["value"] for p in energy["points"] if p["count"]] == [20]
    assert service.metric_metadata(device["id"])["preparation"]["ready"]
    states = service.aggregate_series(device["id"], "CO", START, END, 100)
    assert [p["value"] for p in states["points"] if p["count"]] == [9]


def test_reports_keep_missing_and_arrays_and_flag_incomplete(service):
    device = install_device(service)
    payload = {
        "Onbed_valid": 1,
        "sleep_valid": 1,
        "sleep_time": "21:33",
        "wake_time": "06:01",
        "sleep_duration": 123,
        "ave_hr": None,
        "hrs": [60, None, 70],
        "stages": [{"code": 999}],
    }
    archive(
        service,
        device,
        [
            record(device, 1, message="SLEEP_REPORT", payload=payload),
            record(device, 2, message="SLEEP_REPORT", payload={"Onbed_valid": 0, "sleep_valid": 0}),
            record(device, 3, message="SLEEP_REPORT", payload={"Onbed_valid": 1, "sleep_valid": 1}),
        ],
    )
    page = insights.entries(service.catalog, device["id"], "reports", START, END, 1, valid=True)
    assert page["total"] == 1 and page["incomplete"] == 2
    rid = page["items"][0]["id"]
    assert "fields" not in page["items"][0]["summary"]
    with client_for(service, admin=False) as client:
        detail = client.get(f"/api/v1/external-devices/devices/{device['id']}/reports/{rid}").json()
    fields = {item["key"]: item for item in detail["summary"]["fields"]}
    assert fields["ave_hr"]["value"] is None
    assert fields["hrs"]["value"] == [60, None, 70]
    assert fields["stages"]["value"] == [{"code": 999}]
    assert "未确认" in fields["sleep_duration"]["unit"]
    assert "deep_duration" not in fields


def test_event_buckets_synthetic_boundary_and_pagination(service):
    device = install_device(service, kind="radar-watch")
    archive(
        service,
        device,
        [
            record(device, i, message="UALM", payload={"TY": "DROP", "_synthetic": i != 3})
            for i in range(1, 4)
        ],
    )
    result = insights.events(service.catalog, device["id"], START, END, 100)
    assert result["total"] == 3
    assert sorted((p["synthetic"], p["count"]) for p in result["points"]) == [(False, 1), (True, 2)]
    assert (
        insights.events(service.catalog, device["id"], "2026-10-01T15:00:00.000Z", START, 100)[
            "total"
        ]
        == 0
    )
    with client_for(service, admin=False) as client:
        path = f"/api/v1/external-devices/devices/{device['id']}/events/records"
        page = client.get(path, params={"start": START, "end": END, "limit": 2}).json()
        following = client.get(
            path, params={"start": START, "end": END, "limit": 2, "cursor": page["next_cursor"]}
        ).json()
        assert len(page["items"]) == 2 and len(following["items"]) == 1
        assert {r["id"] for r in page["items"]}.isdisjoint(r["id"] for r in following["items"])
        assert all(r["summary"]["code"] == "DROP" for r in page["items"])
        assert (
            client.get(
                path, params={"start": START, "end": END, "cursor": "not-base64"}
            ).status_code
            == 422
        )


def make_legacy(service):
    """Emulate the deployed DB before additive projections were introduced."""
    with service.catalog.connect() as db:
        for table in (
            "insight_progress",
            "record_insights",
            "insight_field_counts",
            "insight_signatures",
            "insight_reports",
            "insight_events",
            "insight_dirty_minutes",
        ):
            db.execute(f"DELETE FROM {table}")
        for row in db.execute("SELECT id,metric_json FROM records").fetchall():
            old = {
                key: value
                for key, value in json.loads(row["metric_json"]).items()
                if key not in insights.NEW_METRICS
            }
            db.execute("UPDATE records SET metric_json=? WHERE id=?", (json.dumps(old), row["id"]))
        db.execute("DELETE FROM meta WHERE key='aggregation_version'")
    aggregation.rebuild(service.catalog)


def source_rows(service):
    return service.catalog.rows(
        "SELECT id,source_id,received_at,message_type,synthetic,hash,archive_id,position,"
        "fetched_at FROM records ORDER BY id"
    )


def test_backfill_resumes_current_version_only_without_upstream_or_raw_mutation(
    service, monkeypatch
):
    times = iter(("2026-10-02T00:00:00.000Z", "2026-10-02T00:00:01.000Z"))
    monkeypatch.setattr("imu_data_collector.external_device_service.now", lambda: next(times))
    device = install_device(service, kind="radar-watch")
    old = record(device, message="LK", payload={"KCAL": 100, "DTC": 100})
    archive(service, device, [old])
    updated = copy.deepcopy(old)
    updated["payload"].update(KCAL=250, DTC=450)
    archive(
        service,
        device,
        [updated, record(device, 2, message="UALM", payload={"TY": "DROP", "_synthetic": True})],
    )
    make_legacy(service)
    before = source_rows(service)
    coverage = service.catalog.rows("SELECT * FROM coverage")
    from imu_data_collector.external_device_service import HistoryClient

    monkeypatch.setattr(
        HistoryClient, "request", lambda *_a, **_k: pytest.fail("Migration must not call upstream")
    )
    assert insights.backfill_step(service, limit=1)
    assert not service.metric_metadata(device["id"])["preparation"]["ready"]
    assert "energy_kcal" not in {m["key"] for m in service.metric_metadata(device["id"])["metrics"]}
    assert service.aggregate_series(device["id"], "KCAL", START, END, 100)["count"] == 1
    with client_for(service, admin=False) as client:
        prefix = f"/api/v1/external-devices/devices/{device['id']}"
        assert (
            client.get(
                prefix + "/series", params={"start": START, "end": END, "metric": "energy_kcal"}
            ).status_code
            == 422
        )
        partial = client.get(prefix + "/records", params={"start": START, "end": END}).json()
        assert all("energy_kcal" not in row["metrics"] for row in partial["records"])
    # A new process/cache resumes from its database checkpoint.
    while insights.backfill_step(service, limit=1, cache=OrderedDict()):
        pass
    assert before == source_rows(service)
    assert coverage == service.catalog.rows("SELECT * FROM coverage")
    assert service.metric_metadata(device["id"])["preparation"]["ready"]
    assert service.original(before[0]) in [
        updated,
        record(device, 2, message="UALM", payload={"TY": "DROP", "_synthetic": True}),
    ]
    result = service.aggregate_series(device["id"], "energy_kcal", START, END, 100)
    assert [p["value"] for p in result["points"] if p["count"]] == [25]
    fields = insights.fields(service.catalog, device["id"])["fields"]
    assert next(f for f in fields if f["path"] == "payload.KCAL")["count"] == 1
    assert insights.events(service.catalog, device["id"], START, END, 100)["total"] == 1
    counts = service.catalog.rows("SELECT * FROM insight_field_counts ORDER BY signature")
    assert not insights.backfill_step(service)
    assert counts == service.catalog.rows("SELECT * FROM insight_field_counts ORDER BY signature")


def test_corrupt_archive_does_not_advance_checkpoint_or_publish(service, monkeypatch):
    device = install_device(service)
    archive(service, device, [record(device)])
    make_legacy(service)
    monkeypatch.setattr(
        service, "original", lambda *_: (_ for _ in ()).throw(ValueError("checksum"))
    )
    with pytest.raises(ValueError, match="checksum"):
        insights.backfill_step(service)
    state = insights.preparation(service.catalog, device["id"])
    assert state["processed"] == 0 and not state["ready"]
    assert not service.catalog.rows("SELECT * FROM record_insights")


def test_replaced_record_removes_old_field_counts_and_old_event(service, monkeypatch):
    times = iter(("2026-10-02T00:00:00.000Z", "2026-10-02T00:00:01.000Z"))
    monkeypatch.setattr("imu_data_collector.external_device_service.now", lambda: next(times))
    device = install_device(service, kind="radar-watch")
    archive(
        service,
        device,
        [record(device, message="UALM", payload={"TY": "DROP", "_synthetic": True})],
    )
    archive(service, device, [record(device, message="LK", payload={"CO": 1, "KCAL": 123})])
    fields = insights.fields(service.catalog, device["id"])["fields"]
    assert {f["message"] for f in fields} == {"LK"}
    assert insights.events(service.catalog, device["id"], START, END, 100)["total"] == 0
    assert all(f["count"] == 1 for f in fields)


def test_csv_preserves_old_columns_and_appends_normalized_metrics(service):
    device = install_device(service, kind="radar-watch")
    archive(service, device, [record(device, message="LK", payload={"KCAL": 123, "DTC": 456})])
    job = service.catalog.full_export_job(device=device["id"])
    service.build_export(job)
    job = service.catalog.rows("SELECT * FROM exports WHERE id=?", (job["id"],))[0]
    with zipfile.ZipFile(io.BytesIO(service.store.read_bytes(job["object_key"]))) as zipped:
        path = next(name for name in zipped.namelist() if name.endswith("records.csv"))
        reader = csv.DictReader(io.StringIO(zipped.read(path).decode("utf-8-sig")))
        assert reader.fieldnames[:27] == [
            "record_id",
            "source_id",
            "received_at",
            "message_type",
            "synthetic",
            *LEGACY_METRIC_KEYS,
            "payload_json",
            "rawPayload",
        ]
        row = next(reader)
        assert (
            row["KCAL"] == "123.0" and row["energy_kcal"] == "12.3" and row["distance_km"] == "4.56"
        )


def test_all_insight_endpoints_members_and_device_scope(service):
    device = install_device(service)
    archive(service, device, [record(device, message="SLEEP_REPORT", payload={"Onbed_valid": 0})])
    rid = service.catalog.rows("SELECT id FROM insight_reports")[0]["id"]
    with client_for(service, admin=False) as client:
        prefix = f"/api/v1/external-devices/devices/{device['id']}"
        for suffix in (
            "/fields",
            "/reports",
            f"/reports/{rid}",
            f"/events?start={START}&end={END}",
            f"/events/records?start={START}&end={END}",
        ):
            assert client.get(prefix + suffix).status_code == 200
            assert client.get(prefix + suffix, headers={"x-test-login": "no"}).status_code == 401
        other = install_device(service, number="other")
        assert (
            client.get(f"/api/v1/external-devices/devices/{other['id']}/reports/{rid}").status_code
            == 404
        )
        assert client.post("/api/v1/external-devices/sync").status_code == 403


def test_failed_batch_rolls_back_fields_metrics_and_checkpoint(service, monkeypatch):
    device = install_device(service)
    archive(
        service, device, [record(device, i, payload={"params": {"Amp_value": i}}) for i in (1, 2)]
    )
    make_legacy(service)
    original = insights.index_record
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        result = original(*args)
        if calls == 2:
            raise RuntimeError("simulated interruption")
        return result

    monkeypatch.setattr(insights, "index_record", fail_second)
    with pytest.raises(RuntimeError, match="interruption"):
        insights.backfill_step(service)
    assert not service.catalog.rows("SELECT * FROM record_insights")
    assert not service.catalog.rows("SELECT * FROM insight_field_counts")
    assert insights.preparation(service.catalog, device["id"])["processed"] == 0
    assert all(
        "Amp_value" not in row["metric_json"]
        for row in service.catalog.rows("SELECT metric_json FROM records")
    )
    monkeypatch.setattr(insights, "index_record", original)
    while insights.backfill_step(service):
        pass
    assert service.aggregate_series(device["id"], "Amp_value", START, END, 100)["count"] == 2


def test_ingestion_during_migration_is_not_overwritten(service, monkeypatch):
    times = iter(("2026-10-02T00:00:00.000Z", "2026-10-02T00:00:01.000Z"))
    monkeypatch.setattr("imu_data_collector.external_device_service.now", lambda: next(times))
    device = install_device(service, kind="radar-watch")
    archive(
        service, device, [record(device, i, message="LK", payload={"KCAL": i * 10}) for i in (1, 2)]
    )
    make_legacy(service)
    assert insights.backfill_step(service, limit=1)
    archive(
        service,
        device,
        [
            record(device, 1, message="LK", payload={"KCAL": 330, "CO": 2}),
            record(device, 3, message="UALM", payload={"TY": "DROP"}),
        ],
    )
    while insights.backfill_step(service, limit=1):
        pass
    rows = service.catalog.rows("SELECT metric_json FROM records WHERE source_id='1'")
    assert json.loads(rows[0]["metric_json"])["energy_kcal"] == 33
    fields = insights.fields(service.catalog, device["id"])["fields"]
    assert next(f for f in fields if f["path"] == "payload.KCAL")["count"] == 2
    assert insights.events(service.catalog, device["id"], START, END, 100)["total"] == 1


def test_mattress_warning_and_unknown_field_inventory(service):
    device = install_device(service)
    archive(
        service,
        device,
        [
            record(
                device,
                1,
                message="REALTIME",
                payload={
                    "params": {"Amp_value": None, "HRV": 32, "C": 1, "RDS_ID": 45, "Pressure_On": 7}
                },
            ),
            record(
                device, 2, message="WARNING", payload={"params": {"code": 99, "Amp_value": 500}}
            ),
        ],
    )
    fields = insights.fields(service.catalog, device["id"])["fields"]
    for key in ("HRV", "C", "RDS_ID", "Pressure_On"):
        item = next(f for f in fields if f["path"] == f"payload.params.{key}")
        assert item["unit"] == "未确认" and item["statuses"] == {"raw": 1}
    assert not any(
        m["key"] == "Amp_value" for m in service.metric_metadata(device["id"])["metrics"]
    )
    events = insights.entries(service.catalog, device["id"], "events", START, END, 10)
    assert events["total"] == 1
    assert events["items"][0]["summary"]["code"] == "99"
