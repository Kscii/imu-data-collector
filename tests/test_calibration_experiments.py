import asyncio
import copy
import itertools
import time

import h5py
import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from imu_data_collector.calibration_experiments import (
    DIRECTIONS,
    ExperimentCreate,
    ExperimentStore,
    analyze_experiment,
)
from imu_data_collector.capture_api import create_capture_app
from imu_data_collector.config import Settings

ORDER = [2, 0, 1]
SIGNS = [-1, 1, -1]
ACCEL_BIAS = np.array([101.0, -57.0, 89.0])
GYRO_BIAS = np.array([12.0, -8.0, 3.0])


def evidence(tmp_path):
    store = ExperimentStore(tmp_path)
    experiment = store.create(
        ExperimentCreate(
            sensor_sn="IMU-0002-R01",
            operator_id="xfan0282",
            directions={key: f"Housing {key}" for key in DIRECTIONS},
        ),
        {"firmware": {"version": "unchanged"}},
    )
    arrays = {}
    for kind, role in itertools.product(("accel", "gyro"), ("fit", "validation")):
        repetitions = {
            ("accel", "fit"): 3,
            ("accel", "validation"): 1,
            ("gyro", "fit"): 5,
            ("gyro", "validation"): 2,
        }[kind, role]
        for _repeat in range(repetitions):
            for axis, sign in itertools.product("XYZ", (1, -1)):
                index = len(experiment["trials"])
                trial_id = f"trial-{index}"
                i = "XYZ".index(axis)
                if kind == "accel":
                    clock = np.arange(0, 10001, 20)
                    phases = {"measure": {"start_ns": 0, "end_ns": 10_001_000_000}}
                    raw = np.tile(np.r_[ACCEL_BIAS, GYRO_BIAS], (len(clock), 1))
                    raw[:, ORDER[i]] += 12800 * sign * SIGNS[i]
                    angle = None
                else:
                    # Unequal intervals with an exactly known constant rate over 4 s.
                    motion = np.r_[0, np.cumsum(np.tile([15, 25], 100))]
                    clock = np.r_[0, 2500, 4999, 5000 + motion, 9001, 11500, 14000]
                    raw = np.tile(np.r_[ACCEL_BIAS, GYRO_BIAS], (len(clock), 1))
                    angle = sign * (360 if role == "fit" else 720)
                    raw[3:-3, 3 + ORDER[i]] += 128 * angle / 4 * SIGNS[i]
                    phases = {
                        "before": {"start_ns": 0, "end_ns": 5_000_000_000},
                        "measure": {"start_ns": 5_000_000_000, "end_ns": 9_000_000_001},
                        "after": {"start_ns": 9_001_000_000, "end_ns": 14_001_000_000},
                    }
                arrays[trial_id] = {
                    "raw": raw,
                    "time_ns": clock * 1_000_000,
                    "device_ms": clock,
                    "epoch": np.zeros(len(clock)),
                }
                experiment["trials"].append(
                    {
                        "trial_id": trial_id,
                        "recording_id": trial_id,
                        "kind": kind,
                        "role": role,
                        "axis": axis,
                        "sign": sign,
                        "reference_angle_deg": angle,
                        "status": "complete",
                        "excluded": False,
                        "phases": phases,
                        "notes": "",
                    }
                )
    return experiment, arrays


def test_known_coefficients_axis_mapping_and_all_repetitions(tmp_path):
    experiment, arrays = evidence(tmp_path)
    report = analyze_experiment(experiment, arrays)
    candidate = report["candidate"]
    assert candidate["raw_axis_order"] == ORDER
    assert candidate["axis_signs"] == SIGNS
    assert candidate["accel_counts_per_g"] == pytest.approx(12800)
    assert candidate["gyro_counts_per_dps"] == pytest.approx(128)
    assert candidate["accel_bias_counts"] == pytest.approx(ACCEL_BIAS)
    assert candidate["gyro_bias_counts"] == pytest.approx(GYRO_BIAS)
    assert len(report["trials"]) == 66
    assert report["warnings"] == []
    for trial in report["trials"]:
        assert trial["result"].get("vector_error_g", 0) == pytest.approx(0)
        assert trial["result"].get("angle_error_deg", 0) == pytest.approx(0, abs=1e-9)


def test_validation_cannot_change_fit_or_rezero_its_own_bias(tmp_path):
    experiment, arrays = evidence(tmp_path)
    before = analyze_experiment(experiment, arrays)
    validation = [row for row in experiment["trials"] if row["role"] == "validation"]
    for trial in validation:
        raw = arrays[trial["recording_id"]]["raw"]
        raw[:, :3] += 500
        raw[:, 3:] += 128
    after = analyze_experiment(experiment, arrays)
    assert before["candidate"] == after["candidate"]
    assert any(abs(row["result"].get("angle_error_deg", 0)) > 3.9 for row in after["trials"])
    assert any(row["result"].get("vector_error_g", 0) > 0.05 for row in after["trials"])


def test_extra_repetitions_exclusions_and_original_ids_are_retained(tmp_path):
    experiment, arrays = evidence(tmp_path)
    extra = copy.deepcopy(experiment["trials"][0])
    extra["trial_id"] = "extra-independent-trial"
    experiment["trials"].append(extra)
    report = analyze_experiment(experiment, arrays)
    assert len(report["trials"]) == 67
    assert report["completed_counts"]["accel_fit_+X"] == 4
    extra.update(excluded=True, exclusion_reason="Fixture slipped")
    report = analyze_experiment(experiment, arrays)
    assert len(report["trials"]) == 67
    assert report["completed_counts"]["accel_fit_+X"] == 3
    assert report["trials"][-1]["exclusion_reason"] == "Fixture slipped"


@pytest.mark.parametrize("fault", ["reset", "epoch", "missing_clock", "empty"])
def test_invalid_trials_keep_evidence_without_fabricated_integrals(tmp_path, fault):
    experiment, arrays = evidence(tmp_path)
    trial = next(row for row in experiment["trials"] if row["kind"] == "gyro")
    source = arrays[trial["recording_id"]]
    if fault == "reset":
        source["device_ms"][25:] -= 1000
    elif fault == "epoch":
        source["epoch"][25:] = 1
    elif fault == "missing_clock":
        source["device_ms"] = None
    else:
        source["time_ns"] += 100_000_000_000
    report = analyze_experiment(experiment, arrays)
    row = next(row for row in report["trials"] if row["trial_id"] == trial["trial_id"])
    assert row["warnings"]
    assert "angle_deg" not in row["result"]
    assert report["candidate"]["gyro_counts_per_dps"] == pytest.approx(128)


def test_report_snapshots_are_immutable_and_omit_local_paths(tmp_path):
    experiment, arrays = evidence(tmp_path)
    experiment["trials"] = experiment["trials"][:1]
    source = arrays["trial-0"]
    path = tmp_path / "_diagnostics" / "trial-0.h5"
    path.parent.mkdir()
    with h5py.File(path, "w") as handle:
        group = handle.create_group("imu/samples")
        group["raw_counts"] = source["raw"]
        group["recording_time_ns"] = source["time_ns"]
        group["device_time_ms"] = source["device_ms"]
    experiment["sources"] = [{"recording_id": "trial-0", "local_path": "_diagnostics/trial-0.h5"}]
    store = ExperimentStore(tmp_path)
    store.save(experiment)
    report, first = store.snapshot(experiment["experiment_id"])
    old_bytes = first.read_bytes()
    assert "local_path" not in report["sources"][0]
    assert store.snapshot(experiment["experiment_id"])[1] == first
    changed = store.read(experiment["experiment_id"])
    changed["trials"][0].update(excluded=True, exclusion_reason="Test")
    store.save(changed)
    _, second = store.snapshot(experiment["experiment_id"])
    assert second != first
    assert first.read_bytes() == old_bytes
    assert len(store.read(experiment["experiment_id"])["reports"]) == 2


async def test_api_can_save_incomplete_report_and_copy_unverified_workspace(tmp_path):
    app = create_capture_app(
        Settings(data_root=tmp_path / "data", catalog_path=tmp_path / "catalog.sqlite3")
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    request = {
        "sensor_sn": "IMU-0002-R01",
        "operator_id": "xfan0282",
        "directions": {key: key for key in DIRECTIONS},
    }
    response = await client.post("/api/v1/calibration-experiments", json=request)
    assert response.status_code == 201, response.text
    experiment_id = response.json()["experiment_id"]
    root = f"/api/v1/calibration-experiments/{experiment_id}"
    response = await client.post(f"{root}/report")
    assert response.status_code == 200, response.text
    assert response.json()["report"]["candidate"]["accel_counts_per_g"] is None
    assert response.json()["report"]["warnings"]
    assert (await client.get(f"{root}/candidate.yaml")).status_code == 200
    response = await client.post(f"{root}/copy-to-workspace")
    assert response.status_code == 200, response.text
    assert response.json()["production_authority"] is False
    workspace = app.state.coordinator.settings.configuration_manager.workspace()
    device = next(item for item in workspace.content.devices if item.sensor_sn == "IMU-0002-R01")
    assert device.allowed_data_tiers == ["test"]
    assert device.si_profile.verified is False
    assert device.si_profile.coordinate_system == request["directions"]

    await client.aclose()


def test_experiment_ids_cannot_escape_store(tmp_path):
    store = ExperimentStore(tmp_path)
    with pytest.raises(ValueError):
        store.read("../../other")
    with pytest.raises(ValueError):
        store.source_path({"local_path": "../../private.h5"})


async def test_server_timing_cancellation_and_continued_capture(tmp_path, monkeypatch):
    from test_preflight import _FakeBle

    from imu_data_collector.calibration_experiments import TrialCreate

    app = create_capture_app(
        Settings(
            data_root=tmp_path / "data",
            catalog_path=tmp_path / "catalog.sqlite3",
            minimum_free_gib=0,
        )
    )
    controller = app.state.calibration
    monkeypatch.setattr(
        "imu_data_collector.coordinator.CW12EUBleSource", lambda _settings: _FakeBle()
    )
    experiment = controller.store.create(
        ExperimentCreate(
            sensor_sn="IMU-0002-R01",
            operator_id="xfan0282",
            directions={key: key for key in DIRECTIONS},
        ),
        {},
    )
    experiment_id = experiment["experiment_id"]
    durations = []

    async def wait(seconds):
        durations.append(seconds)
        # Keep a positive interval even with Windows' coarse monotonic clock.
        await asyncio.sleep(0.02)

    monkeypatch.setattr(controller, "_wait_seconds", wait)
    await controller.start(experiment_id)
    spec = TrialCreate(kind="accel", role="fit", axis="X", sign=1)
    await controller.start_trial(experiment_id, spec)
    await controller.task
    assert durations == [5, 10], controller.detail(experiment_id)["trials"]
    first = controller.detail(experiment_id)["trials"][0]
    assert first["status"] == "complete"
    assert set(first["phases"]) == {"settle", "measure"}
    await controller.start_trial(
        experiment_id, TrialCreate(kind="gyro", role="validation", axis="Y", sign=-1)
    )
    while controller.phase != "measure":
        await asyncio.sleep(0)
    assert not controller.task.done()
    await asyncio.sleep(0.02)
    controller.rotation_done.set()
    await controller.task
    assert durations == [5, 10, 5, 5]
    assert controller.detail(experiment_id)["trials"][1]["reference_angle_deg"] == -720

    # Cancellation immediately after scheduling must still leave an interrupted record.
    await controller.start_trial(experiment_id, spec)
    await controller.cancel_trial(experiment_id)
    assert controller.detail(experiment_id)["trials"][-1]["status"] == "interrupted"

    with monkeypatch.context() as frozen_clock:
        now = time.monotonic_ns()
        frozen_clock.setattr(time, "monotonic_ns", lambda: now)
        await controller.start_trial(experiment_id, spec)
        await controller.task
        interrupted = controller.detail(experiment_id)["trials"][-1]
        assert interrupted["status"] == "interrupted"
        assert "Clock did not advance" in interrupted["error"]
        assert controller.coordinator.current_stage is None
    # A zero-duration interrupted stage must not prevent the next real trial.
    await controller.start_trial(experiment_id, spec)
    await controller.task
    assert controller.detail(experiment_id)["trials"][-1]["status"] == "complete"
    await controller.stop(experiment_id)
    old = controller.store.source_path(controller.detail(experiment_id)["sources"][0])
    old_bytes = old.read_bytes()
    await controller.start(experiment_id)
    await controller.stop(experiment_id)
    assert len(controller.detail(experiment_id)["sources"]) == 2
    assert old.read_bytes() == old_bytes
    await app.state.coordinator.shutdown()
