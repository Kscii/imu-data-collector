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
PROTECTED = {
    "fall",
    "stumble_recovery",
    "jumping",
    "posture_transition",
    "other_activity",
}


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
            out[:, i] = (
                0.6 * still * tall
                if "posture_transition" in labels and "bending" not in labels
                else 0.6 * still * upright * tall - 0.6 * horizontal
            )
        elif code == "bending":
            out[:, i] = 0.5 * (1 - upright) * tall - 0.4 * (f[:, 1] < 0.35)
        elif code == "lying":
            out[:, i] = 0.5 * horizontal * (f[:, 14] < 0.3) - 0.5 * upright * tall
    return out * f[:, 18, None]


def duration_path(
    emission,
    valid,
    matrix,
    weight,
    minimum,
    boundary=None,
    strength=0.4,
    hard_minimum=1,
):
    """Viterbi with capped state ages and a finite short-run exit penalty.

    Soft preferences allow clip edges; hard_minimum applies at every valid edge.
    No maximum duration exists.
    A boundary prediction can reduce, but never increase, the switching cost.
    """
    t_count, k = emission.shape
    d = max(hard_minimum, int(max(minimum, default=0)), 1)
    result = np.full(t_count, -1, np.int64)
    transition = weight * np.log(matrix)
    ages = np.arange(1, d + 1)[None, :]
    penalty = strength * np.maximum(0, 1 - ages / np.maximum(1, np.array(minimum)[:, None]))
    edges = np.flatnonzero(np.diff(np.r_[False, valid, False]))
    for a, b in zip(edges[::2], edges[1::2], strict=True):
        if b - a < hard_minimum:
            continue
        delta = np.full((k, d), -np.inf)
        # A recording may start halfway through an action.
        delta[:, 0 if hard_minimum > 1 else -1] = emission[a]
        back = np.empty((b - a, k, d), np.int32)
        for t in range(a + 1, b):
            boundary_weight = 1 - 0.75 * (float(boundary[t]) if boundary is not None else 0)
            exit_values = delta - penalty * boundary_weight
            exit_values[:, : hard_minimum - 1] = -np.inf
            best_age = exit_values.argmax(1)
            choices = exit_values[np.arange(k), best_age][:, None] + transition * boundary_weight
            np.fill_diagonal(choices, -np.inf)
            previous_class = choices.argmax(0)
            new = np.full_like(delta, -np.inf)
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
        delta[:, : hard_minimum - 1] = -np.inf
        if not np.isfinite(delta).any():
            continue
        state = int(delta.argmax())
        for t in range(b - 1, a - 1, -1):
            code, age = divmod(state, d)
            result[t] = code
            if t > a:
                state = int(back[t - a, code, age])
    return result


SEGMENTATION_VERSION = "product-duration-20261010-v3"
REFINEMENT_VERSION = "product-refinement-20261010-v1"
SOFT_SECONDS = {
    **dict.fromkeys(
        ("standing", "sitting", "lying", "squatting", "kneeling", "walking", "running"),
        0.6,
    ),
    **dict.fromkeys(("stair_up", "stair_down"), 0.8),
    **dict.fromkeys(("ground_support", "bending", "other_activity"), 0.4),
    **dict.fromkeys(("posture_transition", "jumping"), 0.32),
    "fall": 0.0,
    "stumble_recovery": 0.0,
}


def segmentation_policy(hard_seconds=0.1, soft_strength=0.4):
    """Experimental preferences, not physiological limits or changed human truth."""
    return {
        "version": SEGMENTATION_VERSION,
        "hard_seconds": hard_seconds,
        "soft_strength": soft_strength,
        "soft_seconds": dict(SOFT_SECONDS),
    }


def refinement_policy(soft_strength=0, gap_seconds=0, boundary_seconds=0):
    """Fixed research grid; legacy duration policies keep their original behavior."""
    if soft_strength not in (0, 0.4, 1.2):
        raise ValueError("Unknown refinement duration preference")
    if gap_seconds not in (0, 0.2, 0.4) or boundary_seconds not in (0, 0.2):
        raise ValueError("Unknown local refinement window")
    return {
        **segmentation_policy(0.1, soft_strength),
        "version": REFINEMENT_VERSION,
        "gap_seconds": gap_seconds,
        "boundary_seconds": boundary_seconds,
    }


def refine_local_path(path, valid, probability, unknown, labels, policy, features, blocked):
    """Evidence-supported local repair, with no fixed class priority.

    Unknown holes may become either adjacent ordinary class. The untouched
    unknown hypothesis competes on the same evidence scale. Boundary moves
    are bounded by five frames and never create a short run. This does not
    calibrate confidence, infer contact, or invent a third/critical activity.
    """
    gap = round(policy["gap_seconds"] * 25)
    radius = round(policy["boundary_seconds"] * 25)
    minimum = int(np.ceil(policy["hard_seconds"] * 25 - 1e-9))
    result = path.copy()
    filled, shifted = np.zeros(len(path), bool), np.zeros(len(path), bool)
    ordinary = {
        i
        for i, code in enumerate(labels)
        if code not in ("fall", "stumble_recovery", "other_activity")
    }
    evidence = np.log(np.maximum(probability, 1e-20))
    if features is not None:
        if features.shape != (len(path), len(FEATURE_NAMES)):
            raise ValueError("Refinement geometry and path differ")
        evidence += 0.15 * semantic_bias(features, labels)

    def safe(a, b):
        return bool(valid[a:b].all() and not blocked[a:b].any())

    def change(point):
        if features is None or point < 3 or point + 3 > len(path):
            return 0.0
        if not safe(point - 3, point + 3):
            return 0.0
        cols = [0, 1, 6, 7, 8]
        before = np.median(features[point - 3 : point, cols], axis=0)
        after = np.median(features[point : point + 3, cols], axis=0)
        return float(np.clip(np.abs(after - before) / [0.2, 0.15, 0.3, 0.3, 0.4], 0, 1).mean())

    if gap:
        edges = np.r_[0, np.flatnonzero(np.diff(path)) + 1, len(path)]
        for a, b in pairwise(edges):
            if path[a] >= 0 or b - a > gap or a == 0 or b == len(path):
                continue
            left, right = int(result[a - 1]), int(result[b])
            if left not in ordinary or right not in ordinary or not safe(a - 1, b + 1):
                continue
            abstain = float(np.log(np.maximum(unknown[a:b], 1e-20)).sum())
            if left == right:
                score, point = float(evidence[a:b, left].sum()), b
            else:
                choices = [
                    (
                        float(evidence[a:t, left].sum() + evidence[t:b, right].sum())
                        - 0.15
                        + 0.35 * change(t),
                        t,
                    )
                    for t in range(a, b + 1)
                ]
                score, point = max(choices, key=lambda x: (x[0], -abs(x[1] - (a + b) / 2)))
            if score > abstain + 1e-8:
                result[a:point], result[point:b] = left, right
                filled[a:b] = True

    if radius:
        # Anchors are frozen before this pass; sequential moves cannot drift an
        # anchor by repeated updates or cross an adjacent boundary.
        edges = np.r_[0, np.flatnonzero(np.diff(result)) + 1, len(result)]
        previous = int(edges[0])
        for index, anchor in enumerate(edges[1:-1], 1):
            end = int(edges[index + 1])
            left, right = int(result[anchor - 1]), int(result[anchor])
            lo, hi = max(previous + minimum, anchor - radius), min(end - minimum, anchor + radius)
            if left not in ordinary or right not in ordinary or lo > hi:
                previous = int(anchor)
                continue
            a, b = min(lo, anchor), max(hi, anchor)
            if not safe(max(previous, a - 1), min(end, b + 1)):
                previous = int(anchor)
                continue

            def score(t, a=a, b=b, left=left, right=right):
                return float(
                    evidence[a:t, left].sum() + evidence[t:b, right].sum()
                ) + 0.35 * change(t)

            point = max(range(lo, hi + 1), key=lambda t: (score(t), -abs(t - anchor)))
            if score(point) > score(int(anchor)) + 1e-8:
                result[a:point], result[point:b] = left, right
                shifted[min(point, anchor) : max(point, anchor)] = True
                previous = point
            else:
                previous = int(anchor)
    return result, filled, shifted


def finalize_segments(decoded, metadata, *, start=0, stop=None):
    """Constrain the final (possibly cropped) candidate path, after rejection.

    Invalid data stay masked. Critical event cores are pinned, and gaps between
    distinct events retain an unknown separator. Infeasible blocks abstain and
    retain their original event evidence in diagnostics; no ordinary tag absorbs it.
    Changed frames never acquire calibrated high-confidence status.
    """
    policy = metadata.get("segmentation_policy")
    if not policy:
        return decoded
    if policy.get("version") not in (
        SEGMENTATION_VERSION,
        REFINEMENT_VERSION,
        "product-duration-20261010-v1",
        "product-duration-20261010-v2",
    ):
        raise ValueError("Unknown segmentation policy")
    refinement = policy["version"] == REFINEMENT_VERSION
    if refinement and policy != refinement_policy(
        policy["soft_strength"], policy["gap_seconds"], policy["boundary_seconds"]
    ):
        raise ValueError("Refinement policy differs from the fixed research grid")
    preserve_evidence = policy["version"] in (SEGMENTATION_VERSION, REFINEMENT_VERSION)
    hard = float(policy["hard_seconds"])
    strength = float(policy["soft_strength"])
    if not np.isfinite([hard, strength]).all() or not 0.1 <= hard <= 2 or not 0 <= strength <= 4:
        raise ValueError("Invalid duration policy")
    minimum = int(np.ceil(hard * 25 - 1e-9))
    labels = metadata["labels"]
    original = np.asarray(decoded["class_indices"])
    stop = len(original) if stop is None else stop
    if not 0 <= start < stop <= len(original):
        raise ValueError("Invalid projection extent")
    old = original[start:stop]
    valid = np.asarray(decoded["valid"], bool)[start:stop]
    k = len(labels)
    # Full-sequence posterior already contains both past and future evidence.
    local = np.asarray(decoded["local_scores"])[start:stop]
    posterior = np.asarray(decoded["sequence_scores"])[start:stop]
    probability = np.sqrt(np.maximum(local * posterior, 1e-20))
    cutoff = metadata["decoder"].get("candidate_min_local_score", 0.45)
    unknown = np.maximum(cutoff, 1 - probability.max(1))
    emission = np.log(np.maximum(np.column_stack((probability, unknown)), 1e-20))
    # Retain accepted sequence evidence unless the duration/evidence gain exceeds it.
    emission[np.arange(len(old)), np.where(old >= 0, old, k)] += 0.5
    supported = metadata["supported_indices"]
    emission[:, [i for i in range(k) if i not in supported]] = -np.inf
    critical = [i for i, code in enumerate(labels) if code in ("fall", "stumble_recovery")]
    cores = []
    edges = np.r_[0, np.flatnonzero(np.diff(old)) + 1, len(old)]
    for a, b in pairwise(edges):
        if old[a] in critical:
            cores.append((int(a), int(b), int(old[a])))
    outside_evidence = []
    if preserve_evidence:
        # A second pass after projection must not forget events masked by the
        # full-recording pass. Reintroduce the original cores, not guessed labels.
        for a, b, code in decoded.get("duration_event_evidence", []):
            if b <= start or a >= stop:
                outside_evidence.append((int(a), int(b), int(code)))
            else:
                cores.append((max(0, int(a) - start), min(stop, int(b)) - start, int(code)))
        cores = sorted(set(cores))
    for a, b, code in cores:
        emission[a:b] = -np.inf
        emission[a:b, code] = 0
    # Do not create a new critical event during duration cleanup.
    for i in critical:
        allowed = np.zeros(len(old), bool)
        for a, b, code in cores:
            if code == i:
                allowed[max(0, a - minimum + 1) : min(len(old), b + minimum - 1)] = True
        emission[~allowed, i] = -np.inf
    for (_, b, _), (c, _, _) in pairwise(cores):
        if c > b:
            point = (b + c - 1) // 2
            emission[point] = -np.inf
            emission[point, k] = 0
    soft = policy["soft_seconds"]
    ages = [max(minimum, int(np.ceil(soft.get(code, 0) * 25))) for code in labels] + [minimum]
    matrix = np.full((k + 1, k + 1), 1 / (k + 1))
    np.fill_diagonal(matrix, 1)
    path = duration_path(
        emission, valid, matrix, 0.15, ages, strength=strength, hard_minimum=minimum
    )
    if policy["version"] != "product-duration-20261010-v1" and (valid & (path < 0)).any():
        # Conflicting tiny critical events must not erase a usable recording.
        # Preserve their evidence as local abstentions, solve the remaining islands.
        blocked = np.zeros(len(old), bool)
        edges = np.flatnonzero(np.diff(np.r_[False, valid, False]))
        for a, b in zip(edges[::2], edges[1::2], strict=True):
            if b - a < minimum or not (path[a:b] < 0).all():
                continue
            for c, d, _ in cores:
                if a <= c < b:
                    blocked[max(a, c - minimum + 1) : min(b, d + minimum - 1)] = True
        if blocked.any():
            remaining = valid & ~blocked
            edges = np.flatnonzero(np.diff(np.r_[False, remaining, False]))
            for a, b in zip(edges[::2], edges[1::2], strict=True):
                if b - a < minimum:
                    blocked[a:b] = True
            path = duration_path(
                emission,
                valid & ~blocked,
                matrix,
                0.15,
                ages,
                strength=strength,
                hard_minimum=minimum,
            )
    unsegmentable = valid & (path < 0)
    path[path == k] = -1
    filled, shifted = np.zeros(len(path), bool), np.zeros(len(path), bool)
    if refinement and not decoded.get("local_refinement_applied", False):
        blocked = unsegmentable.copy()
        for a, b, _ in cores:
            blocked[max(0, a - 10) : min(len(path), b + 10)] = True
        geometry = decoded.get("refinement_features")
        path, filled, shifted = refine_local_path(
            path,
            valid,
            probability,
            unknown,
            labels,
            policy,
            geometry[start:stop] if geometry is not None else None,
            blocked,
        )
    result = dict(decoded)
    result["class_indices"] = original.copy()
    result["class_indices"][start:stop] = path
    if refinement:
        result["local_refinement_applied"] = True
        for key, mask in (("short_unknown_filled", filled), ("boundary_adjusted", shifted)):
            result[key] = np.asarray(decoded.get(key, np.zeros(len(original), bool))).copy()
            result[key][start:stop] |= mask
    changed = result["class_indices"] != original
    result["high_confidence_indices"] = decoded["high_confidence_indices"].copy()
    result["high_confidence_indices"][changed] = -1
    chosen = np.maximum(result["class_indices"], 0)
    rows = np.arange(len(original))
    reliability = np.minimum(
        decoded["local_scores"][rows, chosen], decoded["sequence_scores"][rows, chosen]
    )
    result["reliability"] = np.where(result["class_indices"] >= 0, reliability, 0).astype(
        np.float32
    )
    result["duration_adjusted"] = changed | np.asarray(
        decoded.get("duration_adjusted", np.zeros(len(original), bool))
    )
    result["duration_unsegmentable"] = (
        np.asarray(decoded.get("duration_unsegmentable", np.zeros(len(original), bool))).copy()
        if preserve_evidence
        else np.zeros(len(original), bool)
    )
    result["duration_unsegmentable"][start:stop] = unsegmentable
    result["duration_event_evidence"] = np.array(
        outside_evidence
        + [(a + start, b + start, c) for a, b, c in cores if unsegmentable[a:b].any()],
        dtype=np.int64,
    ).reshape(-1, 3)
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
    evidence = []
    for key, label in (("movement_stepping", "检测到连续交替踏步"),
                       ("movement_transition_context", "前后稳定姿态之间存在支撑姿态变化")):
        values = np.asarray(decoded.get(key, []))[start:stop]
        if values.size and values.any():
            evidence.append({"kind": "observation", "code": key,
                             "seconds": float(values.sum()) / 25,
                             "text": label + "；仅作为运动证据，不代表人工确认"})
    stationary = np.asarray(decoded.get("movement_stationary", []))[start:stop]
    if code == "standing" and stationary.size:
        evidence.append({"kind": "observation", "code": "stationary_legs",
                         "value": float(np.median(stationary)), "unit": "score",
                         "text": "结合下肢静止及站姿几何判断；弯腰不另分类"})
    duration = int(np.asarray(decoded.get("duration_adjusted", []))[start:stop].sum())
    if duration:
        evidence.append(
            {
                "kind": "adjustment",
                "code": "minimum_duration",
                "seconds": round(duration / 25, 3),
                "text": f"{duration / 25:.2f} 秒经过最短时长及前后序列证据调整，仍需人工检查",
            }
        )
    if "semantic_features" not in decoded or stop <= start:
        return evidence[:3]
    f = decoded["semantic_features"][start:stop]
    f = f[f[:, 18] > 0]
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
        elif code in (
            "bending",
            "lying",
            "ground_support",
            "sitting",
            "squatting",
            "kneeling",
        ):
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
