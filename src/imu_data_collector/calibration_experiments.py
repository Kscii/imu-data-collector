"""Versioned, local calibration evidence. Raw capture HDF5 remains unchanged."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import h5py
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from imu_data_collector.hdf5_store import sha256_file

SCHEMA = "imu_calibration_experiment_v1"
ID_PATTERN = r"^cal-[0-9a-f]{32}$"
DIRECTIONS = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")
MINIMUMS = {"accel_fit": 3, "accel_validation": 1, "gyro_fit": 5, "gyro_validation": 2}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_bytes(json_bytes(value))
    temporary.replace(path)


class ExperimentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sensor_sn: str = Field(pattern=r"^IMU-[0-9]{4}-R[0-9]{2}$")
    operator_id: str = Field(pattern=r"^[a-z][a-z0-9]{3,15}$")
    directions: dict[str, str]
    orientation_session_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def check_directions(self):
        if set(self.directions) != set(DIRECTIONS):
            raise ValueError("Describe all six directions: +X, -X, +Y, -Y, +Z, -Z")
        if any(not text.strip() or len(text) > 200 for text in self.directions.values()):
            raise ValueError("Each direction needs a description of 1–200 characters")
        self.directions = {key: value.strip() for key, value in self.directions.items()}
        return self


class TrialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["accel", "gyro"]
    role: Literal["fit", "validation"]
    axis: Literal["X", "Y", "Z"]
    sign: Literal[-1, 1]
    notes: str = Field(default="", max_length=2000)

    @property
    def reference_angle_deg(self) -> int | None:
        return self.sign * (360 if self.role == "fit" else 720) if self.kind == "gyro" else None


class TrialExclusion(BaseModel):
    excluded: bool
    reason: str = Field(min_length=1, max_length=1000)


class ExperimentStore:
    def __init__(self, data_root: Path):
        self.data_root = data_root.resolve()
        self.root = self.data_root / "_calibration_experiments"

    def directory(self, experiment_id: str) -> Path:
        if not re.fullmatch(ID_PATTERN, experiment_id):
            raise ValueError("Invalid experiment ID")
        return self.root / experiment_id

    def create(self, request: ExperimentCreate, device: dict) -> dict:
        value = {
            "schema_version": SCHEMA,
            "experiment_id": f"cal-{uuid.uuid4().hex}",
            "created_at_utc": utc_now(),
            **request.model_dump(mode="json"),
            "device": device,
            "training_eligible": False,
            "data_tier": "test",
            "minimums_per_axis_direction": MINIMUMS,
            "sources": [],
            "trials": [],
            "reports": [],
        }
        self.save(value)
        return value

    def save(self, value: dict) -> None:
        atomic_json(self.directory(value["experiment_id"]) / "experiment.json", value)

    def read(self, experiment_id: str) -> dict:
        return json.loads(
            (self.directory(experiment_id) / "experiment.json").read_text(encoding="utf-8")
        )

    def list(self) -> list[dict]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob("cal-*/experiment.json"), reverse=True)
        ]

    def source_path(self, source: dict) -> Path:
        path = (self.data_root / source["local_path"]).resolve()
        if not path.is_relative_to(self.data_root / "_diagnostics"):
            raise ValueError("Raw evidence must be inside the diagnostics directory")
        return path

    def analyze(self, experiment: dict) -> dict:
        arrays = {}
        warnings = []
        for source in experiment["sources"]:
            try:
                with h5py.File(self.source_path(source), "r") as handle:
                    group = handle["imu/samples"]
                    arrays[source["recording_id"]] = {
                        "raw": np.asarray(group["raw_counts"], dtype=np.float64),
                        "time_ns": np.asarray(group["recording_time_ns"], dtype=np.int64),
                        "device_ms": np.asarray(group["device_time_ms"], dtype=np.int64)
                        if "device_time_ms" in group
                        else None,
                        "epoch": np.asarray(group["device_clock_epoch"])
                        if "device_clock_epoch" in group
                        else None,
                    }
            except (OSError, KeyError) as error:
                warnings.append(
                    f"Source unavailable: {source['recording_id']} ({type(error).__name__})"
                )
        return analyze_experiment(experiment, arrays, warnings)

    def snapshot(self, experiment_id: str) -> tuple[dict, Path]:
        experiment = self.read(experiment_id)
        for source in experiment["sources"]:
            path = self.source_path(source)
            if ".partial." in path.name:
                source["interrupted"] = True
            source.update(sha256=sha256_file(path), size_bytes=path.stat().st_size)
        report = self.analyze(experiment)
        payload = json_bytes(report)
        digest = hashlib.sha256(payload).hexdigest()
        path = self.directory(experiment_id) / f"report-{digest}.json"
        if not path.exists():
            path.write_bytes(payload)
        if not any(item["sha256"] == digest for item in experiment["reports"]):
            experiment["reports"].append({"sha256": digest, "created_at_utc": utc_now()})
        self.save(experiment)
        return report, path


def _median(values: list) -> Any:
    return np.median(np.asarray(values), axis=0) if values else None


def analyze_experiment(experiment: dict, sources: dict, warnings: list[str] | None = None) -> dict:
    """Fit only fit trials; evaluate held-out trials using the same fixed SI profile."""
    warnings = list(warnings or [])
    if any(source.get("interrupted") for source in experiment["sources"]):
        warnings.append("interrupted_capture_review_raw_evidence")
    rows = []
    series = {}
    for trial in experiment["trials"]:
        row = {**trial, "warnings": [], "result": {}}
        rows.append(row)
        if trial["status"] != "complete" or trial.get("excluded"):
            continue
        source = sources.get(trial["recording_id"])
        if source is None:
            row["warnings"].append("raw_source_unavailable")
            continue
        phases = {}
        for phase, bounds in trial["phases"].items():
            # Stage times and reconstructed recording times share the host monotonic origin.
            mask = (source["time_ns"] >= bounds["start_ns"]) & (
                source["time_ns"] < bounds["end_ns"]
            )
            phases[phase] = {
                key: value[mask] if value is not None else None for key, value in source.items()
            }
        measurement = phases.get("measure", {})
        raw = measurement.get("raw", np.empty((0, 6)))
        row["result"]["sample_count"] = len(raw)
        if not len(raw):
            row["warnings"].append("no_measurement_samples")
            continue
        row["result"]["median_counts"] = np.median(raw, axis=0).tolist()
        row["result"]["std_counts"] = np.std(raw, axis=0).tolist()
        if np.any((raw <= -32768) | (raw >= 32767)):
            row["warnings"].append("possible_sensor_saturation")
        if trial["kind"] == "gyro":
            clock = measurement.get("device_ms")
            epoch = measurement.get("epoch")
            if (
                clock is None
                or len(clock) < 2
                or np.any(np.diff(clock) <= 0)
                or (epoch is not None and len(np.unique(epoch)) != 1)
            ):
                row["warnings"].append("invalid_device_clock_integral_unavailable")
                continue
            dt = np.diff(clock)
            if np.any(dt > 3 * np.median(dt)):
                row["warnings"].append("possible_missing_samples")
            if any(len(phases.get(name, {}).get("raw", [])) < 2 for name in ("before", "after")):
                row["warnings"].append("stationary_bias_windows_unavailable")
                continue
        series[trial["trial_id"]] = phases

    fitting = [row for row in rows if row["role"] == "fit" and row["trial_id"] in series]
    accel = [row for row in fitting if row["kind"] == "accel"]
    # The first complete six-face set fixes the mapping. Validation never selects axes.
    first = {(row["axis"], row["sign"]): row for row in reversed(accel)}
    order, signs = [0, 1, 2], [1, 1, 1]
    mapping_available = len(first) == 6
    accel_scale, gyro_scale = None, None
    accel_bias, gyro_bias = np.zeros(3), np.zeros(3)
    axis_scales = {}
    if mapping_available:
        deltas = np.array(
            [
                np.array(first[(axis, 1)]["result"]["median_counts"][:3])
                - first[(axis, -1)]["result"]["median_counts"][:3]
                for axis in "XYZ"
            ]
        )
        order = list(
            max(
                itertools.permutations(range(3)),
                key=lambda perm: sum(abs(deltas[i, perm[i]]) for i in range(3)),
            )
        )
        signs = [1 if deltas[i, order[i]] >= 0 else -1 for i in range(3)]
        if any(
            np.argmax(np.abs(deltas[i])) != order[i] or abs(deltas[i, order[i]]) < 1
            for i in range(3)
        ):
            warnings.append("axis_mapping_ambiguous_check_physical_directions")
        for i, axis in enumerate("XYZ"):
            pos = _median(
                [
                    row["result"]["median_counts"][:3]
                    for row in accel
                    if row["axis"] == axis and row["sign"] == 1
                ]
            )
            neg = _median(
                [
                    row["result"]["median_counts"][:3]
                    for row in accel
                    if row["axis"] == axis and row["sign"] == -1
                ]
            )
            value = float((pos[order[i]] - neg[order[i]]) * signs[i] / 2)
            axis_scales[axis] = value
            accel_bias[order[i]] = (pos[order[i]] + neg[order[i]]) / 2
        positive_scales = [value for value in axis_scales.values() if value > 0]
        accel_scale = float(np.median(positive_scales)) if positive_scales else None
    else:
        warnings.append("six_fitting_faces_needed_for_axis_mapping")

    gyro_fits = [row for row in fitting if row["kind"] == "gyro"]
    biases = []
    gyro_estimates = []
    for row in gyro_fits:
        phases = series[row["trial_id"]]
        bias = np.median(
            [np.median(phases[name]["raw"][:, 3:], axis=0) for name in ("before", "after")], axis=0
        )
        biases.append(bias)
        if mapping_available:
            i = "XYZ".index(row["axis"])
            measure = phases["measure"]
            seconds = (measure["device_ms"] - measure["device_ms"][0]) / 1000
            integral = np.trapezoid(measure["raw"][:, 3:] - bias, seconds, axis=0)
            estimate = float(integral[order[i]] * signs[i] / row["reference_angle_deg"])
            row["result"]["gyro_counts_per_dps_estimate"] = estimate
            if np.argmax(np.abs(integral)) != order[i] or estimate <= 0:
                row["warnings"].append("rotation_axis_or_direction_conflicts_with_shared_mapping")
            if estimate > 0:
                gyro_estimates.append(estimate)
    if biases:
        gyro_bias = np.median(biases, axis=0)
    if gyro_estimates:
        gyro_scale = float(np.median(gyro_estimates))

    raw_axes = (
        (experiment.get("orientation_setup") or {}).get("axis_definition") == "raw_accelerometer"
    )
    if raw_axes and mapping_available and (order != [0, 1, 2] or signs != [1, 1, 1]):
        warnings.append("raw_axis_setup_conflicts_with_fitting_faces")
        # Do not silently reinterpret named housing faces for native-axis experiments.
        mapping_available = False
        accel_scale = gyro_scale = None

    candidate = {
        "verified": False,
        "accel_counts_per_g": accel_scale,
        "gyro_counts_per_dps": gyro_scale,
        "accel_bias_counts": accel_bias.tolist(),
        "gyro_bias_counts": gyro_bias.tolist(),
        "raw_axis_order": order,
        "axis_signs": signs,
        "coordinate_system": experiment["directions"],
        "method": "six_face_medians_and_device_clock_known_angle_v1",
    }
    candidate_hash = hashlib.sha256(json_bytes(candidate)).hexdigest()
    for row in rows:
        phases = series.get(row["trial_id"])
        if phases is None or not mapping_available:
            continue
        measure = phases["measure"]
        i = "XYZ".index(row["axis"])
        row["result"]["candidate_sha256"] = candidate_hash
        if row["kind"] == "accel" and accel_scale:
            vector = (
                (np.median(measure["raw"][:, :3], axis=0) - accel_bias)[order] * signs / accel_scale
            )
            target = np.zeros(3)
            target[i] = row["sign"]
            row["result"].update(
                acceleration_g=vector.tolist(),
                vector_error_g=float(np.linalg.norm(vector - target)),
            )
        if row["kind"] == "gyro" and gyro_scale:
            seconds = (measure["device_ms"] - measure["device_ms"][0]) / 1000
            # Deliberately use the fixed *fitted* bias, including on validation trials.
            angles = np.trapezoid(
                (measure["raw"][:, 3:] - gyro_bias)[:, order] * signs / gyro_scale, seconds, axis=0
            )
            row["result"].update(
                angle_deg=float(angles[i]),
                angle_error_deg=float(angles[i] - row["reference_angle_deg"]),
                cross_axis_angle_deg=angles.tolist(),
            )

    counts = {}
    for kind, role in itertools.product(("accel", "gyro"), ("fit", "validation")):
        for axis, sign in itertools.product("XYZ", (1, -1)):
            key = f"{kind}_{role}_{'+' if sign == 1 else '-'}{axis}"
            counts[key] = sum(
                row["kind"] == kind
                and row["role"] == role
                and row["axis"] == axis
                and row["sign"] == sign
                and row["trial_id"] in series
                for row in rows
            )
            if counts[key] < MINIMUMS[f"{kind}_{role}"]:
                warnings.append(
                    f"below_recommended_minimum:{key}:{counts[key]}/{MINIMUMS[f'{kind}_{role}']}"
                )
    if any(row["warnings"] for row in rows):
        warnings.append("review_trial_warnings")
    return {
        "schema_version": SCHEMA,
        "experiment_id": experiment["experiment_id"],
        "created_at_utc": experiment["created_at_utc"],
        "sensor_sn": experiment["sensor_sn"],
        "operator_id": experiment["operator_id"],
        "device": experiment["device"],
        "directions": experiment["directions"],
        "orientation_setup": experiment.get("orientation_setup"),
        "notes": experiment["notes"],
        "training_eligible": False,
        "data_tier": "test",
        "sources": [
            {key: value for key, value in source.items() if key != "local_path"}
            for source in experiment["sources"]
        ],
        "trials": rows,
        "candidate": candidate,
        "candidate_sha256": candidate_hash,
        "axis_mapping_available": mapping_available,
        "gyro_bias_available": bool(biases),
        "accel_axis_counts_per_g": axis_scales,
        "gyro_scale_spread_counts_per_dps": float(np.std(gyro_estimates))
        if gyro_estimates
        else None,
        "minimums_per_axis_direction": MINIMUMS,
        "completed_counts": counts,
        "warnings": warnings,
    }
