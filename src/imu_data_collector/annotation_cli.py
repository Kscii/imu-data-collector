"""独立标注应用的命令行入口。"""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

import uvicorn

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.annotation_service import AnnotationService
from imu_data_collector.cli import _open_webui_when_ready, _webui_is_ready
from imu_data_collector.config import load_settings
from imu_data_collector.storage import create_object_store


def main() -> None:
    parser = argparse.ArgumentParser(prog="imu-annotation")
    parser.add_argument("--config", type=Path)
    parser.add_argument("command", choices=("serve", "start", "cleanup-orphans"))
    parser.add_argument("--min-age-days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    settings = load_settings(args.config)
    if args.command == "cleanup-orphans":
        from datetime import timedelta

        store = create_object_store(
            settings.storage.backend,
            settings.storage.root,
            settings.storage.bucket,
            settings.storage.project,
        )
        service = AnnotationService(settings, store)
        result = service.cleanup_orphan_uploads(
            min_age=timedelta(days=args.min_age_days),
            dry_run=args.dry_run,
        )
        print(result)
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
