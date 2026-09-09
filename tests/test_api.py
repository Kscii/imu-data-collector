from pathlib import Path

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from imu_data_collector.capture_api import _mjpeg_part, create_capture_app
from imu_data_collector.config import Settings
from imu_data_collector.device_configuration import si_profile_id


def test_mjpeg_part_has_explicit_length_and_valid_boundaries() -> None:
    jpeg = b"\xff\xd8test-frame\xff\xd9"

    part = _mjpeg_part(jpeg)

    assert part.startswith(
        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 14\r\n\r\n"
    )
    assert part.endswith(jpeg + b"\r\n")


def test_local_api_health_config_and_frontend(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    settings = Settings(
        data_root=data_root,
        catalog_path=tmp_path / "catalog.sqlite3",
        activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
    )
    app = create_capture_app(settings)

    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True
        assert health.json()["state"] == "idle"

        config = client.get("/api/v1/config")
        assert config.status_code == 200
        assert len(config.json()["build_id"]) == 16
        assert config.json()["data_root"] == str(data_root)
        assert config.json()["data_tiers"] == ["test", "prod"]
        assert config.json()["default_data_tier"] == "prod"
        assert config.json()["configuration"]["schema_version"] == "2.0"
        assert config.json()["imu"]["sensor_sn"] == "IMU-0001-R01"
        assert config.json()["imu"]["calibration_verified"] is True
        assert "allowed_unikeys" not in config.json()
        assert config.json()["operator_unikeys"] == [
            "rkim6933",
            "zche0826",
            "jzho8728",
            "jzha9115",
            "xfan0282",
            "yniu0950",
            "hche5673",
            "jmia0254",
            "xliu0452",
        ]

        frontend = client.get("/")
        assert frontend.status_code == 200
        if frontend.headers["content-type"].startswith("application/json"):
            assert "前端尚未构建" in frontend.json()["message"]
        else:
            assert "<title>IMU 数采平台</title>" in frontend.text
            assert '<div id="root"></div>' in frontend.text


def test_first_run_creates_missing_data_root_before_health_check(tmp_path: Path) -> None:
    data_root = tmp_path / "new-user" / "IMUData"
    assert not data_root.exists()
    app = create_capture_app(
        Settings(
            data_root=data_root,
            catalog_path=tmp_path / "catalog.sqlite3",
            activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert data_root.is_dir()


def test_device_endpoint_reuses_camera_cache_until_explicit_refresh(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    app = create_capture_app(
        Settings(
            data_root=data_root,
            catalog_path=tmp_path / "catalog.sqlite3",
            activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        )
    )
    calls = 0

    async def list_cameras(*, refresh: bool = False):
        nonlocal calls
        calls += 1
        return [{"camera_id": f"camera-{calls}", "refresh": refresh}]

    app.state.coordinator.list_cameras = list_cameras
    with TestClient(app) as client:
        first = client.get("/api/v1/devices")
        refreshed = client.get("/api/v1/devices?refresh_cameras=true")

    assert first.json()["cameras"] == [{"camera_id": "camera-1", "refresh": False}]
    assert refreshed.json()["cameras"] == [
        {"camera_id": "camera-2", "refresh": True}
    ]


async def test_device_endpoint_lists_v2_profiles_and_accepts_local_test_snapshot(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_root=tmp_path / "data",
        catalog_path=tmp_path / "catalog.sqlite3",
        activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        device_registry_path=Path("configs/imu-devices.yaml").resolve(),
        device_drafts_path=tmp_path / "drafts.json",
        device_candidates_path=tmp_path / "candidates.json",
    )
    app = create_capture_app(settings)

    async def list_cameras(*, refresh: bool = False):
        del refresh
        return []

    app.state.coordinator.list_cameras = list_cameras
    si_payload = {
        "verified": False,
        "accel_counts_per_g": None,
        "gyro_counts_per_dps": None,
        "accel_bias_counts": [0, 0, 0],
        "gyro_bias_counts": [0, 0, 0],
        "raw_axis_order": [0, 1, 2],
        "axis_signs": [1, 1, 1],
        "method": "unverified",
        "evidence_sha256": None,
        "coordinate_system": {},
        "evidence": [],
    }
    device = {
        "sensor_sn": "IMU-0003-R01",
        "hardware_asset_id": "IMU-0003",
        "revision": 1,
        "lifecycle": "active",
        "supersedes_sn": None,
        "display_name": "Lab candidate",
        "identity": {
            "advertised_name": "acce&gyro_LAB",
            "public_address": "AA:BB:CC:DD:EE:FF",
            "address_type": "public",
            "advertised_service_uuid": "0000abf0-0000-1000-8000-00805f9b34fb",
            "gatt_fingerprint_sha256": None,
        },
        "firmware": {
            "version": "unknown",
            "evidence_status": "not_exposed",
            "artifact_sha256": None,
        },
        "protocol_id": "acce_gyro_abf0_v1",
        "expected_rate_hz": 50.0,
        "expected_rate_status": "frontend_commissioning_candidate_unverified",
        "allowed_data_tiers": ["test"],
        "si_profile": {
            "profile_id": si_profile_id("IMU-0003-R01", si_payload),
            **si_payload,
        },
        "audit_document": None,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        initial = await client.get("/api/v1/devices")
        workspace = (await client.get("/api/v1/configuration/workspace")).json()
        workspace["name"] = "local three-device fleet"
        workspace["description"] = "API integration test"
        workspace["content"]["devices"].append(device)
        saved_workspace = await client.put(
            "/api/v1/configuration/workspace", json=workspace
        )
        created = await client.post(
            "/api/v1/configuration/local-snapshots", json=workspace
        )
        selected = await client.post(
            "/api/v1/configuration/select",
            json={"snapshot_id": created.json()["snapshot"]["snapshot_id"]},
        )
        listed = await client.get("/api/v1/devices")
        candidate = await client.put(
            "/api/v1/devices/imu-candidates/IMU-0002-R01",
            json={
                "accel_counts_per_g": 16384.0,
                "gyro_counts_per_dps": None,
                "evidence_status": "operator_local_test",
            },
        )
        candidate_list = await client.get("/api/v1/devices")
        candidate_export = await client.get(
            "/api/v1/devices/imu-candidates/IMU-0002-R01/export"
        )

    assert initial.status_code == 200
    assert initial.json()["default_sensor_sn"] is None
    assert initial.json()["selected_sensor_sn"] is None
    assert [item["sensor_sn"] for item in initial.json()["imu_profiles"]] == [
        "IMU-0001-R01",
        "IMU-0002-R01",
    ]
    assert created.status_code == 200
    assert created.json()["production_authority"] is False
    assert saved_workspace.status_code == 200
    assert selected.status_code == 200
    assert selected.json()["selected_state"] == "local"
    assert listed.json()["suggested_sensor_sn"] == "IMU-0004-R01"
    added = listed.json()["imu_profiles"][-1]
    assert added["source"] == "local"
    assert added["prod_capture_enabled"] is False
    assert candidate.status_code == 200
    assert candidate.json()["production_authority"] is False
    new_profile = candidate_list.json()["imu_profiles"][1]
    assert new_profile["candidate_conversion_source"] == "local_override"
    assert new_profile["candidate_conversion"]["accel_counts_per_g"] == 16384.0
    assert candidate_export.status_code == 200
    assert "production_authority: false" in candidate_export.text


def test_device_endpoint_reports_camera_discovery_failure_as_actionable_error(
    tmp_path: Path,
) -> None:
    app = create_capture_app(
        Settings(
            data_root=tmp_path / "data",
            catalog_path=tmp_path / "catalog.sqlite3",
            activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        )
    )

    async def fail_cameras(*, refresh: bool = False):
        del refresh
        raise RuntimeError("camera permission denied")

    app.state.coordinator.list_cameras = fail_cameras
    with TestClient(app) as client:
        response = client.get("/api/v1/devices")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "camera_discovery_failed"
    assert detail["component"] == "video"
    assert detail["retryable"] is True
    assert "camera permission denied" in detail["message"]


def test_start_contract_rejects_non_unikey_participant_before_hardware_access(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    app = create_capture_app(
        Settings(
            data_root=data_root,
            catalog_path=tmp_path / "catalog.sqlite3",
            activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/recordings/start",
            json={"collection_id": "pilot", "participant_id": "Invalid Name"},
        )

    assert response.status_code == 422


def test_start_contract_rejects_removed_participant_field(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    app = create_capture_app(
        Settings(
            data_root=data_root,
            catalog_path=tmp_path / "catalog.sqlite3",
            activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/recordings/start",
            json={"collection_id": "pilot", "participant_id": "unknown123"},
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


def test_start_contract_rejects_unknown_data_tier_before_hardware_access(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    app = create_capture_app(
        Settings(
            data_root=data_root,
            catalog_path=tmp_path / "catalog.sqlite3",
            activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/recordings/start",
            json={
                "collection_id": "pilot",
                "participant_id": "xfan0282",
                "data_tier": "temporary",
            },
        )

    assert response.status_code == 422


def test_capture_api_turns_empty_timeout_into_structured_nonempty_error(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    app = create_capture_app(
        Settings(
            data_root=data_root,
            catalog_path=tmp_path / "catalog.sqlite3",
            activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        )
    )

    async def fail_preview(_request):
        raise TimeoutError

    app.state.coordinator.start_preview = fail_preview
    with TestClient(app) as client:
        response = client.post("/api/v1/preflight/start", json={"camera_id": None})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "preview_start_failed"
    assert detail["component"] == "ble_video"
    assert "TimeoutError" in detail["message"]
    assert detail["hint"]


def test_preview_requires_explicit_sensor_sn_before_hardware_access(
    tmp_path: Path,
) -> None:
    app = create_capture_app(
        Settings(
            data_root=tmp_path / "data",
            catalog_path=tmp_path / "catalog.sqlite3",
            activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
            device_registry_path=Path("configs/imu-devices.yaml").resolve(),
            require_explicit_sensor_selection=True,
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/preflight/start",
            json={"camera_id": None, "sensor_sn": None},
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "preview_start_failed"
    assert "必须选择 IMU SN" in detail["message"]


def test_preview_endpoint_rejects_inactive_channel_instead_of_returning_empty_200(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    app = create_capture_app(
        Settings(
            data_root=data_root,
            catalog_path=tmp_path / "catalog.sqlite3",
            activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/preview.mjpeg?stream=1")

    assert response.status_code == 409
    assert "预览通道" in response.json()["detail"]["message"]
