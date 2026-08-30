#!/usr/bin/env python3
"""通过 Compute REST API 和 fingerprint 原子更新维护页 URL map。"""

from __future__ import annotations

import argparse
import importlib.util
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote

import google.auth
from google.auth.transport.requests import AuthorizedSession


def _patch_module() -> ModuleType:
    path = Path(__file__).with_name("patch-maintenance-url-map.py")
    spec = importlib.util.spec_from_file_location("maintenance_url_map_patch", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法载入 URL map patch 模块")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response_json(response: Any) -> dict[str, Any]:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Compute API 返回了非 object JSON")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--url-map", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--matcher-name", required=True)
    parser.add_argument("--application-service", required=True)
    parser.add_argument("--error-service", required=True)
    parser.add_argument("--error-path", default="/maintenance.html")
    args = parser.parse_args()

    credentials, _default_project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/compute"]
    )
    session = AuthorizedSession(credentials)
    project = quote(args.project, safe="")
    url_map = quote(args.url_map, safe="")
    resource_url = (
        f"https://compute.googleapis.com/compute/v1/projects/{project}"
        f"/global/urlMaps/{url_map}"
    )
    current = _response_json(session.get(resource_url, timeout=30))
    fingerprint = current.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise RuntimeError("Compute API 未返回 URL map fingerprint")

    patched = _patch_module().patch_url_map(
        current,
        host=args.host,
        matcher_name=args.matcher_name,
        application_service=args.application_service,
        error_service=args.error_service,
        error_path=args.error_path,
        fingerprint=fingerprint,
    )
    operation = _response_json(
        session.put(
            resource_url,
            params={"requestId": str(uuid.uuid4())},
            json=patched,
            timeout=60,
        )
    )
    operation_name = operation.get("name")
    if not isinstance(operation_name, str) or not operation_name:
        raise RuntimeError("Compute API 未返回全局 operation")

    operation_url = (
        f"https://compute.googleapis.com/compute/v1/projects/{project}"
        f"/global/operations/{quote(operation_name, safe='')}"
    )
    for _attempt in range(180):
        state = _response_json(session.get(operation_url, timeout=30))
        if state.get("status") == "DONE":
            if state.get("error"):
                raise RuntimeError(f"URL map 更新失败：{state['error']}")
            print(f"URL map 已使用 fingerprint 原子更新：{args.url_map}")
            return
        time.sleep(1)
    raise TimeoutError("等待 URL map 更新超过 180 秒")


if __name__ == "__main__":
    main()
