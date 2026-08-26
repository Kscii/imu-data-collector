from pathlib import Path

from fastapi.testclient import TestClient

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.capture_api import create_capture_app
from imu_data_collector.config import AnnotationSettings, Settings, StorageSettings
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    ArtifactDescriptor,
    CalibrationProfile,
    CaptureManifestV2,
    DataTier,
)
from imu_data_collector.storage import LocalFilesystemStore


def _settings(tmp_path: Path) -> Settings:
    data_root = tmp_path / "data"
    data_root.mkdir()
    return Settings(
        data_root=data_root,
        catalog_path=tmp_path / "capture.sqlite3",
        activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        storage=StorageSettings(
            backend="local",
            root=tmp_path / "objects",
            cache_root=tmp_path / "cache",
        ),
        annotation=AnnotationSettings(
            catalog_path=tmp_path / "annotation.sqlite3",
            review_policy="single_user",
        ),
    )


def _publish_fixture(store: LocalFilesystemStore, tmp_path: Path) -> str:
    recording_id = "fixture_001"
    source = tmp_path / "source"
    source.mkdir()
    files = {
        "capture_h5": source / "capture.h5",
        "video_mkv": source / "video.mkv",
        "preview_mp4": source / "preview.mp4",
    }
    files["capture_h5"].write_bytes(b"hdf5-fixture")
    files["video_mkv"].write_bytes(b"matroska-fixture")
    files["preview_mp4"].write_bytes(b"0123456789")
    descriptors = []
    for role, path in files.items():
        filename = path.name
        key = f"captures/{recording_id}/{filename}"
        digest = sha256_file(path)
        content_type = {
            "capture_h5": "application/x-hdf5",
            "video_mkv": "video/x-matroska",
            "preview_mp4": "video/mp4",
        }[role]
        store.put_file(
            path,
            key,
            content_type=content_type,
            metadata={"sha256": digest},
        )
        descriptors.append(
            ArtifactDescriptor(
                role=role,
                object_key=key,
                filename=filename,
                size_bytes=path.stat().st_size,
                sha256=digest,
                content_type=content_type,
            )
        )
    manifest = CaptureManifestV2(
        recording_id=recording_id,
        collection_id="pilot",
        participant_id="xfan0282",
        data_tier=DataTier.TEST,
        captured_at_utc="2026-08-26T00:00:00+00:00",
        duration_ns=1_000_000_000,
        source_h5_schema_version="1.0.0",
        software_revision="test",
        calibration=CalibrationProfile(),
        artifacts=descriptors,
    )
    store.write_json(
        f"captures/{recording_id}/manifest.json",
        manifest.model_dump(mode="json"),
        if_generation_match=0,
    )
    return recording_id


def test_capture_app_does_not_expose_annotation_or_export_routes(
    tmp_path: Path,
) -> None:
    app = create_capture_app(_settings(tmp_path))
    paths = {route.path for route in app.routes}

    assert "/api/v1/recordings/{recording_id}/publish" in paths
    assert "/api/v1/recordings/{recording_id}/annotations" not in paths
    assert "/api/v1/training-releases" not in paths


def test_annotation_app_indexes_manifest_and_supports_video_range(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path)
    app = create_annotation_app(settings, store)
    paths = {route.path for route in app.routes}

    assert "/api/v1/recordings/start" not in paths
    assert "/api/v1/recordings/{recording_id}/publish" not in paths

    with TestClient(app) as client:
        refreshed = client.post("/api/v1/index/refresh")
        assert refreshed.json() == {"imported": 1, "skipped": 0}

        recordings = client.get("/api/v1/recordings").json()
        assert recordings[0]["recording_id"] == recording_id
        assert recordings[0]["state"] == "published"
        assert recordings[0]["upload_state"] == "published"

        partial = client.get(
            f"/api/v1/recordings/{recording_id}/video",
            headers={"Range": "bytes=2-5"},
        )
        assert partial.status_code == 206
        assert partial.content == b"2345"
        assert partial.headers["content-range"] == "bytes 2-5/10"

        review = client.get(f"/api/v1/recordings/{recording_id}/review").json()
        assert review["workflow"]["review_policy"] == "single_user"
