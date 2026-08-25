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

from imu_data_collector.ble import CW12EUBleSource
from imu_data_collector.catalog import RecordingCatalog
from imu_data_collector.characterization import write_characterization_report
from imu_data_collector.config import Settings, load_activity_taxonomy
from imu_data_collector.hdf5_store import (
    CaptureH5Writer,
    read_annotations,
    replace_annotations_atomic,
    replace_sync_atomic,
)
from imu_data_collector.models import (
    AnnotationDocument,
    AnnotationEvent,
    CharacterizationStageRequest,
    CharacterizationStartRequest,
    RecordingStartRequest,
    RecordingState,
    RecordingSummary,
    SyncDocument,
)
from imu_data_collector.upload import RcloneRemoteStore
from imu_data_collector.validation import validate_capture_h5
from imu_data_collector.video import (
    FFmpegVideoRecorder,
    discover_video_devices,
    probe_video_frames,
    select_video_device,
)


class RecordingCoordinator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.taxonomy = load_activity_taxonomy(settings.activity_taxonomy_path)
        self.catalog = RecordingCatalog(settings.catalog_path)
        self.remote = RcloneRemoteStore(settings.upload)
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
            self.ble = CW12EUBleSource(self.settings.imu)
            self.video = FFmpegVideoRecorder(
                self.settings.video, str(camera["device"]), mkv_partial
            )
            self._stop_consumer.clear()
            self.packet_count = 0
            self.sample_count = 0
            self.observed_rate_hz = 0.0
            self.latest_raw[:] = 0
            try:
                await self.ble.start()
                self._consumer = asyncio.create_task(self._consume_imu())
                await self.video.start()
            except Exception as error:
                await self._abort_start(error)
                raise
            self.state = RecordingState.RECORDING
            self.current = summary.model_copy(update={"state": self.state})
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
            parsed_count = self.writer.append_notification(
                packet.payload, packet.receive_time_ns
            )
            self.packet_count += 1
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
            self.current = self.current.model_copy(update={"state": self.state})
            self.catalog.upsert(self.current)
            issues: list[str] = []
            assert self.video and self.ble and self.writer
            try:
                await self.video.stop()
            except Exception as error:
                issues.append(str(error))
            try:
                await self.ble.stop()
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
            try:
                rate, _residual = self.writer.reconstruct_times()
                self.observed_rate_hz = rate
                if not partial_mkv.is_file() or partial_mkv.stat().st_size == 0:
                    raise ValueError("FFmpeg produced no MKV data")
                partial_mkv.replace(final_mkv)
                frame_table = await probe_video_frames(
                    final_mkv, self.writer.recording_start_monotonic_ns
                )
                self.writer.write_video_frames(
                    pts_monotonic_ns=frame_table.pts_monotonic_ns,
                    duration_ns=frame_table.duration_ns,
                    key_frame=frame_table.key_frame,
                    video_path=final_mkv,
                    codec=frame_table.codec,
                    width=frame_table.width,
                    height=frame_table.height,
                    requested_fps=self.settings.video.requested_fps,
                )
                self.writer.write_sync([])
                self.writer.finish()
                partial_h5.replace(final_h5)
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
                    if str(handle["sync"].attrs.get("quality", "missing")) != "verified":
                        issues.append("synchronization anchors have not been verified")
                    if not bool(handle.attrs.get("calibration_verified", False)):
                        issues.append("IMU scale calibration has not been verified")
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
            return self.current

    async def start_characterization(
        self, request: CharacterizationStartRequest
    ) -> dict[str, Any]:
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
                body_location="chest",
                protocol_id="imu_characterization_v1",
            )
            self.current = RecordingSummary(
                recording_id=recording_id,
                collection_id="_diagnostics",
                participant_id=request.operator_id,
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
            self.packet_count = 0
            self.sample_count = 0
            self.observed_rate_hz = 0.0
            self.latest_raw[:] = 0
            try:
                await self.ble.start()
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
                await self.ble.stop()
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
        free_gib = shutil.disk_usage(self.settings.data_root).free / 1024**3
        return {
            "state": self.state.value,
            "session_type": self.mode,
            "recording": self.current.model_dump(mode="json") if self.current else None,
            "imu": {
                "connected": bool(ble and ble.connected),
                "raw": self.latest_raw.astype(int).tolist(),
                "packet_count": self.packet_count,
                "sample_count": self.sample_count,
                "last_packet_ns": ble.last_packet_ns if ble else None,
                "callback_drops": ble.dropped_callback_packets if ble else 0,
                "observed_rate_hz": self.observed_rate_hz,
            },
            "video": {
                "frame": video.frame if video else 0,
                "fps": video.fps if video else 0.0,
                "bitrate": video.bitrate if video else "0",
                "speed": video.speed if video else "0x",
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
        annotators = {
            item.annotator_id for item in (*document.segments, *document.events)
        }
        for annotator in annotators:
            self._require_allowed_unikey(annotator, "annotator_id")
        path = Path(summary.h5_path or "")
        enriched = self._enrich_event_indices(path, document)
        replace_annotations_atomic(path, enriched, self.taxonomy)
        self.catalog.enqueue_upload(recording_id, enriched.revision)
        return enriched

    def _enrich_event_indices(
        self, path: Path, document: AnnotationDocument
    ) -> AnnotationDocument:
        with h5py.File(path, "r") as handle:
            video_times = np.asarray(handle["video/frames/recording_time_ns"], dtype=np.int64)
            imu_times = np.asarray(handle["imu/samples/recording_time_ns"], dtype=np.int64)

        def nearest(values: np.ndarray, target: int) -> int | None:
            if not len(values):
                return None
            index = int(np.searchsorted(values, target))
            candidates = [item for item in (index - 1, index) if 0 <= item < len(values)]
            return min(candidates, key=lambda item: abs(int(values[item]) - target))

        events = [
            AnnotationEvent(
                **event.model_dump(exclude={"source_video_frame", "source_imu_sample"}),
                source_video_frame=nearest(video_times, event.time_ns),
                source_imu_sample=nearest(imu_times, event.time_ns),
            )
            for event in document.events
        ]
        return document.model_copy(update={"events": events})

    async def save_sync(self, recording_id: str, document: SyncDocument) -> dict[str, Any]:
        summary = self._required_summary(recording_id)
        model = replace_sync_atomic(Path(summary.h5_path or ""), document.anchors, self.taxonomy)
        return {
            "scale": model.scale,
            "offset_ns": model.offset_ns,
            "residual_rms_ns": model.residual_rms_ns,
            "quality": model.quality,
        }

    def sync(self, recording_id: str) -> dict[str, Any]:
        summary = self._required_summary(recording_id)
        with h5py.File(Path(summary.h5_path or ""), "r") as handle:
            group = handle["sync"]
            imu = np.asarray(group["imu_anchor_ns"], dtype=np.int64)
            video = np.asarray(group["video_anchor_ns"], dtype=np.int64)
            labels = (
                [str(item) for item in group["labels"].asstr()[:]]
                if "labels" in group
                else ["tap"] * len(imu)
            )
            return {
                "anchors": [
                    {
                        "imu_time_ns": int(imu[index]),
                        "video_time_ns": int(video[index]),
                        "label": labels[index],
                    }
                    for index in range(len(imu))
                ],
                "scale": float(group.attrs.get("scale", 1.0)),
                "offset_ns": float(group.attrs.get("offset_ns", 0.0)),
                "residual_rms_ns": float(
                    group.attrs.get("residual_rms_ns", float("nan"))
                ),
                "quality": str(group.attrs.get("quality", "missing")),
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

    def annotations(self, recording_id: str) -> AnnotationDocument:
        summary = self._required_summary(recording_id)
        return read_annotations(Path(summary.h5_path or ""))

    def timeline(self, recording_id: str, max_points: int = 5_000) -> dict[str, Any]:
        summary = self._required_summary(recording_id)
        with h5py.File(Path(summary.h5_path or ""), "r") as handle:
            times = np.asarray(handle["imu/samples/recording_time_ns"], dtype=np.int64)
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

    def _required_summary(self, recording_id: str) -> RecordingSummary:
        summary = self.catalog.get(recording_id)
        if summary is None:
            raise KeyError(recording_id)
        return summary
