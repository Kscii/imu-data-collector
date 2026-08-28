import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.config import (
    AnnotationSettings,
    AuthSettings,
    Settings,
    StorageSettings,
)
from imu_data_collector.dataset_catalog import DatasetCatalog
from imu_data_collector.storage import LocalFilesystemStore


def _settings(tmp_path: Path, *, auth_mode: str = "local") -> Settings:
    return Settings(
        data_root=tmp_path / "data",
        catalog_path=tmp_path / "capture.sqlite3",
        activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        calibration_evidence_path=Path("configs/calibration-evidence.yaml").resolve(),
        storage=StorageSettings(
            backend="local",
            root=tmp_path / "objects",
            cache_root=tmp_path / "cache",
        ),
        auth=AuthSettings(
            mode=auth_mode,
            iap_audience="/projects/1/global/backendServices/1" if auth_mode == "iap" else None,
        ),
        annotation=AnnotationSettings(
            catalog_path=tmp_path / "annotation.sqlite3",
            catalog_refresh_interval_s=0,
        ),
    )


def _install_snapshot(
    store: LocalFilesystemStore,
    tmp_path: Path,
    *,
    kind: str,
    snapshot_id: str,
    created_at_utc: str,
    dataset_id: str,
    current: bool = False,
) -> dict:
    prefix = "benchmark-datasets/base" if kind == "base" else "benchmark-datasets/team/cw12eu"
    filename = f"{dataset_id}.h5"
    object_key = f"{prefix}/{snapshot_id}/datasets/{filename}"
    payload = f"{snapshot_id}:{dataset_id}".encode()
    source = tmp_path / f"{kind}-{snapshot_id}-{filename}"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    store.put_file(
        source,
        object_key,
        content_type="application/x-hdf5",
        metadata={"sha256": digest},
    )
    manifest = {
        "schema_version": "imu_benchmark_dataset_manifest_v1",
        "contract_version": "imu_benchmark_contract_v2",
        "kind": kind,
        "snapshot_id": snapshot_id,
        "created_at_utc": created_at_utc,
        "source": {"repository": "fixture"},
        "files": [
            {
                "dataset_id": dataset_id,
                "object_key": object_key,
                "filename": filename,
                "size_bytes": len(payload),
                "sha256": digest,
                "logical_content_sha256": "a" * 64,
                "hdf5_schema_version": "3.1.0",
                "sampling_rate_hz": 25.0,
                "evaluation_role": ("cross_validation" if kind == "base" else "training_only"),
                "sequences": 2,
                "rows": 50,
                "annotations": 3,
                "events": 1,
                "segments": 2,
                "fall_sequences": 1,
                "participants": 1,
                "body_locations": {"chest": 2},
                "supervision": {"temporal": 2},
            }
        ],
    }
    manifest_key = f"{prefix}/{snapshot_id}/manifest.json"
    store.write_json(manifest_key, manifest, if_generation_match=0)
    manifest_sha = hashlib.sha256(store.read_bytes(manifest_key)).hexdigest()
    if current:
        store.write_json(
            f"{prefix}/current.json",
            {
                "schema_version": "imu_benchmark_current_v1",
                "kind": kind,
                "snapshot_id": snapshot_id,
                "manifest_object": manifest_key,
                "manifest_sha256": manifest_sha,
                "updated_at_utc": created_at_utc,
            },
            if_generation_match=0,
        )
    return {"manifest": manifest, "payload": payload, "manifest_key": manifest_key}


def test_catalog_lists_current_then_newest_history_and_optional_team(
    tmp_path: Path,
) -> None:
    store = LocalFilesystemStore(tmp_path / "objects")
    _install_snapshot(
        store,
        tmp_path,
        kind="base",
        snapshot_id="base-current",
        created_at_utc="2026-08-29T03:00:00Z",
        dataset_id="cgu_bes",
        current=True,
    )
    _install_snapshot(
        store,
        tmp_path,
        kind="base",
        snapshot_id="base-old",
        created_at_utc="2026-08-27T03:00:00Z",
        dataset_id="uci_455",
    )
    _install_snapshot(
        store,
        tmp_path,
        kind="base",
        snapshot_id="base-newer-history",
        created_at_utc="2026-08-28T03:00:00Z",
        dataset_id="kfall",
    )

    summary = DatasetCatalog(store).summary()

    base, team = summary["collections"]
    assert base["current"]["snapshot_id"] == "base-current"
    assert [item["snapshot_id"] for item in base["history"]] == [
        "base-newer-history",
        "base-old",
    ]
    assert team["available"] is False
    assert team["current"] is None


def test_invalid_current_and_history_are_reported_but_not_downloadable(
    tmp_path: Path,
) -> None:
    store = LocalFilesystemStore(tmp_path / "objects")
    fixture = _install_snapshot(
        store,
        tmp_path,
        kind="base",
        snapshot_id="base-current",
        created_at_utc="2026-08-29T03:00:00Z",
        dataset_id="cgu_bes",
        current=True,
    )
    current, generation = store.read_json("benchmark-datasets/base/current.json")
    current["manifest_sha256"] = "0" * 64
    store.write_json(
        "benchmark-datasets/base/current.json",
        current,
        if_generation_match=generation,
    )
    store.write_json(
        "benchmark-datasets/base/broken/manifest.json",
        {"schema_version": "unknown"},
        if_generation_match=0,
    )

    base = DatasetCatalog(store).collection("base")

    assert base["available"] is False
    assert base["current"] is None
    assert any("SHA-256" in warning for warning in base["warnings"])
    assert any("broken" in warning for warning in base["warnings"])
    assert fixture["manifest"]["snapshot_id"] in {item["snapshot_id"] for item in base["history"]}


def test_catalog_api_supports_manifest_h5_range_and_rejects_path_injection(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    fixture = _install_snapshot(
        store,
        tmp_path,
        kind="base",
        snapshot_id="base-current",
        created_at_utc="2026-08-29T03:00:00Z",
        dataset_id="cgu_bes",
        current=True,
    )
    app = create_annotation_app(settings, store=store)

    with TestClient(app) as client:
        catalog = client.get("/api/v1/dataset-catalog")
        manifest = client.get("/api/v1/dataset-catalog/base/base-current/manifest/download")
        partial = client.get(
            "/api/v1/dataset-catalog/base/base-current/cgu_bes/download",
            headers={"Range": "bytes=1-4"},
        )
        missing = client.get("/api/v1/dataset-catalog/base/base-current/not-present/download")

    assert catalog.status_code == 200
    assert manifest.status_code == 200
    assert manifest.json()["snapshot_id"] == "base-current"
    assert partial.status_code == 206
    assert partial.content == fixture["payload"][1:5]
    assert partial.headers["content-range"].startswith("bytes 1-4/")
    assert missing.status_code == 404
    try:
        DatasetCatalog(store).snapshot("base", "../current")
    except ValueError as error:
        assert "snapshot_id" in str(error)
    else:
        raise AssertionError("路径注入应被拒绝")


def test_catalog_api_keeps_iap_authentication_boundary(tmp_path: Path) -> None:
    settings = _settings(tmp_path, auth_mode="iap")
    store = LocalFilesystemStore(settings.storage.root)
    app = create_annotation_app(
        settings,
        store=store,
        token_verifier=lambda _token, _audience: {},
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/dataset-catalog")

    assert response.status_code == 401
