"""只负责设备、录制、诊断、发布与本地维护的采集 API。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from imu_data_collector.ble import CW12EUBleSource
from imu_data_collector.build_info import CAPTURE_API_BUILD_ID
from imu_data_collector.config import Settings, load_settings
from imu_data_collector.coordinator import RecordingCoordinator
from imu_data_collector.models import (
    CharacterizationStageRequest,
    CharacterizationStartRequest,
    PreviewStartRequest,
    QuarantineRequest,
    RecordingDeleteRequest,
    RecordingStartRequest,
)
from imu_data_collector.validation import validate_capture_h5
from imu_data_collector.video import discover_video_devices


def _mjpeg_part(jpeg: bytes) -> bytes:
    return (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
        + jpeg
        + b"\r\n"
    )


def _operation_error_detail(
    error: BaseException, *, code: str, component: str, hint: str
) -> dict[str, Any]:
    message = str(error).strip() or f"设备操作失败（{type(error).__name__}）"
    return {
        "code": code,
        "component": component,
        "message": message,
        "hint": hint,
        "retryable": True,
    }


def create_capture_app(settings: Settings | None = None) -> FastAPI:
    active = settings or load_settings()
    coordinator = RecordingCoordinator(active)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await coordinator.shutdown()

    app = FastAPI(title="IMU 数据采集端", version="0.2.0", lifespan=lifespan)
    app.state.coordinator = coordinator

    @app.middleware("http")
    async def static_cache_policy(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    def required(recording_id: str):
        summary = coordinator.catalog.get(recording_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="找不到该录制")
        return summary

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "application": "capture",
            "build_id": CAPTURE_API_BUILD_ID,
            **coordinator.snapshot(),
        }

    @app.get("/api/v1/config")
    async def config() -> dict[str, Any]:
        return {
            "application": "capture",
            "build_id": CAPTURE_API_BUILD_ID,
            "data_root": str(active.data_root),
            "minimum_free_gib": active.minimum_free_gib,
            "allowed_unikeys": list(active.identity.allowed_unikeys),
            "data_tiers": ["test", "prod"],
            "default_data_tier": "test",
            "imu": {
                "name": active.imu.name,
                "address": active.imu.address,
                "expected_rate_hz": active.imu.expected_rate_hz,
                "calibration_profile_id": active.imu.calibration_profile_id,
                "calibration_verified": bool(
                    active.imu.calibration_verified
                    and active.imu.accel_counts_per_g
                    and active.imu.gyro_counts_per_dps
                ),
            },
            "video": {
                "width": active.video.width,
                "height": active.video.height,
                "requested_fps": active.video.requested_fps,
                "preview_fps": active.video.preview_fps,
                "bitrate": active.video.bitrate,
                "manual_controls_enabled": active.video.manual_controls_enabled,
                "prod_min_source_fps": active.video.prod_min_source_fps,
                "prod_min_span_fps": active.video.prod_min_span_fps,
            },
            "publish": {
                "backend": active.storage.backend,
                "bucket": active.storage.bucket,
            },
        }

    @app.get("/api/v1/taxonomy")
    async def taxonomy() -> dict[str, Any]:
        return coordinator.taxonomy

    @app.get("/api/v1/devices")
    async def devices(
        scan_ble: Annotated[bool, Query(description="执行五秒主动 BLE 扫描")] = False,
    ) -> dict[str, Any]:
        cameras = await discover_video_devices(active.video)
        ble: list[dict[str, Any]] = []
        if scan_ble:
            try:
                ble = await CW12EUBleSource.discover(settings=active.imu)
            except Exception as error:
                ble = [{"error": str(error)}]
        return {"cameras": cameras, "ble": ble}

    @app.post("/api/v1/recordings/start")
    async def start(request: RecordingStartRequest) -> dict[str, Any]:
        try:
            return (await coordinator.start(request)).model_dump(mode="json")
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(
                status_code=409,
                detail=_operation_error_detail(
                    error,
                    code="recording_start_failed",
                    component="ble_video",
                    hint="检查 IMU、摄像头和设备预览状态后重试",
                ),
            ) from error

    @app.post("/api/v1/recordings/stop")
    async def stop() -> dict[str, Any]:
        try:
            return (await coordinator.stop()).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/preflight/start")
    async def preview_start(request: PreviewStartRequest) -> dict[str, Any]:
        try:
            return await coordinator.start_preview(request)
        except Exception as error:
            raise HTTPException(
                status_code=409,
                detail=_operation_error_detail(
                    error,
                    code="preview_start_failed",
                    component="ble_video",
                    hint="确认 IMU 未被手机占用并处于可连接状态",
                ),
            ) from error

    @app.post("/api/v1/preflight/stop")
    async def preview_stop() -> dict[str, Any]:
        try:
            return await coordinator.stop_preview()
        except Exception as error:
            raise HTTPException(
                status_code=409,
                detail=_operation_error_detail(
                    error,
                    code="preview_release_failed",
                    component="ble_video",
                    hint="可再次点击释放；该操作是幂等的",
                ),
            ) from error

    @app.post("/api/v1/preflight/camera")
    async def preview_camera(request: PreviewStartRequest) -> dict[str, Any]:
        try:
            return await coordinator.switch_preview_camera(request)
        except Exception as error:
            raise HTTPException(
                status_code=409,
                detail=_operation_error_detail(
                    error,
                    code="camera_switch_failed",
                    component="video",
                    hint="检查摄像头是否被其他程序占用",
                ),
            ) from error

    @app.post("/api/v1/characterizations/start")
    async def characterization_start(
        request: CharacterizationStartRequest,
    ) -> dict[str, Any]:
        try:
            return await coordinator.start_characterization(request)
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/characterizations/stages/start")
    async def characterization_stage_start(
        request: CharacterizationStageRequest,
    ) -> dict[str, Any]:
        try:
            return await coordinator.start_characterization_stage(request)
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/characterizations/stages/stop")
    async def characterization_stage_stop() -> dict[str, Any]:
        try:
            return await coordinator.stop_characterization_stage()
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/characterizations/stop")
    async def characterization_stop() -> dict[str, Any]:
        try:
            return await coordinator.stop_characterization()
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/v1/characterizations")
    async def characterizations() -> list[dict[str, Any]]:
        return coordinator.list_characterizations()

    @app.get("/api/v1/recordings")
    async def recordings() -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in coordinator.catalog.list()]

    @app.get("/api/v1/recordings/{recording_id}")
    async def recording(recording_id: str) -> dict[str, Any]:
        return required(recording_id).model_dump(mode="json")

    @app.get("/api/v1/recordings/{recording_id}/publish/estimate")
    async def publish_estimate(recording_id: str) -> dict[str, Any]:
        required(recording_id)
        try:
            return coordinator.publish_estimate(recording_id)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/recordings/{recording_id}/publish")
    async def publish(recording_id: str) -> dict[str, Any]:
        required(recording_id)
        try:
            return await coordinator.publish(recording_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/v1/recordings/{recording_id}/publish/status")
    async def publish_status(recording_id: str) -> dict[str, Any]:
        required(recording_id)
        try:
            return (
                await coordinator.refresh_publish_status(recording_id)
            ).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.delete("/api/v1/recordings/{recording_id}")
    async def delete(
        recording_id: str, request: RecordingDeleteRequest
    ) -> dict[str, str]:
        required(recording_id)
        try:
            return {
                "status": "deleted",
                "path": str(
                    coordinator.delete_recording(recording_id, request.confirmation)
                ),
            }
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/maintenance/incomplete")
    async def incomplete() -> list[dict[str, object]]:
        return coordinator.incomplete_files()

    @app.post("/api/v1/maintenance/quarantine")
    async def quarantine(request: QuarantineRequest) -> dict[str, str]:
        try:
            return {
                "status": "quarantined",
                "path": str(coordinator.quarantine_file(request.relative_path)),
            }
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/maintenance/rebuild-catalog")
    async def rebuild() -> dict[str, int]:
        return coordinator.rebuild_catalog()

    @app.get("/api/v1/recordings/{recording_id}/validate")
    async def validate(recording_id: str) -> dict[str, Any]:
        summary = required(recording_id)
        report = validate_capture_h5(Path(summary.h5_path or ""), coordinator.taxonomy)
        return {
            "ready": report.ready,
            "issues": report.issues,
            "metrics": report.metrics,
        }

    @app.get("/api/v1/recordings/{recording_id}/video")
    async def video(recording_id: str) -> FileResponse:
        summary = required(recording_id)
        path = Path(summary.mkv_path or "")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="找不到视频")
        return FileResponse(path, media_type="video/x-matroska", filename=path.name)

    @app.get("/api/v1/preview.mjpeg")
    async def preview(
        request: Request,
        stream: Annotated[int, Query(ge=1)],
    ) -> StreamingResponse:
        initial = coordinator.preview_stream.snapshot()
        if not initial.active or initial.session_id != stream:
            raise HTTPException(status_code=409, detail="预览通道尚未建立或已经释放")

        async def body():
            generation = -1
            while True:
                if await request.is_disconnected():
                    return
                frame = coordinator.preview_stream.snapshot()
                if not frame.active or frame.session_id != initial.session_id:
                    return
                if frame.jpeg is not None and frame.generation != generation:
                    generation = frame.generation
                    yield _mjpeg_part(frame.jpeg)
                elif frame.jpeg is None:
                    generation = frame.generation
                await coordinator.preview_stream.wait_for_change(
                    initial.session_id,
                    generation,
                )

        return StreamingResponse(
            body(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "X-Accel-Buffering": "no",
            },
        )

    @app.websocket("/api/v1/live")
    async def live(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(coordinator.snapshot())
                await asyncio.sleep(0.25)
        except (WebSocketDisconnect, RuntimeError):
            return

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist-capture"
    if frontend.is_dir():
        assets = frontend / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="capture-assets")

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
            return {"message": "采集前端尚未构建"}

    return app
