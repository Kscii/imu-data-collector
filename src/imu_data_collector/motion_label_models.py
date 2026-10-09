"""Explicit production model promotion; training never changes the active pointer."""

import hashlib
import math
from pathlib import Path

from .motion_annotations import require
from .motion_label_service import now
from .production_gate import production_gate, validated_capabilities
from .storage import ObjectConflictError
from .synthetic_temporal_contract import IDENTIFIER, SHA256, document_sha256


def active_model(service):
    return service.optional(service.root + "/active-model.json")[0]


def gate(report):
    require(
        report.get("reference_completed") == 20 and report.get("frozen_before_validation") is True,
        "需要 20 条独立参考及验证前冻结的模型方案",
    )
    a, b = report["baseline"], report["candidate"]
    keys = (
        "identified_precision",
        "known_reference_coverage",
        "boundary_mae_s",
        "boundary_p90_s",
        "matched_segments",
    )
    require(
        all(type(m.get(k)) in (int, float) and math.isfinite(m[k]) for m in (a, b) for k in keys),
        "验证指标不完整或非有限数",
    )
    require(
        all(0 <= m[k] <= 1 for m in (a, b) for k in keys[:2])
        and all(m[k] >= 0 for m in (a, b) for k in keys[2:])
        and all(type(m["matched_segments"]) is int for m in (a, b)),
        "验证指标范围无效",
    )
    require(
        b["identified_precision"] >= a["identified_precision"] - 0.01 - 1e-12,
        "已识别区间准确率下降超过 1 个百分点",
    )
    require(
        b["known_reference_coverage"] >= a["known_reference_coverage"] + 0.10 - 1e-12,
        "可识别时长占比提升不足 10 个百分点",
    )
    require(
        b["boundary_mae_s"] <= a["boundary_mae_s"]
        and b["boundary_p90_s"] <= a["boundary_p90_s"]
        and b["matched_segments"] >= a["matched_segments"],
        "区间边界质量退步",
    )
    require(report.get("out_of_vocabulary_regression") is False, "需要词表外误识别检查")


def validate_model(service, model):
    require(
        model.get("schema") == "imu.production_model.v1"
        and IDENTIFIER.fullmatch(model["model_id"]),
        "Invalid production model identity",
    )
    require(
        model["feature_version"] == "motion-body22-world-25hz-v1", "Unsupported feature version"
    )
    for name in ("onnx", "metadata"):
        artifact = model[name]
        require(
            artifact["object_key"].startswith(service.root + "/model-artifacts/")
            and SHA256.fullmatch(artifact["sha256"])
            and artifact["byte_length"] > 0,
            "Model artifact escapes namespace",
        )
        stat = service.store.stat(artifact["object_key"])
        require(
            stat and stat.size_bytes == artifact["byte_length"], "Model artifact is not uploaded"
        )
    return model


def list_models(service):
    return [
        validate_model(service, service.store.read_json(item.key)[0])
        for item in service.store.list(service.root + "/models/")
    ]


def promote_model(service, model_id, actor, expected_active_sha256):
    with service.publication_lock():
        return _promote_model(service, model_id, actor, expected_active_sha256)


def _promote_model(service, model_id, actor, expected_active_sha256):
    require(IDENTIFIER.fullmatch(model_id), "Invalid model ID")
    model = validate_model(
        service, service.store.read_json(service.root + f"/models/{model_id}.json")[0]
    )
    prior, generation = service.optional(service.root + "/active-model.json")
    if (document_sha256(prior) if prior else None) != expected_active_sha256:
        raise ObjectConflictError("正式模型已改变，请重新加载")
    baseline = model.get("release_kind") == "existing-baseline"
    previously_released = any(
        service.store.read_json(item.key)[0]["model"] == model
        for item in service.store.list(service.root + "/model-releases/")
    )
    require(
        not baseline or prior is None or previously_released,
        "现用基线只允许首次引入；后续模型必须通过独立验证",
    )
    if not baseline:
        report = model["validation"]
        runtime_sha = hashlib.sha256(
            Path(__file__).with_name("motion_label_runtime.py").read_bytes()
        ).hexdigest()
        require(report.get("runtime_sha256") == runtime_sha, "验证报告与当前推理代码不同")
        require(
            report["candidate_model_sha256"] == model["onnx"]["sha256"]
            and report["candidate_metadata_sha256"] == model["metadata"]["sha256"],
            "验证报告模型或解码参数不匹配",
        )
        if prior and not previously_released:
            require(
                report["baseline_model_sha256"] == prior["model"]["onnx"]["sha256"]
                and report["baseline_metadata_sha256"] == prior["model"]["metadata"]["sha256"],
                "验证基线与当前正式模型不同，请重新比较",
            )
        production_gate(report)
    if prior and prior["model"] == model:
        return prior
    release = {
        "model": model,
        "activated_by": actor,
        "activated_at_utc": now(),
        "previous_sha256": document_sha256(prior) if prior else None,
        "scope": "all-motion-features",
        "human_results": "preserve",
        "capabilities": validated_capabilities(model.get("validation") or {}),
    }
    service.reference("model-releases", release)
    service.store.write_json(
        service.root + "/active-model.json", release, if_generation_match=generation
    )
    return release
