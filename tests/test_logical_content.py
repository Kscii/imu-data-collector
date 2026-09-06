from __future__ import annotations

import h5py
import numpy as np

from imu_data_collector.logical_content import logical_content_sha256


def test_logical_digest_matches_shared_sequence_local_contract() -> None:
    text = h5py.string_dtype("utf-8")
    sequences = np.asarray(
        [
            (
                0,
                2,
                "cw12eu:r1",
                "cw12eu:subject-001",
                "cw12eu:r1",
                "chest",
                "walking",
                False,
                "temporal",
                24.9,
            ),
            (
                2,
                4,
                "cw12eu:r2",
                "cw12eu:subject-002",
                "cw12eu:r2",
                "chest",
                "forward_fall",
                True,
                "temporal",
                25.1,
            ),
        ],
        dtype=np.dtype(
            [
                ("sample_start", "<i8"),
                ("sample_stop", "<i8"),
                ("source_file", text),
                ("participant_id", text),
                ("recording_id", text),
                ("body_location", text),
                ("activity_code", text),
                ("is_fall", "?"),
                ("supervision_kind", text),
                ("source_sampling_rate_hz", "<f8"),
            ]
        ),
    )
    annotations = np.asarray(
        [
            (0, "activity", 0, 2, "walking"),
            (1, "activity", 0, 2, "forward_fall"),
            (1, "onset", 0, 0, "forward_fall"),
            (1, "impact", 1, 1, "forward_fall"),
        ],
        dtype=np.dtype(
            [
                ("sequence_index", "<i4"),
                ("kind", text),
                ("start_sample", "<i8"),
                ("stop_sample", "<i8"),
                ("code", text),
            ]
        ),
    )
    values = np.arange(24, dtype=np.float32).reshape(4, 6) / 10

    assert logical_content_sha256(
        values,
        sequences,
        annotations,
        dataset_id="cw12eu",
        sampling_rate_hz=25.0,
    ) == "2d54af705392da8a73f218066c75f21533417a4a7fe192844ee023e3ff25e6c8"
