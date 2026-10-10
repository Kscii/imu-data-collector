"""Portable production-v2 launch gate. No model rollback bypasses batch quality."""

import math


def production_gate(report, capability=None):
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def numeric(value):
        return type(value) in (int, float) and math.isfinite(value)

    version = report.get("schema")
    require(
        version in ("imu.motion_model_validation.v2", "imu.motion_model_validation.v3"),
        "需要新版独立验证报告",
    )
    capability = capability or ("daily_activity" if version.endswith(".v3") else "fall")
    require(capability in ("daily_activity", "fall"), "Unknown release capability")
    if version.endswith(".v3"):
        require(report.get("taxonomy_version") in ("3.0.0", "4.0.0"), "需要同词表口径的独立验证")
    else:
        require(capability == "fall", "旧报告不能绕过原联合门槛")
    require(
        report.get("reference_completed", 0) >= 20
        and report.get("frozen_before_validation") is True,
        "需要至少 20 条参考及验证前冻结的完整模型方案",
    )
    require(report.get("group_disjoint") is True, "训练、校准和验证来源组必须隔离")
    a, b = report["baseline"], report["candidate"]
    keys = (
        "identified_precision",
        "known_reference_coverage",
        "boundary_mae_s",
        "boundary_p90_s",
        "matched_segments",
        "out_of_vocabulary_identified",
    )
    require(
        all(numeric(m.get(k)) and m[k] >= 0 for m in (a, b) for k in keys),
        "验证指标不完整",
    )
    require(all(m[k] <= 1 for m in (a, b) for k in keys[:2]), "精度或覆盖率超出范围")
    require(
        numeric(b.get("ordinary_precision"))
        and 0.95 <= b["ordinary_precision"] <= 1
        and numeric(b.get("ordinary_assessed_seconds"))
        and b["ordinary_assessed_seconds"] > 0,
        "普通主活动高可信精度不足 95% 或没有可评估预测",
    )
    require(
        b["identified_precision"] >= a["identified_precision"] - 0.01 - 1e-12,
        "已识别区间准确率下降超过 1 个百分点",
    )
    require(
        b["known_reference_coverage"] >= a["known_reference_coverage"] + 0.1 - 1e-12,
        "高可信时长覆盖率提升不足 10 个百分点",
    )
    require(
        b["boundary_mae_s"] <= a["boundary_mae_s"]
        and b["boundary_p90_s"] <= a["boundary_p90_s"]
        and b["matched_segments"] >= a["matched_segments"],
        "区间边界质量退步",
    )
    require(
        b["out_of_vocabulary_identified"] <= a["out_of_vocabulary_identified"],
        "词表外误标增加",
    )
    require(
        b.get("per_class") and b.get("full_candidate"), "需要逐类支持与完整候选覆盖报告"
    )
    if capability == "daily_activity":
        return True
    fall = b.get("fall", {})
    for name, minimum in (
        ("reference_events", 20),
        ("source_groups", 5),
        ("hard_negative_intervals", 40),
    ):
        require(
            type(fall.get(name)) is int and fall[name] >= minimum,
            "跌倒独立样本支持不足: " + name,
        )
    for name in ("event_precision", "event_recall"):
        require(
            numeric(fall.get(name)) and 0.9 <= fall[name] <= 1,
            "跌倒事件精度/召回不足 90%",
        )
    for name, maximum in (
        ("onset_p90_s", 0.5),
        ("stop_p90_s", 0.5),
        ("impact_p90_s", 0.2),
    ):
        require(
            numeric(fall.get(name)) and 0 <= fall[name] <= maximum,
            "跌倒边界/撞击误差不达标: " + name,
        )
    require(fall.get("missing_impacts") == 0, "跌倒事件仍缺撞击点")
    require(
        numeric(fall.get("negative_hours"))
        and fall["negative_hours"] > 0
        and type(fall.get("false_events")) is int
        and fall["false_events"] >= 0,
        "需要跌倒误报数和负例时长",
    )
    require(
        b.get("per_class") and b.get("full_candidate"), "需要逐类支持与完整候选覆盖报告"
    )
    return True


def validated_capabilities(report):
    """Recompute capabilities from evidence; never trust a supplied status flag."""
    results = {}
    for name in ("daily_activity", "fall"):
        try:
            production_gate(
                report,
                capability=name
                if report.get("schema") == "imu.motion_model_validation.v3"
                else "fall",
            )
            results[name] = {"passed": True, "error": None}
        except (ValueError, KeyError, TypeError) as error:
            results[name] = {"passed": False, "error": str(error)}
    return results
