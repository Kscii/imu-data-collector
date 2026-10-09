"""Explicit, fail-closed cloud batch admission with a shared wall-clock deadline."""

import math
import time

from .motion_annotations import require
from .production_gate import production_gate
from .synthetic_temporal_contract import SHA256, document_sha256


def batch_policy(service, release, *, at=None, control=None):
    if control is None:
        control, _ = service.optional(service.root + "/batch-control.json")
    if not control or control.get("mode") == "paused" or not release:
        return None
    at = time.time() if at is None else at
    require(control.get("schema") == "imu.motion_batch.v1", "Invalid batch control")
    require(
        control.get("release_sha256") == document_sha256(release), "Batch model release changed"
    )
    mode = control.get("mode")
    require(mode in ("sample", "full", "incremental"), "Invalid batch mode")
    if mode != "incremental":
        start, deadline = control.get("started_unix"), control.get("deadline_unix")
        require(
            all(type(v) in (int, float) and math.isfinite(v) for v in (start, deadline))
            and 0 < deadline - start <= 7200,
            "Shared batch budget must be within two hours",
        )
        if at < start or at >= deadline:
            return None
    motions = control.get("motion_sha256", [])
    require(
        isinstance(motions, list)
        and len(motions) == len(set(motions))
        and all(isinstance(m, str) and SHA256.fullmatch(m) for m in motions),
        "Invalid batch motion allowlist",
    )
    require(bool(motions) or mode == "incremental", "Batch requires an explicit motion allowlist")
    if mode == "sample":
        require(len(motions) <= 100, "Cloud sample is limited to 100 motions")
    else:
        # An existing baseline may serve and be rolled back, but cannot open the
        # first full batch or unbounded incremental inference.
        production_gate(release["model"].get("validation") or {})
        if mode == "full":
            forecast = control.get("forecast", {})
            require(
                forecast.get("sample_count") == 100
                and forecast.get("cloud_measured") is True
                and forecast.get("release_sha256") == document_sha256(release),
                "Need a 100-motion cloud measurement of the final release",
            )
            estimate = forecast.get("total_seconds_with_margin")
            require(
                type(estimate) in (int, float)
                and math.isfinite(estimate)
                and 0 < estimate <= deadline - start
                and forecast.get("margin_fraction", 0) >= 0.3,
                "Forecast plus 30% margin exceeds the shared two-hour budget",
            )
    return control


def configure_batch(service, control, expected_sha256, actor):
    from .motion_label_models import active_model
    from .motion_label_service import now
    from .storage import ObjectConflictError

    with service.publication_lock():
        prior, generation = service.optional(service.root + "/batch-control.json")
        if (document_sha256(prior) if prior else None) != expected_sha256:
            raise ObjectConflictError("批处理计划已改变，请重新加载")
        require(control.get("schema") == "imu.motion_batch.v1", "Invalid batch control")
        if control.get("mode") != "paused":
            require(
                batch_policy(service, active_model(service), control=control) is not None,
                "Batch deadline expired or no active release",
            )
        value = {**control, "configured_by": actor, "configured_at_utc": now()}
        service.reference("batch-plans", value)
        service.store.write_json(
            service.root + "/batch-control.json", value, if_generation_match=generation
        )
        return value


def forecast_batch(sample, population, *, elapsed_seconds, release_sha256):
    """Length- and source-weighted sequential upper estimate; includes spent time.

    Each sample supplies its actual end-to-end seconds (prepare/upload/discover/
    inference/persist). No Ubuntu-only inference timing is a cloud measurement.
    """
    require(len(sample) == 100, "Exactly 100 cloud samples are required")
    seen = set()
    for row in sample:
        require(
            row["motion_sha256"] not in seen and row.get("cloud_measured") is True,
            "Cloud sample identities must be unique",
        )
        seen.add(row["motion_sha256"])
        require(row["frames"] > 0 and row["end_to_end_seconds"] > 0, "Invalid cloud timing")

    def stratum(row):
        # Explicit native-frame bands capture expensive FK on long/high-rate sources.
        return row["source_dataset"], min(3, int(math.log2(max(1, row["frames"] / 1000))))

    rates = {}
    for row in sample:
        rates.setdefault(stratum(row), []).append(row["end_to_end_seconds"] / row["frames"])
    remainder = 0.0
    for row in population:
        if row["motion_sha256"] in seen:
            continue
        values = rates.get(stratum(row))
        if not values:
            # Missing length band uses the slowest measured rate of the source.
            values = [
                v
                for (source, _), group in rates.items()
                if source == row["source_dataset"]
                for v in group
            ]
        require(bool(values), "Every source needs a cloud sample")
        remainder += max(values) * row["frames"]
    return {
        "sample_count": 100,
        "cloud_measured": True,
        "release_sha256": release_sha256,
        "elapsed_seconds": elapsed_seconds,
        "remaining_estimate_seconds": remainder,
        "margin_fraction": 0.3,
        "total_seconds_with_margin": elapsed_seconds + remainder * 1.3,
    }
