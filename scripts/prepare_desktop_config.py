#!/usr/bin/env python3
"""为桌面安装包生成不含云端秘密的团队上传配置目录。"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import yaml


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("configs"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--broker-url", required=True)
    parser.add_argument("--oauth-client-id", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    broker_url = args.broker_url.rstrip("/")
    if not broker_url.startswith("https://"):
        raise SystemExit("broker URL 必须使用 HTTPS")
    if not args.oauth_client_id.endswith(".apps.googleusercontent.com"):
        raise SystemExit("Google OAuth client ID 格式无效")

    if args.output.exists():
        shutil.rmtree(args.output)
    shutil.copytree(args.source, args.output)
    config_path = args.output / "default.yaml"
    value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    value["publish"] = {"mode": "broker"}
    value["storage"] = {
        **value.get("storage", {}),
        "backend": "local",
        "bucket": None,
        "project": None,
    }
    cloud = {
        **value.get("cloud", {}),
        "broker_url": broker_url,
        "google_oauth_client_id": args.oauth_client_id,
    }
    # 即使源配置未来出现服务器专用字段，也不允许进入桌面安装包。
    cloud.pop("google_oauth_client_secret", None)
    value["cloud"] = cloud
    config_path.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
