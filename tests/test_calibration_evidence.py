import hashlib
from pathlib import Path

import pytest

from imu_data_collector.config import load_calibration_evidence, load_settings


def test_default_profile_matches_versioned_calibration_evidence() -> None:
    settings = load_settings()
    evidence = load_calibration_evidence(settings.calibration_evidence_path)

    assert settings.imu.calibration_verified
    assert evidence["profile_id"] == settings.imu.calibration_profile_id
    assert settings.imu.accel_counts_per_g == 4096.0
    assert settings.imu.gyro_counts_per_dps == 32.8
    assert settings.imu.raw_axis_order == (0, 1, 2)
    assert settings.imu.axis_signs == (1, -1, 1)
    assert len(evidence["evidence"]) == 15
    assert settings.imu.calibration_evidence_sha256 == hashlib.sha256(
        settings.calibration_evidence_path.read_bytes()
    ).hexdigest()


def test_calibration_evidence_rejects_duplicate_recording_ids(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.yaml"
    path.write_text(
        """
schema_version: "1.0.0"
profile_id: "fixture"
evidence:
  - recording_id: "duplicate"
  - recording_id: "duplicate"
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="存在且唯一"):
        load_calibration_evidence(path)
