import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.config import AnnotationSettings, Settings, StorageSettings
from imu_data_collector.model_catalog import ModelCatalog
from imu_data_collector.storage import LocalFilesystemStore


def _settings(tmp_path: Path) -> Settings:
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
        annotation=AnnotationSettings(
            catalog_path=tmp_path / "annotation.sqlite3",
            catalog_refresh_interval_s=0,
        ),
    )


def _put(
    store: LocalFilesystemStore, tmp_path: Path, key: str, payload: bytes
) -> dict:
    path = tmp_path / key.replace("/", "-")
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    store.put_file(
        path,
        key,
        content_type="application/octet-stream",
        metadata={"sha256": digest},
    )
    return {"size_bytes": len(payload), "sha256": digest}


def _install_experiment(store: LocalFilesystemStore, tmp_path: Path) -> str:
    run_id = "engineering-example"
    prefix = f"benchmark-model-catalog/experiments/{run_id}"
    result_prefix = f"benchmark-results/engineering/{run_id}"
    bundle = _put(store, tmp_path, f"{result_prefix}/run.tar.gz", b"bundle")
    onnx = _put(store, tmp_path, f"{prefix}/onnx/model-fold0.onnx", b"onnx")
    marker = {
        "schema_version": "imu_experiment_catalog_v0",
        "contract_version": "0.1.0",
        "publication_id": run_id,
        "run_id": run_id,
        "experiment_id": "onnx_full_parity_preflight_v1",
        "evidence_level": "engineering",
        "created_at_utc": "2026-08-29T00:00:00+00:00",
        "scheduled_jobs": 7,
        "source": {"commit": "abc", "dirty": False},
        "data": {
            "base_snapshot_id": "imu_25hz_snapshot_v2",
            "snapshot_sha256": "1" * 64,
        },
        "evaluation_fingerprint": "2" * 64,
        "known_limitations": ["engineering evidence"],
        "result_evidence": {
            "schema_version": "imu_benchmark_result_manifest_v2",
            "manifest": {
                "filename": "manifest.json",
                "object_key": f"{result_prefix}/manifest.json",
                "size_bytes": 123,
                "sha256": "3" * 64,
                "content_type": "application/json",
            },
            "bundle": {
                "filename": "run.tar.gz",
                "object_key": f"{result_prefix}/run.tar.gz",
                "content_type": "application/gzip",
                **bundle,
            },
        },
        "methods": [
            {
                "method_id": "model-natural",
                "model_id": "model",
                "training_recipe": "natural",
                "folds": 1,
                "metrics": {},
            }
        ],
        "artifacts": [
            {
                "artifact_id": "model-fold0",
                "decision": {
                    "score_threshold": {
                        "value": 0.5,
                        "selection_split": "validation",
                        "comparison": ">=",
                    },
                    "anchor": "window_end",
                    "trigger_policies": [
                        {
                            "policy_id": "one_of_one",
                            "required_positive_windows": 1,
                            "lookback_windows": 1,
                            "consecutive": True,
                            "cooldown_seconds": 10.0,
                            "reference_policy": True,
                            "validation_pareto": True,
                        }
                    ],
                },
                "onnx": {
                    "filename": "model-fold0.onnx",
                    "object_key": f"{prefix}/onnx/model-fold0.onnx",
                    "content_type": "application/octet-stream",
                    **onnx,
                },
            }
        ],
    }
    store.write_json(f"{prefix}/metadata.json", marker, if_generation_match=0)
    store.write_json(
        f"{prefix}/state.json",
        {
            "schema_version": "imu_model_catalog_state_v0",
            "kind": "experiment",
            "publication_id": run_id,
            "status": "available",
            "updated_at_utc": "2026-08-29T00:00:00+00:00",
            "updated_by": "xfan0282",
            "history": [],
        },
        if_generation_match=0,
    )
    return run_id


def _install_model(store: LocalFilesystemStore, tmp_path: Path) -> str:
    release_id = "model-release-v1"
    prefix = f"benchmark-model-catalog/models/{release_id}"
    model = _put(store, tmp_path, f"{prefix}/model.onnx", b"model")
    marker = {
        "schema_version": "imu_model_release_v0",
        "contract_version": "0.1.0",
        "release_id": release_id,
        "model_code": "model",
        "name": "Model release",
        "created_at_utc": "2026-08-29T01:00:00+00:00",
        "source": {"commit": "abc", "dirty": False},
        "decision": {
            "score_threshold": {"value": 0.5, "comparison": ">="},
            "trigger_policy": {"policy_id": "one_of_one"},
            "anchor": "window_end",
        },
        "model": {
            "filename": "model.onnx",
            "object_key": f"{prefix}/model.onnx",
            "content_type": "application/octet-stream",
            **model,
        },
    }
    store.write_json(f"{prefix}/metadata.json", marker, if_generation_match=0)
    store.write_json(
        f"{prefix}/state.json",
        {
            "schema_version": "imu_model_catalog_state_v0",
            "kind": "model",
            "publication_id": release_id,
            "status": "available",
            "updated_at_utc": "2026-08-29T01:00:00+00:00",
            "updated_by": "xfan0282",
            "history": [],
        },
        if_generation_match=0,
    )
    return release_id


def test_model_catalog_lists_downloads_and_admin_deprecates(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    run_id = _install_experiment(store, tmp_path)
    release_id = _install_model(store, tmp_path)
    app = create_annotation_app(settings, store=store)

    with TestClient(app) as client:
        catalog = client.get("/api/v1/model-catalog")
        detail = client.get(f"/api/v1/model-catalog/experiment/{run_id}")
        partial = client.get(
            f"/api/v1/model-catalog/experiment/{run_id}/files/onnx-model-fold0/download",
            headers={"Range": "bytes=1-2"},
        )
        deprecated = client.post(
            f"/api/v1/model-catalog/experiment/{run_id}/deprecate",
            json={"expected_generation": detail.json()["state_generation"]},
        )

    assert catalog.status_code == 200
    assert catalog.json()["experiments"][0]["evidence_level"] == "engineering"
    assert catalog.json()["models"][0]["release_id"] == release_id
    assert detail.status_code == 200
    assert detail.json()["marker"]["methods"][0]["model_id"] == "model"
    assert partial.status_code == 206
    assert partial.content == b"nn"
    assert deprecated.status_code == 200
    assert deprecated.json()["status"] == "deprecated"
    assert deprecated.json()["history"][-1]["actor"] == "xfan0282"


def test_model_catalog_quarantines_artifact_without_sha_metadata(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path / "objects")
    run_id = _install_experiment(store, tmp_path)
    metadata = store._metadata_path(
        f"benchmark-model-catalog/experiments/{run_id}/onnx/model-fold0.onnx"
    )
    metadata.unlink()

    summary = ModelCatalog(store, cache_ttl_s=0).refresh(force=True)

    assert summary["experiments"] == []
    assert len(summary["invalid_publications"]) == 1
    assert "身份" in summary["invalid_publications"][0]["detail"]
