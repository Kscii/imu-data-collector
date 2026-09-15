"""Uncalibrated face identification and a leased, IMU-only setup session."""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from collections import deque
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from imu_data_collector.models import PreviewStartRequest

Direction = Literal["+X", "-X", "+Y", "-Y", "+Z", "-Z"]
LEASE_SECONDS = 15.0


class OrientationWindow:
    """One entry per actual decoded sample; web polling never adds samples."""

    def __init__(self):
        self.samples = deque(maxlen=4000)

    def clear(self):
        self.samples.clear()

    def add(self, raw, received_ns: int):
        self.samples.append((received_ns, np.asarray(raw, dtype=float).copy()))
        while self.samples and self.samples[0][0] < received_ns - 2_000_000_000:
            self.samples.popleft()

    def evaluate(self, now_ns: int | None = None) -> dict:
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        result = {"direction": None, "reason": "waiting_samples", "sample_count": 0}
        if not self.samples:
            return result
        if now_ns - self.samples[-1][0] > 500_000_000:
            return {**result, "reason": "stale"}
        samples = [(t, r) for t, r in self.samples if t >= now_ns - 2_000_000_000]
        if not samples:
            return result
        times, raw = zip(*samples, strict=True)
        raw = np.asarray(raw)
        result.update(sample_count=len(raw), start_ns=times[0], end_ns=times[-1])
        if len(raw) < 20 or times[-1] - times[0] < 1_500_000_000:
            return result
        # Sample gaps and duplicated device packets cannot establish a stable pose.
        if np.max(np.diff(times)) > 500_000_000:
            return {**result, "reason": "waiting_samples"}
        if not np.isfinite(raw).all() or np.any((raw <= -32768) | (raw >= 32767)):
            return {**result, "reason": "saturated"}
        median = np.median(raw, axis=0)
        norm = float(np.linalg.norm(median[:3]))
        if norm < 1:
            return {**result, "reason": "no_gravity_signal"}
        dispersion = float(
            np.quantile(np.linalg.norm(raw[:, :3] - median[:3], axis=1), 0.95) / norm
        )
        result.update(median_counts=median.tolist(), relative_scatter=dispersion)
        if dispersion > 0.03:
            return {**result, "reason": "moving"}
        axis = int(np.argmax(np.abs(median[:3])))
        dominance = float(abs(median[axis]) / norm)
        result["dominance"] = dominance
        if dominance < 0.95:
            return {**result, "reason": "tilted"}
        result.update(direction=("+" if median[axis] > 0 else "-") + "XYZ"[axis], reason="ready")
        return result


def face_conflicts(faces: dict) -> list[str]:
    conflicts = []
    for index, axis in enumerate("XYZ"):
        positive, negative = faces.get("+" + axis), faces.get("-" + axis)
        if not positive or not negative:
            continue
        delta = (
            np.array(positive["sample"]["median_counts"][:3])
            - negative["sample"]["median_counts"][:3]
        )
        norm = float(np.linalg.norm(delta))
        if norm < 1 or delta[index] / norm < 0.95:
            conflicts.append(axis)
    return conflicts


class OrientationStart(PreviewStartRequest):
    sensor_sn: str = Field(pattern=r"^IMU-[0-9]{4}-R[0-9]{2}$")
    owner_id: str = Field(pattern=r"^[a-zA-Z0-9-]{16,80}$")


class SessionRequest(BaseModel):
    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class FaceRequest(SessionRequest):
    direction: Direction
    description: str = Field(min_length=1, max_length=200)
    replace: bool = False


class OrientationController:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.lock = asyncio.Lock()
        self.session: dict | None = None
        self.deadline = 0.0
        self.watchdog: asyncio.Task | None = None

    def require(self, session_id: str) -> dict:
        if not self.session or self.session["session_id"] != session_id:
            raise HTTPException(
                409, "方向预览已结束，请重新连接 / Orientation session ended; reconnect"
            )
        if time.monotonic() >= self.deadline:
            raise HTTPException(
                409, "方向预览已超时，请重新连接 / Orientation session expired; reconnect"
            )
        return self.session

    def status(self, session_id: str) -> dict:
        session = self.require(session_id)
        ble = self.coordinator.ble
        connected = bool(
            self.coordinator.mode == "imu_orientation_preview" and ble and ble.connected
        )
        if not connected:
            self.coordinator.orientation_window.clear()
        sample = (
            self.coordinator.orientation_window.evaluate()
            if connected
            else {
                "direction": None,
                "reason": "disconnected",
                "sample_count": 0,
            }
        )
        conflicts = face_conflicts(session["faces"])
        return {
            **copy.deepcopy(session),
            "connected": connected,
            "sample": sample,
            "conflicts": conflicts,
            "ready": len(session["faces"]) == 6 and not conflicts,
        }

    async def start(self, request: OrientationStart) -> dict:
        async with self.lock:
            if self.session and time.monotonic() >= self.deadline:
                await self._stop()
            if self.session:
                if self.session["owner_id"] != request.owner_id:
                    raise HTTPException(
                        409,
                        "另一页面正在认轴，请先关闭 / Another page owns orientation preview",
                    )
                if self.session["sensor_sn"] != request.sensor_sn:
                    raise HTTPException(
                        409, "请先断开当前设备 / Disconnect the current device first"
                    )
                if self.coordinator.ble and self.coordinator.ble.connected:
                    self.deadline = time.monotonic() + LEASE_SECONDS
                    return self.status(self.session["session_id"])
                await self.coordinator.stop_orientation_preview()
            await self.coordinator.start_orientation_preview(request)
            if self.session is None:
                self.session = {
                    "session_id": uuid.uuid4().hex,
                    "owner_id": request.owner_id,
                    "sensor_sn": request.sensor_sn,
                    "faces": {},
                }
            self.deadline = time.monotonic() + LEASE_SECONDS
            if self.watchdog is None or self.watchdog.done():
                self.watchdog = asyncio.create_task(self._watch())
            return self.status(self.session["session_id"])

    async def _watch(self):
        while self.session:
            await asyncio.sleep(1)
            async with self.lock:
                if self.session and time.monotonic() >= self.deadline:
                    await self._stop()

    async def _stop(self):
        if self.coordinator.mode == "imu_orientation_preview":
            await self.coordinator.stop_orientation_preview()
        self.session = None

    async def stop(self, session_id: str):
        async with self.lock:
            # A delayed old-page cleanup must never close a new session.
            if self.session and self.session["session_id"] == session_id:
                await self._stop()

    async def record(self, request: FaceRequest):
        async with self.lock:
            session = self.require(request.session_id)
            status = self.status(request.session_id)
            sample = status["sample"]
            if sample["direction"] != request.direction or sample["reason"] != "ready":
                raise HTTPException(
                    409, "方向已变化或尚未稳定，请重新摆稳 / Direction changed or is not stable"
                )
            description = request.description.strip()
            if not description:
                raise HTTPException(422, "请填写外壳特征 / Describe the housing face")
            if request.direction in session["faces"] and not request.replace:
                raise HTTPException(
                    409, "该方向已记录，请确认替换 / Confirm replacement of this face"
                )
            session["faces"][request.direction] = {"description": description, "sample": sample}
            return self.status(request.session_id)

    async def finish(self, session_id: str, sensor_sn: str, directions: dict) -> dict:
        async with self.lock:
            status = self.status(session_id)
            if not status["ready"] or status["sensor_sn"] != sensor_sn:
                raise HTTPException(
                    409, "请完成同一设备的六面确认 / Confirm all six faces of this device"
                )
            recorded = {key: value["description"] for key, value in status["faces"].items()}
            if recorded != directions:
                raise HTTPException(
                    409, "外壳描述已变化，请刷新 / Face descriptions changed; refresh"
                )
            setup = {"axis_definition": "raw_accelerometer", "faces": status["faces"]}
            await self._stop()
            return setup

    async def close(self):
        async with self.lock:
            await self._stop()
        if self.watchdog:
            self.watchdog.cancel()
            await asyncio.gather(self.watchdog, return_exceptions=True)


def register_orientation_api(app: FastAPI, coordinator) -> OrientationController:
    controller = OrientationController(coordinator)
    base = "/api/v1/calibration-orientation"
    app.state.orientation = controller

    @app.post(base + "/start")
    async def start(request: OrientationStart):
        try:
            return await controller.start(request)
        except (ValueError, RuntimeError) as error:
            raise HTTPException(409, str(error)) from error

    @app.get(base)
    async def status(session_id: str):
        return controller.status(session_id)

    @app.post(base + "/heartbeat")
    async def heartbeat(request: SessionRequest):
        async with controller.lock:
            controller.require(request.session_id)
            controller.deadline = time.monotonic() + LEASE_SECONDS
            return {"ok": True}

    @app.post(base + "/stop")
    async def stop(request: SessionRequest):
        await controller.stop(request.session_id)
        return {"stopped": True}

    @app.post(base + "/faces")
    async def record(request: FaceRequest):
        return await controller.record(request)

    @app.delete(base + "/faces/{direction}")
    async def remove(direction: Direction, session_id: str):
        async with controller.lock:
            session = controller.require(session_id)
            session["faces"].pop(direction, None)
            return controller.status(session_id)

    @app.put(base + "/faces")
    async def rename(request: FaceRequest):
        async with controller.lock:
            session = controller.require(request.session_id)
            if request.direction not in session["faces"] or not request.description.strip():
                raise HTTPException(
                    422, "请先记录该面并填写描述 / Record this face and provide a description"
                )
            session["faces"][request.direction]["description"] = request.description.strip()
            return controller.status(request.session_id)

    return controller
