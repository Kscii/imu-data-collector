"""独立标注应用的命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import threading
import webbrowser
from pathlib import Path

import uvicorn
import yaml

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.annotation_service import AnnotationService
from imu_data_collector.cli import _open_webui_when_ready, _webui_is_ready
from imu_data_collector.config import load_settings
from imu_data_collector.models import UNIKEY_RE
from imu_data_collector.storage import create_object_store


def _manage_member(args: argparse.Namespace) -> None:
    """以显式预览/应用两阶段维护服务器私有邮箱映射。"""

    if args.config is None:
        raise SystemExit("成员管理必须显式传入 --config /etc/imu-annotation/config.yaml")
    path = args.config.resolve()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    identity = payload.setdefault("identity", {})
    mappings = identity.setdefault("email_to_unikey", {})
    if not isinstance(mappings, dict):
        raise SystemExit("identity.email_to_unikey 必须是映射")
    if args.command == "member-list":
        print(json.dumps(dict(sorted(mappings.items())), ensure_ascii=False, indent=2))
        return
    email = str(args.email or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise SystemExit("--email 不是有效邮箱")
    project = args.project
    service = args.backend_service
    member = f"user:{email}"
    role = "roles/iap.httpsResourceAccessor"
    if args.command == "member-add":
        unikey = str(args.unikey or "").strip().lower()
        if not UNIKEY_RE.fullmatch(unikey):
            raise SystemExit("--unikey 格式无效")
        allowed = identity.get("allowed_unikeys", [])
        if unikey not in allowed:
            raise SystemExit(f"UniKey {unikey} 不在 identity.allowed_unikeys 中")
        existing = mappings.get(email)
        if existing not in {None, unikey}:
            raise SystemExit(f"该邮箱已经映射为 {existing}")
        conflicting_email = next(
            (key for key, value in mappings.items() if value == unikey and key != email),
            None,
        )
        if conflicting_email:
            raise SystemExit(f"UniKey {unikey} 已映射到另一个邮箱")
        mappings[email] = unikey
        affected_unikey = unikey
        iap_verb = "add-iam-policy-binding"
    else:
        if email not in mappings:
            raise SystemExit("该邮箱不在应用映射中")
        if args.unikey and mappings[email] != args.unikey:
            raise SystemExit(f"邮箱当前映射为 {mappings[email]}，与 --unikey 不一致")
        affected_unikey = str(mappings[email])
        del mappings[email]
        iap_verb = "remove-iam-policy-binding"
    command = [
        "gcloud",
        "iap",
        "web",
        iap_verb,
        "--resource-type=backend-services",
        f"--service={service}",
        f"--member={member}",
        f"--role={role}",
        f"--project={project}",
    ]
    result = {
        "apply": bool(args.apply),
        "config": str(path),
        "action": args.command,
        "email": email,
        "unikey": affected_unikey,
        "iap_command": command,
    }
    if args.apply:
        mode = path.stat().st_mode & 0o777
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            yaml.safe_dump(
                payload,
                handle,
                allow_unicode=True,
                sort_keys=False,
            )
        try:
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="imu-annotation")
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "command",
        choices=(
            "serve",
            "start",
            "cleanup-orphans",
            "archive-calibration-evidence",
            "member-list",
            "member-add",
            "member-remove",
        ),
    )
    parser.add_argument("--min-age-days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际应用当前维护操作；省略时只输出计划",
    )
    parser.add_argument(
        "--delete-source",
        action="store_true",
        help="归档校验成功后删除普通录制区中的源对象",
    )
    parser.add_argument("--email")
    parser.add_argument("--unikey")
    parser.add_argument(
        "--project",
        default="project-51b589c7-8d5e-4e78-a10",
    )
    parser.add_argument("--backend-service", default="imu-annotation-backend")
    args = parser.parse_args()
    if args.command.startswith("member-"):
        _manage_member(args)
        return
    settings = load_settings(args.config)
    if args.command in {"cleanup-orphans", "archive-calibration-evidence"}:
        from datetime import timedelta

        store = create_object_store(
            settings.storage.backend,
            settings.storage.root,
            settings.storage.bucket,
            settings.storage.project,
        )
        service = AnnotationService(settings, store)
        if args.command == "cleanup-orphans":
            result = service.cleanup_orphan_uploads(
                min_age=timedelta(days=args.min_age_days),
                dry_run=args.dry_run,
            )
        else:
            if args.delete_source and not args.apply:
                parser.error("--delete-source 必须与 --apply 一起使用")
            result = service.archive_calibration_evidence(
                apply=args.apply,
                delete_source=args.delete_source,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    url = (
        f"http://{settings.annotation.server_host}:"
        f"{settings.annotation.server_port}"
    )
    print(f"标注 WebUI：{url}")
    if args.command == "start":
        if _webui_is_ready(url):
            print("检测到标注服务已运行，直接打开页面。")
            webbrowser.open(url)
            return
        threading.Thread(
            target=_open_webui_when_ready,
            args=(url,),
            daemon=True,
        ).start()
    uvicorn.run(
        create_annotation_app(settings),
        host=settings.annotation.server_host,
        port=settings.annotation.server_port,
    )
