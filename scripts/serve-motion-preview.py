"""Loopback-only production workbench against an isolated local store."""

import argparse
import re
from copy import deepcopy
from pathlib import Path

import uvicorn
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.config import load_settings


def review_write_path(path):
    return bool(
        re.fullmatch(
            r"/api/v1/synthetic/motions/[0-9a-f]{64}/"
            r"(?:claim|release|variants/[^/]+/[^/]+/(?:draft|review))",
            path,
        )
    )


def mount_calibration(app, settings, store, state):
    """Independent reviews with no model activation or training snapshot writes."""
    if store.resolve() == settings.storage.root.resolve():
        raise ValueError("Calibration must be isolated from development reviews")
    if state.resolve() / "catalog.sqlite3" == settings.annotation.catalog_path.resolve():
        raise ValueError("Calibration needs a separate catalog")
    calibration = deepcopy(settings)
    calibration.storage.root = store.resolve(strict=True)
    calibration.storage.cache_root = state.resolve() / "cache"
    calibration.annotation.catalog_path = state.resolve() / "catalog.sqlite3"
    child = create_annotation_app(calibration)
    # Mounted ASGI apps do not run their own lifespan. Discover this bounded
    # cohort before serving its first request, including a fresh cache directory.
    while child.state.motion_label_service.refresh(force=True) == 256:
        pass
    # Keep only whole-recording editing mutations. In particular this cohort
    # cannot accidentally become a production training snapshot through the API.
    for route in child.router.routes:
        methods = getattr(route, "methods", set())
        if methods - {"GET", "HEAD", "OPTIONS"}:
            path = getattr(route, "path", "")
            if not path.startswith("/api/v1/synthetic/motions/{mid}/"):
                route.endpoint = blocked_calibration_write
                route.app = Route(path, blocked_calibration_write, methods=list(methods)).app
    app.router.routes.insert(0, Mount("/calibration", app=child))
    return child


async def blocked_calibration_write(request):
    return JSONResponse(
        {"detail": "独立校准只接受录制审核，不启用模型或生成训练快照"}, status_code=403
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-store", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8772)
    parser.add_argument("--actor", default="xfan0282")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Reject all editing requests in research comparisons.",
    )
    parser.add_argument(
        "--reviews-only",
        action="store_true",
        help="Allow recording review writes only in an isolated development task.",
    )
    parser.add_argument("--calibration-store", type=Path)
    parser.add_argument("--calibration-state", type=Path)
    parser.add_argument(
        "--trial-guide", type=Path, help="Optional local HTML guide served at /trial."
    )
    parser.add_argument(
        "--trial-rules", type=Path, help="Temporary rules document at /trial/rules."
    )
    args = parser.parse_args()
    if bool(args.calibration_store) != bool(args.calibration_state):
        parser.error("Calibration needs its own store and state directory")
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
    # This loopback workbench has no background inference worker to discover
    # features. Populate its actual API catalog before the first lookup.
    while app.state.motion_label_service.refresh(force=True) == 256:
        pass
    if args.read_only:

        @app.middleware("http")
        async def read_only(request, call_next):
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                return JSONResponse({"detail": "本页仅用于只读实验对照"}, status_code=403)
            return await call_next(request)

    if args.reviews_only:

        @app.middleware("http")
        async def reviews_only(request, call_next):
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                path = request.url.path
                if not review_write_path(path):
                    return JSONResponse(
                        {"detail": "本开发任务只接受录制审核，不启用模型或创建训练快照"},
                        status_code=403,
                    )
            return await call_next(request)

    if args.calibration_store:
        mount_calibration(app, settings, args.calibration_store, args.calibration_state)
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
