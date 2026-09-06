import hashlib
import json
from pathlib import Path

from imu_data_collector.client_hdf5 import CORE_DATASET_SCHEMA_VERSION
from imu_data_collector.dataset_catalog import DATASET_HANDOFF_VERSION
from imu_data_collector.model_catalog import (
    EXPERIMENT_CONTRACT_VERSION,
    MODEL_CONTRACT_VERSION,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_synced_annotation_benchmark_contract_matches_lock() -> None:
    lock = json.loads(
        (
            PROJECT_ROOT
            / "configs/contracts/annotation-benchmark-contract.lock.json"
        ).read_text(encoding="utf-8")
    )
    contract = PROJECT_ROOT / lock["canonical_path"]

    assert hashlib.sha256(contract.read_bytes()).hexdigest() == lock["sha256"]
    assert lock["upstream_repository"] == "Kscii/imu-fall-benchmark"
    assert len(lock["upstream_commit"]) == 40
    assert lock["module_versions"] == {
        "dataset_handoff": DATASET_HANDOFF_VERSION,
        "experiment_catalog": EXPERIMENT_CONTRACT_VERSION,
        "model_release": MODEL_CONTRACT_VERSION,
    }


def test_synced_hdf5_contract_matches_lock() -> None:
    lock = json.loads(
        (PROJECT_ROOT / "configs/contracts/imu-hdf5-v3.2.lock.json").read_text(
            encoding="utf-8"
        )
    )
    contract = PROJECT_ROOT / lock["canonical_path"]

    assert hashlib.sha256(contract.read_bytes()).hexdigest() == lock["sha256"]
    assert lock["upstream_repository"] == "Kscii/imu-fall-benchmark"
    assert len(lock["upstream_commit"]) == 40
    assert lock["hdf5_schema_version"] == CORE_DATASET_SCHEMA_VERSION
    assert lock["profiles"] == ["training_dataset", "client_delivery"]
