"""独立同步、标注和训练快照 API；不初始化采集硬件。"""

from __future__ import annotations

import json
import logging
import re
import threading
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from imu_data_collector.annotation_service import AnnotationService
from imu_data_collector.api_errors import error_detail, structured_http_error_handler
from imu_data_collector.auth import (
    Actor,
    AuthenticationError,
    Authenticator,
    AuthorizationError,
    TokenVerifier,
)
from imu_data_collector.broker_models import ModelPublicationRestoreRequest
from imu_data_collector.build_info import ANNOTATION_API_BUILD_ID
from imu_data_collector.config import Settings, load_settings
from imu_data_collector.dataset_catalog import DatasetCatalog
from imu_data_collector.host import resource_path
from imu_data_collector.model_catalog import ModelCatalog
from imu_data_collector.models import (
    ActivityTaxonomyCreateRequest,
    ActivityTaxonomyMigrationApplyRequest,
    ActivityTaxonomyMigrationPreviewRequest,
    ActivityTaxonomyUpdateRequest,
    AnnotationRecordingDeleteRequest,
    AnnotationReviewWorkflowRequest,
    AnnotationSaveRequest,
    ParticipantConfirmRequest,
    ParticipantSelectRequest,
    SyncSaveRequest,
    TrainingSnapshotDeleteRequest,
)
from imu_data_collector.review import ReviewConflictError
from imu_data_collector.storage import (
    ObjectConflictError,
    ObjectStore,
    create_object_store,
)

logger = logging.getLogger(__name__)


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
    dataset_catalog = DatasetCatalog(object_store)
    model_catalog = ModelCatalog(object_store)
    authenticator = Authenticator(active, token_verifier)
    refresh_stop = threading.Event()

    def refresh_catalog_loop() -> None:
        interval = active.annotation.catalog_refresh_interval_s
        while not refresh_stop.is_set():
            try:
                result = service.refresh()
                if result["imported"] or result["skipped"]:
                    logger.info("后台刷新录制索引：%s", result)
            except Exception:
                logger.exception("后台刷新录制索引失败，继续使用已有 catalog")
            refresh_stop.wait(interval)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        interval = active.annotation.catalog_refresh_interval_s
        refresh_thread: threading.Thread | None = None
        service.publish_capabilities()
        if interval > 0:
            refresh_stop.clear()
            refresh_thread = threading.Thread(
                target=refresh_catalog_loop,
                name="annotation-catalog-refresh",
                daemon=True,
            )
            refresh_thread.start()
        try:
            yield
        finally:
            refresh_stop.set()
            if refresh_thread is not None:
                refresh_thread.join(timeout=2.0)

    app = FastAPI(title="IMU 标注平台", version="0.2.0", lifespan=lifespan)
    app.add_exception_handler(HTTPException, structured_http_error_handler)
    app.state.annotation_service = service
    app.state.authenticator = authenticator
    app.state.model_catalog = model_catalog

    @app.middleware("http")
    async def authenticate_api(request: Request, call_next):
        if request.url.path.startswith("/api/v1/") and request.url.path != "/api/v1/health":
            try:
                request.state.actor = authenticator.authenticate(
                    request.headers.get("x-goog-iap-jwt-assertion")
                )
            except AuthenticationError as error:
                return JSONResponse(
                    status_code=401,
                    content={"detail": error_detail("authentication_failed", str(error))},
                )
            except AuthorizationError as error:
                return JSONResponse(
                    status_code=403,
                    content={"detail": error_detail("authorization_failed", str(error))},
                )
        response = await call_next(request)
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

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
            "build_id": ANNOTATION_API_BUILD_ID,
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
            "catalog_refresh_interval_s": active.annotation.catalog_refresh_interval_s,
            "storage": {
                "backend": active.storage.backend,
                "bucket": active.storage.bucket,
            },
        }

    @app.get("/api/v1/session")
    def session(request: Request) -> dict[str, Any]:
        return current_actor(request).public_dict()

    @app.get("/api/v1/dataset-catalog")
    def dataset_catalog_summary() -> dict[str, Any]:
        return dataset_catalog.summary()

    @app.get(
        "/api/v1/dataset-catalog/{kind}/{snapshot_id}/manifest/download"
    )
    def dataset_catalog_manifest_download(
        kind: Literal["base", "team"], snapshot_id: str
    ) -> Response:
        try:
            payload, filename = dataset_catalog.manifest_download(kind, snapshot_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该数据集快照") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return Response(
            payload,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "private, max-age=300",
            },
        )

    @app.get(
        "/api/v1/dataset-catalog/{kind}/{snapshot_id}/{dataset_id}/download"
    )
    def dataset_catalog_h5_download(
        kind: Literal["base", "team"],
        snapshot_id: str,
        dataset_id: str,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> StreamingResponse:
        try:
            entry, info = dataset_catalog.dataset_download(
                kind, snapshot_id, dataset_id
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该数据文件") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        start, end, status_code = 0, info.size_bytes - 1, 200
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
            if not match:
                raise HTTPException(status_code=416, detail="只支持单个 bytes Range")
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else end
            if start > end or end >= info.size_bytes:
                raise HTTPException(status_code=416, detail="H5 Range 越界")
            status_code = 206

        def stream():
            cursor = start
            while cursor <= end:
                chunk_end = min(end, cursor + 1024 * 1024 - 1)
                yield object_store.read_bytes(info.key, cursor, chunk_end)
                cursor = chunk_end + 1

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Disposition": f'attachment; filename="{entry["filename"]}"',
            "Cache-Control": "private, max-age=300",
            "X-Content-SHA256": str(entry["sha256"]),
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{info.size_bytes}"
        return StreamingResponse(
            stream(),
            status_code=status_code,
            media_type="application/x-hdf5",
            headers=headers,
        )

    @app.get("/api/v1/model-catalog")
    def model_catalog_summary(
        refresh: Annotated[bool, Query()] = False,
        include_deprecated: Annotated[bool, Query()] = False,
    ) -> dict[str, Any]:
        if refresh:
            model_catalog.refresh(force=True)
        return model_catalog.summary(include_deprecated=include_deprecated)

    @app.get("/api/v1/model-catalog/{kind}/{publication_id}")
    def model_catalog_detail(
        kind: Literal["experiment", "model"], publication_id: str
    ) -> dict[str, Any]:
        try:
            return model_catalog.detail(kind, publication_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该模型发布") from error

    @app.get("/api/v1/model-catalog/{kind}/{publication_id}/marker/download")
    def model_catalog_marker_download(
        kind: Literal["experiment", "model"], publication_id: str
    ) -> Response:
        try:
            payload, filename = model_catalog.marker(kind, publication_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该模型发布") from error
        return Response(
            payload,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "private, max-age=60",
            },
        )

    @app.get("/api/v1/model-catalog/{kind}/{publication_id}/files/{file_id}/download")
    def model_catalog_file_download(
        kind: Literal["experiment", "model"],
        publication_id: str,
        file_id: str,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> StreamingResponse:
        try:
            descriptor, info = model_catalog.file(kind, publication_id, file_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该模型文件") from error
        start, end, status_code = 0, info.size_bytes - 1, 200
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
            if not match:
                raise HTTPException(status_code=416, detail="只支持单个 bytes Range")
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else end
            if start > end or end >= info.size_bytes:
                raise HTTPException(status_code=416, detail="模型文件 Range 越界")
            status_code = 206

        def stream():
            cursor = start
            while cursor <= end:
                chunk_end = min(end, cursor + 1024 * 1024 - 1)
                yield object_store.read_bytes(info.key, cursor, chunk_end)
                cursor = chunk_end + 1

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Disposition": f'attachment; filename="{descriptor["filename"]}"',
            "Cache-Control": "private, max-age=300",
            "X-Content-SHA256": descriptor["sha256"],
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{info.size_bytes}"
        return StreamingResponse(
            stream(),
            status_code=status_code,
            media_type=descriptor.get("content_type") or "application/octet-stream",
            headers=headers,
        )

    @app.post("/api/v1/model-catalog/{kind}/{publication_id}/deprecate")
    def deprecate_model_publication(
        kind: Literal["experiment", "model"],
        publication_id: str,
        body: ModelPublicationRestoreRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = admin_actor(request)
        try:
            return model_catalog.deprecate(
                kind,
                publication_id,
                actor=actor.unikey,
                expected_generation=body.expected_generation,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该模型发布") from error
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail="模型发布状态已经变化") from error

    @app.get("/api/v1/taxonomy")
    def taxonomy(
        version: Annotated[str | None, Query(max_length=80)] = None,
    ) -> dict[str, Any]:
        try:
            return service.taxonomy_definition(version)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该活动标签版本") from error

    @app.get("/api/v1/taxonomy/manage")
    def taxonomy_management(request: Request) -> dict[str, Any]:
        current_actor(request)
        return service.taxonomy_management_summary()

    @app.post("/api/v1/taxonomy/activities")
    def create_taxonomy_activity(
        body: ActivityTaxonomyCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = current_actor(request)
        try:
            return service.create_taxonomy_activity(body, actor.unikey)
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.patch("/api/v1/taxonomy/activities/{code}")
    def update_taxonomy_activity(
        code: str,
        body: ActivityTaxonomyUpdateRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = current_actor(request)
        try:
            return service.update_taxonomy_activity(code, body, actor.unikey)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该活动标签") from error
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.delete("/api/v1/taxonomy/activities/{code}")
    def delete_taxonomy_activity(
        code: str,
        request: Request,
        expected_version: Annotated[str, Query(min_length=1, max_length=80)],
    ) -> dict[str, Any]:
        actor = current_actor(request)
        try:
            return service.delete_taxonomy_activity(
                code, expected_version, actor.unikey
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该活动标签") from error
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/taxonomy/migrations/preview")
    def preview_taxonomy_migration(
        body: ActivityTaxonomyMigrationPreviewRequest,
        request: Request,
    ) -> dict[str, Any]:
        current_actor(request)
        try:
            return service.preview_taxonomy_migration(body)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到源标签或目标标签") from error
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/taxonomy/migrations/apply")
    def apply_taxonomy_migration(
        body: ActivityTaxonomyMigrationApplyRequest,
        request: Request,
    ) -> dict[str, Any]:
        actor = current_actor(request)
        try:
            return service.apply_taxonomy_migration(body, actor.unikey)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到源标签或目标标签") from error
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/index/refresh")
    def refresh(request: Request) -> dict[str, Any]:
        admin_actor(request)
        return service.refresh()

    @app.get("/api/v1/recordings")
    def recordings() -> list[dict[str, Any]]:
        return [service.recording_summary(item) for item in service.list_recordings()]

    @app.get("/api/v1/calibration-evidence")
    def calibration_evidence() -> dict[str, Any]:
        return service.calibration_evidence_summary()

    @app.get("/api/v1/calibration-evidence/{recording_id}/video")
    def calibration_evidence_video(
        recording_id: str,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> StreamingResponse:
        try:
            artifact, info = service.calibration_evidence_artifact(
                recording_id, "preview_mp4"
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该校准证据") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        start, end, status_code = 0, info.size_bytes - 1, 200
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
            if not match:
                raise HTTPException(status_code=416, detail="只支持单个 bytes Range")
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else end
            if start > end or end >= info.size_bytes:
                raise HTTPException(status_code=416, detail="视频 Range 越界")
            status_code = 206

        def stream():
            cursor = start
            while cursor <= end:
                chunk_end = min(end, cursor + 1024 * 1024 - 1)
                yield object_store.read_bytes(
                    str(artifact["object_key"]), cursor, chunk_end
                )
                cursor = chunk_end + 1

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Cache-Control": "private, max-age=3600",
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{info.size_bytes}"
        return StreamingResponse(
            stream(), status_code=status_code, media_type="video/mp4", headers=headers
        )

    @app.get("/api/v1/calibration-evidence/{recording_id}/analysis")
    def calibration_evidence_analysis(recording_id: str) -> dict[str, Any]:
        try:
            return service.calibration_evidence_analysis(recording_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该校准证据") from error
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/calibration-evidence/{recording_id}/capture-h5/download")
    def calibration_evidence_h5(recording_id: str) -> StreamingResponse:
        try:
            artifact, info = service.calibration_evidence_artifact(
                recording_id, "capture_h5"
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该校准证据") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        def stream():
            cursor = 0
            while cursor < info.size_bytes:
                chunk_end = min(info.size_bytes - 1, cursor + 1024 * 1024 - 1)
                yield object_store.read_bytes(
                    str(artifact["object_key"]), cursor, chunk_end
                )
                cursor = chunk_end + 1

        return StreamingResponse(
            stream(),
            media_type="application/x-hdf5",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{recording_id}.capture.h5"'
                ),
                "Content-Length": str(info.size_bytes),
                "Cache-Control": "private, no-store",
            },
        )

    @app.get("/api/v1/recordings/{recording_id}")
    def recording(recording_id: str) -> dict[str, Any]:
        return service.recording_summary(required(recording_id))


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

    @app.get("/api/v1/recordings/{recording_id}/capture-h5/download")
    def capture_h5_download(recording_id: str) -> StreamingResponse:
        manifest = required(recording_id)
        artifact = service._artifact(manifest, "capture_h5")

        def stream():
            cursor = 0
            while cursor < artifact.size_bytes:
                chunk_end = min(artifact.size_bytes - 1, cursor + 1024 * 1024 - 1)
                yield object_store.read_bytes(artifact.object_key, cursor, chunk_end)
                cursor = chunk_end + 1

        return StreamingResponse(
            stream(),
            media_type="application/x-hdf5",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{recording_id}.capture.h5"'
                ),
                "Content-Length": str(artifact.size_bytes),
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
        body: AnnotationSaveRequest,
        request: Request,
    ) -> dict[str, Any]:
        required(recording_id)
        try:
            return service.save_annotations(
                recording_id,
                body.document,
                current_actor(request).unikey,
                body.expected_revision,
            ).model_dump(
                mode="json"
            )
        except ReviewConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.put("/api/v1/recordings/{recording_id}/participant")
    def select_participant(
        recording_id: str,
        body: ParticipantSelectRequest,
        request: Request,
    ) -> dict[str, Any]:
        required(recording_id)
        try:
            return service.select_participant(
                recording_id, body, current_actor(request).unikey
            ).model_dump(mode="json")
        except ReviewConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/recordings/{recording_id}/participant/confirm")
    def confirm_participant(
        recording_id: str,
        body: ParticipantConfirmRequest,
        request: Request,
    ) -> dict[str, Any]:
        required(recording_id)
        try:
            return service.confirm_participant(
                recording_id, body, current_actor(request).unikey
            ).model_dump(mode="json")
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
        body: SyncSaveRequest,
        request: Request,
    ) -> dict[str, Any]:
        required(recording_id)
        try:
            return service.save_sync(
                recording_id,
                body.document,
                current_actor(request).unikey,
                body.expected_revision,
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

    def aligned_download_response(
        recording_id: str, *, legacy_30hz_only: bool
    ) -> StreamingResponse:
        manifest = required(recording_id)
        if manifest.data_tier.value != "prod":
            raise HTTPException(
                status_code=403,
                detail="test 数据永久禁止下载训练 H5",
            )
        try:
            reference, info = service.active_export(recording_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if legacy_30hz_only and reference.sampling_rate_hz != 30.0:
            raise HTTPException(
                status_code=410,
                detail="该录制已经使用 25 Hz 新合同；请使用通用训练 HDF5 下载入口",
            )
        key = reference.object_key

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
                    f'attachment; filename="{recording_id}.{reference.filename}"'
                ),
                "Content-Length": str(info.size_bytes),
                "Cache-Control": "private, no-store",
            },
        )

    @app.get("/api/v1/recordings/{recording_id}/aligned/download")
    def aligned_download(recording_id: str) -> StreamingResponse:
        return aligned_download_response(recording_id, legacy_30hz_only=False)

    @app.get("/api/v1/recordings/{recording_id}/aligned30/download")
    def aligned30_download(recording_id: str) -> StreamingResponse:
        """只为已经存在的历史 30 Hz 导出保留读取能力。"""

        return aligned_download_response(recording_id, legacy_30hz_only=True)

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

    @app.get("/api/v1/training-snapshots")
    def training_snapshots() -> list[dict[str, Any]]:
        return service.list_training_snapshots()

    @app.post("/api/v1/training-snapshots")
    def create_training_snapshot(request: Request) -> dict[str, Any]:
        try:
            return service.create_training_snapshot(current_actor(request).unikey)
        except ObjectConflictError as error:
            raise HTTPException(
                status_code=409,
                detail="训练数据 current pointer 被其他发布操作更新，请刷新后重试",
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/training-snapshots/{snapshot_id}/download")
    def training_snapshot_download(
        snapshot_id: str,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> StreamingResponse:
        try:
            payload, archive = service.training_snapshot_download(snapshot_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该训练快照") from error
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
                raise HTTPException(status_code=416, detail="训练快照 Range 越界")
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
                f'attachment; filename="cw12eu_{payload["snapshot_id"]}.tar"'
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

    @app.post("/api/v1/training-snapshots/{snapshot_id}/activate-benchmark")
    def activate_benchmark_snapshot(
        snapshot_id: str, request: Request
    ) -> dict[str, Any]:
        try:
            return service.activate_benchmark_snapshot(
                snapshot_id, current_actor(request).unikey
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该训练快照") from error
        except ObjectConflictError as error:
            raise HTTPException(
                status_code=409,
                detail="benchmark current 已被其他成员更新，请刷新后重试",
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/training-snapshots/{snapshot_id}/benchmark-h5/download")
    def benchmark_snapshot_download(snapshot_id: str) -> StreamingResponse:
        try:
            _payload, artifact = service.benchmark_snapshot_download(snapshot_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该训练快照") from error
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        def stream():
            cursor = 0
            while cursor < artifact.size_bytes:
                end = min(artifact.size_bytes - 1, cursor + 1024 * 1024 - 1)
                yield object_store.read_bytes(artifact.key, cursor, end)
                cursor = end + 1

        return StreamingResponse(
            stream(),
            media_type="application/x-hdf5",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="cw12eu_{snapshot_id}.h5"'
                ),
                "Content-Length": str(artifact.size_bytes),
                "Cache-Control": "private, no-store",
            },
        )

    @app.delete("/api/v1/training-snapshots/{snapshot_id}")
    def delete_training_snapshot(
        snapshot_id: str,
        body: TrainingSnapshotDeleteRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            admin_actor(request)
            return service.delete_training_snapshot(
                snapshot_id,
                actor_id=current_actor(request).unikey,
                confirmation=body.confirmation,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该训练快照") from error
        except ObjectConflictError as error:
            raise HTTPException(status_code=409, detail="清理期间对象已更新，请重试") from error
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

    frontend = resource_path("frontend/dist-annotation")
    if frontend.is_dir():
        assets = frontend / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="annotation-assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            candidate = frontend / path
            target = candidate if path and candidate.is_file() else frontend / "index.html"
            cache_control = (
                "public, max-age=31536000, immutable"
                if target.parent == assets
                else "no-store, no-cache, must-revalidate, max-age=0"
            )
            return FileResponse(target, headers={"Cache-Control": cache_control})
    else:

        @app.get("/", include_in_schema=False)
        async def no_frontend() -> dict[str, str]:
            return {"message": "标注前端尚未构建"}

    return app
