#!/usr/bin/env python3
"""给指定 host 增加独立的 5xx 静态维护页策略。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


def patch_url_map(
    document: dict[str, Any],
    *,
    host: str,
    matcher_name: str,
    application_service: str,
    error_service: str,
    error_path: str,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    host_rules = document.setdefault("hostRules", [])
    path_matchers = document.setdefault("pathMatchers", [])
    if not isinstance(host_rules, list) or not isinstance(path_matchers, list):
        raise ValueError("URL map 的 hostRules/pathMatchers 结构无效")

    retained_rules: list[dict[str, Any]] = []
    for raw_rule in host_rules:
        if not isinstance(raw_rule, dict) or not isinstance(raw_rule.get("hosts"), list):
            raise ValueError("URL map 包含无效 hostRule")
        rule = dict(raw_rule)
        rule["hosts"] = [value for value in rule["hosts"] if value != host]
        if rule["hosts"]:
            retained_rules.append(rule)
    retained_rules.append({"hosts": [host], "pathMatcher": matcher_name})
    document["hostRules"] = retained_rules

    policy = {
        "errorResponseRules": [{"matchResponseCodes": ["5xx"], "path": error_path}],
        "errorService": error_service,
    }
    replacement = {
        "name": matcher_name,
        "defaultService": application_service,
        "defaultCustomErrorResponsePolicy": policy,
    }
    document["pathMatchers"] = [
        item
        for item in path_matchers
        if not isinstance(item, dict) or item.get("name") != matcher_name
    ] + [replacement]
    if fingerprint:
        document["fingerprint"] = fingerprint
    return document


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--matcher-name", required=True)
    parser.add_argument("--application-service", required=True)
    parser.add_argument("--error-service", required=True)
    parser.add_argument("--error-path", default="/maintenance.html")
    parser.add_argument("--fingerprint")
    args = parser.parse_args()
    document = yaml.safe_load(args.input.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit("URL map YAML 根节点必须是 object")
    patched = patch_url_map(
        document,
        host=args.host,
        matcher_name=args.matcher_name,
        application_service=args.application_service,
        error_service=args.error_service,
        error_path=args.error_path,
        fingerprint=args.fingerprint,
    )
    args.output.write_text(
        yaml.safe_dump(patched, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
