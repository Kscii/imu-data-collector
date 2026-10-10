"""Standalone NumPy/ONNX Runtime adapter, also shipped with each model bundle."""

from __future__ import annotations

import json
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

import numpy as np

SCORE_RUNTIME_VERSION = "body22-overlap-v1"
SEQUENCE_RUNTIME_VERSION = "offline-hmm-v1"
SEMANTIC_RUNTIME_VERSION = "semantic-duration-v1"


def is_sequence_model(metadata):
    return metadata.get("decoder", {}).get("kind") in (
        SEQUENCE_RUNTIME_VERSION,
        SEMANTIC_RUNTIME_VERSION,
    )


def semantic_module():
    if __package__:
        from . import semantic_runtime
    else:
        import semantic_runtime
    return semantic_runtime


def default_sequence_decoder(labels):
    """Finite soft priors, including direct transitions and a uniform initial state."""
    k = len(labels)
    matrix = np.ones((k, k), dtype=np.float64)
    relationships = {
        "sit_down": ("sitting", "stand_up"),
        "stand_up": ("standing", "walking", "sit_down"),
        "get_up": ("standing", "walking"),
        "lie_down": ("lying", "get_up"),
        "squatting_down": ("squatting", "stand_up"),
        "sitting": ("stand_up", "lie_down"),
        "lying": ("get_up",),
        "squatting": ("stand_up",),
        "kneeling": ("stand_up",),
        "crawling": ("get_up",),
    }
    if "posture_transition" in labels:
        supports = (
            "standing",
            "sitting",
            "lying",
            "squatting",
            "kneeling",
            "ground_support",
        )
        relationships.update({code: ("posture_transition",) for code in supports})
        relationships["posture_transition"] = supports + (
            "walking",
            "running",
            "bending",
        )
        relationships["bending"] = ("standing", "walking", "posture_transition")
    transitions = {
        "posture_transition",
        "sit_down",
        "stand_up",
        "get_up",
        "lie_down",
        "squatting_down",
        "fall",
    }
    for i, code in enumerate(labels):
        for following in relationships.get(code, ()):
            if following in labels:
                matrix[i, labels.index(following)] = 3
        persistence = 0.90 if code in transitions else 0.97
        matrix[i, i] = 0
        matrix[i] *= (1 - persistence) / matrix[i].sum()
        matrix[i, i] = persistence
    return {
        "kind": SEQUENCE_RUNTIME_VERSION,
        "transition": matrix.tolist(),
        "transition_weight": 0.35,
        "temperature": 1.0,
        "outside_threshold": 0.5,
        "candidate_min_local_score": 0.45 if "posture_transition" in labels else 0.0,
        "gap_fill_seconds": 0.0,
        "impact_threshold": 0.5,
        "calibration": {"status": "unvalidated", "high_thresholds": {}},
    }


def _logsumexp(values, axis):
    maximum = values.max(axis=axis, keepdims=True)
    return np.squeeze(
        maximum + np.log(np.exp(values - maximum).sum(axis=axis, keepdims=True)), axis
    )


def sequence_decode(
    scores, valid, metadata, *, outside_scores=None, joints=None, boundary_scores=None
):
    """Viterbi + forward/backward per continuous valid run; no fabricated gap labels.

    Posterior scores alone are not correctness probabilities. High-confidence
    output requires a separately calibrated local AND contextual threshold.
    """
    if metadata.get("movement_policy"):
        if __package__:
            from .motion_logic import adjust_scores
        else:
            from motion_logic import adjust_scores
        if joints is None:
            raise ValueError("Movement policy requires canonical motion geometry")
        adjusted, detail = adjust_scores(scores, joints, valid, metadata["labels"], metadata["movement_policy"])
        base = dict(metadata)
        base.pop("movement_policy")
        result = sequence_decode(adjusted, valid, base, outside_scores=outside_scores,
                                 joints=joints, boundary_scores=boundary_scores)
        result.update({"movement_" + key: np.asarray(value) for key, value in detail.items()})
        return result
    if metadata.get("segmentation_policy"):
        base = dict(metadata)
        base.pop("segmentation_policy")
        decoded = sequence_decode(
            scores,
            valid,
            base,
            outside_scores=outside_scores,
            joints=joints,
            boundary_scores=boundary_scores,
        )
        semantic = semantic_module()
        if (
            metadata["segmentation_policy"].get("version")
            == semantic.REFINEMENT_VERSION
            and joints is not None
        ):
            decoded["refinement_features"] = semantic.semantic_features(
                joints, decoded["valid"]
            )
        return semantic.finalize_segments(decoded, metadata)
    if metadata.get("decoder", {}).get("kind") == SEMANTIC_RUNTIME_VERSION:
        semantic = semantic_module()
        return semantic.semantic_decode(
            scores,
            valid,
            metadata,
            features=semantic.semantic_features(joints, valid) if joints is not None else None,
            boundary_scores=boundary_scores,
            outside_scores=outside_scores,
            base_decoder=sequence_decode,
        )
    scores = np.asarray(scores, dtype=np.float64)
    valid = np.asarray(valid, bool).copy()
    cfg, labels = metadata["decoder"], metadata["labels"]
    k = len(labels)
    matrix = np.asarray(cfg["transition"], dtype=np.float64)
    temperature, weight = float(cfg["temperature"]), float(cfg["transition_weight"])
    if (
        cfg["kind"] != SEQUENCE_RUNTIME_VERSION
        or scores.shape != (len(valid), k)
        or matrix.shape != (k, k)
        or not np.isfinite(matrix).all()
        or (matrix <= 0).any()
        or not np.allclose(matrix.sum(1), 1)
        or not np.isfinite(scores).all()
        or (scores < 0).any()
        or not np.isfinite([temperature, weight]).all()
        or temperature <= 0
        or weight < 0
    ):
        raise ValueError("Invalid sequence decoder inputs")
    allowed = np.isin(np.arange(k), metadata["supported_indices"])
    if not allowed.any():
        raise ValueError("No supported classes")
    thresholds = cfg.get("calibration", {}).get("high_thresholds", {})
    if not isinstance(thresholds, dict) or any(
        code not in labels
        or type(value) not in (int, float)
        or not np.isfinite(value)
        or not 0 < value <= 1
        for code, value in thresholds.items()
    ):
        raise ValueError("Invalid calibrated thresholds")
    if any(
        type(cfg.get(name)) not in (int, float)
        or not np.isfinite(cfg[name])
        or not 0 < cfg[name] <= 1
        for name in ("outside_threshold", "impact_threshold")
    ):
        raise ValueError("Invalid event thresholds")
    valid &= scores.sum(1) > 0
    if outside_scores is not None:
        outside = np.asarray(outside_scores, dtype=np.float64)
        if (
            outside.shape != valid.shape
            or not np.isfinite(outside).all()
            or ((outside < 0) | (outside > 1)).any()
        ):
            raise ValueError("Invalid outside scores")
        valid &= outside < cfg["outside_threshold"]
    emission = np.log(np.maximum(scores, 1e-30)) / temperature
    emission[:, ~allowed] = -1e12
    emission -= _logsumexp(emission, axis=1)[:, None]
    local = np.exp(emission)
    transition = weight * np.log(matrix)
    path = np.full(len(valid), -1, dtype=np.int64)
    posterior = np.zeros_like(scores)
    edges = np.flatnonzero(np.diff(np.r_[False, valid, False]))
    for start, stop in zip(edges[::2], edges[1::2], strict=True):
        n = stop - start
        back = np.zeros((n, k), dtype=np.int32)
        alpha = np.empty((n, k), dtype=np.float64)
        delta = emission[start].copy()
        alpha[0] = delta - _logsumexp(delta, axis=0)
        for t in range(1, n):
            choices = delta[:, None] + transition
            back[t] = choices.argmax(axis=0)
            delta = choices.max(axis=0) + emission[start + t]
            alpha[t] = _logsumexp(alpha[t - 1, :, None] + transition, axis=0) + emission[start + t]
            alpha[t] -= _logsumexp(alpha[t], axis=0)
        state = int(delta.argmax())
        beta = np.zeros(k)
        for t in range(n - 1, -1, -1):
            path[start + t] = state
            p = alpha[t] + beta
            posterior[start + t] = np.exp(p - _logsumexp(p, axis=0))
            state = int(back[t, state])
            beta = _logsumexp(transition + emission[start + t][None, :] + beta[None, :], axis=1)
            beta -= _logsumexp(beta, axis=0)
    indices = np.maximum(path, 0)
    rows = np.arange(len(valid))
    contextual = posterior[rows, indices]
    evidence = local[rows, indices]
    reliability = np.minimum(contextual, evidence)
    calibration = cfg.get("calibration", {})
    thresholds = calibration.get("high_thresholds", {})
    cutoff = np.array([thresholds.get(code, 2.0) for code in labels])
    high = path.copy()
    high[(reliability < cutoff[indices]) | ~valid] = -1
    if calibration.get("status") != "calibrated":
        high[:] = -1
    proposal = path.copy()
    minimum = cfg.get("candidate_min_local_score", 0.0)
    if not isinstance(minimum, (int, float)) or not 0 <= minimum <= 1:
        raise ValueError("Invalid proposal evidence threshold")
    proposal[evidence < minimum] = -1
    gap_s = cfg.get("gap_fill_seconds", 0.0)
    if gap_s not in (0.0, 0.2, 0.5):
        raise ValueError("Gap fill must be 0, 0.2 or 0.5 seconds")
    # This modifies review candidates only; confidence still needs calibrated evidence.
    if gap_s:
        edges = np.r_[0, np.flatnonzero(np.diff(proposal)) + 1, len(proposal)]
        protected = {"fall", "stumble_recovery", "posture_transition", "other_activity"}
        for a, b in pairwise(edges):
            if a == 0 or b == len(proposal) or proposal[a] != -1 or b - a > int(gap_s * 25):
                continue
            code = proposal[a - 1]
            if code < 0 or proposal[b] != code or labels[code] in protected or not valid[a:b].all():
                continue
            if (path[a:b] == code).all() and (local[a:b, code] >= minimum * 0.8).all():
                proposal[a:b] = code
    return {
        "class_indices": proposal,
        "high_confidence_indices": high,
        "local_scores": local.astype(np.float32),
        "sequence_scores": posterior.astype(np.float32),
        "reliability": reliability.astype(np.float32),
        "valid": valid,
    }


def impact_candidates(indices, scores, labels, threshold):
    """Only a learned, finite interior peak can propose an impact."""
    if scores is None:
        return {}
    scores = np.asarray(scores)
    if scores.shape != np.asarray(indices).shape or not np.isfinite(scores).all():
        raise ValueError("Invalid impact scores")
    result = {}
    edges = np.r_[0, np.flatnonzero(np.diff(indices)) + 1, len(indices)]
    for a, b in pairwise(edges):
        if indices[a] >= 0 and labels[indices[a]] == "fall" and b - a >= 3:
            point = int(a + 1 + scores[a + 1 : b].argmax())
            if scores[point] >= threshold:
                result[int(a)] = point
    return result


def predict_outputs(session, joints, valid, metadata):
    if metadata.get("semantic_features"):
        semantic = semantic_module()
        if metadata.get("semantic_recipe") != semantic.FEATURE_RECIPE or metadata.get(
            "semantic_feature_names"
        ) != list(semantic.FEATURE_NAMES):
            raise ValueError("Semantic network feature recipe differs")
    auxiliary = metadata.get("event_heads", False)

    def forward(x, mask):
        inputs = {"joints_world_m": x, "valid": mask}
        if metadata.get("semantic_features"):
            inputs["semantic_features"] = np.stack(
                [semantic_module().semantic_features(a, b) for a, b in zip(x, mask, strict=True)]
            )
        output = session.run(None, inputs)
        return np.concatenate(output, axis=-1) if auxiliary else output[0]

    values = predict_scores(forward, joints, valid)
    network_labels = metadata.get("network_labels", metadata["labels"])
    k = len(network_labels)
    expected = k + (2 if auxiliary else 0) + (1 if metadata.get("boundary_head") else 0)
    if values.shape != (len(joints), expected) or not np.isfinite(values).all():
        raise ValueError("Network output shape or values differ")
    scores = values[:, :k]
    if network_labels != metadata["labels"]:
        expected_labels = [c for c in network_labels if c != "bending"]
        if expected_labels != metadata["labels"] or "bending" not in network_labels:
            raise ValueError("Unsupported network class projection")
        scores = np.column_stack([scores[:, i] for i, c in enumerate(network_labels) if c != "bending"])
        scores[:, metadata["labels"].index("standing")] += values[:, network_labels.index("bending")]
    return {
        "scores": scores,
        **(
            {"impact_scores": values[:, k], "outside_scores": values[:, k + 1]} if auxiliary else {}
        ),
        **({"boundary_scores": values[:, k + 2]} if metadata.get("boundary_head") else {}),
    }


def windows(joints, valid):
    x = np.asarray(joints, np.float32)
    valid = np.asarray(valid, bool)
    if x.ndim != 3 or x.shape[1:] != (22, 3) or len(x) == 0 or valid.shape != (len(x),):
        raise ValueError("Expected joints [T,22,3] and valid [T] at 25 Hz")
    if not np.isfinite(x).all():
        raise ValueError("Joint positions must be finite metres")
    for start in range(-50, len(x), 100):
        indices = np.arange(start, start + 200)
        clipped = np.clip(indices, 0, len(x) - 1)
        mask = (indices >= 0) & (indices < len(x)) & valid[clipped]
        yield start, x[clipped], mask


def predict_scores(forward, joints, valid):
    scores = None
    weights = np.zeros(len(joints), np.float64)
    taper = np.maximum(0.01, np.hanning(200))
    for start, x, mask in windows(joints, valid):
        values = np.asarray(forward(x[None], mask[None].astype(np.float32)))[0]
        if scores is None:
            scores = np.zeros((len(joints), values.shape[-1]), np.float64)
        a, b = max(0, start), min(len(joints), start + 200)
        local = slice(a - start, b - start)
        w = taper[local] * mask[local]
        scores[a:b] += values[local] * w[:, None]
        weights[a:b] += w
    return (scores / np.maximum(weights[:, None], 1e-8)).astype(np.float32)


def reject(scores, joints, valid, metadata):
    if is_sequence_model(metadata):
        return sequence_decode(scores, valid, metadata, joints=joints)["high_confidence_indices"]
    result = np.argmax(scores, axis=1).astype(np.int64)
    confidence = scores.max(1)
    result[(confidence < metadata["threshold"]) | ~np.asarray(valid, bool)] = -1
    supported = np.asarray(metadata["supported_indices"], np.int64)
    result[~np.isin(result, supported)] = -1
    allowed = np.broadcast_to(np.isin(np.arange(scores.shape[1]), supported), scores.shape).copy()
    allowed &= np.asarray(valid, bool)[:, None]
    if len(joints) > 1:
        velocity = np.gradient(joints, 0.04, axis=0)
        speed = np.linalg.norm(velocity[:, [0, 4, 5, 7, 8, 10, 11]], axis=2).max(1)
        # Suppress a locomotion prediction only after sustained lower-body stillness.
        width = min(11, len(speed))
        kernel = np.ones(width) / width
        smooth = np.convolve(
            np.pad(speed, (width // 2, width - 1 - width // 2), mode="edge"),
            kernel,
            mode="valid",
        )
        moving = [metadata["labels"].index(c) for c in ("walking", "running")]
        result[(smooth < 0.09) & np.isin(result, moving)] = -1
        for index in moving:
            allowed[smooth < 0.09, index] = False
    decoder = metadata.get("decoder")
    if decoder:
        if decoder.get("kind") != "class-hysteresis-v1":
            raise ValueError("Unsupported temporal decoder")
        state = -1
        for frame in range(len(scores)):
            eligible = np.where(allowed[frame], scores[frame], -1)
            best = int(np.argmax(eligible))
            code = metadata["labels"][best]
            entry = decoder["entry_thresholds"].get(code, metadata["threshold"])
            if eligible[best] >= entry:
                state = best
            elif state >= 0:
                exit_score = decoder["exit_thresholds"].get(
                    metadata["labels"][state], metadata["threshold"]
                )
                if not allowed[frame, state] or scores[frame, state] < exit_score:
                    state = -1
            result[frame] = state
    edges = np.r_[0, np.flatnonzero(np.diff(result)) + 1, len(result)]
    for a, b in pairwise(edges):
        minimum = (
            decoder["minimum_frames"].get(metadata["labels"][result[a]], 6)
            if decoder and result[a] >= 0
            else metadata.get("rejection_minimum_frames", 6)
        )
        if b - a < minimum:
            result[a:b] = -1
    return result


@lru_cache(maxsize=2)
def cpu_session(model_path):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
    )
    return session


def infer_bundle(directory, joints, valid):
    directory = Path(directory)
    metadata = json.loads((directory / "metadata.json").read_text())
    session = cpu_session(str(directory.resolve() / "model.onnx"))
    output = predict_outputs(session, joints, valid, metadata)
    if is_sequence_model(metadata):
        decoded = sequence_decode(
            output["scores"],
            valid,
            metadata,
            joints=joints,
            boundary_scores=output.get("boundary_scores"),
            outside_scores=output.get("outside_scores")
            if metadata.get("outside_supported")
            else None,
        )
        decoded["impacts"] = impact_candidates(
            decoded["class_indices"],
            output.get("impact_scores") if metadata.get("impact_supported") else None,
            metadata["labels"],
            metadata["decoder"]["impact_threshold"],
        )
        return {**output, **decoded}
    return {
        **output,
        "class_indices": reject(output["scores"], joints, valid, metadata),
    }
