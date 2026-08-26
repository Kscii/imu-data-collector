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
        self._video_stream_id = 0
        self.preview_error: str | None = None

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

    def _new_video_recorder(
        self, device: str, output_path: Path | None
    ) -> FFmpegVideoRecorder:
        """创建新的视频进程，并让前端能识别同一会话内的视频流切换。"""

        self._video_stream_id += 1
        return FFmpegVideoRecorder(self.settings.video, device, output_path)

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

    async def _start_preview_locked(self, camera_id: str | None) -> None:
        if self.mode == "devices_preview" and self.ble and self.ble.connected:
            return
        if self.mode is not None or self.state in {
            RecordingState.ARMING,
            RecordingState.RECORDING,
            RecordingState.FINALIZING,
        }:
            raise RuntimeError("当前有其他设备会话，不能开始设备预览")
        camera = await self._resolve_camera(camera_id)
        self.mode = "devices_preview"
        self._monitoring_requested = True
        self._preview_camera_id = str(camera["camera_id"])
        self.preview_error = None
        self.ble = CW12EUBleSource(self.settings.imu)
        self.video = self._new_video_recorder(str(camera["device"]), None)
        self._stop_consumer.clear()
        self._reset_live_imu_metrics()
        try:
            await self.ble.start()
            self._consumer = asyncio.create_task(self._consume_imu_preview())
            await self.video.start()
        except Exception as error:
            self.preview_error = str(error)
            await self._stop_preview_locked(preserve_request=False)
            raise

    async def start_preview(self, request: PreviewStartRequest) -> dict[str, Any]:
        """连接 IMU 与摄像头，只在内存中提供实时预览，不创建采集文件。"""

        async with self._lock:
            await self._start_preview_locked(request.camera_id)
            return self.snapshot()

    async def switch_preview_camera(
        self, request: PreviewStartRequest
    ) -> dict[str, Any]:
        """仅重启摄像头预览；保留当前 BLE 连接和 IMU 曲线。"""

        async with self._lock:
            if self.mode != "devices_preview" or not self.ble or not self.ble.connected:
                raise RuntimeError("请先连接预览设备，再切换摄像头")
            camera = await self._resolve_camera(request.camera_id)
            camera_id = str(camera["camera_id"])
            if camera_id == self._preview_camera_id and self.video:
                return self.snapshot()
            previous_id = self._preview_camera_id
            previous_device = self.video.device if self.video else None
            if self.video:
                await self.video.stop()
            try:
                replacement = self._new_video_recorder(str(camera["device"]), None)
                await replacement.start()
            except Exception:
                if previous_device:
                    rollback = self._new_video_recorder(previous_device, None)
                    await rollback.start()
                    self.video = rollback
                    self._preview_camera_id = previous_id
                raise
            self.video = replacement
            self._preview_camera_id = camera_id
            self.preview_error = None
            return self.snapshot()

    async def _stop_preview_locked(self, *, preserve_request: bool) -> None:
        if self.mode != "devices_preview":
            return
        if self.video:
            try:
                await self.video.stop()
            except Exception:
                pass
        if self.ble:
            try:
                await self.ble.stop()
            except Exception:
                pass
        await self._stop_consumer_locked()
        self.ble = None
        self.video = None
        self.mode = None
        if not preserve_request:
            self._monitoring_requested = False
            self._preview_camera_id = None

    async def stop_preview(self) -> dict[str, Any]:
        async with self._lock:
            if self.mode != "devices_preview":
                raise RuntimeError("当前没有正在运行的设备预览")
            await self._stop_preview_locked(preserve_request=False)
            return self.snapshot()

    async def shutdown(self) -> None:
        """服务退出时释放仅用于预览的设备句柄。"""

        async with self._lock:
            if self.mode == "devices_preview":
                await self._stop_preview_locked(preserve_request=False)

    async def _consume_imu_preview(self) -> None:
        assert self.ble is not None
        while not self._stop_consumer.is_set() or not self.ble.queue.empty():
            try:
                packet = await asyncio.wait_for(self.ble.queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            self.packet_count += 1
            if self._first_packet_ns is None:
                self._first_packet_ns = packet.receive_time_ns
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

    async def start(self, request: RecordingStartRequest) -> RecordingSummary:
        async with self._lock:
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
            if self.mode == "devices_preview":
                if self.video:
                    await self.video.stop()
                await self._stop_consumer_locked()
                retained_ble = self.ble if self.ble and self.ble.connected else None
                self.video = None
                self.mode = None
            self._monitoring_requested = True
            self._preview_camera_id = str(camera["camera_id"])
            self.preview_error = None
            self.mode = "capture"
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
            self.state = RecordingState.ARMING
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
            self.ble = retained_ble or CW12EUBleSource(self.settings.imu)
            self.video = self._new_video_recorder(
                str(camera["device"]), mkv_partial
            )
            self._stop_consumer.clear()
            self._reset_live_imu_metrics()
            try:
                if retained_ble:
                    self.ble.dropped_callback_packets = 0
                    self.writer.append_connection_event(
                        "ble_reused_from_preview", time.monotonic_ns()
                    )
                else:
                    await self.ble.start()
                    self.writer.append_connection_event(
                        "ble_connected", time.monotonic_ns()
                    )
                await self.video.start()
                capture_start_ns = self.video.started_monotonic_ns or time.monotonic_ns()
                capture_started_at = datetime.now(UTC).isoformat()
                self.writer.set_recording_start(
                    capture_start_ns, capture_started_at
                )
                self._consumer = asyncio.create_task(self._consume_imu())
            except Exception as error:
                await self._abort_start(error)
                raise
            self.state = RecordingState.RECORDING
            self.current = summary.model_copy(
                update={"state": self.state, "started_at_utc": capture_started_at}
            )
            self.catalog.upsert(self.current)
            return self.current

    async def _abort_start(self, error: Exception) -> None:
        if self.video:
            try:
                await self.video.stop()
            except Exception:
                pass
        if self.ble:
            try:
                await self.ble.stop()
            except Exception:
                pass
        if self._consumer:
            self._stop_consumer.set()
            self._consumer.cancel()
            await asyncio.gather(self._consumer, return_exceptions=True)
        if self.writer:
            self.writer.abort_close()
        self.state = RecordingState.FAILED
        if self.current:
            self.current = self.current.model_copy(
                update={"state": self.state, "issues": [str(error)]}
            )
            if self.mode == "capture":
                self.catalog.upsert(self.current)
        self.mode = None

    async def _consume_imu(self) -> None:
        assert self.ble is not None and self.writer is not None
        while not self._stop_consumer.is_set() or not self.ble.queue.empty():
            try:
                packet = await asyncio.wait_for(self.ble.queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            if packet.receive_time_ns < self.writer.recording_start_monotonic_ns:
                continue
            parsed_count = self.writer.append_notification(
                packet.payload, packet.receive_time_ns
            )
            self.packet_count += 1
            if self._first_packet_ns is None:
                self._first_packet_ns = packet.receive_time_ns
            self.sample_count += parsed_count
            self.latest_packet_samples = parsed_count
            if parsed_count:
                self.latest_raw = np.asarray(
                    self.writer.handle["imu/samples/raw_counts"][-1], dtype=np.int16
                )

    async def stop(self) -> RecordingSummary:
        async with self._lock:
            if (
                self.state != RecordingState.RECORDING
                or not self.current
                or self.mode != "capture"
            ):
                raise RuntimeError("no active recording")
            self.state = RecordingState.FINALIZING
            capture_ended_at = datetime.now(UTC).isoformat()
            self.current = self.current.model_copy(update={"state": self.state})
            self.catalog.upsert(self.current)
            issues: list[str] = []
            assert self.video and self.ble and self.writer
            try:
                await self.video.stop()
            except Exception as error:
                issues.append(str(error))
            try:
                self.writer.append_connection_event(
                    "ble_stop_requested", time.monotonic_ns()
                )
                await self.ble.stop()
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
            partial_mkv = Path(self.current.mkv_path or "")
            final_h5 = partial_h5.with_name(partial_h5.name.replace(".partial.h5", ".h5"))
            final_mkv = partial_mkv.with_name(partial_mkv.name.replace(".partial.mkv", ".mkv"))
            normalizing_mkv = partial_mkv.with_name(
                partial_mkv.name.replace(".partial.mkv", ".normalizing.mkv")
            )
            try:
                rate, _residual = self.writer.reconstruct_times()
                self.observed_rate_hz = rate
                self.writer.handle["imu"].attrs["callback_drops"] = (
                    self.ble.dropped_callback_packets
                )
                if not partial_mkv.is_file() or partial_mkv.stat().st_size == 0:
                    raise ValueError("FFmpeg produced no MKV data")
                frame_table = await probe_video_frames(
                    partial_mkv, self.writer.recording_start_monotonic_ns
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
                self.writer.write_video_frames(
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
                    ffmpeg_diagnostics=self.video.progress.errors,
                )
                self.writer.write_sync([])
                self.writer.finish(ended_at_utc=capture_ended_at)
                partial_h5.replace(final_h5)
                partial_mkv.unlink(missing_ok=True)
            except Exception as error:
                issues.append(str(error))
                self.writer.abort_close()

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
                    ended_at = str(handle.attrs.get("ended_at_utc", datetime.now(UTC).isoformat()))
            else:
                duration_ns = None
                ended_at = datetime.now(UTC).isoformat()
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
            self.mode = None
            self.ble = None
            self.video = None
            self.writer = None
            if self._monitoring_requested:
                try:
                    await self._start_preview_locked(self._preview_camera_id)
                except Exception as error:
                    self.preview_error = f"录制已完成，但自动恢复设备预览失败：{error}"
            return self.current

    async def start_characterization(
        self, request: CharacterizationStartRequest
    ) -> dict[str, Any]:
        async with self._lock:
            if self.mode == "devices_preview":
                await self._stop_preview_locked(preserve_request=True)
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
            issues: list[str] = []
            if self.current_stage is not None:
                self._close_characterization_stage()
            assert self.ble is not None and self.writer is not None
            try:
                self.writer.append_connection_event(
                    "ble_stop_requested", time.monotonic_ns()
                )
                await self.ble.stop()
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
                    self.ble.dropped_callback_packets
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
        video = self.video.progress if self.video else None
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
            "recording": self.current.model_dump(mode="json") if self.current else None,
            "imu": {
                "connected": bool(ble and ble.connected),
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
                "frame": video.frame if video else 0,
                "fps": video.fps if video else 0.0,
                "bitrate": video.bitrate if video else "0",
                "speed": video.speed if video else "0x",
                "preview_only": self.mode == "devices_preview",
                "stream_id": self._video_stream_id,
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
