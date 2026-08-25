"""仅限本机访问的 FastAPI 采集、监控与标注应用。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from imu_data_collector.ble import CW12EUBleSource
from imu_data_collector.config import Settings, load_settings
from imu_data_collector.coordinator import RecordingCoordinator
from imu_data_collector.models import (
    AnnotationDocument,
    CharacterizationStageRequest,
    CharacterizationStartRequest,
    PreviewStartRequest,
    RecordingStartRequest,
    SyncDocument,
)
from imu_data_collector.validation import validate_capture_h5
from imu_data_collector.video import discover_video_devices


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or load_settings()
    coordinator = RecordingCoordinator(active_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await coordinator.shutdown()

    app = FastAPI(title="IMU 数据采集平台", version="0.1.0", lifespan=lifespan)
    app.state.coordinator = coordinator
    app.state.settings = active_settings

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, **coordinator.snapshot()}

    @app.get("/api/v1/config")
    async def config() -> dict[str, Any]:
        return {
            "data_root": str(active_settings.data_root),
            "minimum_free_gib": active_settings.minimum_free_gib,
            "allowed_unikeys": list(active_settings.identity.allowed_unikeys),
            "data_tiers": ["test", "prod"],
            "default_data_tier": "test",
            "imu": {
                "name": active_settings.imu.name,
                "address": active_settings.imu.address,
                "expected_rate_hz": active_settings.imu.expected_rate_hz,
                "expected_rate_status": active_settings.imu.expected_rate_status,
                "calibration_verified": bool(
                    active_settings.imu.accel_counts_per_g
                    and active_settings.imu.gyro_counts_per_dps
                ),
            },
            "video": {
                "width": active_settings.video.width,
                "height": active_settings.video.height,
                "requested_fps": active_settings.video.requested_fps,
                "bitrate": active_settings.video.bitrate,
            },
            "upload": {
                "enabled": active_settings.upload.enabled,
                "remote": active_settings.upload.remote,
                "remote_root_configured": bool(active_settings.upload.remote_root),
            },
        }

    @app.get("/api/v1/taxonomy")
    async def taxonomy() -> dict[str, Any]:
        return coordinator.taxonomy

    @app.get("/api/v1/devices")
    async def devices(
        scan_ble: Annotated[bool, Query(description="执行五秒主动 BLE 扫描")] = False,
    ) -> dict[str, Any]:
        cameras = await discover_video_devices(active_settings.video)
        ble: list[dict[str, Any]] = []
        if scan_ble:
            try:
                ble = await CW12EUBleSource.discover(settings=active_settings.imu)
            except Exception as error:
                ble = [{"error": str(error)}]
        return {"cameras": cameras, "ble": ble}

    @app.post("/api/v1/recordings/start")
    async def start(request: RecordingStartRequest) -> dict[str, Any]:
        try:
            summary = await coordinator.start(request)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return summary.model_dump(mode="json")

    @app.post("/api/v1/preflight/start")
    async def start_preview(request: PreviewStartRequest) -> dict[str, Any]:
        try:
            return await coordinator.start_preview(request)
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/preflight/stop")
    async def stop_preview() -> dict[str, Any]:
        try:
            return await coordinator.stop_preview()
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/recordings/stop")
    async def stop() -> dict[str, Any]:
        try:
            summary = await coordinator.stop()
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return summary.model_dump(mode="json")

    @app.post("/api/v1/characterizations/start")
    async def start_characterization(
        request: CharacterizationStartRequest,
    ) -> dict[str, Any]:
        try:
            return await coordinator.start_characterization(request)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/characterizations/stages/start")
    async def start_characterization_stage(
        request: CharacterizationStageRequest,
    ) -> dict[str, Any]:
        try:
            return await coordinator.start_characterization_stage(request)
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/characterizations/stages/stop")
    async def stop_characterization_stage() -> dict[str, Any]:
        try:
            return await coordinator.stop_characterization_stage()
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/characterizations/stop")
    async def stop_characterization() -> dict[str, Any]:
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

    def required_recording(recording_id: str):
        summary = coordinator.catalog.get(recording_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="找不到该录制")
        return summary

    @app.get("/api/v1/recordings/{recording_id}")
    async def recording(recording_id: str) -> dict[str, Any]:
        return required_recording(recording_id).model_dump(mode="json")

    @app.get("/api/v1/recordings/{recording_id}/timeline")
    async def timeline(
        recording_id: str,
        max_points: Annotated[int, Query(ge=100, le=20_000)] = 5_000,
    ) -> dict[str, Any]:
        required_recording(recording_id)
        return coordinator.timeline(recording_id, max_points)

    @app.get("/api/v1/recordings/{recording_id}/annotations")
    async def annotations(recording_id: str) -> dict[str, Any]:
        required_recording(recording_id)
        return coordinator.annotations(recording_id).model_dump(mode="json")

    @app.put("/api/v1/recordings/{recording_id}/annotations")
    async def save_annotations(
        recording_id: str, document: AnnotationDocument
    ) -> dict[str, Any]:
        required_recording(recording_id)
        try:
            saved = await coordinator.save_annotations(recording_id, document)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return saved.model_dump(mode="json")

    @app.put("/api/v1/recordings/{recording_id}/sync")
    async def save_sync(recording_id: str, document: SyncDocument) -> dict[str, Any]:
        required_recording(recording_id)
        try:
            return await coordinator.save_sync(recording_id, document)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/recordings/{recording_id}/sync")
    async def sync(recording_id: str) -> dict[str, Any]:
        required_recording(recording_id)
        return coordinator.sync(recording_id)

    @app.post("/api/v1/recordings/{recording_id}/upload")
    async def upload(recording_id: str) -> dict[str, str]:
        required_recording(recording_id)
        try:
            await coordinator.upload(recording_id)
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"status": "verified"}

    @app.get("/api/v1/recordings/{recording_id}/validate")
    async def validate(recording_id: str) -> dict[str, Any]:
        summary = required_recording(recording_id)
        report = validate_capture_h5(
            Path(summary.h5_path or ""), coordinator.taxonomy, require_sync=False
        )
        return {
            "ready": report.ready,
            "issues": report.issues,
            "metrics": report.metrics,
        }

    @app.get("/api/v1/recordings/{recording_id}/video")
    async def video(recording_id: str) -> FileResponse:
        summary = required_recording(recording_id)
        path = Path(summary.mkv_path or "")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="找不到视频文件")
        return FileResponse(path, media_type="video/x-matroska", filename=path.name)

    @app.get("/api/v1/preview.mjpeg")
    async def preview(request: Request) -> StreamingResponse:
        async def stream():
            generation = -1
            while True:
                if await request.is_disconnected():
                    return
                recorder = coordinator.video
                if recorder is None:
                    return
                if recorder and recorder.latest_jpeg and recorder.preview_generation != generation:
                    generation = recorder.preview_generation
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                        + recorder.latest_jpeg
                        + b"\r\n"
                    )
                await asyncio.sleep(0.05)

        return StreamingResponse(
            stream(), media_type="multipart/x-mixed-replace; boundary=frame"
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

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend.is_dir():
        assets = frontend / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            candidate = frontend / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(frontend / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        async def no_frontend() -> dict[str, str]:
            return {"message": "前端尚未构建；请在 frontend/ 中运行 npm run build"}

    return app
