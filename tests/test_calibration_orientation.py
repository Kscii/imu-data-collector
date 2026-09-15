import asyncio
import struct
import time

import numpy as np
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from imu_data_collector import coordinator as coordinator_module
from imu_data_collector.ble import NotificationPacket
from imu_data_collector.calibration_orientation import (
    OrientationStart,
    OrientationWindow,
    face_conflicts,
)
from imu_data_collector.capture_api import create_capture_app
from imu_data_collector.config import Settings
from imu_data_collector.models import (
    CharacterizationStartRequest,
    PreviewStartRequest,
    RecordingStartRequest,
)

SN = "IMU-0002-R01"
OWNER = "test-orientation-owner-one"


def fill_window(window, raw, *, now=None, count=101):
    now = time.monotonic_ns() if now is None else now
    window.clear()
    for t in np.linspace(now - 2_000_000_000, now, count).astype(np.int64):
        window.add(raw, int(t))
    return now


@pytest.mark.parametrize("axis", range(3))
@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("scale", [512, 12800, 32000])
def test_raw_axis_detection_with_unknown_scale_bias_and_noise(axis, sign, scale):
    window = OrientationWindow()
    now = time.monotonic_ns()
    rng = np.random.default_rng(17)
    for t in np.linspace(now - 2_000_000_000, now, 101).astype(np.int64):
        raw = rng.normal(0, scale * 0.003, 6)
        raw[:3] += [3, -5, 2]
        raw[axis] += sign * scale
        window.add(raw, int(t))
    result = window.evaluate(now)
    assert result["direction"] == ("+" if sign > 0 else "-") + "XYZ"[axis]
    assert result["reason"] == "ready"
    assert result["sample_count"] == 101


def test_missing_stale_tilted_saturated_and_moving_samples():
    window = OrientationWindow()
    assert window.evaluate()["reason"] == "waiting_samples"
    now = fill_window(window, [12000, 0, 0, 0, 0, 0])
    assert window.evaluate(now + 500_000_001)["reason"] == "stale"
    # Repeated web polling does not turn one packet into an observation window.
    window.clear()
    window.add([12000, 0, 0, 0, 0, 0], now)
    for _ in range(30):
        assert window.evaluate(now)["reason"] == "waiting_samples"
    for raw, reason in [
        ([8000, 8000, 0, 0, 0, 0], "tilted"),
        ([32767, 0, 0, 0, 0, 0], "saturated"),
        ([float("nan"), 0, 0, 0, 0, 0], "saturated"),
        ([0, 0, 0, 0, 0, 0], "no_gravity_signal"),
    ]:
        fill_window(window, raw, now=now)
        assert window.evaluate(now)["reason"] == reason
    window.clear()
    for i, t in enumerate(np.linspace(now - 2_000_000_000, now, 101).astype(np.int64)):
        window.add([12000 + (i % 2) * 3000, 0, 0, 0, 0, 0], int(t))
    assert window.evaluate(now)["reason"] == "moving"


def test_opposite_face_evidence_is_checked():
    faces = {
        "+X": {"sample": {"median_counts": [12800, 0, 0]}},
        "-X": {"sample": {"median_counts": [-12800, 0, 0]}},
    }
    assert face_conflicts(faces) == []
    faces["-X"]["sample"]["median_counts"] = [16000, 0, 0]
    assert face_conflicts(faces) == ["X"]


class FakeBle:
    opened = 0

    def __init__(self, settings):
        self.settings = settings
        self.device_identifier = None
        self.queue = asyncio.Queue()
        self.connected = self.notifying = False
        self.last_packet_ns = None
        self.dropped_callback_packets = 0
        self.disconnect_reason = None

    async def start(self):
        type(self).opened += 1
        self.connected = self.notifying = True

    async def stop(self):
        self.connected = self.notifying = False
        self.disconnect_reason = "local_stop"

    async def send(self, raw, ticks):
        self.last_packet_ns = time.monotonic_ns()
        await self.queue.put(
            NotificationPacket(
                struct.pack("<6hQ", *raw, ticks) + b"\r\n",
                self.last_packet_ns,
            )
        )


@pytest.fixture
def app(tmp_path, monkeypatch):
    FakeBle.opened = 0
    monkeypatch.setattr(coordinator_module, "CW12EUBleSource", FakeBle)
    value = create_capture_app(
        Settings(
            data_root=tmp_path / "data",
            catalog_path=tmp_path / "catalog.sqlite3",
            minimum_free_gib=0,
            device_registry_auto_refresh=False,
        )
    )

    async def forbid_camera(*args, **kwargs):
        pytest.fail("Orientation setup must not open the camera")

    monkeypatch.setattr(value.state.coordinator, "_open_preview_video", forbid_camera)
    return value


@pytest.mark.asyncio
async def test_imu_only_owner_lease_duplicate_packets_and_cleanup(app):
    orientation = app.state.orientation
    coordinator = app.state.coordinator
    request = OrientationStart(sensor_sn=SN, owner_id=OWNER)
    try:
        session = await orientation.start(request)
        session_id = session["session_id"]
        assert coordinator.mode == "imu_orientation_preview"
        assert coordinator.video is coordinator.writer is None
        assert not list(coordinator.settings.data_root.rglob("*.h5"))
        assert (await orientation.start(request))["session_id"] == session_id
        assert FakeBle.opened == 1
        with pytest.raises(HTTPException, match="409"):
            await orientation.start(OrientationStart(sensor_sn=SN, owner_id="another-browser-tab"))
        with pytest.raises(RuntimeError):
            await coordinator.start_preview(PreviewStartRequest(sensor_sn=SN))
        with pytest.raises(RuntimeError):
            await coordinator.start(RecordingStartRequest(collection_id="test", data_tier="test"))
        # The real shared notification consumer supplies the buffer and deduplicates ticks.
        for tick in [10, 10, 10, 20]:
            await coordinator.ble.send([12800, 0, 0, 0, 0, 0], tick)
        for _ in range(20):
            if coordinator.sample_count == 4:
                break
            await asyncio.sleep(0.01)
        assert len(coordinator.orientation_window.samples) == 2
        fill_window(coordinator.orientation_window, [12800, 0, 0, 0, 0, 0])
        coordinator.ble.connected = False
        assert orientation.status(session_id)["sample"]["reason"] == "disconnected"
        assert not coordinator.orientation_window.samples
        session = await orientation.start(request)
        assert session["session_id"] == session_id
        orientation.deadline = time.monotonic() - 1
        with pytest.raises(HTTPException):
            orientation.status(session_id)
        for _ in range(150):
            if orientation.session is None:
                break
            await asyncio.sleep(0.01)
        assert orientation.session is None
        assert coordinator.mode is coordinator.ble is None
        second = await orientation.start(request)
        await orientation.stop(session_id)
        assert coordinator.ble.connected
        await orientation.stop(second["session_id"])
        assert coordinator.mode is None
    finally:
        await app.state.calibration.close()
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_faces_api_rejects_motion_duplicates_and_freezes_setup_on_creation(app):
    coordinator = app.state.coordinator
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        try:
            session = (
                await client.post(
                    "/api/v1/calibration-orientation/start",
                    json={
                        "sensor_sn": SN,
                        "owner_id": OWNER,
                    },
                )
            ).json()
            session_id = session["session_id"]
            payload = {"session_id": session_id, "direction": "+X", "description": "Connector"}
            assert (
                await client.post("/api/v1/calibration-orientation/faces", json=payload)
            ).status_code == 409
            fill_window(coordinator.orientation_window, [12800, 0, 0, 0, 0, 0])
            assert (
                await client.post("/api/v1/calibration-orientation/faces", json=payload)
            ).status_code == 200
            assert (
                await client.post("/api/v1/calibration-orientation/faces", json=payload)
            ).status_code == 409
            for axis in "XYZ":
                for sign in [1, -1]:
                    key = ("+" if sign == 1 else "-") + axis
                    raw = [0] * 6
                    raw["XYZ".index(axis)] = 12800 * sign
                    fill_window(coordinator.orientation_window, raw)
                    response = await client.post(
                        "/api/v1/calibration-orientation/faces",
                        json={
                            **payload,
                            "direction": key,
                            "description": "Housing " + key,
                            "replace": key == "+X",
                        },
                    )
                    assert response.status_code == 200, response.text
            assert response.json()["ready"]
            fill_window(coordinator.orientation_window, [0, 0, 12800, 0, 0, 0])
            assert (
                await client.post(
                    "/api/v1/calibration-orientation/faces",
                    json={
                        **payload,
                        "replace": True,
                    },
                )
            ).status_code == 409
            assert (
                await client.put(
                    "/api/v1/calibration-orientation/faces",
                    json={
                        **payload,
                        "description": "Renamed connector",
                    },
                )
            ).status_code == 200
            directions = {
                key: value["description"]
                for key, value in app.state.orientation.session["faces"].items()
            }
            response = await client.post(
                "/api/v1/calibration-experiments",
                json={
                    "sensor_sn": SN,
                    "operator_id": "xfan0282",
                    "directions": directions,
                    "orientation_session_id": session_id,
                },
            )
            assert response.status_code == 201, response.text
            experiment = response.json()
            assert experiment["orientation_setup"]["axis_definition"] == "raw_accelerometer"
            assert len(experiment["orientation_setup"]["faces"]) == 6
            assert experiment["trials"] == []
            assert experiment["data_tier"] == "test"
            assert coordinator.mode is coordinator.ble is coordinator.writer is None
            assert not list(coordinator.settings.data_root.rglob("*.h5"))
        finally:
            await app.state.calibration.close()
            await coordinator.shutdown()


@pytest.mark.asyncio
async def test_active_capture_is_not_preempted_and_failed_connect_releases_reservation(
    app, monkeypatch
):
    coordinator = app.state.coordinator
    controller = app.state.orientation
    original_sn = coordinator.settings.imu.sensor_sn
    coordinator.mode = "capture"
    request = OrientationStart(sensor_sn=SN, owner_id=OWNER)
    with pytest.raises(RuntimeError):
        await controller.start(request)
    assert coordinator.mode == "capture"
    assert coordinator.settings.imu.sensor_sn == original_sn
    coordinator.mode = None

    async def fail(*args):
        raise RuntimeError("device unavailable")

    monkeypatch.setattr(coordinator, "_open_preview_ble", fail)
    with pytest.raises(RuntimeError, match="device unavailable"):
        await controller.start(request)
    assert coordinator.mode is coordinator.ble is None
    assert not coordinator._preview_open_in_flight
    await app.state.calibration.close()
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_configuration_can_be_selected_after_characterization_releases_devices(app):
    coordinator = app.state.coordinator
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        workspace = (await client.get("/api/v1/configuration/workspace")).json()
        workspace["name"] = "New calibration for local testing"
        snapshot = (await client.post(
            "/api/v1/configuration/local-snapshots", json=workspace
        )).json()["snapshot"]
        selection = {"snapshot_id": snapshot["snapshot_id"]}
        try:
            await app.state.orientation.start(OrientationStart(sensor_sn=SN, owner_id=OWNER))
            assert (await client.post(
                "/api/v1/configuration/select", json=selection
            )).status_code == 409
            await app.state.orientation.stop(app.state.orientation.session["session_id"])
            await coordinator.start_characterization(
                CharacterizationStartRequest(sensor_sn=SN, operator_id="xfan0282")
            )
            assert (await client.post(
                "/api/v1/configuration/select", json=selection
            )).status_code == 409
            result = await coordinator.stop_characterization()
            assert coordinator.current is coordinator.mode is coordinator.ble is None
            assert coordinator.state.value == "idle"
            assert coordinator.last_characterization == result
            assert result["recording_id"] and result["training_eligible"] is False
            selected = await client.post("/api/v1/configuration/select", json=selection)
            assert selected.status_code == 200, selected.text
            assert selected.json()["selected_snapshot_id"] == snapshot["snapshot_id"]
        finally:
            await app.state.calibration.close()
            await coordinator.shutdown()
