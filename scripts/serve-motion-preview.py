"""Loopback-only production workbench against an isolated local store."""

import argparse
from pathlib import Path

import uvicorn
from starlette.responses import FileResponse
from starlette.routing import Route

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.config import load_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-store", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8772)
    parser.add_argument("--actor", default="xfan0282")
    parser.add_argument(
        "--trial-guide", type=Path, help="Optional local HTML guide served at /trial."
    )
    parser.add_argument(
        "--trial-rules", type=Path, help="Temporary rules document at /trial/rules."
    )
    args = parser.parse_args()
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = args.local_store.resolve()
    settings.storage.cache_root = args.state_dir.resolve() / "cache"
    settings.annotation.catalog_path = args.state_dir.resolve() / "catalog.sqlite3"
    settings.annotation.synthetic_target = "prod"
    settings.annotation.synthetic_run_id = None
    settings.annotation.synthetic_bucket = None
    settings.auth.mode = "local"
    settings.auth.local_actor_id = args.actor
    settings.identity.allowed_unikeys = tuple(set(settings.identity.allowed_unikeys) | {args.actor})
    settings.identity.admins = tuple(set(settings.identity.admins) | {args.actor})
    app = create_annotation_app(settings)
    if args.trial_guide:
        guide = args.trial_guide.resolve(strict=True)

        async def trial_guide(request):
            return FileResponse(guide, media_type="text/html")

        app.router.routes.insert(0, Route("/trial", trial_guide, methods=["GET"]))
    if args.trial_rules:
        rules = args.trial_rules.resolve(strict=True)

        async def trial_rules(request):
            return FileResponse(rules, media_type="text/plain; charset=utf-8")

        app.router.routes.insert(0, Route("/trial/rules", trial_rules, methods=["GET"]))
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
