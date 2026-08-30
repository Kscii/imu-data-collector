import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from imu_data_collector import model_catalog as model_catalog_module
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


def _selection_evidence() -> dict:
    return {
        "source_run_id": "selection-run",
        "source_commit": "a" * 40,
        "model_id": "model",
        "training_recipe": "natural",
        "data_snapshot_fingerprint": "1" * 64,
        "split_fingerprint": "2" * 64,
        "selection_scope": "validation_only_oof",
        "metric_split": "validation_oof",
        "selection_eligible": True,
        "source_stride_seconds": 1.0,
        "participant_once": {
            "status": "PASS",
            "participant_count": 5,
            "appearances_per_participant": 1,
            "validation_fold_participant_counts": [1, 1, 1, 1, 1],
            "assignment_sha256": "3" * 64,
        },
        "threshold_selection": {
            "method": "validation_balanced_accuracy",
            "tie_break": "lower_threshold",
        },
        "trigger_policy_selection": {
            "method": "validation_pareto",
            "tie_break": "policy_id",
        },
    }


def _golden_fixtures() -> list[dict]:
    return [
        {
            "fixture_id": name,
            "input_values": [[0.0] * 6 for _ in range(50)],
            "expected_fall_score": 0.0,
            "atol": 1e-6,
            "rtol": 1e-6,
        }
        for name in ("stationary", "adl-like", "impact-like")
    ]


def _install_experiment(
    store: LocalFilesystemStore, tmp_path: Path, *, legacy: bool = False
) -> str:
    run_id = "engineering-example"
    prefix = f"benchmark-model-catalog/experiments/{run_id}"
    result_prefix = f"benchmark-results/engineering/{run_id}"
    bundle = _put(store, tmp_path, f"{result_prefix}/run.tar.gz", b"bundle")
    result_manifest = _put(
        store, tmp_path, f"{result_prefix}/manifest.json", b'{"run":"fixture"}'
    )
    onnx = _put(store, tmp_path, f"{prefix}/onnx/model-fold0.onnx", b"onnx")
    marker = {
        "schema_version": "imu_experiment_catalog_v1",
        "contract_version": "1.0.0",
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
                "content_type": "application/json",
                **result_manifest,
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
                "fold_count": 1,
                "metric_split": "test",
                "selection_eligible": False,
                "metrics": {},
                "artifact_ids": ["model-fold0"],
            }
        ],
        "artifacts": [
            {
                "artifact_id": "model-fold0",
                "method_id": "model-natural",
                "fold": 0,
                "metrics": {"metric_split": "test", "selection_eligible": False},
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
    if legacy:
        marker["schema_version"] = "imu_experiment_catalog_v0"
        marker["contract_version"] = "0.1.0"
        for method in marker["methods"]:
            method.pop("metric_split")
            method.pop("selection_eligible")
        for artifact in marker["artifacts"]:
            artifact.pop("method_id")
            artifact["metrics"].pop("metric_split")
            artifact["metrics"].pop("selection_eligible")
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
        "schema_version": "imu_model_release_v1",
        "contract_version": "1.0.0",
        "release_id": release_id,
        "model_code": "model",
        "name": "Model release",
        "created_at_utc": "2026-08-29T01:00:00+00:00",
        "release_stage": "research_candidate",
        "source": {
            "selection_evidence": _selection_evidence(),
            "final_training": {
                "commit": "a" * 40,
                "dirty": False,
                "seed": 3888,
                "fixed_epoch_source": "validation_oof",
                "training_scope": "development_participants",
                "actual_epochs": 4,
            },
        },
        "data": {
            "snapshot_fingerprint": "1" * 64,
            "split_fingerprint": "2" * 64,
        },
        "input": {
            "semantic": "si_window",
            "dtype": "float32",
            "shape": [None, 50, 6],
            "sampling_rate_hz": 25,
            "channels": ["ax", "ay", "az", "gx", "gy", "gz"],
            "axis_frame": "sensor_local",
            "gravity": "retained",
        },
        "output": {
            "semantic": "fall_score",
            "dtype": "float32",
            "shape": [None],
            "probability_calibrated": False,
        },
        "preprocessing": {
            "location": "onnx_graph",
            "normalization": {
                "embedded": True,
                "mean": [0.0] * 6,
                "scale": [1.0] * 6,
            },
        },
        "windowing": {
            "window_seconds": 2.0,
            "training_stride_seconds": 0.5,
            "inference_interval_seconds": 1.0,
            "anchor": "window_end",
            "reset_on": ["new_sequence", "stream_gap"],
            "refill_frames_after_reset": 50,
        },
        "decision": {
            "score_threshold": {"value": 0.5, "comparison": ">="},
            "trigger_policy": {
                "policy_id": "one_of_one",
                "required_positive_windows": 1,
                "lookback_windows": 1,
                "consecutive": True,
                "cooldown_seconds": 10.0,
            },
            "status": "provisional_validation_derived",
        },
        "metrics": {
            "metric_split": "validation_oof",
            "selection_eligible": True,
            "final_model_independently_evaluated": False,
        },
        "verification": {
            "golden_fixtures": _golden_fixtures()
        },
        "validation": {
            "onnx_checker": {"status": "PASS"},
            "python_onnxruntime_parity": {
                "status": "PASS",
                "scope": "all_final_training_windows",
                "windows": 10,
            },
            "external_runtime": {"status": "not_tested"},
            "device_replay": {"status": "not_tested"},
        },
        "known_limitations": ["fixture"],
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


def test_model_catalog_lists_downloads_and_admin_deprecates(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(model_catalog_module.time, "monotonic", lambda: 30.0)
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    run_id = _install_experiment(store, tmp_path)
    release_id = _install_model(store, tmp_path)
    app = create_annotation_app(settings, store=store)

    with TestClient(app) as client:
        catalog = client.get("/api/v1/model-catalog")
        detail = client.get(f"/api/v1/model-catalog/experiment/{run_id}")
        assert detail.status_code == 200, detail.json()
        partial = client.get(
            f"/api/v1/model-catalog/experiment/{run_id}/files/onnx-model-fold0/download",
            headers={"Range": "bytes=1-2"},
        )
        bundle = client.get(
            f"/api/v1/model-catalog/experiment/{run_id}/files/result-bundle/download"
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
    assert {item["file_id"] for item in detail.json()["files"]} == {
        "onnx-model-fold0",
        "result-bundle",
    }
    assert partial.status_code == 206
    assert partial.content == b"nn"
    assert bundle.status_code == 200
    assert bundle.content == b"bundle"
    assert bundle.headers["x-content-sha256"] == hashlib.sha256(b"bundle").hexdigest()
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


def test_legacy_experiment_is_read_only_and_hidden_after_deprecation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    run_id = _install_experiment(store, tmp_path, legacy=True)
    app = create_annotation_app(settings, store=store)

    with TestClient(app) as client:
        detail = client.get(f"/api/v1/model-catalog/experiment/{run_id}")
        deprecated = client.post(
            f"/api/v1/model-catalog/experiment/{run_id}/deprecate",
            json={"expected_generation": detail.json()["state_generation"]},
        )
        ordinary = client.get("/api/v1/model-catalog")
        audit = client.get("/api/v1/model-catalog?include_deprecated=true")

    assert detail.status_code == 200
    assert detail.json()["contract_status"] == "legacy_pre_v1"
    assert detail.json()["interpretation"] == {
        "metric_split": "test",
        "selection_eligible": False,
        "contract_status": "legacy_pre_v1",
    }
    assert deprecated.status_code == 200
    assert ordinary.json()["experiments"] == []
    assert audit.json()["experiments"][0]["contract_status"] == "legacy_pre_v1"
