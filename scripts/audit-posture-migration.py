"""Read-only impact report and review guide; never write to the annotation store."""

import argparse
import html
import json
from pathlib import Path
from urllib.parse import urlencode

from imu_data_collector.motion_annotations import project_motion, validate_motion
from imu_data_collector.synthetic_temporal_contract import (
    CURRENT_RULES_VERSION,
    CURRENT_TAXONOMY_VERSION,
    current_document,
    document_sha256,
    partial_fall_events,
    submission_issues,
)


def audit(store, output):
    store = store.resolve(strict=True)
    output = output.resolve()
    if output.is_relative_to(store):
        raise ValueError("Report output must be outside the immutable annotation store")
    output.mkdir(parents=True, exist_ok=True)
    prefix = "synthetic-motion/prod/v1/motion-labels"
    observed = {}

    def read(key):
        path = (store / key).resolve(strict=True)
        if not path.is_relative_to(store):
            raise ValueError("Reference escapes local store")
        value = json.loads(path.read_text())
        observed[key] = document_sha256(value)
        return value

    def resolve(ref):
        if not ref:
            return None
        value = read(ref["revision_key"])
        if document_sha256(value) != ref["revision_sha256"]:
            raise ValueError("Review reference hash mismatch")
        return value

    variants = {}
    for path in sorted((store / prefix / "variants").glob("*.json")):
        variant = read(str(path.relative_to(store)))
        variants.setdefault(variant["motion_sha256"], []).append(variant)
    records = []
    for path in sorted((store / prefix / "feature-manifests").glob("*.json")):
        feature = read(str(path.relative_to(store)))
        mid = feature["binding"]["motion_sha256"]
        key = f"{prefix}/states/{mid}.json"
        state = read(key) if (store / key).exists() else {}
        accepted = resolve(state.get("accepted"))
        draft = resolve(state.get("draft"))
        prediction = resolve(state.get("prediction"))
        base = (draft or accepted or prediction or {"document": feature["template"]})["document"]
        migrated = current_document(base)
        validate_motion(migrated)
        prior = [
            e for e in migrated["source_evidence"] if e.get("kind") == "posture-taxonomy-migration"
        ]
        changes = prior[-1]["review_required"] if prior else []
        # Prefer the full coverage variant; every link binds an exact IMU selection.
        options = sorted(
            variants.get(mid, []), key=lambda v: v["clock"]["duration_ns"], reverse=True
        )
        links, issues = [], []
        for variant in options:
            c = variant["candidate"]
            doc = project_motion(migrated, c, variant["clock"])
            issues.extend(submission_issues(doc))
            links.append(
                "/?"
                + urlencode(
                    {
                        "view": "synthetic",
                        "lang": "zh-CN",
                        "candidate": c["candidate_id"],
                        "version": c["version_id"],
                    }
                )
            )
        current_acceptance = bool(
            accepted
            and accepted.get("decision") == "pass"
            and accepted["document"]["taxonomy"]["version"] == CURRENT_TAXONOMY_VERSION
        )
        records.append(
            {
                "motion_sha256": mid,
                "source_member": feature["source_member"],
                "review_pointer": state.get("accepted"),
                "draft_pointer": state.get("draft"),
                "source_document_sha256": document_sha256(base),
                "source_taxonomy_version": base["taxonomy"]["version"],
                "current_acceptance": current_acceptance,
                "review_required": changes,
                "submission_issues": issues,
                "links": links,
                "unknown_intervals": sum(s["primary_code"] is None for s in migrated["segments"]),
                "partial_fall_events": len(partial_fall_events(migrated)),
            }
        )
    for key, digest in observed.items():
        if document_sha256(json.loads((store / key).read_text())) != digest:
            raise ValueError("Source changed during audit; rerun before publishing the report")
    report = {
        "schema": "imu.posture_migration_impact.v1",
        "rules_version": CURRENT_RULES_VERSION,
        "taxonomy_version": CURRENT_TAXONOMY_VERSION,
        "records": records,
        "source_fingerprints": observed,
        "store_writes": 0,
        "human_decisions_written": 0,
    }
    with (output / "migration-impact.json").open("x") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    rows = []
    for i, row in enumerate(records, 1):
        status = (
            "已按新规则通过"
            if row["current_acceptance"]
            else "旧审核，需核对"
            if row["review_pointer"]
            else "尚未人工通过"
        )
        details = [
            f"{c['start_ns'] / 1e9:.2f}–{c['stop_ns'] / 1e9:.2f}s：{c['reason']}"
            for c in row["review_required"]
        ]
        details += [v["message"] for v in row["submission_issues"]]
        link = html.escape(row["links"][0], quote=True) if row["links"] else "#"
        detail_html = "<br>".join(html.escape(d) for d in dict.fromkeys(details))
        detail_html = detail_html or "核对原待判区间和新规则"
        rows.append(
            f"<tr><td>{i}</td><td><a href='{link}'>{html.escape(row['source_member'])}</a></td>"
            f"<td>{status}</td><td>{detail_html}</td></tr>"
        )
    guide = (
        """<!doctype html><meta charset="utf-8"><title>新规则复核清单</title>
<style>
body{max-width:1200px;margin:32px auto;padding:0 20px;background:#0b1421;
color:#dbe9fa;font:16px/1.6 sans-serif}a{color:#8dcbff}
table{width:100%;border-collapse:collapse}
th,td{padding:10px;border-bottom:1px solid #33435a;text-align:left;vertical-align:top}
td:nth-child(2){max-width:350px;overflow-wrap:anywhere}</style>
<h1>主活动新规则复核</h1><p><a href="/trial/rules">临时标签定义与切分说明</a> ·
<a href="/?view=synthetic&amp;lang=zh-CN">打开审核工作台</a></p>
<p>本清单为生成时快照。旧人工记录未改写；请先看临时规则，再按以下清单定向复核。批准后保持当前位置。新模型训练和全量处理等待本轮复核完成。</p>
<p>切分 S；合并下一段 M；上一条 P；下一条 N；空格播放；
Ctrl/⌘+Enter 校验并通过。未确定的跌倒撞击点可留空。</p>
<table><thead><tr><th>#</th><th>录制</th><th>审核状态</th><th>重点</th></tr></thead><tbody>"""
        + "".join(rows)
        + "</tbody></table>"
    )
    with (output / "guide.html").open("x") as f:
        f.write(guide)
    return {
        "records": len(records),
        "accepted": sum(bool(r["review_pointer"]) for r in records),
        "affected_accepted": sum(
            bool(
                r["review_pointer"]
                and not r["current_acceptance"]
                and (r["review_required"] or r["submission_issues"])
            )
            for r in records
        ),
        "human_decisions_written": 0,
        "output": str(output),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.local_store, args.output), ensure_ascii=False))
