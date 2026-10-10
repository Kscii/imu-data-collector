"""Verify grouped v4 predictions against a fresh runtime, then publish a read-only view."""

import argparse
import html
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
from imu_motion_simulator.contracts.common import sha256_file
from imu_motion_simulator.contracts.temporal import (
    CURRENT_PRIMARY,
    STANDING_PRIMARY,
    STANDING_RULES_VERSION,
    document_sha256,
    standing_document,
)
from imu_motion_simulator.production_evaluation import decode_case, evaluate
from imu_motion_simulator.production_validation import load_cases
from imu_motion_simulator.publication import LocalPublicationStore
from imu_motion_simulator.semantic_runtime import interval_evidence
from imu_motion_simulator.standing_experiment import projected_cases

from imu_data_collector.motion_label_previews import MotionLabelPreviews
from imu_data_collector.motion_label_service import MotionLabelService
from imu_data_collector.storage import LocalFilesystemStore
from imu_data_collector.synthetic_motion import SyntheticReviewService


def read(path):
    return json.loads(path.read_text())


def main(base, root):
    previous = base / "product-iteration-20261010"
    original = read(previous / "experiments/frozen-feedback.json")
    binding = read(root / "binding.json")
    assert (
        sha256_file(previous / "experiments/frozen-feedback.json")
        == binding["source_feedback_sha256"]
    )
    selection = read(root / "selection.json")
    seed, recipe = selection["seed"], selection["recipe"]
    run = read(base / "sequence-20261009/primary-sequence-corpus-20261009-corrected/run.json")
    refs = [
        (r["entry"], standing_document(r["document"]), r["review"]) for r in original["records"]
    ]
    old_refs = [(r["entry"], r["document"], r["review"]) for r in original["records"]]
    source = LocalPublicationStore(previous / "preview-store")
    reports = {r: read(root / f"{r}-{seed}.json") for r in ("baseline", recipe)}
    expected = {
        r: {p["candidate"]["candidate"]["candidate_id"]: p["candidate"] for p in v["per_clip"]}
        for r, v in reports.items()
    }
    qualities, checked, models = {}, 0, []
    protected = set(binding["protected_groups"])
    for fold, held in enumerate(binding["folds"]):
        bundle = previous / "experiments" / f"model-C-{seed}-{fold}"
        cases, _ = load_cases(
            source,
            run,
            [r for r in old_refs if r[0]["group_key"] in held],
            bundle,
            evaluation_labels=CURRENT_PRIMARY,
        )
        before = read(previous / "local-abstention-complete" / f"C-{seed}-fold-{fold}.json")
        raw, mapped, mapped_meta = projected_cases(cases, before)
        if recipe == "rules":
            candidate_cases = raw
            candidate_meta = read(root / f"rules-{seed}-{fold}.json")["metadata"]
        else:
            candidate_cases, _ = load_cases(
                source,
                run,
                [r for r in refs if r[0]["group_key"] in held],
                root / f"{recipe}-{seed}-{fold}",
                evaluation_labels=STANDING_PRIMARY,
            )
            candidate_meta = read(root / f"selected-{recipe}-{seed}-{fold}.json")["metadata"]
        for role, cases_, metadata in (
            ("baseline", mapped, mapped_meta),
            ("candidate", candidate_cases, candidate_meta),
        ):
            report_role = "baseline" if role == "baseline" else recipe
            rows = evaluate(cases_, metadata, view="candidate")["per_clip"]
            for case, row in zip(cases_, rows, strict=True):
                doc = row["candidate"]
                cid = doc["candidate"]["candidate_id"]
                assert document_sha256(doc) == document_sha256(expected[report_role][cid])
                decoded, start, _ = decode_case(case, metadata)
                intervals = []
                for s in doc["segments"]:
                    a, b = start + s["start_ns"] // 40_000_000, start + s["stop_ns"] // 40_000_000
                    intervals.append(
                        {
                            "start_ns": s["start_ns"],
                            "stop_ns": s["stop_ns"],
                            "primary_code": s["primary_code"],
                            "score": float(np.asarray(decoded["reliability"])[a:b].mean()),
                            "high_confidence_fraction": 0,
                            "boundary_reliable": False,
                            "evidence": interval_evidence(decoded, a, b, s["primary_code"]),
                            "reason": "experimental_fall"
                            if s["primary_code"] == "fall"
                            else "low_confidence_candidate",
                        }
                    )
                qualities[cid, role] = {
                    "document_sha256": document_sha256(doc),
                    "intervals": intervals,
                }
                checked += 1
    # Audit every actual fit, not just the selected model. Weak early stopping
    # groups are included in exclusions; human calibration reviews are never read.
    for path in sorted(root.glob("*/training-manifest.json")):
        value = read(path)
        if path.parent.name == "research-candidate":
            continue
        checksums = read(path.parent / "checksums.json")
        assert all(sha256_file(path.parent / name) == digest for name, digest in checksums.items())
        assert read(path.parent / "metadata.json")["training_manifest_sha256"] == sha256_file(path)
        train = set(value["groups"]["train"])
        early = set(value["groups"]["calibration"])
        assert not train & early and not (train | early) & protected
        held = set()
        if not path.parent.name.startswith("full-"):
            held = set(binding["folds"][int(path.parent.name.rsplit("-", 1)[-1])])
            assert not (train | early) & held
        assert value["corrections"] == {
            entry["candidate"]["candidate_id"]: document_sha256(doc)
            for entry, doc, _ in refs if entry["group_key"] not in held
        }
        assert value["activity_rules"]["rules"]["taxonomy_version"] == "4.0.0"
        models.append({"model": path.parent.name, "training_manifest_sha256": sha256_file(path)})
    destination = root / "comparison-store"
    # Files must live inside the store boundary. Older runner revisions used
    # symlinks; replace only those references with hard links, never source data.
    for path in destination.rglob("*"):
        if path.is_symlink():
            target = path.resolve(strict=True)
            relative = path.relative_to(destination)
            assert target == (previous / "preview-store" / relative).resolve(strict=True)
            temporary = path.with_name(path.name + ".materializing")
            os.link(target, temporary)
            temporary.replace(path)
    service = MotionLabelService(
        SyntheticReviewService(LocalFilesystemStore(destination), None, target="prod"),
        catalog_path=root / "comparison-state/motion-labels.sqlite3",
    )
    while service.refresh(force=True) == 256:
        pass
    preview_id = "standing-frozen-20261010-verified"
    key = service.root + "/previews/" + preview_id + ".json"
    existing, _ = service.optional(key)
    records = []
    for source_row, (entry, doc, _) in zip(original["records"], refs, strict=True):
        c = entry["candidate"]
        cid = c["candidate_id"]
        records.append(
            {
                "candidate": c,
                "motion_sha256": c["input_hashes"]["motion"],
                "human_revision": source_row["review_pointer"],
                "documents": {
                    "human": doc,
                    "baseline": expected["baseline"][cid],
                    "candidate": expected[recipe][cid],
                },
                "qualities": {r: qualities[cid, r] for r in ("baseline", "candidate")},
                "diagnostics": [
                    r for r in reports[recipe]["short_errors"] if r["candidate_id"] == cid
                ],
            }
        )
    manifest = {
        "schema": "imu.motion_preview.v1",
        "preview_id": preview_id,
        "title": "15 类站姿模型 · 按来源组留出对照",
        "models": {"baseline": "旧 C，统一映射为15类", "candidate": recipe + "；研究候选"},
        "activity_rules_version": STANDING_RULES_VERSION,
        "created_at_utc": existing["created_at_utc"] if existing else datetime.now(UTC).isoformat(),
        "records": records,
        "read_only": True,
        "binding_sha256": sha256_file(root / "binding.json"),
        "comparison_sha256": sha256_file(root / "comparison.json"),
    }
    service.immutable(key, manifest)
    for record in records:
        MotionLabelPreviews(service).case(preview_id, record["motion_sha256"])
    parts = [
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>站姿模型研究对照</title><style>'
        'body{background:#0b1524;color:#e3ebf7;font:16px/1.65 system-ui;'
        'max-width:1100px;margin:24px auto;padding:0 20px}a{color:#8cc8ff}'
        'td,th{padding:8px;border-bottom:1px solid #345;text-align:left}'
        'table{width:100%;border-collapse:collapse}</style>'
        '<h1>15 类站姿模型研究对照</h1><p>已有34条人工参考、旧 C、新候选；'
        '全部按来源组留出。只读，无需重新标注。原始人工修订和正式模型保留。</p>'
    ]
    passed = recipe in selection["eligible_recipes"]
    parts.append(
        "<p>"
        + (
            "通过开发门槛，尚未独立验收。"
            if passed
            else "未通过完整开发门槛，保留用于分析，不替换正式模型。"
        )
        + "</p>"
    )
    parts.append(
        f"<p>下方回放对照使用种子 {seed}；完整两种子结果如下，不能用单一种子代替总体结论。</p>"
        "<table><tr><th>方案／种子</th><th>产品分</th><th>已识别精确率</th>"
        "<th>正确覆盖率</th><th>错误短段</th><th>边界P90</th></tr>"
    )
    names = {
        "baseline": "旧 C 映射",
        "rules": "旧权重＋规则搜索",
        "control": "15 类控制重训",
        "enhanced": "15 类增强监督",
    }
    comparison = read(root / "comparison.json")
    for name, title in names.items():
        for result_seed, r in sorted(comparison[name].items()):
            parts.append(
                f"<tr><td>{title}／{html.escape(result_seed)}</td>"
                f"<td>{r['product_score']:.2f}</td><td>{r['identified_precision']:.2%}</td>"
                f"<td>{r['correct_coverage']:.2%}</td><td>{r['wrong_short_segments']}</td>"
                f"<td>{r['boundary_p90_s']:.3f}s</td></tr>"
            )
    parts.append(
        "</table><p>“人工参考”沿用原纠正，只把弯腰映射为站姿。开发集结果不能代表独立测试或全库准确率。</p><table><tr><th>录制</th><th>时长</th></tr>"
    )
    for entry, _, _ in refs:
        c = entry["candidate"]
        query = urlencode(
            dict(
                view="synthetic",
                lang="zh-CN",
                motion_preview=preview_id,
                candidate=c["candidate_id"],
                version=c["version_id"],
            )
        )
        parts.append(
            f'<tr><td><a href="/?{html.escape(query, quote=True)}">'
            f'{html.escape(c["candidate_id"])}</a></td>'
            f'<td>{entry["clock"]["duration_ns"] / 1e9:.2f}s</td></tr>'
        )
    parts.append('</table><p><a href="/trial/rules">临时标签定义</a></p></html>')
    (root / "verified-guide.html").write_text("".join(parts))
    receipt = {
        "verified_predictions": checked,
        "records": len(records),
        "models": models,
        "candidate_model_sha256": sha256_file(root / "research-candidate/model.onnx"),
        "candidate_metadata_sha256": sha256_file(root / "research-candidate/metadata.json"),
        "protected_groups": len(protected),
        "preview_id": preview_id,
        "manifest_sha256": document_sha256(manifest),
        "human_decisions_written": 0,
        "calibration_labels_read": False,
        "blind_labels_read": False,
        "production_model_activated": False,
    }
    (root / "preview-verification.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2)
    )
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    main(args.base, args.root)
