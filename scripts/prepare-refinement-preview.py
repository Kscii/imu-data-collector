"""Publish an isolated, verified old-C versus refined-C development comparison."""

import argparse
import html
import json
import shutil
from pathlib import Path
from urllib.parse import urlencode

from imu_motion_simulator.contracts.common import sha256_file
from imu_motion_simulator.contracts.temporal import CURRENT_PRIMARY, document_sha256
from imu_motion_simulator.production_evaluation import evaluate
from imu_motion_simulator.production_validation import load_cases
from imu_motion_simulator.publication import LocalPublicationStore

from imu_data_collector.motion_label_previews import MotionLabelPreviews
from imu_data_collector.motion_label_service import MotionLabelService
from imu_data_collector.storage import LocalFilesystemStore
from imu_data_collector.synthetic_motion import SyntheticReviewService


def read(path):
    return json.loads(path.read_text())


def main(args):
    previous = args.base / "product-iteration-20261010"
    feedback = read(previous / "experiments/frozen-feedback.json")
    original = read(previous / "experiments/experiment.json")
    assert sha256_file(previous / "experiments/frozen-feedback.json") == original["feedback_sha256"]
    run = read(args.base / "sequence-20261009/primary-sequence-corpus-20261009-corrected/run.json")
    destination = args.output / "comparison-store"
    if not destination.exists():
        shutil.copytree(previous / "preview-store", destination)
    state = args.output / "comparison-state"
    service = MotionLabelService(
        SyntheticReviewService(LocalFilesystemStore(destination), None, target="prod"),
        catalog_path=state / "motion-labels.sqlite3",
    )
    while service.refresh(force=True) == 256:
        pass
    source = LocalPublicationStore(previous / "preview-store")
    refs = [(r["entry"], r["document"], r["review"]) for r in feedback["records"]]
    summary = read(args.output / "decoder-study/summary.json")
    models, checked, links = [], 0, []
    for seed in (20261010, 20261011):
        before = read(args.output / f"decoder-study/C-{seed}-before.json")
        after = read(args.output / f"decoder-study/C-{seed}-after.json")
        documents = {
            name: {
                r["candidate"]["candidate"]["candidate_id"]: r["candidate"]
                for r in report["per_clip"]
            }
            for name, report in (("baseline", before), ("candidate", after))
        }
        for fold, held in enumerate(original["folds"]):
            bundle = previous / "experiments" / f"model-C-{seed}-{fold}"
            cases, _ = load_cases(
                source,
                run,
                [r for r in refs if r[0]["group_key"] in held],
                bundle,
                evaluation_labels=CURRENT_PRIMARY,
            )
            policies = {
                "baseline": read(
                    previous / "local-abstention-complete" / f"C-{seed}-fold-{fold}.json"
                ),
                "candidate": read(args.output / "decoder-study" / f"seed-{seed}-fold-{fold}.json")[
                    "metadata"
                ],
            }
            for role, meta in policies.items():
                for row in evaluate(cases, meta, view="candidate")["per_clip"]:
                    doc = row["candidate"]
                    assert document_sha256(doc) == document_sha256(
                        documents[role][doc["candidate"]["candidate_id"]]
                    )
                    checked += 1
        records = []
        pid = f"refinement-dev-20261010-{seed}"
        for row in feedback["records"]:
            c = row["entry"]["candidate"]
            cid = c["candidate_id"]
            records.append(
                {
                    "candidate": c,
                    "motion_sha256": c["input_hashes"]["motion"],
                    "human_revision": row["review_pointer"],
                    "documents": {
                        "human": row["document"],
                        "baseline": documents["baseline"][cid],
                        "candidate": documents["candidate"][cid],
                    },
                    "diagnostics": [r for r in after["short_errors"] if r["candidate_id"] == cid],
                }
            )
        manifest = {
            "schema": "imu.motion_preview.v1",
            "preview_id": pid,
            "title": f"C 局部修正 / {seed} · 未通过完整门槛",
            "models": {"baseline": "同折 C 原分段", "candidate": "同折 C 局部修正；实验候选"},
            "activity_rules_version": feedback["activity_rules"]["version"],
            "created_at_utc": feedback["created_at_utc"],
            "records": records,
            "experiment_sha256": sha256_file(args.output / "experiment.json"),
            "analysis_sha256": sha256_file(args.output / "decoder-study/summary.json"),
            "read_only": True,
        }
        service.immutable(service.root + "/previews/" + pid + ".json", manifest)
        for r in records:
            MotionLabelPreviews(service).case(pid, r["motion_sha256"])
        models.append(pid)
        links.append(
            '<a href="/?'
            + html.escape(
                urlencode({"view": "synthetic", "lang": "zh-CN", "motion_preview": pid}), quote=True
            )
            + '">种子 '
            + str(seed)
            + "</a>"
        )
    parts = [
        '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>局部修正只读对照</title><style>
        body{background:#0b1524;color:#e3ebf7;font:16px/1.65 system-ui;
        max-width:1050px;margin:24px auto;padding:0 20px}a{color:#8cc8ff}
        table{border-collapse:collapse;width:100%}
        td,th{padding:9px;border-bottom:1px solid #345;text-align:left}
        </style><h1>C 局部修正：只读开发对照</h1>''',
        "<p>旧34条，按来源组留出。可切换冻结人工／同折 C 原分段／同折 C 局部修正。"
        "无需重新标注旧参考。</p>",
        "<p>"
        + " · ".join(links)
        + "</p><table><tr><th>种子</th><th>产品评分</th><th>错误短段</th>"
        "<th>边界 P90</th><th>完整开发门槛</th></tr>",
    ]
    for seed, r in summary["results"].items():
        a, b = r["before"], r["after"]
        parts.append(
            f"<tr><td>{seed}</td><td>{a['product_score']:.2f} → {b['product_score']:.2f}</td>"
            f"<td>{a['wrong_short_segments']} → {b['wrong_short_segments']}</td>"
            f"<td>{a['boundary_p90_s']:.3f} → {b['boundary_p90_s']:.3f}s</td>"
            "<td>未通过</td></tr>"
        )
    parts.append(
        "</table><p>短段有所减少，但两个种子的坐姿召回均小幅下降，"
        "种子20261010多吞并了一个不同动作核心；边界尚未达到旧基础模型的正式非退步门槛。"
        "因此不替换现用模型，也不把本次修正作为补标初始建议。</p>"
        '<p><a href="http://127.0.0.1:8775/trial">进入新增20条开发补标</a> · '
        '原独立校准仍在 <a href="http://127.0.0.1:8772/trial">8772</a>。'
        "这里的开发结果不代表独立校准或盲测。</p></html>"
    )
    (args.output / "comparison-guide.html").write_text("".join(parts))
    result = {
        "preview_ids": models,
        "actual_predictions_verified": checked,
        "human_decisions_written": 0,
        "read_only": True,
        "model_activated": False,
    }
    (args.output / "comparison-receipt.json").write_text(json.dumps(result, indent=2))
    print(result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
