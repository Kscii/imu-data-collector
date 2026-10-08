"""Standalone NumPy/ONNX Runtime adapter, also shipped with each model bundle."""

from __future__ import annotations

import json
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

import numpy as np


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
    scores = predict_scores(
        lambda x, mask: session.run(None, {"joints_world_m": x, "valid": mask})[0],
        joints,
        valid,
    )
    return {"scores": scores, "class_indices": reject(scores, joints, valid, metadata)}
