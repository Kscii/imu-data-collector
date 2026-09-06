"""Canonical container-independent digest for v3 training HDF5 content."""

from __future__ import annotations

import hashlib
import json

import numpy as np


def _text(value: object) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def logical_content_sha256(
    values: np.ndarray,
    sequences: np.ndarray,
    annotations: np.ndarray,
    *,
    dataset_id: str,
    sampling_rate_hz: float,
) -> str:
    """Hash ordered sequence-local content using the shared benchmark contract.

    Global table offsets and sequence indexes are deliberately excluded. Each
    sequence contributes its own metadata, local annotations, shape, and little
    endian float32 samples. This is the algorithm used by the benchmark data
    writer, so a per-recording export and the same recording inside a merged H5
    have one stable logical identity.
    """

    sample_values = np.asarray(values)
    if sample_values.ndim != 2 or sample_values.shape[1] != 6:
        raise ValueError("samples must have shape [N, 6]")

    digest = hashlib.sha256()
    kind_order = {"activity": 0, "onset": 1, "impact": 2, "exclude": 3}
    for sequence_index, row in enumerate(sequences):
        start = int(row["sample_start"])
        stop = int(row["sample_stop"])
        local_annotations = sorted(
            (
                {
                    "kind": _text(item["kind"]),
                    "start_sample": int(item["start_sample"]),
                    "stop_sample": int(item["stop_sample"]),
                    "code": _text(item["code"]),
                }
                for item in annotations
                if int(item["sequence_index"]) == sequence_index
            ),
            key=lambda item: (
                item["start_sample"],
                kind_order.get(str(item["kind"]), 99),
                item["stop_sample"],
                item["code"],
            ),
        )
        metadata = {
            "dataset_id": dataset_id,
            "source_file": _text(row["source_file"]),
            "participant_id": _text(row["participant_id"]),
            "recording_id": _text(row["recording_id"]),
            "body_location": _text(row["body_location"]),
            "activity": _text(row["activity_code"]),
            "is_fall": bool(row["is_fall"]),
            "sampling_rate_hz": float(sampling_rate_hz),
            "original_sampling_rate_hz": float(row["source_sampling_rate_hz"]),
            "supervision_kind": _text(row["supervision_kind"]),
            "annotations": local_annotations,
        }
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        sequence_values = np.asarray(sample_values[start:stop], dtype="<f4", order="C")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)
        digest.update(np.asarray(sequence_values.shape, dtype="<i8").tobytes())
        digest.update(sequence_values.tobytes())
    return digest.hexdigest()
