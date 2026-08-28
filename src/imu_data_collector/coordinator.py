"""串联 BLE、摄像头、HDF5 和发布的本机录制状态机。"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from imu_data_collector.ble import BleOperationError, CW12EUBleSource
from imu_data_collector.broker_client import (
    publish_recording_via_broker,
    read_index_receipt_via_broker,
)
from imu_data_collector.catalog import RecordingCatalog
from imu_data_collector.characterization import write_characterization_report
from imu_data_collector.config import Settings, load_activity_taxonomy
from imu_data_collector.cw12eu import (
    NotificationKind,
    classify_notification,
    parse_notification,
)
from imu_data_collector.desktop_auth import DesktopOAuthManager, OAuthLoginRequired
from imu_data_collector.finalization import (
    cleanup_partial_inputs,
    finalize_recording,
)
from imu_data_collector.hdf5_store import CaptureH5Writer
from imu_data_collector.maintenance import (
    hard_delete_recording,
    quarantine_incomplete,
    rebuild_catalog,
    scan_incomplete_files,
)
from imu_data_collector.models import (
    BackgroundJobKind,
    BackgroundJobState,
    CharacterizationStageRequest,
    CharacterizationStartRequest,
    DataTier,
    DeviceSessionState,
    IndexReceipt,
    PreviewStartRequest,
    PublishState,
    PublishTarget,
    RecordingStartRequest,
    RecordingState,
    RecordingSummary,
)
from imu_data_collector.publisher import publish_recording
from imu_data_collector.storage import create_object_store
from imu_data_collector.upload import RcloneRemoteStore
from imu_data_collector.validation import validate_capture_h5
from imu_data_collector.video import (
    FFmpegVideoRecorder,
    PreviewFrameHub,
    discover_video_devices,
    select_video_device,
)

logger = logging.getLogger(__name__)


class AuthenticationRequiredError(RuntimeError):
    """后台发布等待用户完成桌面 Google 登录。"""


class RecordingCoordinator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # 首次安装的 Windows/macOS 用户尚没有数据目录；健康接口需要在录制前
        # 就能统计磁盘空间，因此由应用启动负责创建，而不是等第一条录制。
        self.settings.data_root.mkdir(parents=True, exist_ok=True)
        self.taxonomy = load_activity_taxonomy(settings.activity_taxonomy_path)
        self.catalog = RecordingCatalog(settings.catalog_path)
        self.remote = RcloneRemoteStore(settings.upload)
        self.object_store = create_object_store(
            settings.storage.backend,
            settings.storage.root,
            settings.storage.bucket,
            settings.storage.project,
        )
        self.cloud_auth = DesktopOAuthManager(settings.cloud)
        # 目的地不一致必须在服务启动时暴露，不能等用户录完后才失败。
        _ = self.publish_target
        self._reconcile_legacy_publication_states()
        self.state = RecordingState.IDLE
        self.current: RecordingSummary | None = None
        self.ble: CW12EUBleSource | None = None
        self.video: FFmpegVideoRecorder | None = None
        self.writer: CaptureH5Writer | None = None
        self._consumer: asyncio.Task[Any] | None = None
        self._lock = asyncio.Lock()
        self._stop_consumer = asyncio.Event()
        self.latest_raw = np.zeros(6, dtype=np.int16)
        self.latest_packet_samples = 0
        self.packet_count = 0
        self.sample_count = 0
        self.observed_rate_hz = 0.0
        self.mode: str | None = None
        self.current_stage: dict[str, Any] | None = None
        self.last_characterization: dict[str, Any] | None = None
        self.preview_parse_errors = 0
        self._first_packet_ns: int | None = None
        self._monitoring_requested = False
        self._preview_camera_id: str | None = None
        self._preview_camera_profile: dict[str, Any] | None = None
        self.preview_stream = PreviewFrameHub()
        self.preview_error: str | None = None
        self.device_state = DeviceSessionState.IDLE
        self.device_error: dict[str, Any] | None = None
        self._device_operation_id = 0
        self._device_operation_started_ns: int | None = None
        self._preview_watchdog: asyncio.Task[Any] | None = None
        self._preview_reconnect_attempt = 0
        self._imu_reconnect_attempt = 0
        self._video_reconnect_attempt = 0
        self._imu_state = "idle"
        self._video_state = "idle"
        self._video_transition: str | None = None
        self._recording_accepts_imu = False
        self._preview_open_in_flight = False
        self._job_worker: asyncio.Task[Any] | None = None
        self._stop_jobs = asyncio.Event()
        self._jobs_changed = asyncio.Event()
        self._active_job: dict[str, str] | None = None
        self._camera_cache: list[dict[str, Any]] = []
        self._camera_cache_lock = asyncio.Lock()

    @property
    def publish_target(self) -> PublishTarget:
        target = PublishTarget(self.settings.publish.mode)
        if target == PublishTarget.BROKER and not self.cloud_auth.configured:
            raise RuntimeError("broker 发布模式缺少上传代理 URL 或 Google OAuth client ID")
        if target == PublishTarget.LOCAL and self.settings.storage.backend != "local":
            raise RuntimeError("local 发布模式要求 storage.backend=local")
        if target == PublishTarget.DIRECT_GCS and (
            self.settings.storage.backend != "gcs" or not self.settings.storage.bucket
        ):
            raise RuntimeError("direct_gcs 发布模式要求配置 GCS Bucket")
        return target

    def _reconcile_legacy_publication_states(self) -> None:
        """首个桌面正式版把旧本地“已上传”误报恢复为可云端发布。"""

        configured = PublishTarget(self.settings.publish.mode)
        for summary in self.catalog.list():
            if summary.publish_target != PublishTarget.DISABLED:
                continue
            if summary.upload_state not in {
                PublishState.UPLOADED,
                PublishState.PUBLISHED,
            }:
                continue
            if configured == PublishTarget.DIRECT_GCS:
                self.catalog.upsert(
                    summary.model_copy(
                        update={"publish_target": PublishTarget.DIRECT_GCS}
                    )
                )
                continue
            self.catalog.delete_job(summary.recording_id, BackgroundJobKind.PUBLISH)
            self.catalog.upsert(
                summary.model_copy(
                    update={
                        "upload_state": PublishState.STORED_LOCAL,
                        "publish_target": PublishTarget.LOCAL,
                        "index_state": "not_requested",
                        "index_message": (
                            "旧版本只写入了本机对象目录；登录后可重新上传到团队云端"
                        ),
                        "manifest_generation": None,
                    }
                )
            )

    def _require_allowed_unikey(self, value: str, field_name: str) -> None:
        if value not in self.settings.identity.allowed_unikeys:
            raise ValueError(f"{field_name} 不在当前 UniKey 白名单中：{value}")

    async def _resolve_camera(
        self, requested_camera_id: str | None = None
    ) -> dict[str, Any]:
        devices = await self.list_cameras()
        return select_video_device(devices, self.settings.video, requested_camera_id)

    async def list_cameras(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """复用本次后端会话的摄像头枚举；只有人工重扫才强制运行 FFmpeg 探测。"""

        async with self._camera_cache_lock:
            if refresh or not self._camera_cache:
                self._camera_cache = await discover_video_devices(self.settings.video)
            return [dict(item) for item in self._camera_cache]

    def _check_disk(self) -> None:
        self.settings.data_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.settings.data_root).free
        minimum = self.settings.minimum_free_gib * 1024**3
        if free < minimum:
            raise RuntimeError(
                f"free disk space {free / 1024**3:.1f} GiB is below "
                f"the configured {self.settings.minimum_free_gib} GiB minimum"
            )

    def _reset_live_imu_metrics(self) -> None:
        self.packet_count = 0
        self.sample_count = 0
        self.observed_rate_hz = 0.0
        self.latest_raw[:] = 0
        self.preview_parse_errors = 0
        self._first_packet_ns = None

    @staticmethod
    def _error_message(error: BaseException, fallback: str) -> str:
        """TimeoutError 等异常可能没有文本，用户界面不能因此显示空白。"""

        message = str(error).strip()
        return message or f"{fallback}（{type(error).__name__}）"

    def _next_device_operation(self, state: DeviceSessionState) -> int:
        self._device_operation_id += 1
        self._device_operation_started_ns = time.monotonic_ns()
        self.device_state = state
        return self._device_operation_id

    def _set_device_error(
        self,
        *,
        code: str,
        component: str,
        error: BaseException,
        hint: str,
        retryable: bool,
        fallback: str = "设备操作失败",
    ) -> None:
        if isinstance(error, BleOperationError):
            code = error.code
            component = "ble"
            hint = error.hint
            retryable = error.retryable
        message = self._error_message(error, fallback)
        self.device_error = {
            "code": code,
            "component": component,
            "phase": error.phase if isinstance(error, BleOperationError) else None,
            "message": message,
            "hint": hint,
            "retryable": retryable,
            "time_monotonic_ns": time.monotonic_ns(),
        }
        self.preview_error = message

    @staticmethod
    async def _bounded_cleanup(
        video: FFmpegVideoRecorder | None,
        ble: CW12EUBleSource | None,
        consumer: asyncio.Task[Any] | None,
    ) -> list[str]:
        """尽最大努力释放设备；任何单个驱动都不能无限阻塞状态机。"""

        issues: list[str] = []
        if consumer and consumer is not asyncio.current_task():
            consumer.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(consumer, return_exceptions=True), timeout=2.0
                )
            except TimeoutError:
                issues.append("IMU 消费任务在 2 秒内没有退出")
        if video:
            try:
                await asyncio.wait_for(video.stop(), timeout=6.0)
            except Exception as error:
                message = str(error).strip() or type(error).__name__
                issues.append(f"摄像头释放失败：{message}")
        if ble:
            try:
                await asyncio.wait_for(ble.stop(), timeout=5.0)
            except Exception as error:
                message = str(error).strip() or type(error).__name__
                issues.append(f"BLE 释放失败：{message}")
        return issues

    @staticmethod
    def _video_is_healthy(video: FFmpegVideoRecorder | None) -> bool:
        if video is None:
            return False
        process = getattr(video, "process", None)
        # 测试替身没有 process；真实 FFmpeg 对象必须仍在运行。
        return process is None or process.returncode is None

    def _new_video_recorder(
        self, camera: dict[str, Any] | str, output_path: Path | None
    ) -> FFmpegVideoRecorder:
        """创建视频进程；浏览器预览通道独立于具体 FFmpeg 进程。"""

        if isinstance(camera, dict):
            device = str(camera["device"])
            profile = dict(camera.get("selected_profile") or {})
        else:
            device = camera
            profile = dict(self._preview_camera_profile or {})

        return FFmpegVideoRecorder(
            self.settings.video,
            device,
            output_path,
            preview_hub=self.preview_stream,
            profile=profile or None,
        )

    async def _require_prod_camera_preflight(
        self, video: FFmpegVideoRecorder, data_tier: DataTier
    ) -> None:
        """正式数据必须在落盘前证明真实输入接近 30 FPS。"""

        if data_tier != DataTier.PROD:
            return
        deadline = time.monotonic() + 3.0
        while video.source_frame_count < 15 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if not video.control_state.ready:
            details = "；".join(video.control_state.errors) or "控制值未完整读回"
            raise RuntimeError(f"正式录制摄像头控制预检失败：{details}")
        if video.source_fps < self.settings.video.prod_min_source_fps:
            raise RuntimeError(
                "正式录制摄像头输入帧率不足："
                f"{video.source_fps:.2f} FPS < "
                f"{self.settings.video.prod_min_source_fps:.2f} FPS；"
                "请检查照明和摄像头后重试"
            )

    async def _stop_consumer_locked(self) -> None:
        self._stop_consumer.set()
        if not self._consumer:
            return
        try:
            await asyncio.wait_for(self._consumer, timeout=3.0)
        except TimeoutError:
            self._consumer.cancel()
            await asyncio.gather(self._consumer, return_exceptions=True)
        finally:
            self._consumer = None

    async def _open_preview_ble(self) -> CW12EUBleSource:
        """只连接 IMU；摄像头故障不应迫使 BLE 重新连接。"""

        ble = CW12EUBleSource(self.settings.imu)
        try:
            await asyncio.wait_for(ble.start(), timeout=20.0)
        except BaseException:
            await self._bounded_cleanup(None, ble, None)
            raise
        return ble

    async def _open_preview_video(
        self, camera_id: str | None
    ) -> tuple[dict[str, Any], FFmpegVideoRecorder]:
        """只启动摄像头；BLE 会话在视频切换时保持不变。"""

        camera = await self._resolve_camera(camera_id)
        video = self._new_video_recorder(camera, None)
        try:
            await asyncio.wait_for(video.start(), timeout=5.0)
        except BaseException:
            await self._bounded_cleanup(video, None, None)
            raise
        return camera, video

    def _start_preview_watchdog(self, operation_id: int) -> None:
        previous = self._preview_watchdog
        if previous and previous is not asyncio.current_task():
            previous.cancel()
        self._preview_watchdog = asyncio.create_task(
            self._preview_watchdog_loop(operation_id)
        )

    async def _preview_watchdog_loop(self, operation_id: int) -> None:
        """分别监控 IMU 与摄像头，任一故障都不得拆掉另一组件。"""

        try:
            while True:
                await asyncio.sleep(0.5)
                async with self._lock:
                    if (
                        operation_id != self._device_operation_id
                        or self.mode != "devices_preview"
                        or not self._monitoring_requested
                    ):
                        return
                    ble_healthy = bool(self.ble and self.ble.connected)
                    video_healthy = self._video_is_healthy(self.video)
                    if ble_healthy and video_healthy:
                        continue
                reconnects: list[asyncio.Task[bool]] = []
                if not ble_healthy:
                    reconnects.append(
                        asyncio.create_task(self._reconnect_preview_ble(operation_id))
                    )
                if not video_healthy:
                    reconnects.append(
                        asyncio.create_task(self._reconnect_preview_video(operation_id))
                    )
                if reconnects and not all(await asyncio.gather(*reconnects)):
                    return
        except asyncio.CancelledError:
            return

    async def _preview_operation_is_current(self, operation_id: int) -> bool:
        async with self._lock:
            return bool(
                operation_id == self._device_operation_id
                and self.mode == "devices_preview"
                and self._monitoring_requested
            )

    async def _reconnect_preview_ble(self, operation_id: int) -> bool:
        async with self._lock:
            if not (
                operation_id == self._device_operation_id
                and self.mode == "devices_preview"
                and self._monitoring_requested
            ):
                return False
            old_ble, old_consumer = self.ble, self._consumer
            self.ble = None
            self._consumer = None
            self._imu_state = "reconnecting"
            self.device_state = DeviceSessionState.RECONNECTING
        await self._bounded_cleanup(None, old_ble, old_consumer)
        last_error: BaseException = RuntimeError("IMU 预览连接已断开")
        for attempt, delay in enumerate((1.0, 2.0, 4.0), start=1):
            self._imu_reconnect_attempt = attempt
            self._preview_reconnect_attempt = attempt
            await asyncio.sleep(delay)
            if not await self._preview_operation_is_current(operation_id):
                return False
            try:
                replacement = await self._open_preview_ble()
            except Exception as error:
                last_error = error
                continue
            async with self._lock:
                if not (
                    operation_id == self._device_operation_id
                    and self.mode == "devices_preview"
                    and self._monitoring_requested
                ):
                    accepted = False
                else:
                    accepted = True
                    self.ble = replacement
                    self._stop_consumer.clear()
                    self._consumer = asyncio.create_task(self._consume_imu())
                    self._imu_state = "connected"
                    self._imu_reconnect_attempt = 0
                    self._preview_reconnect_attempt = 0
                    if self._video_is_healthy(self.video):
                        self.device_state = DeviceSessionState.CONNECTED
                        self.device_error = None
                        self.preview_error = None
            if accepted:
                return True
            await self._bounded_cleanup(None, replacement, None)
            return False
        async with self._lock:
            if operation_id == self._device_operation_id:
                self._imu_state = "error"
                self.device_state = DeviceSessionState.ERROR
                self._set_device_error(
                    code=(
                        last_error.code
                        if isinstance(last_error, BleOperationError)
                        else "imu_reconnect_exhausted"
                    ),
                    component="ble",
                    error=last_error,
                    hint=(
                        last_error.hint
                        if isinstance(last_error, BleOperationError)
                        else "摄像头会继续预览；确认 IMU 电量与占用状态后重新连接设备"
                    ),
                    retryable=True,
                    fallback="IMU 自动重连三次均失败",
                )
        return False

    async def _reconnect_preview_video(self, operation_id: int) -> bool:
        async with self._lock:
            if not (
                operation_id == self._device_operation_id
                and self.mode == "devices_preview"
                and self._monitoring_requested
            ):
                return False
            old_video = self.video
            self.video = None
            camera_id = self._preview_camera_id
            self._video_state = "reconnecting"
            self._video_transition = "camera_reconnect"
            self.device_state = DeviceSessionState.RECONNECTING
        await self._bounded_cleanup(old_video, None, None)
        last_error: BaseException = RuntimeError("摄像头预览进程已退出")
        for attempt, delay in enumerate((1.0, 2.0, 4.0), start=1):
            self._video_reconnect_attempt = attempt
            self._preview_reconnect_attempt = attempt
            await asyncio.sleep(delay)
            if not await self._preview_operation_is_current(operation_id):
                return False
            try:
                camera, replacement = await self._open_preview_video(camera_id)
            except Exception as error:
                last_error = error
                continue
            async with self._lock:
                if not (
                    operation_id == self._device_operation_id
                    and self.mode == "devices_preview"
                    and self._monitoring_requested
                ):
                    accepted = False
                else:
                    accepted = True
                    self.video = replacement
                    self._preview_camera_id = str(camera["camera_id"])
                    self._preview_camera_profile = dict(
                        camera.get("selected_profile") or {}
                    )
                    self._video_state = "live"
                    self._video_transition = None
                    self._video_reconnect_attempt = 0
                    self._preview_reconnect_attempt = 0
                    if self.ble and self.ble.connected:
                        self.device_state = DeviceSessionState.CONNECTED
                        self.device_error = None
                        self.preview_error = None
            if accepted:
                return True
            await self._bounded_cleanup(replacement, None, None)
            return False
        async with self._lock:
            if operation_id == self._device_operation_id:
                self._video_state = "error"
                self._video_transition = None
                self.device_state = DeviceSessionState.ERROR
                self._set_device_error(
                    code="camera_reconnect_exhausted",
                    component="video",
                    error=last_error,
                    hint="IMU 会继续接收；检查摄像头后重新连接设备",
                    retryable=True,
                    fallback="摄像头自动重启三次均失败",
                )
        return False

    async def start_preview(self, request: PreviewStartRequest) -> dict[str, Any]:
        """连接 IMU 与摄像头，只在内存中提供实时预览，不创建采集文件。"""

        async with self._lock:
            if self._preview_open_in_flight:
                raise RuntimeError("已有设备连接操作正在进行，请等待其完成或清理")
            if self.device_state in {
                DeviceSessionState.CONNECTING,
                DeviceSessionState.RECONNECTING,
                DeviceSessionState.RELEASING,
            }:
                raise RuntimeError("设备正在连接、重连或释放，请等待当前操作完成")
            if (
                self.mode == "devices_preview"
                and self.ble
                and self.ble.connected
                and self._video_is_healthy(self.video)
            ):
                return self.snapshot()
            if self.mode not in {None, "devices_preview"} or self.state in {
                RecordingState.ARMING,
                RecordingState.RECORDING,
                RecordingState.FINALIZING,
            }:
                raise RuntimeError("当前有其他设备会话，不能开始设备预览")
            operation_id = self._next_device_operation(DeviceSessionState.CONNECTING)
            if not self._monitoring_requested:
                self.preview_stream.activate()
            self.mode = "devices_preview"
            self._monitoring_requested = True
            self.device_error = None
            self.preview_error = None
            keep_video = self.video if self._video_is_healthy(self.video) else None
            keep_ble = self.ble if self.ble and self.ble.connected else None
            keep_consumer = self._consumer if keep_ble else None
            stale_video = None if keep_video else self.video
            stale_ble = None if keep_ble else self.ble
            stale_consumer = None if keep_consumer else self._consumer
            stale_watchdog = self._preview_watchdog
            self.video = keep_video
            self.ble = keep_ble
            self._consumer = keep_consumer
            self._preview_watchdog = None
            self._imu_state = "connected" if keep_ble else "connecting"
            self._video_state = "live" if keep_video else "starting"
            self._video_transition = None if keep_video else "initial_preview"
            self._preview_open_in_flight = True
        if stale_watchdog:
            stale_watchdog.cancel()
        await self._bounded_cleanup(stale_video, stale_ble, stale_consumer)

        opening: list[tuple[str, asyncio.Task[Any]]] = []
        if keep_ble is None:
            opening.append(("ble", asyncio.create_task(self._open_preview_ble())))
        if keep_video is None:
            opening.append(
                (
                    "video",
                    asyncio.create_task(self._open_preview_video(request.camera_id)),
                )
            )
        try:
            results = await asyncio.gather(
                *(task for _component, task in opening),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            for _component, task in opening:
                task.cancel()
            cancelled_results = await asyncio.gather(
                *(task for _component, task in opening),
                return_exceptions=True,
            )
            cancelled_ble = next(
                (
                    result
                    for (component, _task), result in zip(
                        opening, cancelled_results, strict=True
                    )
                    if component == "ble" and not isinstance(result, BaseException)
                ),
                None,
            )
            cancelled_video_result = next(
                (
                    result
                    for (component, _task), result in zip(
                        opening, cancelled_results, strict=True
                    )
                    if component == "video" and not isinstance(result, BaseException)
                ),
                None,
            )
            cancelled_video = (
                cancelled_video_result[1] if cancelled_video_result is not None else None
            )
            await self._bounded_cleanup(cancelled_video, cancelled_ble, None)
            async with self._lock:
                self._preview_open_in_flight = False
            raise

        new_ble: CW12EUBleSource | None = None
        new_video: FFmpegVideoRecorder | None = None
        camera: dict[str, Any] | None = None
        opening_errors: list[tuple[str, BaseException]] = []
        for (component, _task), result in zip(opening, results, strict=True):
            if isinstance(result, BaseException):
                opening_errors.append((component, result))
            elif component == "ble":
                new_ble = result
            else:
                camera, new_video = result

        async with self._lock:
            self._preview_open_in_flight = False
            cancelled = bool(
                operation_id != self._device_operation_id
                or self.mode != "devices_preview"
            )
            if not cancelled:
                if new_ble is not None:
                    self.ble = new_ble
                if new_video is not None and camera is not None:
                    self.video = new_video
                    self._preview_camera_id = str(camera["camera_id"])
                    self._preview_camera_profile = dict(
                        camera.get("selected_profile") or {}
                    )
                if self.ble and (self._consumer is None or self._consumer.done()):
                    self._stop_consumer.clear()
                    self._consumer = asyncio.create_task(self._consume_imu())
                self._imu_state = (
                    "connected" if self.ble and self.ble.connected else "error"
                )
                self._video_state = (
                    "live" if self._video_is_healthy(self.video) else "error"
                )
                self._video_transition = None
                if opening_errors:
                    failed_component, error = opening_errors[0]
                    self.device_state = DeviceSessionState.ERROR
                    self._set_device_error(
                        code="preview_start_failed",
                        component=failed_component,
                        error=error,
                        hint="健康组件会保持运行；失败组件将自动重试三次",
                        retryable=True,
                        fallback="连接预览设备超时",
                    )
                else:
                    self.device_state = DeviceSessionState.CONNECTED
                    self.device_error = None
                    self.preview_error = None
                self._start_preview_watchdog(operation_id)
                snapshot = self.snapshot()
        if cancelled:
            await self._bounded_cleanup(new_video, new_ble, None)
            raise RuntimeError("设备连接已被释放操作取消")
        if opening_errors:
            _component, error = opening_errors[0]
            raise RuntimeError(
                self._error_message(error, "连接预览设备超时")
            ) from error
        return snapshot

    async def switch_preview_camera(
        self, request: PreviewStartRequest
    ) -> dict[str, Any]:
        """仅重启摄像头预览；保留当前 BLE 连接和 IMU 曲线。"""

        async with self._lock:
            if self._preview_open_in_flight:
                raise RuntimeError("已有设备连接操作正在进行，请等待其完成或清理")
            if self.device_state in {
                DeviceSessionState.CONNECTING,
                DeviceSessionState.RECONNECTING,
                DeviceSessionState.RELEASING,
            }:
                raise RuntimeError("设备正在连接、重连或释放，请等待当前操作完成")
            if self.mode != "devices_preview" or not self.ble or not self.ble.connected:
                raise RuntimeError("请先连接预览设备，再切换摄像头")
            operation_id = self._next_device_operation(DeviceSessionState.CONNECTING)
            watchdog = self._preview_watchdog
            self._preview_watchdog = None
            previous_video = self.video
            previous_id = self._preview_camera_id
            previous_profile = self._preview_camera_profile
            previous_device = previous_video.device if previous_video else None
            self.video = None
            self._video_state = "switching"
            self._video_transition = "camera_switch"
        if watchdog:
            watchdog.cancel()
        await self._bounded_cleanup(previous_video, None, None)
        replacement: FFmpegVideoRecorder | None = None
        try:
            camera, replacement = await self._open_preview_video(request.camera_id)
            camera_id = str(camera["camera_id"])
        except Exception as error:
            rollback: FFmpegVideoRecorder | None = None
            if previous_device:
                try:
                    rollback = self._new_video_recorder(previous_device, None)
                    await asyncio.wait_for(rollback.start(), timeout=5.0)
                except Exception:
                    rollback = None
            async with self._lock:
                if operation_id == self._device_operation_id:
                    self.video = rollback
                    self._preview_camera_id = previous_id
                    self._preview_camera_profile = previous_profile
                    self._video_state = "live" if rollback else "error"
                    self._video_transition = None
                    self.device_state = (
                        DeviceSessionState.CONNECTED
                        if rollback and self.ble and self.ble.connected
                        else DeviceSessionState.ERROR
                    )
                    self._set_device_error(
                        code="camera_switch_failed",
                        component="video",
                        error=error,
                        hint="摄像头切换失败；已尝试恢复原摄像头",
                        retryable=True,
                        fallback="摄像头切换失败",
                    )
                    if self.device_state == DeviceSessionState.CONNECTED:
                        self._start_preview_watchdog(operation_id)
            raise RuntimeError(self._error_message(error, "摄像头切换失败")) from error
        async with self._lock:
            cancelled = operation_id != self._device_operation_id
            if not cancelled:
                self.video = replacement
                self._preview_camera_id = camera_id
                self._preview_camera_profile = dict(
                    camera.get("selected_profile") or {}
                )
                self._video_state = "live"
                self._video_transition = None
                self.device_state = DeviceSessionState.CONNECTED
                self.device_error = None
                self.preview_error = None
                self._start_preview_watchdog(operation_id)
                snapshot = self.snapshot()
        if cancelled:
            await self._bounded_cleanup(replacement, None, None)
            raise RuntimeError("摄像头切换已被释放操作取消")
        return snapshot

    def _detach_preview_locked(
        self, *, preserve_request: bool
    ) -> tuple[
        FFmpegVideoRecorder | None,
        CW12EUBleSource | None,
        asyncio.Task[Any] | None,
        asyncio.Task[Any] | None,
    ]:
        resources = self.video, self.ble, self._consumer, self._preview_watchdog
        self.video = None
        self.ble = None
        self._consumer = None
        self._preview_watchdog = None
        self.mode = None
        if not preserve_request:
            self._monitoring_requested = False
            self._preview_camera_id = None
            self._preview_camera_profile = None
            self.preview_stream.deactivate()
            self._imu_state = "idle"
            self._video_state = "idle"
            self._video_transition = None
            self._recording_accepts_imu = False
        return resources

    async def stop_preview(self) -> dict[str, Any]:
        """幂等释放预览；即使会话已残缺也会把逻辑状态恢复为空闲。"""

        async with self._lock:
            if self.mode not in {None, "devices_preview"}:
                raise RuntimeError("正式录制或 IMU 表征正在进行，不能通过预览释放接口停止")
            operation_id = self._next_device_operation(DeviceSessionState.RELEASING)
            video, ble, consumer, watchdog = self._detach_preview_locked(
                preserve_request=False
            )
        if watchdog and watchdog is not asyncio.current_task():
            watchdog.cancel()
        issues = await self._bounded_cleanup(video, ble, consumer)
        async with self._lock:
            if operation_id == self._device_operation_id:
                self.device_state = DeviceSessionState.IDLE
                self.device_error = None
                self.preview_error = None
                self._preview_reconnect_attempt = 0
                self._imu_reconnect_attempt = 0
                self._video_reconnect_attempt = 0
            snapshot = self.snapshot()
        if issues:
            snapshot["release_warnings"] = issues
        return snapshot

    async def start_background_jobs(self) -> dict[str, int]:
        """恢复被服务重启中断的任务，并启动唯一后台 worker。"""

        requeued = self.catalog.requeue_interrupted_jobs()
        self._stop_jobs.clear()
        if self._job_worker is None or self._job_worker.done():
            self._job_worker = asyncio.create_task(
                self._background_job_loop(), name="recording-background-jobs"
            )
        self._jobs_changed.set()
        return {"requeued": requeued}

    async def stop_background_jobs(self) -> None:
        self._stop_jobs.set()
        self._jobs_changed.set()
        worker = self._job_worker
        self._job_worker = None
        if worker is None:
            return
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        self._active_job = None

    def _wake_background_jobs(self) -> None:
        self._jobs_changed.set()

    async def _background_job_loop(self) -> None:
        poll = max(0.1, self.settings.background_jobs.poll_interval_seconds)
        while not self._stop_jobs.is_set():
            if (
                not self.settings.background_jobs.allow_during_recording
                and self.state == RecordingState.RECORDING
            ):
                try:
                    await asyncio.wait_for(self._jobs_changed.wait(), timeout=poll)
                except TimeoutError:
                    pass
                self._jobs_changed.clear()
                continue
            claimed = await asyncio.to_thread(self.catalog.claim_next_job)
            if claimed is None:
                try:
                    await asyncio.wait_for(self._jobs_changed.wait(), timeout=poll)
                except TimeoutError:
                    pass
                self._jobs_changed.clear()
                continue
            recording_id, job, payload = claimed
            self._active_job = {
                "recording_id": recording_id,
                "kind": job.kind.value,
            }
            try:
                if job.kind == BackgroundJobKind.FINALIZE:
                    await self._run_finalization_job(recording_id, payload)
                else:
                    await self._run_publish_job(recording_id)
            except asyncio.CancelledError:
                raise
            except (AuthenticationRequiredError, OAuthLoginRequired):
                await asyncio.to_thread(self.catalog.wait_for_auth, recording_id)
                summary = self.catalog.get(recording_id)
                if summary is not None:
                    self.catalog.upsert(
                        summary.model_copy(
                            update={
                                "upload_state": PublishState.AUTH_REQUIRED,
                                "publish_target": PublishTarget.BROKER,
                                "index_state": "not_requested",
                                "index_message": "等待 Google 登录后自动继续上传",
                            }
                        )
                    )
                logger.info("后台发布等待 Google 登录：%s", recording_id)
            except Exception as error:
                message = self._error_message(error, "后台任务失败")
                status = await asyncio.to_thread(
                    self.catalog.fail_job,
                    recording_id,
                    job.kind,
                    message,
                    self.settings.background_jobs.retry_delays_seconds,
                )
                summary = self.catalog.get(recording_id)
                if summary is not None:
                    if job.kind == BackgroundJobKind.FINALIZE:
                        update: dict[str, Any] = {"state": RecordingState.FINALIZING}
                        if status.state == BackgroundJobState.FAILED:
                            update = {
                                "state": RecordingState.NEEDS_ATTENTION,
                                "issues": [message],
                            }
                    else:
                        update = {
                            "upload_state": (
                                "retry_wait"
                                if status.state == BackgroundJobState.RETRY_WAIT
                                else "failed"
                            )
                        }
                    self.catalog.upsert(summary.model_copy(update=update))
                logger.exception(
                    "后台任务失败：recording=%s kind=%s state=%s",
                    recording_id,
                    job.kind.value,
                    status.state.value,
                )
            finally:
                self._active_job = None

    async def _run_finalization_job(
        self, recording_id: str, payload: dict[str, Any]
    ) -> None:
        summary = self._required_summary(recording_id)

        def phase(value: str) -> None:
            self.catalog.update_job_phase(
                recording_id, BackgroundJobKind.FINALIZE, value
            )

        result = await finalize_recording(
            summary, payload, self.settings, self.taxonomy, phase
        )
        preserved_issues = [str(item) for item in payload.get("preserved_issues") or []]
        validation_issues = list(dict.fromkeys(result.report.issues))
        warnings = list(
            dict.fromkeys((*result.report.warnings, *result.recovery_warnings))
        )
        state = (
            RecordingState.READY
            if not preserved_issues and not validation_issues
            else RecordingState.NEEDS_ATTENTION
        )
        updated = summary.model_copy(
            update={
                "state": state,
                "ended_at_utc": result.ended_at_utc,
                "duration_ns": result.duration_ns,
                "h5_path": str(result.h5_path),
                "mkv_path": str(result.mkv_path),
                "issues": preserved_issues,
                "validation_issues": validation_issues,
                "quality_warnings": warnings,
            }
        )
        await asyncio.to_thread(self.catalog.commit_finalization, updated)
        try:
            await asyncio.to_thread(cleanup_partial_inputs, summary)
        except OSError:
            # 最终制品和目录索引已经原子提交；遗留 partial 只占空间，不能把
            # 已成功的收尾重新标成失败。维护扫描可随后清理它。
            logger.warning("收尾成功但清理 partial 失败：%s", recording_id, exc_info=True)
        configured_target = PublishTarget(self.settings.publish.mode)
        if (
            state == RecordingState.READY
            and updated.data_tier == DataTier.PROD
            and configured_target != PublishTarget.DISABLED
        ):
            try:
                self.catalog.enqueue_job(
                    recording_id,
                    BackgroundJobKind.PUBLISH,
                    max_attempts=(
                        len(self.settings.background_jobs.retry_delays_seconds) + 1
                    ),
                )
                self.catalog.upsert(
                    updated.model_copy(
                        update={
                            "upload_state": PublishState.QUEUED,
                            "publish_target": configured_target,
                        }
                    )
                )
                self._wake_background_jobs()
            except Exception:
                # 收尾已经原子完成，发布排队属于下一生命周期，不能反向回滚
                # 或把有效的最终 H5/MKV 改回 finalizing。
                self.catalog.upsert(
                    updated.model_copy(update={"upload_state": "failed"})
                )
                logger.exception("正式录制已收尾，但自动发布入队失败：%s", recording_id)

    async def _run_publish_job(self, recording_id: str) -> None:
        summary = self._required_summary(recording_id)
        if summary.state != RecordingState.READY:
            raise ValueError("只有通过采集验证的 ready 录制可以发布")
        target = self.publish_target
        if target == PublishTarget.DISABLED:
            raise ValueError("当前配置已禁用发布")
        if target == PublishTarget.BROKER and not self.cloud_auth.logged_in:
            raise AuthenticationRequiredError("尚未登录 Google")
        self.catalog.update_job_phase(
            recording_id, BackgroundJobKind.PUBLISH, "packaging"
        )
        summary = summary.model_copy(
            update={
                "upload_state": PublishState.PACKAGING,
                "publish_target": target,
            }
        )
        self.catalog.upsert(summary)
        self.catalog.update_job_phase(
            recording_id, BackgroundJobKind.PUBLISH, "uploading"
        )
        summary = summary.model_copy(update={"upload_state": PublishState.UPLOADING})
        self.catalog.upsert(summary)

        def progress(current: int, total: int, role: str) -> None:
            self.catalog.update_job_progress(
                recording_id,
                BackgroundJobKind.PUBLISH,
                current,
                total,
                f"uploading:{role}",
            )

        if target == PublishTarget.BROKER:
            manifest, manifest_generation = await publish_recording_via_broker(
                summary,
                self.settings,
                self.cloud_auth,
                progress,
            )
        else:
            manifest, manifest_generation = await publish_recording(
                summary, self.settings, self.object_store
            )
        self.catalog.update_job_phase(
            recording_id, BackgroundJobKind.PUBLISH, "verifying"
        )
        if target in {PublishTarget.LOCAL, PublishTarget.DIRECT_GCS}:
            manifest_key = f"captures/{recording_id}/manifest.json"
            if await asyncio.to_thread(self.object_store.stat, manifest_key) is None:
                raise RuntimeError("发布目标 manifest 验证失败")
        if target == PublishTarget.LOCAL:
            update = {
                "upload_state": PublishState.STORED_LOCAL,
                "publish_target": target,
                "index_state": "not_requested",
                "index_message": "已保存到本机对象目录，尚未上传团队云端",
                "manifest_generation": manifest_generation,
            }
        else:
            update = {
                "upload_state": PublishState.UPLOADED,
                "publish_target": target,
                "index_state": "pending",
                "index_message": "团队云端已接收，等待标注端扫描并回执",
                "manifest_generation": manifest_generation,
            }
        updated = summary.model_copy(update=update)
        self.catalog.upsert(updated)
        self.catalog.complete_job(recording_id, BackgroundJobKind.PUBLISH)
        logger.info("后台发布完成：%s (%s)", recording_id, manifest.schema_version)

    async def shutdown(self) -> None:
        """服务退出时释放设备；正式会话保留 partial 并明确标记失败。"""

        await self.stop_background_jobs()

        async with self._lock:
            mode = self.mode
            current = self.current
        if mode in {None, "devices_preview"}:
            await self.stop_preview()
            return
        if current and self.state in {RecordingState.ARMING, RecordingState.RECORDING}:
            await self._fail_running_session_on_disconnect(
                current.recording_id, mode, reason="service_shutdown"
            )
            return
        async with self._lock:
            video, ble, consumer = self.video, self.ble, self._consumer
            self.video = None
            self.ble = None
            self._consumer = None
            if self.writer:
                self.writer.abort_close()
            self.writer = None
            self.mode = None
            self.device_state = DeviceSessionState.IDLE
            self.preview_stream.deactivate()
            self._imu_state = "idle"
            self._video_state = "idle"
            self._video_transition = None
            self._recording_accepts_imu = False
        await self._bounded_cleanup(video, ble, consumer)

    async def _consume_imu_preview(self) -> None:
        """旧测试入口；实际预览与录制共用同一个常驻通知泵。"""

        await self._consume_imu()

    async def start(self, request: RecordingStartRequest) -> RecordingSummary:
        async with self._lock:
            if self._preview_open_in_flight:
                raise RuntimeError("预览设备仍在连接或清理，暂不能开始录制")
            if self.device_state in {
                DeviceSessionState.CONNECTING,
                DeviceSessionState.RECONNECTING,
                DeviceSessionState.RELEASING,
            }:
                raise RuntimeError("设备正在连接、重连或释放，暂不能开始录制")
            if self.state not in {
                RecordingState.IDLE,
                RecordingState.READY,
                RecordingState.NEEDS_ATTENTION,
                RecordingState.FAILED,
            }:
                raise RuntimeError(f"cannot start while coordinator is {self.state.value}")
            self._require_allowed_unikey(request.participant_id, "participant_id")
            self._check_disk()
            camera = await self._resolve_camera(request.camera_id)
            retained_ble = None
            camera_was_previewed = False
            if self.mode == "devices_preview":
                self._next_device_operation(DeviceSessionState.CONNECTING)
                watchdog = self._preview_watchdog
                self._preview_watchdog = None
                if watchdog:
                    watchdog.cancel()
                preview_video = self.video
                camera_was_previewed = self._video_is_healthy(preview_video)
                if preview_video:
                    await self._require_prod_camera_preflight(
                        preview_video, request.data_tier
                    )
                retained_ble = self.ble if self.ble and self.ble.connected else None
                self.video = None
                self.mode = None
                self._video_state = "switching"
                self._video_transition = "arming_recording"
                await self._bounded_cleanup(preview_video, None, None)
            else:
                self._next_device_operation(DeviceSessionState.CONNECTING)
                if not self._monitoring_requested:
                    self.preview_stream.activate()
            self._monitoring_requested = True
            self._preview_camera_id = str(camera["camera_id"])
            self._preview_camera_profile = dict(
                camera.get("selected_profile") or {}
            )
            self.preview_error = None
            self.mode = "capture"
            self.device_state = DeviceSessionState.CONNECTING
            self.state = RecordingState.ARMING
            self.current = None
            self.writer = None
            self.ble = retained_ble or CW12EUBleSource(self.settings.imu)
            self._stop_consumer.clear()
            self._recording_accepts_imu = False
            try:
                if not retained_ble:
                    await asyncio.wait_for(self.ble.start(), timeout=20.0)
                    self._imu_state = "connected"
                if self._consumer is None or self._consumer.done():
                    self._consumer = asyncio.create_task(self._consume_imu())
                if not camera_was_previewed:
                    camera_probe = self._new_video_recorder(camera, None)
                    self.video = camera_probe
                    await asyncio.wait_for(camera_probe.start(), timeout=5.0)
                    await self._require_prod_camera_preflight(
                        camera_probe, request.data_tier
                    )
                    probe_cleanup_issues = await self._bounded_cleanup(
                        camera_probe, None, None
                    )
                    if probe_cleanup_issues:
                        raise RuntimeError("；".join(probe_cleanup_issues))
                    self.video = None
            except Exception as error:
                await self._abort_start(error)
                raise
            now = datetime.now(UTC)
            recording_id = f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}_{request.participant_id}"
            directory = self.settings.data_root / request.collection_id / recording_id
            directory.mkdir(parents=True, exist_ok=False)
            h5_partial = directory / f"{recording_id}.partial.h5"
            mkv_partial = directory / f"{recording_id}.partial.mkv"
            start_ns = time.monotonic_ns()
            summary = RecordingSummary(
                recording_id=recording_id,
                collection_id=request.collection_id,
                participant_id=request.participant_id,
                data_tier=request.data_tier,
                state=RecordingState.ARMING,
                started_at_utc=now.isoformat(),
                h5_path=str(h5_partial),
                mkv_path=str(mkv_partial),
            )
            self.current = summary
            self.catalog.upsert(summary)
            self.writer = CaptureH5Writer(
                h5_partial,
                request,
                recording_id,
                start_ns,
                self.settings.imu,
                self.taxonomy,
            )
            self.writer.set_capture_backends(
                ble_backend=str(getattr(self.ble, "backend_name", "test_unknown")),
                local_device_id=getattr(self.ble, "device_identifier", None),
                video_backend=str(camera.get("backend", "ffmpeg_unknown")),
                video_timestamp_mapping=(
                    "source_pts_monotonic"
                    if str(camera.get("backend")) == "ffmpeg_v4l2"
                    else "first_source_pts_to_host_monotonic"
                ),
                camera_control_policy=(
                    "fixed_verified"
                    if self.settings.video.manual_controls_enabled
                    and str(camera.get("backend")) == "ffmpeg_v4l2"
                    else "observed_fps_gate"
                ),
            )
            self.writer.handle["video"].attrs.update(
                {
                    "camera_id": str(camera["camera_id"]),
                    "capture_device_at_recording": str(camera["device"]),
                    "product": str(camera["product"]),
                    "usb_interface": str(camera["interface"]),
                }
            )
            self.video = self._new_video_recorder(camera, mkv_partial)
            self._video_state = "switching"
            self._video_transition = "arming_recording"
            try:
                if retained_ble:
                    self.ble.dropped_callback_packets = 0
                    self.writer.append_connection_event(
                        "ble_reused_from_preview", time.monotonic_ns()
                    )
                else:
                    self.writer.append_connection_event(
                        "ble_connected", time.monotonic_ns()
                    )
                await self.video.start()
                if (
                    self.current.data_tier == DataTier.PROD
                    and not self.video.control_state.ready
                ):
                    details = "；".join(self.video.control_state.errors)
                    raise RuntimeError(f"正式录制摄像头控制未生效：{details}")
                self.writer.handle["video"].attrs.update(
                    {
                        "camera_controls_requested_json": json.dumps(
                            getattr(self.video.control_state, "requested", {}),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        "camera_controls_effective_json": json.dumps(
                            getattr(self.video.control_state, "effective", {}),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        "camera_control_errors_json": json.dumps(
                            getattr(self.video.control_state, "errors", []),
                            ensure_ascii=False,
                        ),
                    }
                )
                self.writer.handle.flush()
                capture_start_ns = self.video.started_monotonic_ns or time.monotonic_ns()
                capture_started_at = datetime.now(UTC).isoformat()
                self.writer.set_recording_start(
                    capture_start_ns, capture_started_at
                )
                self._recording_accepts_imu = True
                self._video_state = "live"
                self._video_transition = None
            except Exception as error:
                await self._abort_start(error)
                raise
            self.state = RecordingState.RECORDING
            self.device_state = DeviceSessionState.CONNECTED
            self.device_error = None
            self.current = summary.model_copy(
                update={"state": self.state, "started_at_utc": capture_started_at}
            )
            self.catalog.upsert(self.current)
            return self.current

    async def _abort_start(self, error: Exception) -> None:
        video = self.video
        preserve_ble = bool(
            self._monitoring_requested
            and self.ble
            and self.ble.connected
            and self._consumer
            and not self._consumer.done()
        )
        ble = None if preserve_ble else self.ble
        consumer = None if preserve_ble else self._consumer
        self.video = None
        self._recording_accepts_imu = False
        if not preserve_ble:
            self.ble = None
            self._consumer = None
            self._stop_consumer.set()
        await self._bounded_cleanup(video, ble, consumer)
        if self.writer:
            self.writer.abort_close()
        self.writer = None
        self.state = RecordingState.FAILED
        message = self._error_message(error, "设备准备超时")
        if self.current:
            self.current = self.current.model_copy(
                update={"state": self.state, "issues": [message]}
            )
            if self.mode == "capture":
                self.catalog.upsert(self.current)
        self.mode = "devices_preview" if preserve_ble else None
        if not preserve_ble:
            self._monitoring_requested = False
            self.preview_stream.deactivate()
            self._imu_state = "idle"
        self._video_state = "error"
        self._video_transition = None
        self.device_state = DeviceSessionState.ERROR
        self._set_device_error(
            code="session_start_failed",
            component="ble_video",
            error=error,
            hint="确认 IMU 和摄像头可用后重新开始；失败的 partial 文件不能用于训练",
            retryable=True,
            fallback="正式会话设备准备超时",
        )

    async def _consume_imu(self) -> None:
        ble = self.ble
        assert ble is not None
        while not self._stop_consumer.is_set() or not ble.queue.empty():
            try:
                packet = await asyncio.wait_for(ble.queue.get(), timeout=0.25)
            except TimeoutError:
                if (
                    not self._stop_consumer.is_set()
                    and not ble.connected
                    and getattr(ble, "disconnect_reason", None) != "local_stop"
                    and self.current
                    and self.mode in {"capture", "characterization"}
                    and self.state in {RecordingState.ARMING, RecordingState.RECORDING}
                ):
                    asyncio.create_task(
                        self._fail_running_session_on_disconnect(
                            self.current.recording_id, self.mode
                        )
                    )
                    return
                continue
            self.packet_count += 1
            if self._first_packet_ns is None:
                self._first_packet_ns = packet.receive_time_ns
            writer = self.writer
            if (
                self._recording_accepts_imu
                and writer is not None
                and packet.receive_time_ns >= writer.recording_start_monotonic_ns
            ):
                writer.append_notification(packet.payload, packet.receive_time_ns)
            packet_kind = classify_notification(
                packet.payload, self.settings.imu.frame_size_bytes
            )
            if packet_kind == NotificationKind.AUXILIARY_STATUS:
                continue
            if packet_kind == NotificationKind.UNKNOWN_INVALID:
                self.preview_parse_errors += 1
                continue
            try:
                parsed = parse_notification(
                    packet.payload, self.settings.imu.frame_size_bytes
                )
            except ValueError:
                self.preview_parse_errors += 1
                continue
            self.latest_packet_samples = parsed.sample_count
            self.sample_count += parsed.sample_count
            if parsed.sample_count:
                self.latest_raw = parsed.raw_counts[-1].copy()

    async def _fail_running_session_on_disconnect(
        self,
        recording_id: str,
        expected_mode: str | None,
        *,
        reason: str = "ble_disconnect",
    ) -> None:
        """正式会话意外断开时停止媒体并保留 partial，绝不静默继续录制。"""

        async with self._lock:
            if (
                self.state not in {RecordingState.ARMING, RecordingState.RECORDING}
                or not self.current
                or self.current.recording_id != recording_id
                or self.mode != expected_mode
            ):
                return
            service_shutdown = reason == "service_shutdown"
            message = (
                "后端服务在正式会话中停止；已保留 partial 文件"
                if service_shutdown
                else "BLE 在正式会话中意外断开；已停止并保留 partial 文件"
            )
            video, ble, writer = self.video, self.ble, self.writer
            self.video = None
            self.ble = None
            self.writer = None
            self._consumer = None
            self._stop_consumer.set()
            self.state = RecordingState.FAILED
            self.current = self.current.model_copy(
                update={
                    "state": self.state,
                    "ended_at_utc": datetime.now(UTC).isoformat(),
                    "issues": list(dict.fromkeys([*self.current.issues, message])),
                }
            )
            if expected_mode == "capture":
                self.catalog.upsert(self.current)
            if writer:
                try:
                    writer.append_connection_event(reason, time.monotonic_ns())
                except Exception:
                    pass
                writer.abort_close()
            self.mode = None
            self._monitoring_requested = False
            self.device_state = DeviceSessionState.ERROR
            self._set_device_error(
                code=(
                    "service_shutdown_during_session"
                    if service_shutdown
                    else "ble_disconnected_during_session"
                ),
                component="service" if service_shutdown else "ble",
                error=RuntimeError(message),
                hint=(
                    "重启服务后重新采集；该 partial 禁止训练"
                    if service_shutdown
                    else "检查固定与电量，重新连接预览并重新采集；该 partial 禁止训练"
                ),
                retryable=True,
                fallback=(
                    "正式会话期间服务停止"
                    if service_shutdown
                    else "正式会话 BLE 意外断开"
                ),
            )
        await self._bounded_cleanup(video, ble, None)

    async def stop(self) -> RecordingSummary:
        async with self._lock:
            if (
                self.state != RecordingState.RECORDING
                or not self.current
                or self.mode != "capture"
            ):
                raise RuntimeError("no active recording")
            recording = self.current
            self.state = RecordingState.FINALIZING
            self.device_state = DeviceSessionState.RELEASING
            capture_ended_at = datetime.now(UTC).isoformat()
            recording = recording.model_copy(
                update={"state": RecordingState.FINALIZING, "ended_at_utc": capture_ended_at}
            )
            self.catalog.upsert(recording)
            issues: list[str] = []
            assert self.video and self.ble and self.writer
            capture_video = self.video
            capture_ble = self.ble
            capture_writer = self.writer
            self._recording_accepts_imu = False
            self.video = None
            self._video_state = "switching"
            self._video_transition = "finalizing_recording"
            try:
                await capture_video.stop()
            except Exception as error:
                issues.append(str(error))
            try:
                capture_writer.append_connection_event(
                    "ble_retained_after_capture", time.monotonic_ns()
                )
            except Exception as error:
                issues.append(self._error_message(error, "无法记录 BLE 保持事件"))
            try:
                capture_writer.seal_for_finalization(
                    capture_ended_at,
                    callback_drops=capture_ble.dropped_callback_packets,
                )
            except Exception as error:
                issues.append(self._error_message(error, "无法冻结采集 H5"))
                capture_writer.abort_close()
            finally:
                self.writer = None

            context = {
                "ended_at_utc": capture_ended_at,
                "preserved_issues": list(dict.fromkeys(issues)),
                "ffmpeg_diagnostics": list(
                    getattr(capture_video.progress, "errors", [])
                ),
                "camera_controls_requested": dict(
                    getattr(capture_video.control_state, "requested", {})
                ),
                "camera_controls_effective": dict(
                    getattr(capture_video.control_state, "effective", {})
                ),
                "camera_control_errors": list(
                    getattr(capture_video.control_state, "errors", [])
                ),
                "camera_control_policy": getattr(
                    capture_video.control_state, "policy", "observed_fps_gate"
                ),
                "video_backend": getattr(
                    capture_video, "backend_name", "test_unknown"
                ),
                "video_timestamp_mapping": getattr(
                    capture_video,
                    "timestamp_mapping",
                    "source_pts_monotonic",
                ),
            }
            recording = recording.model_copy(
                update={
                    "state": RecordingState.FINALIZING,
                    "issues": [],
                    "validation_issues": [],
                    "quality_warnings": [],
                }
            )
            self.catalog.upsert(recording)
            self.catalog.enqueue_job(
                recording.recording_id,
                BackgroundJobKind.FINALIZE,
                context,
                max_attempts=len(self.settings.background_jobs.retry_delays_seconds) + 1,
                reset=True,
            )
            self._wake_background_jobs()

            self.mode = "devices_preview"
            self.current = None
            self.state = RecordingState.IDLE

            # H5 已关闭后立即恢复预览；耗时收尾只访问冻结的 partial。
            preview_restore_error: Exception | None = None
            self._video_transition = "restoring_preview"
            try:
                camera, preview_video = await self._open_preview_video(
                    self._preview_camera_id
                )
                self.video = preview_video
                self._preview_camera_id = str(camera["camera_id"])
                self._preview_camera_profile = dict(
                    camera.get("selected_profile") or {}
                )
                self._video_state = "live"
                self._video_transition = None
            except Exception as error:
                preview_restore_error = error
                self._video_state = "error"
                self._video_transition = None
            self._imu_state = "connected" if capture_ble.connected else "error"
            self.device_state = (
                DeviceSessionState.CONNECTED
                if capture_ble.connected and self._video_is_healthy(self.video)
                else DeviceSessionState.RECONNECTING
            )
            if preview_restore_error is not None:
                self._set_device_error(
                    code="camera_restore_failed",
                    component="video",
                    error=preview_restore_error,
                    hint="录制文件不受影响；摄像头将独立重试三次",
                    retryable=True,
                    fallback="录制后摄像头预览恢复失败",
                )
            if self._monitoring_requested:
                self._start_preview_watchdog(self._device_operation_id)
            return self.catalog.get(recording.recording_id) or recording

    async def _restore_preview_after_session(self, camera_id: str | None) -> None:
        """录制或表征结束后恢复常驻预览；失败信息由 start_preview 统一记录。"""

        try:
            await self.start_preview(PreviewStartRequest(camera_id=camera_id))
        except Exception:
            return

    async def start_characterization(
        self, request: CharacterizationStartRequest
    ) -> dict[str, Any]:
        if self.mode == "devices_preview":
            previous_camera_id = self._preview_camera_id
            await self.stop_preview()
            self._monitoring_requested = True
            self._preview_camera_id = previous_camera_id
        async with self._lock:
            if self.state not in {
                RecordingState.IDLE,
                RecordingState.READY,
                RecordingState.NEEDS_ATTENTION,
                RecordingState.FAILED,
            }:
                raise RuntimeError(f"当前状态 {self.state.value} 不能开始 IMU 表征")
            self._require_allowed_unikey(request.operator_id, "operator_id")
            self._check_disk()
            now = datetime.now(UTC)
            recording_id = (
                f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}_{request.operator_id}_imu_characterization"
            )
            directory = self.settings.data_root / "_diagnostics" / recording_id
            directory.mkdir(parents=True, exist_ok=False)
            partial_h5 = directory / f"{recording_id}.partial.h5"
            start_ns = time.monotonic_ns()
            capture_request = RecordingStartRequest(
                collection_id="_diagnostics",
                participant_id=request.operator_id,
                data_tier="test",
                body_location="chest",
                protocol_id="imu_characterization_v1",
            )
            self.current = RecordingSummary(
                recording_id=recording_id,
                collection_id="_diagnostics",
                participant_id=request.operator_id,
                data_tier="test",
                state=RecordingState.ARMING,
                started_at_utc=now.isoformat(),
                h5_path=str(partial_h5),
                mkv_path=None,
            )
            self.mode = "characterization"
            self.state = RecordingState.ARMING
            self._next_device_operation(DeviceSessionState.CONNECTING)
            self.writer = CaptureH5Writer(
                partial_h5,
                capture_request,
                recording_id,
                start_ns,
                self.settings.imu,
                self.taxonomy,
                recording_kind="imu_characterization",
                training_eligible=False,
                video_status="not_requested",
            )
            self.writer.handle.attrs["operator_notes"] = request.notes
            self.ble = CW12EUBleSource(self.settings.imu)
            self.video = None
            self.current_stage = None
            self._stop_consumer.clear()
            self._reset_live_imu_metrics()
            try:
                await self.ble.start()
                self.writer.append_connection_event(
                    "ble_connected", time.monotonic_ns()
                )
                self._consumer = asyncio.create_task(self._consume_imu())
            except Exception as error:
                await self._abort_start(error)
                raise
            self.state = RecordingState.RECORDING
            self.device_state = DeviceSessionState.CONNECTED
            self.device_error = None
            self.current = self.current.model_copy(update={"state": self.state})
            return self.characterization_snapshot()

    async def start_characterization_stage(
        self, request: CharacterizationStageRequest
    ) -> dict[str, Any]:
        async with self._lock:
            if self.mode != "characterization" or self.state != RecordingState.RECORDING:
                raise RuntimeError("当前没有正在进行的 IMU 表征")
            if self.current_stage is not None:
                raise RuntimeError("已有实验阶段正在进行，请先结束该阶段")
            assert self.writer is not None
            code = request.stage_code.value
            self.current_stage = {
                "stage_code": code,
                "start_ns": time.monotonic_ns()
                - self.writer.recording_start_monotonic_ns,
                "notes": request.notes,
                "reliability": (
                    "exploratory" if "exploratory" in code else "candidate"
                ),
            }
            return self.characterization_snapshot()

    def _close_characterization_stage(self) -> None:
        if self.current_stage is None:
            raise RuntimeError("当前没有正在进行的实验阶段")
        assert self.writer is not None
        end_ns = time.monotonic_ns() - self.writer.recording_start_monotonic_ns
        self.writer.append_experiment_stage(end_ns=end_ns, **self.current_stage)
        self.current_stage = None

    async def stop_characterization_stage(self) -> dict[str, Any]:
        async with self._lock:
            if self.mode != "characterization" or self.state != RecordingState.RECORDING:
                raise RuntimeError("当前没有正在进行的 IMU 表征")
            self._close_characterization_stage()
            return self.characterization_snapshot()

    async def stop_characterization(self) -> dict[str, Any]:
        async with self._lock:
            if (
                self.mode != "characterization"
                or self.state != RecordingState.RECORDING
                or self.current is None
            ):
                raise RuntimeError("当前没有正在进行的 IMU 表征")
            self.state = RecordingState.FINALIZING
            self.device_state = DeviceSessionState.RELEASING
            issues: list[str] = []
            validation_issues: list[str] = []
            quality_warnings: list[str] = []
            if self.current_stage is not None:
                self._close_characterization_stage()
            assert self.ble is not None and self.writer is not None
            stopped_ble = self.ble
            try:
                self.writer.append_connection_event(
                    "ble_stop_requested", time.monotonic_ns()
                )
                await asyncio.wait_for(self.ble.stop(), timeout=5.0)
                self.writer.append_connection_event(
                    "ble_disconnected", time.monotonic_ns()
                )
            except Exception as error:
                issues.append(str(error))
            self._stop_consumer.set()
            if self._consumer:
                try:
                    await asyncio.wait_for(self._consumer, timeout=3.0)
                except TimeoutError:
                    issues.append("IMU 写入任务未在 3 秒内停止，已强制取消")
                    self._consumer.cancel()
                    await asyncio.gather(self._consumer, return_exceptions=True)
                finally:
                    self._consumer = None
            partial_h5 = Path(self.current.h5_path or "")
            final_h5 = partial_h5.with_name(
                partial_h5.name.replace(".partial.h5", ".h5")
            )
            report_path: Path | None = None
            try:
                rate, _residual = self.writer.reconstruct_times()
                self.observed_rate_hz = rate
                self.writer.handle["imu"].attrs["callback_drops"] = (
                    stopped_ble.dropped_callback_packets
                )
                self.writer.write_sync([])
                self.writer.finish()
                partial_h5.replace(final_h5)
                with h5py.File(final_h5, "r") as handle:
                    duration_ns = int(handle.attrs.get("duration_ns", 0))
                report = validate_capture_h5(
                    final_h5, self.taxonomy, require_video=False, require_sync=False
                )
                validation_issues.extend(report.issues)
                quality_warnings.extend(report.warnings)
                report_path = write_characterization_report(final_h5)
            except Exception as error:
                issues.append(str(error))
                duration_ns = None
                self.writer.abort_close()
            issues = list(dict.fromkeys(issues))
            final_state = (
                RecordingState.READY
                if not issues and not validation_issues
                else RecordingState.NEEDS_ATTENTION
            )
            self.state = final_state
            self.current = self.current.model_copy(
                update={
                    "state": final_state,
                    "ended_at_utc": datetime.now(UTC).isoformat(),
                    "duration_ns": duration_ns,
                    "h5_path": str(final_h5) if final_h5.is_file() else str(partial_h5),
                    "issues": issues,
                    "validation_issues": list(dict.fromkeys(validation_issues)),
                    "quality_warnings": list(dict.fromkeys(quality_warnings)),
                }
            )
            result = {
                **self.current.model_dump(mode="json"),
                "report_path": str(report_path) if report_path else None,
                "training_eligible": False,
            }
            self.last_characterization = result
            self.mode = None
            self.ble = None
            self.writer = None
            self.device_state = DeviceSessionState.IDLE
            if self._monitoring_requested:
                asyncio.create_task(
                    self._restore_preview_after_session(self._preview_camera_id)
                )
            return result

    def characterization_snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "recording_id": self.current.recording_id if self.current else None,
            "operator_id": self.current.participant_id if self.current else None,
            "h5_path": self.current.h5_path if self.current else None,
            "current_stage": self.current_stage,
            "packet_count": self.packet_count,
            "sample_count": self.sample_count,
            "training_eligible": False,
            "last_result": self.last_characterization,
        }

    def list_characterizations(self) -> list[dict[str, Any]]:
        root = self.settings.data_root / "_diagnostics"
        if not root.is_dir():
            return []
        output: list[dict[str, Any]] = []
        for report_path in sorted(
            root.glob("*/*.characterization.json"), reverse=True
        ):
            try:
                import json

                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            output.append(
                {
                    "report_path": str(report_path),
                    "source_h5": payload.get("source_h5"),
                    "observed_rate_hz": payload.get("timing_metrics", {}).get(
                        "observed_rate_hz"
                    ),
                    "packet_count": payload.get("packet_metrics", {}).get(
                        "packet_count"
                    ),
                    "calibration_status": payload.get("calibration_status"),
                    "training_eligible": False,
                }
            )
        return output

    def snapshot(self) -> dict[str, Any]:
        video = self.video
        preview_stream = self.preview_stream.snapshot()
        video_progress = video.progress if video else None
        source_fps = (
            float(getattr(video, "source_fps", getattr(video_progress, "fps", 0.0)))
            if video
            else 0.0
        )
        preview_fps = float(getattr(video, "preview_fps", 0.0)) if video else 0.0
        control_state = getattr(video, "control_state", None) if video else None
        ble = self.ble
        now_ns = time.monotonic_ns()
        packet_span_seconds = (
            (ble.last_packet_ns - self._first_packet_ns) / 1e9
            if ble and ble.last_packet_ns and self._first_packet_ns
            else 0.0
        )
        free_gib = shutil.disk_usage(self.settings.data_root).free / 1024**3
        return {
            "state": self.state.value,
            "session_type": self.mode,
            "monitoring_requested": self._monitoring_requested,
            "preview_error": self.preview_error,
            "device": {
                "state": self.device_state.value,
                "operation_id": self._device_operation_id,
                "operation_age_ms": (
                    (now_ns - self._device_operation_started_ns) / 1e6
                    if self._device_operation_started_ns is not None
                    else None
                ),
                "reconnect_attempt": max(
                    self._preview_reconnect_attempt,
                    self._imu_reconnect_attempt,
                    self._video_reconnect_attempt,
                ),
                "reconnect_max_attempts": 3,
                "error": self.device_error,
            },
            "recording": self.current.model_dump(mode="json") if self.current else None,
            "imu": {
                "connected": bool(ble and ble.connected),
                "session_state": self._imu_state,
                "notifying": bool(ble and getattr(ble, "notifying", ble.connected)),
                "reconnect_attempt": self._imu_reconnect_attempt,
                "raw": self.latest_raw.astype(int).tolist(),
                "packet_count": self.packet_count,
                "sample_count": self.sample_count,
                "last_packet_ns": ble.last_packet_ns if ble else None,
                "last_packet_age_ms": (
                    (now_ns - ble.last_packet_ns) / 1e6
                    if ble and ble.last_packet_ns
                    else None
                ),
                "callback_drops": ble.dropped_callback_packets if ble else 0,
                "parse_errors": self.preview_parse_errors,
                "notification_rate_hz": (
                    (self.packet_count - 1) / packet_span_seconds
                    if self.packet_count > 1 and packet_span_seconds > 0
                    else 0.0
                ),
                "estimated_sample_rate_hz": (
                    (self.sample_count - self.latest_packet_samples) / packet_span_seconds
                    if self.packet_count > 1 and packet_span_seconds > 0
                    else 0.0
                ),
                "observed_rate_hz": self.observed_rate_hz,
            },
            "video": {
                "frame": (
                    int(getattr(video, "frame", getattr(video_progress, "frame", 0)))
                    if video
                    else 0
                ),
                "fps": source_fps,
                "source_fps": source_fps,
                "preview_fps": preview_fps,
                "requested_fps": self.settings.video.requested_fps,
                "preview_fps_limit": self.settings.video.preview_fps,
                "camera_controls_ready": bool(
                    getattr(control_state, "ready", False)
                ),
                "camera_control_errors": list(
                    getattr(control_state, "errors", [])
                ),
                "bitrate": str(getattr(video, "bitrate", "0")) if video else "0",
                "speed": str(getattr(video, "speed", "0x")) if video else "0x",
                "preview_only": self.mode == "devices_preview",
                "stream_id": preview_stream.session_id,
                "preview_ready": bool(preview_stream.active and preview_stream.jpeg),
                "session_state": self._video_state,
                "transition": self._video_transition,
                "reconnect_attempt": self._video_reconnect_attempt,
            },
            "free_disk_gib": free_gib,
            "background_jobs": {
                **self.catalog.job_counts(),
                "active": self._active_job,
                "allow_during_recording": (
                    self.settings.background_jobs.allow_during_recording
                ),
            },
            "characterization": self.characterization_snapshot()
            if self.mode == "characterization"
            else self.last_characterization,
        }

    async def upload(self, recording_id: str) -> None:
        if not self.remote.configured:
            raise RuntimeError("rclone upload is disabled or remote_root is not configured")
        summary = self._required_summary(recording_id)
        await self.remote.upload_pair(
            summary.collection_id,
            summary.recording_id,
            Path(summary.h5_path or ""),
            Path(summary.mkv_path or ""),
        )
        updated = summary.model_copy(update={"upload_state": "verified"})
        self.catalog.upsert(updated)

    async def publish(self, recording_id: str) -> dict[str, Any]:
        """把发布请求持久化入队；实际上传不阻塞 HTTP 请求。"""

        target = self.publish_target
        if target == PublishTarget.DISABLED:
            raise ValueError("当前配置已禁用发布")
        summary = self._required_summary(recording_id)
        if summary.state != RecordingState.READY:
            raise ValueError("只有通过采集验证的 ready 录制可以发布")
        if (
            self.mode == "capture"
            and self.current
            and self.current.recording_id == recording_id
        ):
            raise ValueError("当前会话仍在使用该录制")
        existing = self.catalog.get_job(recording_id, BackgroundJobKind.PUBLISH)
        reset = bool(
            existing and existing[0].state == BackgroundJobState.FAILED
        )
        if summary.publish_target != target and existing is not None:
            self.catalog.delete_job(recording_id, BackgroundJobKind.PUBLISH)
            existing = None
            reset = False
        job = self.catalog.enqueue_job(
            recording_id,
            BackgroundJobKind.PUBLISH,
            max_attempts=len(self.settings.background_jobs.retry_delays_seconds) + 1,
            reset=reset,
        )
        if job.state in {
            BackgroundJobState.QUEUED,
            BackgroundJobState.RETRY_WAIT,
        }:
            upload_state = (
                PublishState.AUTH_REQUIRED
                if target == PublishTarget.BROKER and not self.cloud_auth.logged_in
                else PublishState.QUEUED
            )
            self.catalog.upsert(
                summary.model_copy(
                    update={
                        "upload_state": upload_state,
                        "publish_target": target,
                        "index_state": "not_requested",
                        "index_message": (
                            "等待 Google 登录后自动继续上传"
                            if upload_state == PublishState.AUTH_REQUIRED
                            else ""
                        ),
                    }
                )
            )
            self._wake_background_jobs()
        return {
            "recording_id": recording_id,
            "accepted": True,
            "job": job.model_dump(mode="json"),
            "auth_required": (
                target == PublishTarget.BROKER and not self.cloud_auth.logged_in
            ),
        }

    def resume_uploads_after_login(self) -> list[str]:
        recording_ids = self.catalog.resume_waiting_auth_jobs()
        for recording_id in recording_ids:
            summary = self.catalog.get(recording_id)
            if summary is not None:
                self.catalog.upsert(
                    summary.model_copy(
                        update={
                            "upload_state": PublishState.QUEUED,
                            "publish_target": PublishTarget.BROKER,
                            "index_message": "",
                        }
                    )
                )
        if recording_ids:
            self._wake_background_jobs()
        return recording_ids

    async def retry_finalization(self, recording_id: str) -> dict[str, Any]:
        """人工确认后重新收尾尚未发布的 partial 制品。"""

        summary = self._required_summary(recording_id)
        if summary.upload_state in {"uploaded", "published"}:
            raise ValueError("已经发布的录制禁止重新收尾")
        if summary.state not in {
            RecordingState.NEEDS_ATTENTION,
            RecordingState.FAILED,
            RecordingState.FINALIZING,
        }:
            raise ValueError("只有收尾失败或中断的录制可以重新收尾")
        if self.current and self.current.recording_id == recording_id:
            raise ValueError("当前硬件会话仍在使用该录制")
        partial_h5 = Path(summary.h5_path or "")
        partial_mkv = Path(summary.mkv_path or "")
        if not partial_h5.name.endswith(".partial.h5") or not partial_mkv.name.endswith(
            ".partial.mkv"
        ):
            raise ValueError("该录制没有可重新收尾的 partial H5/MKV")
        if not partial_h5.is_file() or not partial_mkv.is_file():
            raise ValueError("该录制的 partial H5/MKV 已缺失")
        existing = self.catalog.get_job(recording_id, BackgroundJobKind.FINALIZE)
        if existing and existing[0].state in {
            BackgroundJobState.QUEUED,
            BackgroundJobState.RUNNING,
            BackgroundJobState.RETRY_WAIT,
        }:
            raise ValueError("该录制的后台收尾仍在执行或等待自动重试")
        payload = (
            dict(existing[1])
            if existing
            else {
                "ended_at_utc": summary.ended_at_utc,
                "preserved_issues": [],
            }
        )
        job = self.catalog.enqueue_job(
            recording_id,
            BackgroundJobKind.FINALIZE,
            payload,
            max_attempts=len(self.settings.background_jobs.retry_delays_seconds) + 1,
            reset=True,
        )
        self.catalog.upsert(
            summary.model_copy(
                update={
                    "state": RecordingState.FINALIZING,
                    "issues": [],
                    "validation_issues": [],
                    "quality_warnings": [],
                }
            )
        )
        self._wake_background_jobs()
        return {
            "recording_id": recording_id,
            "accepted": True,
            "job": job.model_dump(mode="json"),
        }

    async def refresh_publish_status(self, recording_id: str) -> RecordingSummary:
        """读取标注端回执，严格区分上传成功与实际进入标注索引。"""

        summary = self._required_summary(recording_id)
        if summary.upload_state not in {"uploaded", "published"}:
            return summary
        if summary.publish_target not in {
            PublishTarget.BROKER,
            PublishTarget.DIRECT_GCS,
        }:
            return summary
        if summary.manifest_generation is None:
            updated = summary.model_copy(
                update={
                    "index_state": "pending",
                    "index_message": "旧发布记录缺少 manifest generation，需重新录制并发布",
                }
            )
            self.catalog.upsert(updated)
            return updated
        key = f"index-receipts/{recording_id}.json"
        try:
            if summary.publish_target == PublishTarget.BROKER:
                payload = await read_index_receipt_via_broker(
                    recording_id,
                    self.settings,
                    self.cloud_auth,
                )
            else:
                payload, _generation = await asyncio.to_thread(
                    self.object_store.read_json, key
                )
            receipt = IndexReceipt.model_validate(payload)
        except FileNotFoundError:
            updated = summary.model_copy(
                update={
                    "index_state": "pending",
                    "index_message": "团队云端已接收，等待标注端扫描",
                }
            )
        except RuntimeError as error:
            updated = summary.model_copy(
                update={
                    "index_state": "pending",
                    "index_message": f"需要登录后刷新标注端回执：{error}",
                }
            )
        except ValueError as error:
            updated = summary.model_copy(
                update={
                    "index_state": "pending",
                    "index_message": f"索引回执格式无效：{error}",
                }
            )
        else:
            if receipt.manifest_generation != summary.manifest_generation:
                updated = summary.model_copy(
                    update={
                        "index_state": "pending",
                        "index_message": "回执属于另一版 manifest，等待标注端处理当前版本",
                    }
                )
            else:
                updated = summary.model_copy(
                    update={
                        "index_state": (
                            "indexed" if receipt.status == "indexed" else "rejected"
                        ),
                        "index_message": receipt.message or receipt.code,
                    }
                )
        self.catalog.upsert(updated)
        return updated

    def publish_estimate(self, recording_id: str) -> dict[str, int | str]:
        summary = self._required_summary(recording_id)
        h5_path, mkv_path = self._recording_paths(summary)
        return {
            "recording_id": recording_id,
            "estimated_bytes": h5_path.stat().st_size + 2 * mkv_path.stat().st_size,
        }

    def incomplete_files(self) -> list[dict[str, object]]:
        return scan_incomplete_files(self.settings.data_root)

    def quarantine_file(self, relative_path: str) -> Path:
        return quarantine_incomplete(self.settings.data_root, relative_path)

    def rebuild_catalog(self) -> dict[str, int]:
        return rebuild_catalog(self.settings.data_root, self.catalog)

    def revalidate_unuploaded_recordings(self) -> dict[str, int]:
        """启动时重评未上传的待检查录制，不改动任何源制品。"""

        result = {
            "scanned": 0,
            "updated": 0,
            "ready": 0,
            "still_blocked": 0,
            "skipped": 0,
        }
        for summary in self.catalog.list():
            if summary.state != RecordingState.NEEDS_ATTENTION:
                continue
            if summary.upload_state in {"uploaded", "published"}:
                result["skipped"] += 1
                continue
            result["scanned"] += 1
            h5_path = Path(summary.h5_path or "")
            mkv_path = Path(summary.mkv_path or "")
            if not h5_path.is_file() or not mkv_path.is_file():
                result["skipped"] += 1
                continue
            report = validate_capture_h5(h5_path, self.taxonomy)
            state = (
                RecordingState.READY
                if not summary.issues and not report.issues
                else RecordingState.NEEDS_ATTENTION
            )
            updated = summary.model_copy(
                update={
                    "state": state,
                    "validation_issues": list(report.issues),
                    "quality_warnings": list(report.warnings),
                }
            )
            if updated != summary:
                self.catalog.upsert(updated)
                result["updated"] += 1
            if state == RecordingState.READY:
                result["ready"] += 1
            else:
                result["still_blocked"] += 1
        logger.info("启动自动重评未上传录制：%s", result)
        return result

    def delete_recording(self, recording_id: str, confirmation: str) -> Path:
        summary = self._required_summary(recording_id)
        if self.current and self.current.recording_id == recording_id:
            raise ValueError("当前会话正在使用该录制")
        if self.catalog.has_active_job(recording_id):
            raise ValueError("该录制仍有后台收尾或上传任务，暂不能删除")
        deleted = hard_delete_recording(
            self.settings.data_root, summary, confirmation
        )
        self.catalog.delete(recording_id)
        return deleted

    @staticmethod
    def _recording_paths(summary: RecordingSummary) -> tuple[Path, Path]:
        h5_path = Path(summary.h5_path or "")
        mkv_path = Path(summary.mkv_path or "")
        if not h5_path.is_file() or not mkv_path.is_file():
            raise ValueError("录制的 H5/MKV 源文件不完整")
        return h5_path, mkv_path

    def _required_summary(self, recording_id: str) -> RecordingSummary:
        summary = self.catalog.get(recording_id)
        if summary is None:
            raise KeyError(recording_id)
        return summary
