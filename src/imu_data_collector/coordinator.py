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

    async def _resolve_camera(self) -> str:
        configured = self.settings.video.device
        if configured:
            return configured
        devices = await discover_video_devices()
        preferred = next(
            (item for item in devices if item["supports_default_profile"]), None
        )
        if preferred is None:
            raise RuntimeError("no V4L2 camera supports the configured 1080p30 MJPEG profile")
        return str(preferred["device"])

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
            self._check_disk()
            camera = await self._resolve_camera()
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
            self.ble = CW12EUBleSource(self.settings.imu)
            self.video = FFmpegVideoRecorder(self.settings.video, camera, mkv_partial)
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
            self.catalog.upsert(self.current)

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
            if self.state != RecordingState.RECORDING or not self.current:
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
            return self.current

    def snapshot(self) -> dict[str, Any]:
        video = self.video.progress if self.video else None
        ble = self.ble
        free_gib = shutil.disk_usage(self.settings.data_root).free / 1024**3
        return {
            "state": self.state.value,
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
        }

    async def save_annotations(
        self, recording_id: str, document: AnnotationDocument
    ) -> AnnotationDocument:
        summary = self._required_summary(recording_id)
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
