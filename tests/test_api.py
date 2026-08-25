from pathlib import Path

from fastapi.testclient import TestClient

from imu_data_collector.api import create_app
from imu_data_collector.config import Settings


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
        assert config.json()["data_root"] == str(data_root)
        assert config.json()["imu"]["calibration_verified"] is False

        frontend = client.get("/")
        assert frontend.status_code == 200
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
