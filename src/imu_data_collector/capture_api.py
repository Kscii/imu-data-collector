"""只负责设备、录制、诊断、发布与本地维护的采集 API。"""

from __future__ import annotations

import asyncio
import html
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from imu_data_collector.api_errors import structured_http_error_handler
from imu_data_collector.ble import BleOperationError, CW12EUBleSource
from imu_data_collector.broker_client import (
    publish_device_configuration_via_broker,
    refresh_device_configuration_via_broker,
    reserve_device_identity_via_broker,
)
from imu_data_collector.build_info import CAPTURE_API_BUILD_ID
from imu_data_collector.config import Settings, load_settings
from imu_data_collector.coordinator import RecordingCoordinator
from imu_data_collector.device_binding import DeviceBindingStore
from imu_data_collector.device_configuration import (
    ConfigurationSelectionRequest,
    ConfigurationSnapshotSubmission,
    IdentityRevisionRequest,
    LocalConfigurationManager,
)
from imu_data_collector.device_registry import (
    SENSOR_SN_RE,
    CandidateConversion,
    DeviceCandidateStore,
    DeviceDraftStore,
    ImuDeviceProfile,
    available_device_profiles,
    load_device_registry,
)
from imu_data_collector.host import platform_id, resource_path
from imu_data_collector.imu_protocols import PROTOCOLS
from imu_data_collector.models import (
    CharacterizationStageRequest,
    CharacterizationStartRequest,
    PreviewStartRequest,
    QuarantineRequest,
    RecordingDeleteRequest,
    RecordingStartRequest,
)
from imu_data_collector.storage import ObjectConflictError
from imu_data_collector.validation import validate_capture_h5


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


def _oauth_result_page(
    request: Request,
    *,
    ok: bool,
    zh: str,
    en: str,
) -> str:
    language = request.headers.get("accept-language", "").lower()
    message = en if language.startswith("en") else zh
    title = (
        ("Google sign-in succeeded" if ok else "Google sign-in failed")
        if language.startswith("en")
        else ("Google 登录成功" if ok else "Google 登录失败")
    )
    event = "imu-oauth-success" if ok else "imu-oauth-failed"
    return f"""<!doctype html>
<html lang=\"{'en' if language.startswith('en') else 'zh-CN'}\"><meta charset=\"utf-8\">
<title>{html.escape(title)}</title>
<body><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>
<script>
if (window.opener) window.opener.postMessage({event!r}, window.location.origin);
if ({str(ok).lower()}) window.setTimeout(() => window.close(), 600);
</script></body></html>"""


def create_capture_app(settings: Settings | None = None) -> FastAPI:
    active = settings or load_settings()
    active.use_cached_device_registry_if_newer()
    defaults = Settings()
    configuration_data_root = active.device_configuration_root
    configuration_cache_root = active.device_configuration_cache_root
    if settings is not None and configuration_data_root == defaults.device_configuration_root:
        configuration_data_root = active.catalog_path.parent / "device-config"
    if (
        settings is not None
        and configuration_cache_root == defaults.device_configuration_cache_root
    ):
        configuration_cache_root = active.catalog_path.parent / "device-config-cache"
    configuration_manager = LocalConfigurationManager(
        configuration_data_root,
        configuration_cache_root,
        active.device_registry_path,
    )
    active.device_configuration_root = configuration_data_root
    active.device_configuration_cache_root = configuration_cache_root
    active.configuration_manager = configuration_manager
    preferred_sn = active.default_sensor_sn or active.imu.sensor_sn
    try:
        active.imu = configuration_manager.resolve_imu(preferred_sn)
    except (KeyError, ValueError):
        first_active = next(
            (
                item.sensor_sn
                for item in configuration_manager.selected_snapshot().content.devices
                if item.lifecycle == "active" and item.protocol_id in PROTOCOLS
            ),
            None,
        )
        if first_active is not None:
            try:
                active.imu = configuration_manager.resolve_imu(first_active)
            except ValueError:
                # Keep the local runtime fallback so the UI can explain a
                # revoked selection; session resolution remains blocked.
                pass
    coordinator = RecordingCoordinator(active)
    draft_store = DeviceDraftStore(active.device_drafts_path)
    candidate_store = DeviceCandidateStore(active.device_candidates_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # This scan only touches the local catalog and small H5 headers. Keep it
        # in the lifespan thread so startup completion is deterministic on
        # frozen Windows/macOS runtimes whose default executor may be torn down
        # while the desktop shell is still opening.
        _app.state.startup_revalidation = coordinator.revalidate_unuploaded_recordings()
        _app.state.background_jobs = await coordinator.start_background_jobs()
        registry_task: asyncio.Task[Any] | None = None
        if active.device_registry_auto_refresh and coordinator.cloud_auth.configured:
            logged_in = await asyncio.to_thread(
                lambda: coordinator.cloud_auth.logged_in
            )
            if logged_in:
                async def refresh_registry_safely() -> None:
                    try:
                        await refresh_device_configuration_via_broker(
                            active,
                            coordinator.cloud_auth,
                            configuration_manager,
                        )
                        activate_selected_configuration()
                    except Exception:
                        pass
                    try:
                        await coordinator.refresh_device_registry()
                    except Exception:
                        pass

                registry_task = asyncio.create_task(refresh_registry_safely())
        yield
        if registry_task and not registry_task.done():
            registry_task.cancel()
        await coordinator.shutdown()

    app = FastAPI(title="IMU 数据采集端", version="0.3.0", lifespan=lifespan)
    app.add_exception_handler(HTTPException, structured_http_error_handler)
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

    def require_configuration_idle() -> None:
        if (
            coordinator.mode is not None
            or coordinator.current is not None
            or coordinator.ble is not None
            or coordinator.video is not None
        ):
            raise HTTPException(
                status_code=409,
                detail="请先结束录制并断开预览设备，再切换配置 Snapshot",
            )

    def activate_selected_configuration() -> None:
        snapshot = configuration_manager.selected_snapshot()
        preferred = active.imu.sensor_sn
        available = [
            item
            for item in snapshot.content.devices
            if item.lifecycle == "active" and item.protocol_id in PROTOCOLS
        ]
        selected = next((item for item in available if item.sensor_sn == preferred), None)
        selected = selected or (available[0] if available else None)
        if selected is not None:
            active.imu = active.resolve_imu(selected.sensor_sn)

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
            "operator_unikeys": list(active.identity.allowed_unikeys),
            "data_tiers": ["test", "prod"],
            "default_data_tier": active.capture.default_data_tier,
            "background_jobs": {
                "allow_during_recording": active.background_jobs.allow_during_recording,
                "automatic_prod_publish": active.publish.mode != "disabled",
                "max_attempts": len(active.background_jobs.retry_delays_seconds) + 1,
            },
            "imu": {
                "sensor_sn": active.imu.sensor_sn,
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
                "mode": active.publish.mode,
                "backend": (
                    "broker"
                    if active.publish.mode == "broker"
                    else active.storage.backend
                ),
                "bucket": active.storage.bucket,
                "cloud_configured": coordinator.cloud_auth.configured,
            },
            "device_registry": coordinator.device_registry_status(),
            "configuration": configuration_manager.status(),
            "runtime_configuration": {
                "device_registry_path": str(active.device_registry_path),
                "activity_taxonomy_path": str(active.activity_taxonomy_path),
                "configuration_root": str(active.device_configuration_root),
                "configuration_cache_root": str(active.device_configuration_cache_root),
                "editable": False,
                "restart_required_for_changes": True,
            },
        }

    @app.get("/api/v1/cloud/status")
    async def cloud_status() -> dict[str, Any]:
        return await asyncio.to_thread(coordinator.cloud_auth.status)

    @app.post("/api/v1/cloud/oauth/start")
    async def cloud_oauth_start() -> dict[str, str]:
        redirect_uri = (
            f"http://127.0.0.1:{active.server_port}/api/v1/cloud/oauth/callback"
        )
        try:
            url = await asyncio.to_thread(coordinator.cloud_auth.begin, redirect_uri)
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"authorization_url": url}

    @app.get("/api/v1/cloud/oauth/callback", response_class=HTMLResponse)
    async def cloud_oauth_callback(
        request: Request,
        state: str,
        code: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        if error or not code:
            return HTMLResponse(
                _oauth_result_page(
                    request,
                    ok=False,
                    zh="Google 登录未完成，请关闭此窗口后重试。",
                    en="Google sign-in was not completed. Close this window and try again.",
                ),
                status_code=400,
            )
        try:
            await asyncio.to_thread(
                coordinator.cloud_auth.complete,
                state=state,
                code=code,
            )
        except RuntimeError as caught:
            return HTMLResponse(
                _oauth_result_page(
                    request,
                    ok=False,
                    zh=f"Google 登录失败：{caught}",
                    en=f"Google sign-in failed: {caught}",
                ),
                status_code=400,
            )
        await asyncio.to_thread(coordinator.resume_uploads_after_login)
        if active.device_registry_auto_refresh:
            try:
                await refresh_device_configuration_via_broker(
                    active,
                    coordinator.cloud_auth,
                    configuration_manager,
                )
                activate_selected_configuration()
            except Exception:
                pass
            try:
                await coordinator.refresh_device_registry()
            except Exception:
                pass
        return HTMLResponse(
            _oauth_result_page(
                request,
                ok=True,
                zh="Google 登录成功，待上传任务将自动继续。",
                en="Google sign-in succeeded. Pending uploads will resume automatically.",
            )
        )

    @app.post("/api/v1/cloud/logout")
    async def cloud_logout() -> dict[str, Any]:
        await asyncio.to_thread(coordinator.cloud_auth.logout)
        return await asyncio.to_thread(coordinator.cloud_auth.status)

    @app.get("/api/v1/device-registry/status")
    async def device_registry_status() -> dict[str, Any]:
        return coordinator.device_registry_status()

    @app.post("/api/v1/device-registry/refresh")
    async def refresh_device_registry() -> dict[str, Any]:
        try:
            return await coordinator.refresh_device_registry()
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/v1/configuration/status")
    async def configuration_status() -> dict[str, Any]:
        return configuration_manager.status()

    @app.get("/api/v1/configuration/snapshots")
    async def configuration_snapshots() -> list[dict[str, Any]]:
        return configuration_manager.list_snapshots()

    @app.get("/api/v1/configuration/snapshots/{snapshot_id}")
    async def configuration_snapshot(snapshot_id: str) -> dict[str, Any]:
        try:
            snapshot = configuration_manager.snapshot(snapshot_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="找不到配置 Snapshot") from error
        return {
            "snapshot": snapshot.model_dump(mode="json"),
            "state": configuration_manager.state(snapshot_id),
            "selected": configuration_manager.selected_snapshot_id() == snapshot_id,
            "current": (
                configuration_manager.status().get("current_snapshot_id") == snapshot_id
            ),
        }

    @app.get("/api/v1/configuration/workspace")
    async def configuration_workspace() -> dict[str, Any]:
        return configuration_manager.workspace().model_dump(mode="json")

    @app.put("/api/v1/configuration/workspace")
    async def save_configuration_workspace(
        submission: ConfigurationSnapshotSubmission,
    ) -> dict[str, Any]:
        return configuration_manager.save_workspace(submission).model_dump(mode="json")

    @app.post("/api/v1/configuration/local-snapshots")
    async def save_local_configuration_snapshot(
        submission: ConfigurationSnapshotSubmission,
    ) -> dict[str, Any]:
        try:
            snapshot = configuration_manager.save_local(submission)
        except (ObjectConflictError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "snapshot": snapshot.model_dump(mode="json"),
            "state": "local",
            "production_authority": False,
        }

    @app.post("/api/v1/configuration/publish", status_code=201)
    async def publish_configuration_snapshot(
        submission: ConfigurationSnapshotSubmission,
    ) -> dict[str, Any]:
        try:
            return await publish_device_configuration_via_broker(
                submission,
                active,
                coordinator.cloud_auth,
                configuration_manager,
            )
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/configuration/refresh")
    async def refresh_configuration_snapshots() -> dict[str, Any]:
        require_configuration_idle()
        try:
            result = await refresh_device_configuration_via_broker(
                active,
                coordinator.cloud_auth,
                configuration_manager,
            )
            activate_selected_configuration()
            return result
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/configuration/identities/assets", status_code=201)
    async def reserve_configuration_asset() -> dict[str, Any]:
        try:
            return await reserve_device_identity_via_broker(
                active,
                coordinator.cloud_auth,
            )
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/configuration/identities/revisions", status_code=201)
    async def reserve_configuration_revision(
        request: IdentityRevisionRequest,
    ) -> dict[str, Any]:
        try:
            return await reserve_device_identity_via_broker(
                active,
                coordinator.cloud_auth,
                hardware_asset_id=request.hardware_asset_id,
            )
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/configuration/select")
    async def select_configuration(
        request: ConfigurationSelectionRequest,
    ) -> dict[str, Any]:
        require_configuration_idle()
        try:
            configuration_manager.select(request.snapshot_id)
            activate_selected_configuration()
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="找不到配置 Snapshot") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return configuration_manager.status()

    @app.post("/api/v1/configuration/reset-current")
    async def reset_configuration_current() -> dict[str, Any]:
        require_configuration_idle()
        configuration_manager.reset_current()
        activate_selected_configuration()
        return configuration_manager.status()

    @app.get("/api/v1/device-capabilities")
    async def device_capabilities() -> dict[str, Any]:
        return {
            "configuration_schema_versions": ["2.0"],
            "protocol_ids": sorted(PROTOCOLS),
            "unknown_protocol_policy": "disable_device_keep_snapshot",
            "unknown_schema_policy": "reject_snapshot_keep_lkg",
        }

    @app.get("/api/v1/taxonomy")
    async def taxonomy() -> dict[str, Any]:
        return coordinator.taxonomy

    @app.get("/api/v1/devices")
    async def devices(
        scan_ble: Annotated[bool, Query(description="执行五秒主动 BLE 扫描")] = False,
        refresh_cameras: Annotated[
            bool, Query(description="忽略会话缓存并重新枚举摄像头")
        ] = False,
    ) -> dict[str, Any]:
        try:
            cameras = await coordinator.list_cameras(refresh=refresh_cameras)
        except Exception as error:
            raise HTTPException(
                status_code=409,
                detail=_operation_error_detail(
                    error,
                    code="camera_discovery_failed",
                    component="video",
                    hint=(
                        "请在系统设置 → 隐私与安全性 → 相机中允许 "
                        "IMU Data Collector，然后重新扫描摄像头"
                        if platform_id() == "macos"
                        else "请确认摄像头已连接且未被其他程序占用"
                    ),
                ),
            ) from error
        selected_configuration = configuration_manager.selected_snapshot()
        selected_configuration_state = configuration_manager.state(
            selected_configuration.snapshot_id
        )
        profile_entries = selected_configuration.content.devices
        binding_store = DeviceBindingStore()
        imu_profiles: list[dict[str, Any]] = []
        scan_profiles = []
        for profile in profile_entries:
            protocol_supported = profile.protocol_id in PROTOCOLS
            resolved = (
                active.resolve_imu(profile.sensor_sn)
                if protocol_supported and selected_configuration_state != "revoked"
                else None
            )
            local_candidate = candidate_store.get(profile.sensor_sn)
            binding = binding_store.status(
                sensor_sn=profile.sensor_sn,
                expected_name=profile.identity.advertised_name,
                notify_uuid=(
                    resolved.notify_uuid
                    if resolved is not None
                    else profile.identity.advertised_service_uuid or "unsupported"
                ),
            )
            if resolved is not None and binding["local_device_id"]:
                resolved.local_device_id = binding["local_device_id"]
            if resolved is not None:
                scan_profiles.append(resolved)
            si = profile.si_profile
            diagnostic_conversion = None
            diagnostic_source = None
            if local_candidate is not None:
                diagnostic_conversion = local_candidate.model_dump(mode="json")
                diagnostic_source = "local_override"
            elif selected_configuration_state != "approved" or not si.verified:
                diagnostic_conversion = {
                    "accel_counts_per_g": si.accel_counts_per_g,
                    "gyro_counts_per_dps": si.gyro_counts_per_dps,
                    "accel_bias_counts": list(si.accel_bias_counts),
                    "gyro_bias_counts": list(si.gyro_bias_counts),
                    "raw_axis_order": list(si.raw_axis_order),
                    "axis_signs": list(si.axis_signs),
                    "evidence_status": si.method,
                }
                diagnostic_source = selected_configuration_state
            imu_profiles.append(
                {
                    "sensor_sn": profile.sensor_sn,
                    "display_name": profile.display_name,
                    "advertised_name": profile.identity.advertised_name,
                    "public_address": profile.identity.public_address,
                    "protocol_id": profile.protocol_id,
                    "expected_rate_hz": profile.expected_rate_hz,
                    "lifecycle": profile.lifecycle,
                    "source": configuration_manager.status()["selected_source"],
                    "selectable": bool(
                        profile.lifecycle != "retired"
                        and protocol_supported
                        and selected_configuration_state != "revoked"
                    ),
                    "protocol_supported": protocol_supported,
                    "unsupported_reason": (
                        None
                        if protocol_supported and selected_configuration_state != "revoked"
                        else (
                            "该配置 Snapshot 已撤销，不能用于新采集"
                            if selected_configuration_state == "revoked"
                            else "当前客户端不支持该 protocol ID，请升级客户端"
                        )
                    ),
                    "prod_capture_enabled": bool(
                        selected_configuration_state == "approved"
                        and si.verified
                        and "prod" in profile.allowed_data_tiers
                    ),
                    "si_profile_id": si.profile_id,
                    "si_verified": si.verified,
                    "candidate_conversion": diagnostic_conversion,
                    "candidate_conversion_source": diagnostic_source,
                    "binding": binding,
                }
            )
        next_asset_number = max(
            (int(item.hardware_asset_id[-4:]) for item in profile_entries),
            default=0,
        ) + 1
        ble: list[dict[str, Any]] = []
        ble_scan: dict[str, Any] = {
            "requested": False,
            "adapter_state": "unknown",
            "target_name": active.imu.name,
            "target_address": active.imu.address,
            "target_found": False,
            "elapsed_ms": 0.0,
            "error": None,
        }
        if scan_ble:
            try:
                ble_scan = await CW12EUBleSource.discover_with_diagnostics(
                    settings=active.imu,
                    profiles=scan_profiles,
                )
                ble = list(ble_scan.pop("devices"))
            except BleOperationError as error:
                ble_scan.update(
                    {
                        "requested": True,
                        "adapter_state": (
                            "unavailable"
                            if error.code == "ble_adapter_unavailable"
                            else "error"
                        ),
                        "error": error.as_detail(),
                    }
                )
            except Exception as error:
                ble_scan.update(
                    {
                        "requested": True,
                        "adapter_state": "error",
                        "error": _operation_error_detail(
                            error,
                            code="ble_scan_failed",
                            component="ble",
                            hint=(
                                "请检查系统设置 → 隐私与安全性 → 蓝牙后重试"
                                if platform_id() == "macos"
                                else "请检查 Windows 蓝牙服务和适配器状态后重试"
                                if platform_id() == "windows"
                                else "请检查 BlueZ 服务和适配器状态后重试"
                            ),
                        ),
                    }
                )
        permissions: dict[str, Any] = {}
        if platform_id() == "macos":
            from imu_data_collector.macos_devices import permission_statuses

            permissions = await asyncio.to_thread(permission_statuses)
        report_runtime_selection = (
            coordinator.mode is not None or active.default_sensor_sn is not None
        )
        selected_sensor_sn = (
            active.imu.sensor_sn
            if report_runtime_selection
            and any(
                profile.sensor_sn == active.imu.sensor_sn
                for profile in profile_entries
            )
            else None
        )
        return {
            "cameras": cameras,
            "ble": ble,
            "ble_scan": ble_scan,
            "platform": platform_id(),
            "permissions": permissions,
            "selected_sensor_sn": selected_sensor_sn,
            "default_sensor_sn": active.default_sensor_sn,
            "suggested_sensor_sn": f"IMU-{next_asset_number:04d}-R01",
            "imu_profiles": imu_profiles,
            "imu_binding": binding_store.status(
                sensor_sn=active.imu.sensor_sn,
                expected_name=active.imu.name,
                notify_uuid=active.imu.notify_uuid,
            ),
            "device_registry": coordinator.device_registry_status(),
            "configuration": configuration_manager.status(),
        }

    @app.post("/api/v1/devices/imu-drafts")
    async def add_imu_draft(profile: ImuDeviceProfile) -> dict[str, Any]:
        if profile.calibration_evidence_path is not None:
            raise HTTPException(
                status_code=422,
                detail="本机 commissioning 草稿不能声明正式校准证据",
            )
        registry = load_device_registry(active.device_registry_path)
        if any(item.sensor_sn == profile.sensor_sn for item in registry.devices):
            raise HTTPException(status_code=409, detail="该 SN 已存在于正式设备注册表")
        wanted_address = (profile.identity.public_address or "").upper()
        if wanted_address and any(
            (item.identity.public_address or "").upper() == wanted_address
            for item, _source in available_device_profiles(
                active.device_registry_path, draft_store
            )
        ):
            raise HTTPException(status_code=409, detail="该 BLE 地址已分配给其他 SN")
        try:
            saved = draft_store.save(profile)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "profile": saved.model_dump(mode="json"),
            "source": "local_draft",
            "production_authority": False,
        }

    @app.get("/api/v1/devices/imu-drafts/{sensor_sn}/export")
    async def export_imu_draft(
        sensor_sn: str,
        format: Annotated[str, Query(pattern="^(yaml|markdown)$")] = "yaml",
    ) -> PlainTextResponse:
        if SENSOR_SN_RE.fullmatch(sensor_sn) is None:
            raise HTTPException(status_code=422, detail="SN 格式无效")
        try:
            yaml_text = draft_store.export_yaml(sensor_sn)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到本机设备草稿") from error
        body = yaml_text
        media_type = "application/yaml"
        suffix = "yaml"
        if format == "markdown":
            body = (
                f"# IMU device commissioning draft: {sensor_sn}\n\n"
                "This file is not production authority. Review the evidence and "
                "merge the YAML profile into `configs/imu-devices.yaml`.\n\n"
                f"```yaml\n{yaml_text}```\n"
            )
            media_type = "text/markdown"
            suffix = "md"
        return PlainTextResponse(
            body,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{sensor_sn}.{suffix}"'
            },
        )

    @app.put("/api/v1/devices/imu-candidates/{sensor_sn}")
    async def save_imu_candidate(
        sensor_sn: str,
        candidate: CandidateConversion,
    ) -> dict[str, Any]:
        if not any(
            profile.sensor_sn == sensor_sn
            for profile, _source in available_device_profiles(
                active.device_registry_path, draft_store
            )
        ):
            raise HTTPException(status_code=404, detail="找不到该 IMU SN")
        saved = candidate_store.save(sensor_sn, candidate)
        return {
            "sensor_sn": sensor_sn,
            "candidate_conversion": saved.model_dump(mode="json"),
            "source": "local_override",
            "production_authority": False,
        }

    @app.delete("/api/v1/devices/imu-candidates/{sensor_sn}")
    async def delete_imu_candidate(sensor_sn: str) -> dict[str, Any]:
        return {"removed": candidate_store.delete(sensor_sn)}

    @app.get("/api/v1/devices/imu-candidates/{sensor_sn}/export")
    async def export_imu_candidate(sensor_sn: str) -> PlainTextResponse:
        try:
            body = candidate_store.export_yaml(sensor_sn)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到本机候选 SI") from error
        return PlainTextResponse(
            body,
            media_type="application/yaml",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{sensor_sn}-candidate-conversion.yaml"'
                )
            },
        )

    @app.delete("/api/v1/devices/imu-drafts/{sensor_sn}")
    async def delete_imu_draft(sensor_sn: str) -> dict[str, bool]:
        return {"deleted": draft_store.delete(sensor_sn)}

    @app.delete("/api/v1/devices/imu-binding")
    async def forget_imu_binding(
        sensor_sn: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        """忘记本机 CoreBluetooth UUID；当前已建立的连接不受影响。"""

        store = DeviceBindingStore()
        selected_sn = sensor_sn or active.imu.sensor_sn
        try:
            selected = active.resolve_imu(selected_sn)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到该 IMU SN") from error
        store.forget_imu(selected_sn)
        if selected_sn == active.imu.sensor_sn:
            active.imu.local_device_id = None
        return store.status(
            sensor_sn=selected_sn,
            expected_name=selected.name,
            notify_uuid=selected.notify_uuid,
        )

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

    @app.post("/api/v1/recordings/stop", status_code=202)
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
            detail = coordinator.device_error or _operation_error_detail(
                error,
                code="preview_start_failed",
                component="ble_video",
                hint="确认 IMU 未被手机占用并处于可连接状态",
            )
            raise HTTPException(
                status_code=409,
                detail=detail,
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

    @app.post("/api/v1/recordings/{recording_id}/publish", status_code=202)
    async def publish(recording_id: str) -> dict[str, Any]:
        required(recording_id)
        try:
            return await coordinator.publish(recording_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/api/v1/recordings/{recording_id}/finalization/retry",
        status_code=202,
    )
    async def retry_finalization(recording_id: str) -> dict[str, Any]:
        required(recording_id)
        try:
            return await coordinator.retry_finalization(recording_id)
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
            "blocking_issues": report.issues,
            "quality_warnings": report.warnings,
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

    frontend = resource_path("frontend/dist-capture")
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
