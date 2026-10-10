"""Portable offline movement evidence. No contact, impact or accident truth.

Only the explicitly versioned v4 experiment uses this module. Legacy decoders
and frozen model bundles continue using their original rule recipes.
"""

from itertools import pairwise

import numpy as np

VERSION = "body22-movement-evidence-v1"


def policy(strength=0.4):
    if strength not in (0, 0.4, 1.2):
        raise ValueError("Unknown movement evidence strength")
    return {
        "version": VERSION,
        "strength": strength,
        "entry": 0.55,
        "exit": 0.25,
        "persistence_frames": 3,
        "anchor_frames": 10,
        "maximum_transition_frames": 150,
    }


def intervals(values):
    values = np.asarray(values)
    edges = np.r_[0, np.flatnonzero(np.diff(values)) + 1, len(values)]
    return [(int(a), int(b), values[a]) for a, b in pairwise(edges) if b > a]


def smooth(values, valid, width=11):
    """Centered offline evidence, never smoothing across an invalid sample."""
    values, valid = np.asarray(values), np.asarray(valid, bool)
    result = np.zeros_like(values, dtype=float)
    for a, b, good in intervals(valid):
        if good:
            n = min(width, b - a)
            pad = ((n // 2, n - 1 - n // 2),) + ((0, 0),) * (values.ndim - 1)
            padded = np.pad(values[a:b], pad, mode="edge")
            if values.ndim == 1:
                result[a:b] = np.convolve(padded, np.ones(n) / n, mode="valid")
            else:
                result[a:b] = np.column_stack(
                    [
                        np.convolve(padded[:, i], np.ones(n) / n, mode="valid")
                        for i in range(values.shape[1])
                    ]
                )
    return result


def hysteresis(value, valid, entry=0.65, exit=0.35, persistence=3):
    result = np.zeros(len(value), bool)
    active, count = False, 0
    for i, score in enumerate(value):
        if not valid[i]:
            active, count = False, 0
            continue
        if active and score < exit:
            active, count = False, 0
        if not active:
            count = count + 1 if score >= entry else 0
            if count >= persistence:
                active = True
                result[i - persistence + 1 : i] = True
        result[i] = active
    return result


def evidence(joints, valid, labels, scores=None):
    if __package__:
        from .semantic_runtime import semantic_features
    else:
        from semantic_runtime import semantic_features
    f = semantic_features(joints, valid)
    good = f[:, 18] > 0
    velocity = (f[:, 17] > 0) & good
    s = smooth(f, velocity)
    # A conservative noise floor from valid stationary legs, not a per-recording
    # percentile that would turn a completely moving recording into "stationary".
    quiet = f[velocity & (f[:, 8] < 0.08), 8]
    noise = float(np.median(quiet)) if len(quiet) >= 10 else 0.0
    noise = min(noise, 0.04)
    clip = lambda v: np.clip(v, 0, 1)
    energy = clip((s[:, 16] - noise - 0.10) / 0.40)
    opposition = clip((s[:, 9] - 0.05) / 0.55)
    alternating = energy * opposition
    stepping = hysteresis(alternating, velocity, entry=0.55, exit=0.25, persistence=3)
    for a, b, state in intervals(stepping):
        if state and b - a < 10:
            stepping[a:b] = False
    stationary = clip((0.16 + noise - s[:, 8]) / 0.14)
    stationary *= velocity
    tall = clip((f[:, 1] - 0.55) / 0.25)
    extended = clip((0.65 - np.maximum(f[:, 2], f[:, 3])) / 0.35)
    foot_supported = tall * extended
    vertical = clip((np.abs(s[:, 7]) - 0.05) / 0.25)
    knees = np.maximum(f[:, 2], f[:, 3])
    knee_change = np.r_[0, np.abs(np.diff(knees))] * 25 * velocity
    support_change = vertical * clip(smooth(knee_change, velocity) / 0.6)
    out = np.zeros((len(f), len(labels)), float)
    for i, code in enumerate(labels):
        if code == "standing":
            # Torso inclination deliberately absent: a standing bend is standing.
            out[:, i] = stationary * foot_supported - stepping.astype(float)
        elif code in ("walking", "running", "stair_up", "stair_down"):
            out[:, i] = alternating * stepping - stationary
        elif code == "posture_transition":
            out[:, i] = support_change - 0.5 * stationary
        elif code == "lying":
            out[:, i] = (
                clip((0.35 - np.abs(f[:, 0])) / 0.25)
                * clip((0.25 - f[:, 14]) / 0.20)
                * stationary
            )
            out[:, i] -= 0.5 * foot_supported
    transition_context = np.zeros(len(f), bool)
    if scores is not None:
        scores = np.asarray(scores)
        if scores.shape != out.shape or not np.isfinite(scores).all():
            raise ValueError("Movement evidence score dimensions differ")
        support = {
            c
            for c in (
                "standing",
                "sitting",
                "lying",
                "squatting",
                "kneeling",
                "ground_support",
            )
            if c in labels
        }
        best = scores.argmax(1)
        anchors = np.full(len(f), -1)
        for a, b, code in intervals(best):
            stable = (
                good[a:b] & (scores[a:b, code] >= 0.7) & (support_change[a:b] < 0.2)
            )
            if labels[code] in support:
                for c, d, ok in intervals(stable):
                    if ok and d - c >= 10:
                        anchors[a + c : a + d] = code
        solid = [(a, b, c) for a, b, c in intervals(anchors) if c >= 0]
        for (_, a, left), (b, _, right) in pairwise(solid):
            if 0 < b - a <= 150 and good[a:b].all() and not stepping[a:b].any():
                changed = left != right or (support_change[a:b] > 0.5).any()
                if changed and (support_change[a:b] > 0.2).any():
                    transition_context[a:b] = True
        if "posture_transition" in labels:
            out[transition_context, labels.index("posture_transition")] += 0.8
            for code in ("squatting", "kneeling", "ground_support", "sitting"):
                if code in labels:
                    out[transition_context, labels.index(code)] -= 0.5
        # Rules cannot reinterpret uncertain critical evidence as a routine ADL.
        protected = [
            i
            for i, c in enumerate(labels)
            if c in ("fall", "stumble_recovery", "jumping", "other_activity")
        ]
        danger = (
            scores[:, protected].max(1) >= 0.25 if protected else np.zeros(len(f), bool)
        )
        danger = smooth(danger.astype(float), good, 25) > 0
        out[danger] = 0
    out[~good] = 0
    return np.clip(out, -1, 1), {
        "geometry_valid": good,
        "stationary": stationary,
        "stepping": stepping,
        "support_change": support_change,
        "transition_context": transition_context,
        "noise_floor": noise,
    }


def adjust_scores(scores, joints, valid, labels, config):
    if config != policy(config.get("strength")):
        raise ValueError("Unknown movement evidence policy")
    scores = np.asarray(scores, float)
    bias, detail = evidence(joints, valid, labels, scores)
    adjusted = scores * np.exp(config["strength"] * bias)
    protected = [
        i
        for i, c in enumerate(labels)
        if c in ("fall", "stumble_recovery", "jumping", "other_activity")
    ]
    ordinary = [i for i in range(len(labels)) if i not in protected]
    # Preserve critical probability mass exactly; this evidence is not a new
    # accident detector and cannot silently create or suppress event probability.
    mass = scores[:, ordinary].sum(1)
    adjusted[:, ordinary] *= (mass / np.maximum(adjusted[:, ordinary].sum(1), 1e-20))[
        :, None
    ]
    adjusted[:, protected] = scores[:, protected]
    return adjusted.astype(np.float32), detail


def supervision_mask(joints, valid, labels, targets):
    """Only mask clear contradictions; never convert a rule into human truth."""
    bias, detail = evidence(joints, valid, labels)
    y = np.asarray(targets)
    known = y >= 0
    contradiction = np.zeros(len(y), bool)
    contradiction[known] = bias[np.flatnonzero(known), y[known]] <= -0.65
    contradiction |= known & ~detail["geometry_valid"]
    # Uncertain intermediate support during active lowering/rising is weak
    # evidence, not a stable posture just because the whole clip names one.
    low = [
        i
        for i, c in enumerate(labels)
        if c in ("ground_support", "kneeling", "squatting", "sitting")
    ]
    contradiction |= np.isin(y, low) & (detail["support_change"] > 0.65)
    return contradiction


def filter_pseudo_targets(targets, bias, geometry_valid, added_conflicts):
    """Reject extra geometric conflicts after the original pseudo audit passes."""
    targets = np.asarray(targets)
    keep = targets >= 0
    keep[keep] &= bias[np.flatnonzero(keep), targets[keep]] >= 0.25
    keep &= geometry_valid & ~added_conflicts
    return np.where(keep, targets, -1), int(((targets >= 0) & ~keep).sum())
