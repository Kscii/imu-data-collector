"""串联 BLE、摄像头、HDF5、标注和上传的录制状态机。"""

from __future__ import annotations

import asyncio
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from imu_data_collector.artifacts import (
    create_capture_package,
    create_training_release,
    estimate_capture_package_bytes,
    export_aligned30,
)
from imu_data_collector.ble import CW12EUBleSource
from imu_data_collector.catalog import RecordingCatalog
from imu_data_collector.characterization import write_characterization_report
from imu_data_collector.config import Settings, load_activity_taxonomy
from imu_data_collector.cw12eu import parse_notification
from imu_data_collector.hdf5_store import CaptureH5Writer
from imu_data_collector.maintenance import (
    hard_delete_recording,
    quarantine_incomplete,
    rebuild_catalog,
    scan_incomplete_files,
)
from imu_data_collector.models import (
    AnnotationDocument,
    AnnotationEvent,
    BinaryLabel,
    CharacterizationStageRequest,
    CharacterizationStartRequest,
    DataTier,
    DeviceSessionState,
    EventKind,
    PreviewStartRequest,
    RecordingStartRequest,
    RecordingState,
    RecordingSummary,
    ReviewDocument,
    ReviewWorkflowRequest,
    ReviewWorkflowState,
    SyncDocument,
    SyncExperimentDocument,
)
from imu_data_collector.publisher import publish_recording
from imu_data_collector.review import (
    ReviewConflictError,
    load_review,
    mutate_review,
    workflow_with_timestamp,
)
from imu_data_collector.storage import create_object_store
from imu_data_collector.sync import assess_conditional_fixed_offset
from imu_data_collector.sync_experiment import (
    load_sync_experiment,
    read_frame_times,
    read_sync_window,
    save_sync_experiment,
)
from imu_data_collector.upload import RcloneRemoteStore
from imu_data_collector.validation import validate_annotations, validate_capture_h5
from imu_data_collector.video import (
    FFmpegVideoRecorder,
    PreviewFrameHub,
    discover_video_devices,
    normalize_video_timeline,
    probe_video_frames,
    select_video_device,
)


class RecordingCoordinator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.taxonomy = load_activity_taxonomy(settings.activity_taxonomy_path)
        self.catalog = RecordingCatalog(settings.catalog_path)
        self.remote = RcloneRemoteStore(settings.upload)
        self.object_store = create_object_store(
            settings.storage.backend,
            settings.storage.root,
            settings.storage.bucket,
            settings.storage.project,
        )
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

    def _require_allowed_unikey(self, value: str, field_name: str) -> None:
        if value not in self.settings.identity.allowed_unikeys:
            raise ValueError(f"{field_name} 不在当前 UniKey 白名单中：{value}")

    async def _resolve_camera(
        self, requested_camera_id: str | None = None
    ) -> dict[str, Any]:
        devices = await discover_video_devices(self.settings.video)
        return select_video_device(devices, self.settings.video, requested_camera_id)

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
        message = self._error_message(error, fallback)
        self.device_error = {
            "code": code,
            "component": component,
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
        self, device: str, output_path: Path | None
    ) -> FFmpegVideoRecorder:
        """创建视频进程；浏览器预览通道独立于具体 FFmpeg 进程。"""

        return FFmpegVideoRecorder(
            self.settings.video,
            device,
            output_path,
            preview_hub=self.preview_stream,
        )

    async def _require_prod_camera_preflight(
        self, video: FFmpegVideoRecorder, data_tier: DataTier
    ) -> None:
        """正式数据必须在落盘前证明固定控制生效且真实输入接近 30 FPS。"""

        if data_tier != DataTier.PROD:
            return
        deadline = time.monotonic() + 3.0
        while video.source_frame_count < 15 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if not video.control_state.ready:
            details = "；".join(video.control_state.errors) or "控制值未完整读回"
            raise RuntimeError(f"正式录制摄像头固定曝光预检失败：{details}")
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
        video = self._new_video_recorder(str(camera["device"]), None)
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
                    code="imu_reconnect_exhausted",
                    component="ble",
                    error=last_error,
                    hint="摄像头会继续预览；确认 IMU 电量与占用状态后重新连接设备",
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

    async def shutdown(self) -> None:
        """服务退出时释放设备；正式会话保留 partial 并明确标记失败。"""

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
                    camera_probe = self._new_video_recorder(
                        str(camera["device"]), None
                    )
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
            self.writer.handle["video"].attrs.update(
                {
                    "camera_id": str(camera["camera_id"]),
                    "capture_device_at_recording": str(camera["device"]),
                    "product": str(camera["product"]),
                    "usb_interface": str(camera["interface"]),
                }
            )
            self.video = self._new_video_recorder(
                str(camera["device"]), mkv_partial
            )
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
            self.state = RecordingState.FINALIZING
            self.device_state = DeviceSessionState.RELEASING
            capture_ended_at = datetime.now(UTC).isoformat()
            self.current = self.current.model_copy(update={"state": self.state})
            self.catalog.upsert(self.current)
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
                issues.append(str(error))

            partial_h5 = Path(self.current.h5_path or "")
            partial_mkv = Path(self.current.mkv_path or "")
            final_h5 = partial_h5.with_name(partial_h5.name.replace(".partial.h5", ".h5"))
            final_mkv = partial_mkv.with_name(partial_mkv.name.replace(".partial.mkv", ".mkv"))
            normalizing_mkv = partial_mkv.with_name(
                partial_mkv.name.replace(".partial.mkv", ".normalizing.mkv")
            )
            try:
                rate, _residual = capture_writer.reconstruct_times()
                self.observed_rate_hz = rate
                capture_writer.handle["imu"].attrs["callback_drops"] = (
                    capture_ble.dropped_callback_packets
                )
                if not partial_mkv.is_file() or partial_mkv.stat().st_size == 0:
                    raise ValueError("FFmpeg produced no MKV data")
                frame_table = await probe_video_frames(
                    partial_mkv, capture_writer.recording_start_monotonic_ns
                )
                await normalize_video_timeline(partial_mkv, normalizing_mkv)
                normalized_table = await probe_video_frames(
                    normalizing_mkv,
                    int(frame_table.pts_monotonic_ns[0]),
                    pts_are_monotonic=False,
                )
                if not np.array_equal(
                    normalized_table.pts_monotonic_ns,
                    frame_table.pts_monotonic_ns,
                ):
                    raise RuntimeError("MKV 重封装前后的帧数量或逐帧时间间隔不一致")
                normalizing_mkv.replace(final_mkv)
                capture_writer.write_video_frames(
                    pts_monotonic_ns=frame_table.pts_monotonic_ns,
                    media_time_ns=(
                        frame_table.pts_monotonic_ns - frame_table.pts_monotonic_ns[0]
                    ),
                    duration_ns=frame_table.duration_ns,
                    key_frame=frame_table.key_frame,
                    video_path=final_mkv,
                    codec=frame_table.codec,
                    width=frame_table.width,
                    height=frame_table.height,
                    requested_fps=self.settings.video.requested_fps,
                    ffmpeg_diagnostics=capture_video.progress.errors,
                    camera_controls_requested=capture_video.control_state.requested,
                    camera_controls_effective=capture_video.control_state.effective,
                    camera_control_errors=capture_video.control_state.errors,
                )
                span_fps = (
                    (len(frame_table.pts_monotonic_ns) - 1)
                    * 1e9
                    / (
                        frame_table.pts_monotonic_ns[-1]
                        - frame_table.pts_monotonic_ns[0]
                    )
                    if len(frame_table.pts_monotonic_ns) > 1
                    and frame_table.pts_monotonic_ns[-1]
                    > frame_table.pts_monotonic_ns[0]
                    else 0.0
                )
                if (
                    self.current.data_tier == DataTier.PROD
                    and span_fps < self.settings.video.prod_min_span_fps
                ):
                    issues.append(
                        "正式录制视频全程帧率不足："
                        f"{span_fps:.2f} FPS < "
                        f"{self.settings.video.prod_min_span_fps:.2f} FPS"
                    )
                capture_writer.write_sync([])
                capture_writer.finish(ended_at_utc=capture_ended_at)
                partial_h5.replace(final_h5)
                partial_mkv.unlink(missing_ok=True)
            except Exception as error:
                issues.append(str(error))
                capture_writer.abort_close()
            finally:
                # 后续验证和预览恢复都不应再持有采集 writer。即使文件收尾失败，
                # abort_close() 也会在这里之前尽最大努力释放 H5 写锁。
                self.writer = None

            duration_ns: int | None = None
            ended_at = capture_ended_at
            try:
                report = (
                    validate_capture_h5(final_h5, self.taxonomy)
                    if final_h5.is_file()
                    else None
                )
                if report and report.issues:
                    issues.extend(report.issues)
                if final_h5.is_file():
                    with h5py.File(final_h5, "r") as handle:
                        duration_ns = int(handle.attrs.get("duration_ns", 0))
                        ended_at = str(
                            handle.attrs.get("ended_at_utc", capture_ended_at)
                        )
            except Exception as error:
                # 文件已经原子改名后，验证失败属于“需要处理”，不能让会话永久
                # 停留在 finalizing。具体错误会随目录记录保留下来。
                issues.append(f"H5 收尾验证失败：{self._error_message(error, '未知错误')}")
            issues = list(dict.fromkeys(issues))
            final_state = RecordingState.READY if not issues else RecordingState.NEEDS_ATTENTION
            self.state = final_state
            self.current = self.current.model_copy(
                update={
                    "state": final_state,
                    "ended_at_utc": ended_at,
                    "duration_ns": duration_ns,
                    "h5_path": str(final_h5) if final_h5.is_file() else str(partial_h5),
                    "mkv_path": str(final_mkv) if final_mkv.is_file() else str(partial_mkv),
                    "issues": issues,
                }
            )
            self.catalog.upsert(self.current)
            self.mode = "devices_preview"

            # 必须在 H5 已关闭、最终路径和目录终态均已写回之后，才能创建新的
            # FFmpeg 预览进程，避免子进程继承采集文件描述符及其写锁。
            preview_restore_error: Exception | None = None
            self._video_transition = "restoring_preview"
            try:
                camera, preview_video = await self._open_preview_video(
                    self._preview_camera_id
                )
                self.video = preview_video
                self._preview_camera_id = str(camera["camera_id"])
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
            return self.current

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
                issues.extend(report.issues)
                report_path = write_characterization_report(final_h5)
            except Exception as error:
                issues.append(str(error))
                duration_ns = None
                self.writer.abort_close()
            issues = list(dict.fromkeys(issues))
            final_state = (
                RecordingState.READY if not issues else RecordingState.NEEDS_ATTENTION
            )
            self.state = final_state
            self.current = self.current.model_copy(
                update={
                    "state": final_state,
                    "ended_at_utc": datetime.now(UTC).isoformat(),
                    "duration_ns": duration_ns,
                    "h5_path": str(final_h5) if final_h5.is_file() else str(partial_h5),
                    "issues": issues,
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
            "characterization": self.characterization_snapshot()
            if self.mode == "characterization"
            else self.last_characterization,
        }

    async def save_annotations(
        self, recording_id: str, document: AnnotationDocument
    ) -> AnnotationDocument:
        summary = self._required_summary(recording_id)
        h5_path, mkv_path = self._recording_paths(summary)
        enriched = self._canonicalize_and_enrich_events(h5_path, document)
        annotators = {
            item.annotator_id
            for item in (*enriched.segments, *enriched.events, *enriched.exclusions)
        }
        for annotator in annotators:
            self._require_allowed_unikey(annotator, "annotator_id")
        if len(annotators) > 1:
            raise ValueError("一个 review.json 修订只能由一名标注者保存")
        annotation_issues = validate_annotations(
            enriched, self.taxonomy, summary.duration_ns
        )
        if annotation_issues:
            raise ValueError("；".join(annotation_issues))
        current = load_review(h5_path, mkv_path, self.taxonomy)
        if enriched.revision != current.annotations.revision + 1:
            raise ReviewConflictError(
                f"标注已更新：下一 revision 应为 {current.annotations.revision + 1}"
            )

        def update(review: ReviewDocument) -> ReviewDocument:
            if review.workflow.state in {
                ReviewWorkflowState.SUBMITTED,
                ReviewWorkflowState.ACCEPTED,
                ReviewWorkflowState.EXPORTED,
            }:
                raise ValueError("已提交或已审核的标注必须先驳回/重开才能修改")
            annotator = next(iter(annotators), review.workflow.annotator_id)
            workflow = review.workflow
            if (
                workflow.annotator_id is not None
                and annotator is not None
                and workflow.annotator_id != annotator
            ):
                raise ValueError("当前修订的 annotator_id 与已分配标注者不一致")
            if workflow.state == ReviewWorkflowState.UNASSIGNED:
                workflow = workflow_with_timestamp(
                    workflow,
                    state=ReviewWorkflowState.IN_PROGRESS,
                    annotator_id=annotator,
                )
            return review.model_copy(
                update={"annotations": enriched, "workflow": workflow}
            )

        saved = mutate_review(
            h5_path,
            mkv_path,
            self.taxonomy,
            current.revision,
            update,
        )
        self.catalog.enqueue_upload(recording_id, saved.revision)
        return saved.annotations

    def _canonicalize_and_enrich_events(
        self, path: Path, document: AnnotationDocument
    ) -> AnnotationDocument:
        with h5py.File(path, "r") as handle:
            video_times = np.asarray(handle["video/frames/recording_time_ns"], dtype=np.int64)
            imu_times = np.asarray(
                handle[
                    "imu/samples/aligned_video_time_ns"
                    if "imu/samples/aligned_video_time_ns" in handle
                    else "imu/samples/recording_time_ns"
                ],
                dtype=np.int64,
            )

        def nearest(values: np.ndarray, target: int) -> int | None:
            if not len(values):
                return None
            index = int(np.searchsorted(values, target))
            candidates = [item for item in (index - 1, index) if 0 <= item < len(values)]
            return min(candidates, key=lambda item: abs(int(values[item]) - target))

        canonical_events = [
            event for event in document.events if event.kind != EventKind.ONSET
        ]
        canonical_events.extend(
            AnnotationEvent(
                segment_id=segment.segment_id,
                kind=EventKind.ONSET,
                time_ns=segment.start_ns,
                annotator_id=segment.annotator_id,
            )
            for segment in document.segments
            if segment.binary_label == BinaryLabel.FALL
        )
        canonical_events.sort(
            key=lambda event: (event.time_ns, event.segment_id, event.kind.value)
        )
        events = [
            AnnotationEvent(
                **event.model_dump(exclude={"source_video_frame", "source_imu_sample"}),
                source_video_frame=nearest(video_times, event.time_ns),
                source_imu_sample=nearest(imu_times, event.time_ns),
            )
            for event in canonical_events
        ]
        return document.model_copy(update={"events": events})

    async def save_sync(self, recording_id: str, document: SyncDocument) -> dict[str, Any]:
        summary = self._required_summary(recording_id)
        reviewers = {
            reviewer
            for reviewer in (
                document.reviewer_id,
                *(anchor.reviewer_id for anchor in document.anchors),
            )
            if reviewer
        }
        for reviewer in reviewers:
            self._require_allowed_unikey(reviewer, "reviewer_id")
        h5_path, mkv_path = self._recording_paths(summary)
        self._validate_sync_anchor_sources(h5_path, document)
        assessment = assess_conditional_fixed_offset(document)
        current = load_review(h5_path, mkv_path, self.taxonomy)
        expected_revision = (
            document.expected_revision
            if document.expected_revision is not None
            else current.revision
        )

        def update(review: ReviewDocument) -> ReviewDocument:
            if review.workflow.state in {
                ReviewWorkflowState.ACCEPTED,
                ReviewWorkflowState.EXPORTED,
            }:
                raise ValueError("已审核的同步结论必须先重开才能修改")
            return review.model_copy(update={"sync": document})

        mutate_review(
            h5_path,
            mkv_path,
            self.taxonomy,
            expected_revision,
            update,
        )
        return self._sync_display(h5_path, assessment.as_dict())

    def _validate_sync_anchor_sources(
        self, path: Path, document: SyncDocument
    ) -> None:
        with h5py.File(path, "r") as handle:
            start_ns = int(handle.attrs["recording_start_monotonic_ns"])
            video_times = np.asarray(
                handle["video/frames/recording_time_ns"], dtype=np.int64
            )
            imu_times = (
                np.asarray(handle["imu/samples/time_monotonic_ns"], dtype=np.int64)
                - start_ns
            )
        for anchor in document.anchors:
            if anchor.source_video_frame is not None:
                if anchor.source_video_frame >= len(video_times):
                    raise ValueError("同步锚点引用的视频帧不存在")
                if int(video_times[anchor.source_video_frame]) != anchor.video_time_ns:
                    raise ValueError("同步锚点的视频时间与来源帧不一致")
                expected_video_start = int(
                    video_times[max(0, anchor.source_video_frame - 1)]
                )
                if anchor.video_interval_start_ns != expected_video_start:
                    raise ValueError("同步锚点的视频离散区间与来源帧不一致")
            if anchor.source_imu_sample is not None:
                if anchor.source_imu_sample >= len(imu_times):
                    raise ValueError("同步锚点引用的 IMU 样本不存在")
                if int(imu_times[anchor.source_imu_sample]) != anchor.imu_time_ns:
                    raise ValueError("同步锚点的 IMU 时间与来源样本不一致")
                expected_imu_start = int(
                    imu_times[max(0, anchor.source_imu_sample - 1)]
                )
                if anchor.imu_interval_start_ns != expected_imu_start:
                    raise ValueError("同步锚点的 IMU 离散区间与来源样本不一致")

    def sync(self, recording_id: str) -> dict[str, Any]:
        summary = self._required_summary(recording_id)
        h5_path, mkv_path = self._recording_paths(summary)
        document = load_review(h5_path, mkv_path, self.taxonomy).sync
        if len(document.anchors) != 2:
            result = {
                "policy": document.policy,
                "scale": 1.0,
                "estimated_offset_ns": 0,
                "applied_offset_ns": 0,
                "offset_ns": 0,
                "start_offset_ns": 0,
                "end_offset_ns": 0,
                "anchor_disagreement_ns": 0,
                "residual_rms_ns": float("nan"),
                "residual_upper_bound_ns": 0,
                "recommendation": "none",
                "decision": "host_only",
                "quality": "missing",
            }
        else:
            result = assess_conditional_fixed_offset(document).as_dict()
        result["anchors"] = [item.model_dump(mode="json") for item in document.anchors]
        return self._sync_display(h5_path, result)

    def _sync_display(self, path: Path, result: dict[str, Any]) -> dict[str, Any]:
        with h5py.File(path, "r") as handle:
            frames = (
                np.asarray(handle["video/frames/recording_time_ns"], dtype=np.int64)
                if "video/frames/recording_time_ns" in handle
                else np.empty(0, dtype=np.int64)
            )
            frame_delta = np.diff(frames)
            median_frame_ns = (
                float(np.median(frame_delta[frame_delta > 0]))
                if np.any(frame_delta > 0)
                else 0.0
            )
            observed_rate = float(handle["imu"].attrs.get("observed_rate_hz", 0.0))
        estimated = int(result.get("estimated_offset_ns", 0))
        applied = int(result.get("applied_offset_ns", 0))
        return {
            **result,
            "estimated_offset_seconds": estimated / 1e9,
            "applied_offset_seconds": applied / 1e9,
            "estimated_offset_video_frames": (
                estimated / median_frame_ns if median_frame_ns > 0 else None
            ),
            "applied_offset_video_frames": (
                applied / median_frame_ns if median_frame_ns > 0 else None
            ),
            "estimated_offset_imu_samples": (
                estimated / 1e9 * observed_rate if observed_rate > 0 else None
            ),
            "applied_offset_imu_samples": (
                applied / 1e9 * observed_rate if observed_rate > 0 else None
            ),
            "actual_median_fps": (
                1e9 / median_frame_ns if median_frame_ns > 0 else None
            ),
            "observed_imu_rate_hz": observed_rate or None,
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
        """由用户明确触发，把一次完整录制交付给独立标注存储。"""

        summary = self._required_summary(recording_id)
        if summary.state != RecordingState.READY:
            raise ValueError("只有通过采集验证的 ready 录制可以发布")
        if (
            self.mode == "capture"
            and self.current
            and self.current.recording_id == recording_id
        ):
            raise ValueError("当前会话仍在使用该录制")
        updating = summary.model_copy(update={"upload_state": "packaging"})
        self.catalog.upsert(updating)
        try:
            updating = updating.model_copy(update={"upload_state": "uploading"})
            self.catalog.upsert(updating)
            manifest = await publish_recording(updating, self.settings, self.object_store)
            updating = updating.model_copy(update={"upload_state": "verifying"})
            self.catalog.upsert(updating)
            manifest_key = f"captures/{recording_id}/manifest.json"
            if self.object_store.stat(manifest_key) is None:
                raise RuntimeError("远端 manifest 验证失败")
            updating = updating.model_copy(update={"upload_state": "published"})
            self.catalog.upsert(updating)
            return manifest.model_dump(mode="json")
        except Exception:
            self.catalog.upsert(updating.model_copy(update={"upload_state": "failed"}))
            raise

    def publish_estimate(self, recording_id: str) -> dict[str, int | str]:
        summary = self._required_summary(recording_id)
        h5_path, mkv_path = self._recording_paths(summary)
        return {
            "recording_id": recording_id,
            "estimated_bytes": h5_path.stat().st_size + 2 * mkv_path.stat().st_size,
        }

    def annotations(self, recording_id: str) -> AnnotationDocument:
        summary = self._required_summary(recording_id)
        h5_path, mkv_path = self._recording_paths(summary)
        return load_review(h5_path, mkv_path, self.taxonomy).annotations

    def frame_times(self, recording_id: str) -> dict[str, Any]:
        summary = self._required_summary(recording_id)
        return read_frame_times(Path(summary.h5_path or ""))

    def sync_window(
        self,
        recording_id: str,
        frame_index: int,
        radius_seconds: float,
        expected_video_minus_imu_ns: int | None = None,
    ) -> dict[str, Any]:
        summary = self._required_summary(recording_id)
        return read_sync_window(
            Path(summary.h5_path or ""),
            frame_index,
            radius_seconds,
            expected_video_minus_imu_ns,
        )

    def sync_experiment(self, experiment_id: str) -> SyncExperimentDocument:
        return load_sync_experiment(self.settings.data_root, experiment_id)

    def save_sync_experiment(
        self, experiment_id: str, document: SyncExperimentDocument
    ) -> SyncExperimentDocument:
        if document.experiment_id != experiment_id:
            raise ValueError("URL 与同步实验文档的 experiment_id 不一致")
        reviewers = {item.reviewer_id for item in document.observations}
        for reviewer in reviewers:
            self._require_allowed_unikey(reviewer, "reviewer_id")
        recording_paths: dict[str, tuple[Path, Path]] = {}
        for recording_id in {item.recording_id for item in document.observations}:
            summary = self._required_summary(recording_id)
            if not summary.h5_path or not summary.mkv_path:
                raise ValueError(f"录制文件不完整：{recording_id}")
            recording_paths[recording_id] = (
                Path(summary.h5_path),
                Path(summary.mkv_path),
            )
        return save_sync_experiment(
            self.settings.data_root, document, recording_paths
        )

    def timeline(self, recording_id: str, max_points: int = 5_000) -> dict[str, Any]:
        summary = self._required_summary(recording_id)
        h5_path, mkv_path = self._recording_paths(summary)
        review = load_review(h5_path, mkv_path, self.taxonomy)
        applied_offset_ns = 0
        if len(review.sync.anchors) == 2:
            applied_offset_ns = assess_conditional_fixed_offset(
                review.sync
            ).applied_offset_ns
        with h5py.File(h5_path, "r") as handle:
            times = np.asarray(
                handle["imu/samples/recording_time_ns"],
                dtype=np.int64,
            ) + applied_offset_ns
            values_si = np.asarray(handle["imu/samples/values_si"], dtype=np.float32)
            raw = np.asarray(handle["imu/samples/raw_counts"], dtype=np.int16)
            calibrated = bool(handle.attrs.get("calibration_verified", False))
        values = values_si if calibrated else raw.astype(np.float32)
        step = max(1, int(np.ceil(len(times) / max_points)))
        return {
            "time_s": (times[::step] / 1e9).tolist(),
            "values": values[::step].tolist(),
            "unit": "SI" if calibrated else "raw_counts",
            "downsample_step": step,
        }

    def review(self, recording_id: str) -> ReviewDocument:
        summary = self._required_summary(recording_id)
        h5_path, mkv_path = self._recording_paths(summary)
        return load_review(h5_path, mkv_path, self.taxonomy)

    def update_workflow(
        self, recording_id: str, request: ReviewWorkflowRequest
    ) -> ReviewDocument:
        self._require_allowed_unikey(request.actor_id, "actor_id")
        summary = self._required_summary(recording_id)
        h5_path, mkv_path = self._recording_paths(summary)

        def update(review: ReviewDocument) -> ReviewDocument:
            workflow = review.workflow
            action = request.action
            if action == "assign":
                if workflow.state not in {
                    ReviewWorkflowState.UNASSIGNED,
                    ReviewWorkflowState.IN_PROGRESS,
                }:
                    raise ValueError("当前状态不能重新分配标注者")
                workflow = workflow_with_timestamp(
                    workflow,
                    state=ReviewWorkflowState.IN_PROGRESS,
                    annotator_id=request.actor_id,
                    reviewer_id=None,
                    review_comment="",
                )
            elif action == "submit":
                if workflow.state != ReviewWorkflowState.IN_PROGRESS:
                    raise ValueError("只有进行中的标注可以提交")
                if workflow.annotator_id != request.actor_id:
                    raise ValueError("只有当前标注者可以提交")
                if not review.annotations.finalized:
                    raise ValueError("提交前必须完成并定稿标注")
                if len(review.sync.anchors) != 2 or assess_conditional_fixed_offset(
                    review.sync
                ).quality != "verified":
                    raise ValueError("提交前必须完成同步复核")
                workflow = workflow_with_timestamp(
                    workflow, state=ReviewWorkflowState.SUBMITTED
                )
            elif action in {"accept", "reject"}:
                if workflow.state != ReviewWorkflowState.SUBMITTED:
                    raise ValueError("只有已提交的标注可以审核")
                if workflow.annotator_id == request.actor_id:
                    raise ValueError("标注者不能审核自己的标注")
                if action == "reject" and not request.comment.strip():
                    raise ValueError("驳回时必须填写审核意见")
                workflow = workflow_with_timestamp(
                    workflow,
                    state=(
                        ReviewWorkflowState.ACCEPTED
                        if action == "accept"
                        else ReviewWorkflowState.IN_PROGRESS
                    ),
                    reviewer_id=request.actor_id,
                    review_comment=request.comment.strip(),
                )
            elif action == "reopen":
                if request.actor_id not in self.settings.identity.admins:
                    raise ValueError("只有管理员可以重开已审核标注")
                if workflow.state not in {
                    ReviewWorkflowState.ACCEPTED,
                    ReviewWorkflowState.EXPORTED,
                }:
                    raise ValueError("只有已审核或已导出的标注可以重开")
                workflow = workflow_with_timestamp(
                    workflow,
                    state=ReviewWorkflowState.IN_PROGRESS,
                    reviewer_id=None,
                    review_comment=request.comment.strip(),
                )
            else:
                raise ValueError("mark_exported 只能由成功的导出任务设置")
            return review.model_copy(update={"workflow": workflow})

        return mutate_review(
            h5_path,
            mkv_path,
            self.taxonomy,
            request.expected_revision,
            update,
        )

    def package_estimate(self, recording_id: str) -> dict[str, int | str]:
        summary = self._required_summary(recording_id)
        h5_path, mkv_path = self._recording_paths(summary)
        return {
            "recording_id": recording_id,
            "estimated_bytes": estimate_capture_package_bytes(h5_path, mkv_path),
        }

    def create_package(self, recording_id: str) -> Path:
        summary = self._required_summary(recording_id)
        h5_path, mkv_path = self._recording_paths(summary)
        review = load_review(h5_path, mkv_path, self.taxonomy)
        output = h5_path.parent / f"{recording_id}.capture.tar"
        return create_capture_package(review, h5_path, mkv_path, output)

    def export_training(self, recording_id: str, expected_revision: int) -> Path:
        summary = self._required_summary(recording_id)
        h5_path, mkv_path = self._recording_paths(summary)
        review = load_review(h5_path, mkv_path, self.taxonomy)
        if review.revision != expected_revision:
            raise ReviewConflictError("review.json 已更新，请刷新后重试导出")
        output = h5_path.parent / "aligned30.h5"
        export_aligned30(
            review,
            h5_path,
            mkv_path,
            output,
            self.settings.imu,
            self.taxonomy,
        )

        def mark_exported(current: ReviewDocument) -> ReviewDocument:
            return current.model_copy(
                update={
                    "workflow": workflow_with_timestamp(
                        current.workflow, state=ReviewWorkflowState.EXPORTED
                    )
                }
            )

        mutate_review(
            h5_path,
            mkv_path,
            self.taxonomy,
            expected_revision,
            mark_exported,
        )
        return output

    def create_training_release(self) -> Path:
        files: list[tuple[str, str, Path]] = []
        for summary in self.catalog.list():
            if not summary.h5_path or not summary.mkv_path:
                continue
            h5_path, mkv_path = self._recording_paths(summary)
            review = load_review(h5_path, mkv_path, self.taxonomy)
            aligned = h5_path.parent / "aligned30.h5"
            if (
                review.workflow.state == ReviewWorkflowState.EXPORTED
                and aligned.is_file()
            ):
                files.append((summary.participant_id, summary.recording_id, aligned))
        release_root = self.settings.data_root / "_releases"
        ordinal = 1
        while True:
            output = release_root / f"cw12eu_training_release_{ordinal:04d}.tar"
            if not output.exists():
                break
            ordinal += 1
        return create_training_release(files, output)

    def status(self, recording_id: str) -> dict[str, Any]:
        summary = self._required_summary(recording_id)
        h5_path, mkv_path = self._recording_paths(summary)
        review = load_review(h5_path, mkv_path, self.taxonomy)
        sync_quality = "missing"
        if len(review.sync.anchors) == 2:
            sync_quality = assess_conditional_fixed_offset(review.sync).quality
        calibration = bool(
            self.settings.imu.accel_counts_per_g
            and self.settings.imu.gyro_counts_per_dps
        )
        return {
            "capture": summary.state.value,
            "sync": sync_quality,
            "annotation": review.workflow.state.value,
            "calibration": "verified" if calibration else "unverified",
            "export": (
                "exported"
                if review.workflow.state == ReviewWorkflowState.EXPORTED
                else "not_exported"
            ),
            "review_revision": review.revision,
        }

    def incomplete_files(self) -> list[dict[str, object]]:
        return scan_incomplete_files(self.settings.data_root)

    def quarantine_file(self, relative_path: str) -> Path:
        return quarantine_incomplete(self.settings.data_root, relative_path)

    def rebuild_catalog(self) -> dict[str, int]:
        return rebuild_catalog(self.settings.data_root, self.catalog)

    def delete_recording(self, recording_id: str, confirmation: str) -> Path:
        summary = self._required_summary(recording_id)
        if self.current and self.current.recording_id == recording_id:
            raise ValueError("当前会话正在使用该录制")
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
