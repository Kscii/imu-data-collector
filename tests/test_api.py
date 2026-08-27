from pathlib import Path

from fastapi.testclient import TestClient

from imu_data_collector.api import _mjpeg_part, create_app
from imu_data_collector.capture_api import create_capture_app
from imu_data_collector.config import Settings


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
    app = create_app(settings)

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
        assert config.json()["default_data_tier"] == "test"
        assert config.json()["imu"]["calibration_verified"] is False
        assert config.json()["allowed_unikeys"] == [
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
            assert "IMU 数采平台" in frontend.text


def test_start_contract_rejects_non_unikey_participant_before_hardware_access(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    app = create_app(
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


def test_start_contract_rejects_well_formed_but_unlisted_unikey(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    app = create_app(
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
    assert "白名单" in response.json()["detail"]


def test_start_contract_rejects_unknown_data_tier_before_hardware_access(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    app = create_app(
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
    assert "预览通道" in response.json()["detail"]
