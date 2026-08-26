"""独立标注、同步、审核和训练导出 API；不初始化采集硬件。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from imu_data_collector.annotation_service import AnnotationService
from imu_data_collector.auth import (
    Actor,
    AuthenticationError,
    Authenticator,
    AuthorizationError,
    TokenVerifier,
)
from imu_data_collector.config import Settings, load_settings
from imu_data_collector.models import (
    AnnotationDocument,
    AnnotationRecordingDeleteRequest,
    AnnotationReviewWorkflowRequest,
    RevisionRequest,
    SyncDocument,
    SyncExperimentDocument,
    TrainingReleaseRevokeRequest,
)
from imu_data_collector.review import ReviewConflictError
from imu_data_collector.storage import (
    ObjectConflictError,
    ObjectStore,
    create_object_store,
)


def create_annotation_app(
    settings: Settings | None = None,
    store: ObjectStore | None = None,
    token_verifier: TokenVerifier | None = None,
) -> FastAPI:
    active = settings or load_settings()
    object_store = store or create_object_store(
        active.storage.backend,
        active.storage.root,
        active.storage.bucket,
        active.storage.project,
    )
    service = AnnotationService(active, object_store)
    authenticator = Authenticator(active, token_verifier)
    app = FastAPI(title="IMU 标注平台", version="0.2.0")
    app.state.annotation_service = service
    app.state.authenticator = authenticator

    @app.middleware("http")
    async def authenticate_api(request: Request, call_next):
        if request.url.path.startswith("/api/v1/") and request.url.path != "/api/v1/health":
            try:
                request.state.actor = authenticator.authenticate(
                    request.headers.get("x-goog-iap-jwt-assertion")
                )
            except AuthenticationError as error:
                return JSONResponse(status_code=401, content={"detail": str(error)})
            except AuthorizationError as error:
                return JSONResponse(status_code=403, content={"detail": str(error)})
        return await call_next(request)

    def current_actor(request: Request) -> Actor:
        actor = getattr(request.state, "actor", None)
        if not isinstance(actor, Actor):
            raise HTTPException(status_code=401, detail="请求缺少可信登录身份")
        return actor

    def admin_actor(request: Request) -> Actor:
        actor = current_actor(request)
        if not actor.is_admin:
            raise HTTPException(status_code=403, detail="该操作仅限管理员")
        return actor

    def required(recording_id: str):
        try:
            return service.required_manifest(recording_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该录制") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "application": "annotation",
        }

    @app.get("/api/v1/config")
    def config(request: Request) -> dict[str, Any]:
        actor = current_actor(request)
        return {
            "application": "annotation",
            "allowed_unikeys": list(active.identity.allowed_unikeys),
            "admin_unikeys": list(active.identity.admins),
            "local_actor_id": active.auth.local_actor_id,
            "auth_mode": active.auth.mode,
            "current_unikey": actor.unikey,
            "review_policy": service.review_policy.value,
            "storage": {
                "backend": active.storage.backend,
                "bucket": active.storage.bucket,
            },
        }

    @app.get("/api/v1/session")
    def session(request: Request) -> dict[str, Any]:
        return current_actor(request).public_dict()

    @app.get("/api/v1/taxonomy")
    def taxonomy() -> dict[str, Any]:
        return service.taxonomy

    @app.post("/api/v1/index/refresh")
    def refresh(request: Request) -> dict[str, int]:
        admin_actor(request)
        return service.refresh()

    @app.get("/api/v1/recordings")
    def recordings() -> list[dict[str, Any]]:
        return [service.recording_summary(item) for item in service.list_recordings()]

    @app.get("/api/v1/recordings/{recording_id}")
    def recording(recording_id: str) -> dict[str, Any]:
        return service.recording_summary(required(recording_id))

    @app.get("/api/v1/sync-experiments/{experiment_id}")
    def sync_experiment(experiment_id: str) -> dict[str, Any]:
        try:
            return service.sync_experiment(experiment_id).model_dump(mode="json")
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.put("/api/v1/sync-experiments/{experiment_id}")
    def save_sync_experiment(
        experiment_id: str,
        document: SyncExperimentDocument,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.save_sync_experiment(
                experiment_id,
                document,
                current_actor(request).unikey,
            ).model_dump(mode="json")
        except ReviewConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/recordings/{recording_id}/status")
    def status(recording_id: str) -> dict[str, Any]:
        required(recording_id)
        return service.status(recording_id)

    @app.get("/api/v1/recordings/{recording_id}/review")
    def review(recording_id: str) -> dict[str, Any]:
        required(recording_id)
        return service.review(recording_id).model_dump(mode="json")

    @app.get("/api/v1/recordings/{recording_id}/review/download")
    def review_download(recording_id: str) -> Response:
        required(recording_id)
        payload = service.review(recording_id).model_dump(mode="json")
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{recording_id}.review.json"'
                ),
                "Cache-Control": "private, no-store",
            },
        )

    @app.get("/api/v1/recordings/{recording_id}/timeline")
    def timeline(
        recording_id: str,
        max_points: Annotated[int, Query(ge=100, le=20_000)] = 5_000,
    ) -> dict[str, Any]:
        required(recording_id)
        return service.timeline(recording_id, max_points)

    @app.get("/api/v1/recordings/{recording_id}/frame-times")
    def frame_times(recording_id: str) -> dict[str, Any]:
        required(recording_id)
        return service.frame_times(recording_id)

    @app.get("/api/v1/recordings/{recording_id}/sync-window")
    def sync_window(
        recording_id: str,
        frame_index: Annotated[int, Query(ge=0)],
        radius_seconds: Annotated[float, Query(ge=0.1, le=5.0)] = 1.5,
        expected_video_minus_imu_ns: Annotated[
            int | None, Query(ge=-5_000_000_000, le=5_000_000_000)
        ] = None,
    ) -> dict[str, Any]:
        required(recording_id)
        return service.sync_window(
            recording_id,
            frame_index,
            radius_seconds,
            expected_video_minus_imu_ns,
        )

    @app.get("/api/v1/recordings/{recording_id}/annotations")
    def annotations(recording_id: str) -> dict[str, Any]:
        required(recording_id)
        return service.annotations(recording_id).model_dump(mode="json")

    @app.put("/api/v1/recordings/{recording_id}/annotations")
    def save_annotations(
        recording_id: str,
        document: AnnotationDocument,
        request: Request,
    ) -> dict[str, Any]:
        required(recording_id)
        try:
            return service.save_annotations(
                recording_id,
                document,
                current_actor(request).unikey,
            ).model_dump(
                mode="json"
            )
        except ReviewConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/recordings/{recording_id}/sync")
    def sync(recording_id: str) -> dict[str, Any]:
        required(recording_id)
        return service.sync(recording_id)

    @app.put("/api/v1/recordings/{recording_id}/sync")
    def save_sync(
        recording_id: str,
        document: SyncDocument,
        request: Request,
    ) -> dict[str, Any]:
        required(recording_id)
        try:
            return service.save_sync(
                recording_id,
                document,
                current_actor(request).unikey,
            )
        except ReviewConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/recordings/{recording_id}/workflow")
    def workflow(
        recording_id: str,
        body: AnnotationReviewWorkflowRequest,
        request: Request,
    ) -> dict[str, Any]:
        required(recording_id)
        try:
            return service.update_workflow(
                recording_id,
                body,
                current_actor(request).unikey,
            ).model_dump(
                mode="json"
            )
        except ReviewConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/recordings/{recording_id}/aligned30")
    def aligned30(
        recording_id: str, request: RevisionRequest
    ) -> dict[str, str]:
        required(recording_id)
        try:
            return {
                "status": "exported",
                "object_key": service.export_training(
                    recording_id, request.expected_revision
                ),
            }
        except ReviewConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/recordings/{recording_id}/aligned30/download")
    def aligned30_download(recording_id: str) -> StreamingResponse:
        manifest = required(recording_id)
        if manifest.data_tier.value != "prod":
            raise HTTPException(
                status_code=403,
                detail="test 数据永久禁止下载训练 H5",
            )
        key = f"exports/{recording_id}/aligned30.h5"
        info = object_store.stat(key)
        if info is None:
            raise HTTPException(status_code=404, detail="尚未生成 aligned30.h5")

        def stream():
            cursor = 0
            while cursor < info.size_bytes:
                end = min(info.size_bytes - 1, cursor + 1024 * 1024 - 1)
                yield object_store.read_bytes(key, cursor, end)
                cursor = end + 1

        return StreamingResponse(
            stream(),
            media_type="application/x-hdf5",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{recording_id}.aligned30.h5"'
                ),
                "Content-Length": str(info.size_bytes),
                "Cache-Control": "private, no-store",
            },
        )

    @app.delete("/api/v1/recordings/{recording_id}")
    def delete_recording(
        recording_id: str,
        body: AnnotationRecordingDeleteRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.delete_recording(
                recording_id,
                actor_id=current_actor(request).unikey,
                confirmation=body.confirmation,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该录制") from error
        except ObjectConflictError as error:
            raise HTTPException(
                status_code=409,
                detail="删除期间对象被其他操作更新，请使用同一录制编号重试",
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/training-releases")
    def releases() -> list[dict[str, Any]]:
        return service.list_releases()

    @app.post("/api/v1/training-releases")
    def release(request: Request) -> dict[str, Any]:
        try:
            return service.create_release(current_actor(request).unikey)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/training-releases/{release_id}/status")
    def release_status(release_id: str) -> dict[str, Any]:
        try:
            return service.release_status(release_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该训练发布") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/training-releases/{release_id}/download")
    def release_download(
        release_id: str,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> StreamingResponse:
        try:
            payload, archive = service.release_download(release_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该训练发布") from error
        except RuntimeError as error:
            raise HTTPException(status_code=410, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        start = 0
        end = archive.size_bytes - 1
        status_code = 200
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
            if not match:
                raise HTTPException(status_code=416, detail="只支持单个 bytes Range")
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else end
            if start > end or end >= archive.size_bytes:
                raise HTTPException(status_code=416, detail="训练发布 Range 越界")
            status_code = 206

        def stream():
            cursor = start
            while cursor <= end:
                chunk_end = min(end, cursor + 1024 * 1024 - 1)
                yield object_store.read_bytes(archive.key, cursor, chunk_end)
                cursor = chunk_end + 1

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Cache-Control": "private, no-store",
            "Content-Disposition": (
                f'attachment; filename="cw12eu_{payload["release_id"]}.tar"'
            ),
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{archive.size_bytes}"
        return StreamingResponse(
            stream(),
            status_code=status_code,
            media_type="application/x-tar",
            headers=headers,
        )

    @app.post("/api/v1/training-releases/{release_id}/revoke")
    def revoke_release(
        release_id: str,
        body: TrainingReleaseRevokeRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.revoke_release(
                release_id,
                actor_id=current_actor(request).unikey,
                confirmation=body.confirmation,
                reason=body.reason,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该训练发布") from error
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail="撤销期间对象已更新，请重试") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/recordings/{recording_id}/video")
    def video(
        recording_id: str,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> StreamingResponse:
        manifest = required(recording_id)
        artifact = service._artifact(manifest, "preview_mp4")
        start = 0
        end = artifact.size_bytes - 1
        status_code = 200
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
            if not match:
                raise HTTPException(status_code=416, detail="只支持单个 bytes Range")
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else end
            if start > end or end >= artifact.size_bytes:
                raise HTTPException(status_code=416, detail="视频 Range 越界")
            status_code = 206

        def stream():
            cursor = start
            while cursor <= end:
                chunk_end = min(end, cursor + 1024 * 1024 - 1)
                yield object_store.read_bytes(artifact.object_key, cursor, chunk_end)
                cursor = chunk_end + 1

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Cache-Control": "private, max-age=3600",
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{artifact.size_bytes}"
        return StreamingResponse(
            stream(),
            status_code=status_code,
            media_type="video/mp4",
            headers=headers,
        )

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist-annotation"
    if frontend.is_dir():
        assets = frontend / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="annotation-assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            candidate = frontend / path
            target = candidate if path and candidate.is_file() else frontend / "index.html"
            return FileResponse(target)
    else:

        @app.get("/", include_in_schema=False)
        async def no_frontend() -> dict[str, str]:
            return {"message": "标注前端尚未构建"}

    return app
