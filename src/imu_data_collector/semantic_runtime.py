"""Portable kinematic evidence and duration-aware decoding; no contact truth."""

from __future__ import annotations

from copy import deepcopy
from itertools import pairwise

import numpy as np

FEATURE_RECIPE = "body22-semantic-v1"
DECODER_VERSION = "semantic-duration-v1"
FEATURE_NAMES = (
    "torso_vertical_cos",
    "pelvis_above_feet",
    "left_knee_flexion",
    "right_knee_flexion",
    "left_hip_flexion",
    "right_hip_flexion",
    "root_horizontal_speed",
    "root_vertical_speed",
    "leg_speed",
    "leg_opposition",
    "foot_separation",
    "head_above_feet",
    "knees_above_feet",
    "hands_above_feet",
    "torso_above_feet",
    "wrist_speed",
    "leg_relative_speed",
    "velocity_valid",
    "geometry_valid",
)
PROTECTED = {"fall", "stumble_recovery", "jumping", "posture_transition", "other_activity"}


def semantic_features(joints, valid):
    """Rotation/translation invariant features; distances in leg lengths.

    Feet are a relative reference, not a measured floor. Speeds are leg lengths/s.
    Derivatives never cross an invalid frame. No source names or labels are inputs.
    """
    x, valid = np.asarray(joints, np.float64), np.asarray(valid, bool)
    if x.shape != (len(valid), 22, 3) or not len(x) or not np.isfinite(x).all():
        raise ValueError("Expected finite body22 geometry")

    def norm(v):
        return np.linalg.norm(v, axis=-1)

    lengths = (
        norm(x[:, 1] - x[:, 4])
        + norm(x[:, 4] - x[:, 7])
        + norm(x[:, 2] - x[:, 5])
        + norm(x[:, 5] - x[:, 8])
    ) / 2
    size = max(0.25, float(lengths[valid].mean())) if valid.any() else 1.0
    torso = x[:, 12] - x[:, 0]
    good = valid & (lengths > 0.25) & (norm(torso) > 0.05)
    continuous = good & np.r_[False, good[:-1]]
    velocity = np.vstack([np.zeros((1, 22, 3)), np.diff(x, axis=0)]) * 25 / size
    velocity[~continuous] = 0
    rel_v = velocity - velocity[:, :1]
    floor_proxy = np.minimum(x[:, 10, 2], x[:, 11, 2])

    def height(j):
        return (x[:, j, 2] - floor_proxy) / size

    def cosine(a, b):
        return (a * b).sum(-1) / np.maximum(norm(a) * norm(b), 1e-8)

    def flex(a, b, c):
        return (1 + cosine(x[:, a] - x[:, b], x[:, c] - x[:, b])) / 2

    lv, rv = rel_v[:, 7], rel_v[:, 8]
    values = np.column_stack(
        [
            torso[:, 2] / np.maximum(norm(torso), 1e-8),
            height(0),
            flex(1, 4, 7),
            flex(2, 5, 8),
            flex(12, 1, 4),
            flex(12, 2, 5),
            norm(velocity[:, 0, :2]),
            velocity[:, 0, 2],
            norm(velocity[:, [4, 5, 7, 8, 10, 11]]).max(1),
            -cosine(lv, rv),
            norm((x[:, 7] - x[:, 8])[:, :2]) / size,
            height(15),
            np.minimum(height(4), height(5)),
            np.minimum(height(20), height(21)),
            np.minimum(height(0), height(12)),
            norm(velocity[:, [20, 21]]).max(1),
            norm(lv - rv),
            continuous,
            good,
        ]
    )
    values[~good] = 0
    return np.clip(values, -10, 10).astype(np.float32)


def semantic_bias(features, labels):
    """Bounded evidence, never a hard posture or contact classifier."""
    f = np.asarray(features)
    if f.ndim != 2 or f.shape[1] != len(FEATURE_NAMES) or not np.isfinite(f).all():
        raise ValueError("Invalid semantic features")
    out = np.zeros((len(f), len(labels)), np.float64)
    still = np.clip((0.18 - f[:, 8]) / 0.18, 0, 1) * f[:, 17]
    upright = np.clip((f[:, 0] - 0.65) / 0.3, 0, 1)
    tall = np.clip((f[:, 1] - 0.65) / 0.25, 0, 1)
    horizontal = np.clip((0.45 - np.abs(f[:, 0])) / 0.4, 0, 1)
    alternating = np.clip(f[:, 9], 0, 1) * np.clip((f[:, 16] - 0.2) / 0.8, 0, 1)
    for i, code in enumerate(labels):
        if code in ("walking", "running", "stair_up", "stair_down"):
            out[:, i] = -2 * still + 0.4 * upright * alternating
        elif code == "standing":
            out[:, i] = 0.6 * still * upright * tall - 0.6 * horizontal
        elif code == "bending":
            out[:, i] = 0.5 * (1 - upright) * tall - 0.4 * (f[:, 1] < 0.35)
        elif code == "lying":
            out[:, i] = 0.5 * horizontal * (f[:, 14] < 0.3) - 0.5 * upright * tall
    return out * f[:, 18, None]


def duration_path(emission, valid, matrix, weight, minimum, boundary=None, strength=0.4):
    """Viterbi with capped state ages and a finite short-run exit penalty.

    No minimum is applied at clip/invalid-run edges. No maximum duration exists.
    A boundary prediction can reduce, but never increase, the switching cost.
    """
    t_count, k = emission.shape
    d = max(1, int(max(minimum, default=0)))
    result = np.full(t_count, -1, np.int64)
    transition = weight * np.log(matrix)
    ages = np.arange(1, d + 1)[None, :]
    penalty = strength * np.maximum(0, 1 - ages / np.maximum(1, np.array(minimum)[:, None]))
    edges = np.flatnonzero(np.diff(np.r_[False, valid, False]))
    for a, b in zip(edges[::2], edges[1::2], strict=True):
        delta = np.full((k, d), -1e12)
        # A recording may start halfway through an action.
        delta[:, -1] = emission[a]
        back = np.empty((b - a, k, d), np.int32)
        for t in range(a + 1, b):
            boundary_weight = 1 - 0.75 * (float(boundary[t]) if boundary is not None else 0)
            exit_values = delta - penalty * boundary_weight
            best_age = exit_values.argmax(1)
            choices = exit_values[np.arange(k), best_age][:, None] + transition * boundary_weight
            np.fill_diagonal(choices, -1e12)
            previous_class = choices.argmax(0)
            new = np.full_like(delta, -1e12)
            new[:, 0] = choices[previous_class, np.arange(k)]
            back[t - a, :, 0] = previous_class * d + best_age[previous_class]
            for age in range(1, d):
                new[:, age] = delta[:, age - 1] + transition.diagonal()
                back[t - a, :, age] = np.arange(k) * d + age - 1
            stay = delta[:, -1] + transition.diagonal()
            improve = stay > new[:, -1]
            new[improve, -1] = stay[improve]
            back[t - a, improve, -1] = np.flatnonzero(improve) * d + d - 1
            delta = new + emission[t, :, None]
        state = int(delta.argmax())
        for t in range(b - 1, a - 1, -1):
            code, age = divmod(state, d)
            result[t] = code
            if t > a:
                state = int(back[t - a, code, age])
    return result


def semantic_decode(
    scores, valid, metadata, *, features, boundary_scores, outside_scores, base_decoder
):
    cfg, labels = metadata["decoder"], metadata["labels"]
    if cfg.get("feature_recipe") != FEATURE_RECIPE or features is None:
        raise ValueError("Semantic decoder requires its bound feature recipe")
    scores, valid = np.asarray(scores, float), np.asarray(valid, bool)
    f = np.asarray(features)
    if f.shape != (len(valid), len(FEATURE_NAMES)):
        raise ValueError("Semantic clock differs")
    weight, duration, gap = (
        cfg.get(k, 0) for k in ("semantic_weight", "minimum_seconds", "gap_fill_seconds")
    )
    if (
        weight not in (0.0, 0.5, 1.0)
        or duration not in (0.0, 0.2, 0.4)
        or gap not in (0.0, 0.2, 0.4)
    ):
        raise ValueError("Semantic decoder outside frozen experiment grid")
    if boundary_scores is not None:
        boundary_scores = np.asarray(boundary_scores)
        if (
            boundary_scores.shape != valid.shape
            or not np.isfinite(boundary_scores).all()
            or ((boundary_scores < 0) | (boundary_scores > 1)).any()
        ):
            raise ValueError("Invalid boundary scores")
    biased = scores * np.exp(weight * semantic_bias(f, labels))
    biased /= np.maximum(biased.sum(1, keepdims=True), 1e-12)
    base = deepcopy(metadata)
    base["decoder"].update(
        kind="offline-hmm-v1", gap_fill_seconds=0.0, candidate_min_local_score=0.0
    )
    decoded = base_decoder(biased, valid, base, outside_scores=outside_scores)
    mask = decoded["valid"]
    minimum = [0 if code in PROTECTED else round(duration * 25) for code in labels]
    emission = np.log(np.maximum(biased, 1e-30)) / cfg["temperature"]
    emission[:, ~np.isin(np.arange(len(labels)), metadata["supported_indices"])] = -1e12
    path = duration_path(
        emission,
        mask,
        np.asarray(cfg["transition"]),
        cfg["transition_weight"],
        minimum,
        boundary_scores,
    )
    rows, ix = np.arange(len(valid)), np.maximum(path, 0)
    raw = scores / np.maximum(scores.sum(1, keepdims=True), 1e-12)
    reliability = np.minimum(raw[rows, ix], decoded["sequence_scores"][rows, ix])
    proposal = path.copy()
    proposal[reliability < cfg.get("candidate_min_local_score", 0.45)] = -1
    proposal[~mask] = -1
    filled = np.zeros(len(valid), bool)
    edges = np.r_[0, np.flatnonzero(np.diff(proposal)) + 1, len(proposal)]
    for a, b in pairwise(edges):
        if (
            not gap
            or a == 0
            or b == len(proposal)
            or proposal[a] != -1
            or b - a > round(gap * 25)
            or not mask[a:b].all()
        ):
            continue
        if proposal[a - 1] < 0 or proposal[b] < 0:
            continue
        # Never invent or join independent fall/recovery events through a hole.
        if any(
            labels[i] in {"fall", "stumble_recovery", "other_activity"}
            for i in [proposal[a - 1], proposal[b], *path[a:b]]
            if i >= 0
        ):
            continue
        proposal[a:b] = path[a:b]
        filled[a:b] = True
    cutoff = np.array(
        [cfg.get("calibration", {}).get("high_thresholds", {}).get(code, 2.0) for code in labels]
    )
    high = proposal.copy()
    high[(reliability < cutoff[np.maximum(proposal, 0)]) | filled | ~mask] = -1
    if cfg.get("calibration", {}).get("status") != "calibrated":
        high[:] = -1
    return {
        **decoded,
        "class_indices": proposal,
        "high_confidence_indices": high,
        "local_scores": raw.astype(np.float32),
        "reliability": reliability.astype(np.float32),
        "semantic_features": f.astype(np.float32),
        "inferred_fill": filled,
        "semantic_adjusted": mask & (proposal >= 0) & (proposal != raw.argmax(1)),
    }


def interval_evidence(decoded, start, stop, code):
    """Observable signals and logged edits, not a neural causal explanation."""
    if "semantic_features" not in decoded or stop <= start:
        return []
    f = decoded["semantic_features"][start:stop]
    f = f[f[:, 18] > 0]
    evidence = []
    filled = int(decoded["inferred_fill"][start:stop].sum())
    if filled:
        evidence.append(
            {
                "kind": "adjustment",
                "code": "short_gap_fill",
                "seconds": round(filled / 25, 3),
                "text": f"含 {filled / 25:.2f} 秒短未知补齐，仍属于推断候选",
            }
        )
    if len(f):
        if code in ("walking", "running", "standing", "stair_up", "stair_down"):
            speed = float(np.median(f[:, 8]))
            evidence.append(
                {
                    "kind": "observation",
                    "code": "leg_speed",
                    "value": speed,
                    "unit": "leg-length/s",
                    "text": f"下肢运动中位速度 {speed:.2f} 腿长／秒",
                }
            )
        elif code in ("bending", "lying", "ground_support", "sitting", "squatting", "kneeling"):
            angle = float(np.degrees(np.arccos(np.clip(np.median(f[:, 0]), -1, 1))))
            evidence.append(
                {
                    "kind": "observation",
                    "code": "torso_tilt",
                    "value": angle,
                    "unit": "deg",
                    "text": f"躯干与竖直方向中位夹角 {angle:.0f}°，仅为姿态线索",
                }
            )
        elif code == "posture_transition":
            span = float(np.ptp(f[:, 1]))
            evidence.append(
                {
                    "kind": "observation",
                    "code": "height_change",
                    "value": span,
                    "unit": "leg-length",
                    "text": f"骨盆相对足部高度变化 {span:.2f} 腿长，不代表支撑力",
                }
            )
    changed = int(decoded["semantic_adjusted"][start:stop].sum())
    if changed:
        evidence.append(
            {
                "kind": "adjustment",
                "code": "sequence_adjustment",
                "seconds": round(changed / 25, 3),
                "text": f"{changed / 25:.2f} 秒的候选类别经过运动学及前后文联合调整",
            }
        )
    return evidence[:3]
