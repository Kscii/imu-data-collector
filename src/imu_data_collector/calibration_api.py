"""Desktop calibration workflow; one BLE owner, server-side phase timing."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

from imu_data_collector.calibration_experiments import (
    ExperimentCreate,
    ExperimentStore,
    TrialCreate,
    TrialExclusion,
    utc_now,
)
from imu_data_collector.calibration_orientation import register_orientation_api
from imu_data_collector.device_configuration import ConfigurationSnapshotSubmission, si_profile_id
from imu_data_collector.models import CharacterizationStageRequest, CharacterizationStartRequest


class CalibrationController:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store = ExperimentStore(coordinator.settings.data_root)
        self.active_id: str | None = None
        self.task: asyncio.Task | None = None
        self.phase: str | None = None
        self.deadline: float | None = None
        self.rotation_done = asyncio.Event()
        self.lock = asyncio.Lock()
        self.uploads: dict[str, dict] = {}
        self.upload_tasks: dict[str, asyncio.Task] = {}
        # A process restart never turns an unfinished operation into a complete trial.
        for experiment in self.store.list():
            changed = False
            for trial in experiment["trials"]:
                if trial["status"] == "running":
                    trial.update(status="interrupted", error="Application stopped during trial")
                    changed = True
            if changed:
                self.store.save(experiment)

    def detail(self, experiment_id: str) -> dict:
        value = self.store.read(experiment_id)
        return {
            **value,
            "active": self.active_id == experiment_id,
            "phase": self.phase if self.active_id == experiment_id else None,
            "remaining_seconds": max(0, self.deadline - time.monotonic())
            if self.active_id == experiment_id and self.deadline
            else None,
            "upload": self.uploads.get(experiment_id),
        }

    async def start(self, experiment_id: str) -> dict:
        async with self.lock:
            if self.active_id is not None:
                raise ValueError("Finish the current calibration capture first")
            experiment = self.store.read(experiment_id)
            result = await self.coordinator.start_characterization(
                CharacterizationStartRequest(
                    operator_id=experiment["operator_id"],
                    sensor_sn=experiment["sensor_sn"],
                    notes=experiment["notes"],
                )
            )
            self.active_id = experiment_id
            self.coordinator.writer.handle.attrs["calibration_experiment_id"] = experiment_id
            experiment["sources"].append(
                {
                    "recording_id": result["recording_id"],
                    "local_path": str(
                        Path(result["h5_path"]).resolve().relative_to(self.store.data_root)
                    ),
                }
            )
            self.store.save(experiment)
            return self.detail(experiment_id)

    def require_active(self, experiment_id: str) -> None:
        if self.active_id != experiment_id:
            raise ValueError("Start this experiment's capture first")

    async def start_trial(self, experiment_id: str, request: TrialCreate) -> dict:
        async with self.lock:
            self.require_active(experiment_id)
            if self.task and not self.task.done():
                raise ValueError("A trial is already running")
            experiment = self.store.read(experiment_id)
            trial = {
                **request.model_dump(mode="json"),
                "trial_id": uuid.uuid4().hex,
                "reference_angle_deg": request.reference_angle_deg,
                "status": "running",
                "excluded": False,
                "phases": {},
                "created_at_utc": utc_now(),
                "recording_id": self.coordinator.current.recording_id,
            }
            experiment["trials"].append(trial)
            self.store.save(experiment)
            self.rotation_done.clear()
            self.task = asyncio.create_task(self._run_trial(experiment_id, trial))
            return self.detail(experiment_id)

    async def _phase(self, trial: dict, name: str, seconds: float | None) -> None:
        snapshot = await self.coordinator.start_characterization_stage(
            CharacterizationStageRequest(
                stage_code="calibration",
                notes=json.dumps({"trial_id": trial["trial_id"], "phase": name}),
            )
        )
        start_ns = snapshot["current_stage"]["start_ns"]
        self.phase = name
        self.deadline = time.monotonic() + seconds if seconds is not None else None
        try:
            if seconds is None:
                await self.rotation_done.wait()
            else:
                await self._wait_seconds(seconds)
        finally:
            writer = self.coordinator.writer
            if writer is None or self.coordinator.mode != "characterization":
                raise RuntimeError("Capture stopped before the phase finished")
            end_ns = (
                time.monotonic_ns() - writer.recording_start_monotonic_ns if writer else start_ns
            )
            if end_ns <= start_ns:
                # Python 3.12 on Windows can report the same monotonic tick for
                # a stage started and cancelled immediately. Retain the trial,
                # but do not leave an unwritable zero-length H5 stage active.
                self.coordinator.current_stage = None
                trial["phases"][name] = {"start_ns": start_ns, "end_ns": end_ns}
                raise RuntimeError("Clock did not advance; trial interrupted")
            if self.coordinator.current_stage is not None:
                await self.coordinator.stop_characterization_stage()
            trial["phases"][name] = {"start_ns": start_ns, "end_ns": end_ns}

    async def _wait_seconds(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _run_trial(self, experiment_id: str, trial: dict) -> None:
        try:
            if trial["kind"] == "accel":
                await self._phase(trial, "settle", 5)
                await self._phase(trial, "measure", 10)
            else:
                await self._phase(trial, "before", 5)
                await self._phase(trial, "measure", None)
                await self._phase(trial, "after", 5)
            trial["status"] = "complete"
        except asyncio.CancelledError:
            trial["status"] = "interrupted"
        except Exception as error:
            trial.update(status="interrupted", error=str(error))
        finally:
            trial["ended_at_utc"] = utc_now()
            experiment = self.store.read(experiment_id)
            experiment["trials"] = [
                trial if item["trial_id"] == trial["trial_id"] else item
                for item in experiment["trials"]
            ]
            self.store.save(experiment)
            self.phase = self.deadline = None

    async def cancel_trial(self, experiment_id: str) -> dict:
        self.require_active(experiment_id)
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        experiment = self.store.read(experiment_id)
        for trial in experiment["trials"]:
            if trial["status"] == "running":
                trial.update(status="interrupted", ended_at_utc=utc_now())
        self.store.save(experiment)
        self.phase = self.deadline = None
        return self.detail(experiment_id)

    async def stop(self, experiment_id: str) -> dict:
        async with self.lock:
            await self.cancel_trial(experiment_id)
            experiment = self.store.read(experiment_id)
            if (
                self.coordinator.mode == "characterization"
                and self.coordinator.state.value == "recording"
            ):
                result = await self.coordinator.stop_characterization()
                source = next(
                    item
                    for item in experiment["sources"]
                    if item["recording_id"] == result["recording_id"]
                )
                source["local_path"] = str(
                    Path(result["h5_path"]).resolve().relative_to(self.store.data_root)
                )
            else:
                # A BLE failure may already have closed the coordinator's writer.
                # Preserve the interrupted source and allow a new capture segment.
                experiment["sources"][-1]["interrupted"] = True
            self.store.save(experiment)
            self.active_id = None
            return self.detail(experiment_id)

    async def close(self) -> None:
        if hasattr(self, "orientation"):
            await self.orientation.close()
        if self.active_id:
            await self.stop(self.active_id)
        for task in self.upload_tasks.values():
            task.cancel()
        await asyncio.gather(*self.upload_tasks.values(), return_exceptions=True)


def register_calibration_api(app: FastAPI, coordinator, configuration_manager, candidate_store):
    controller = CalibrationController(coordinator)
    store = controller.store
    app.state.calibration = controller
    controller.orientation = register_orientation_api(app, coordinator)

    def read(experiment_id: str) -> dict:
        try:
            return controller.detail(experiment_id)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(404, str(error)) from error

    def idle(experiment_id: str) -> None:
        read(experiment_id)
        if controller.active_id == experiment_id:
            raise HTTPException(409, "Finish the capture to freeze, export or upload a report")

    @app.get("/api/v1/calibration-experiments")
    async def experiments():
        return [controller.detail(item["experiment_id"]) for item in store.list()]

    @app.post("/api/v1/calibration-experiments", status_code=201)
    async def create(request: ExperimentCreate):
        coordinator._require_allowed_unikey(request.operator_id, "operator_id")
        snapshot = configuration_manager.selected_snapshot()
        device = next(
            (item for item in snapshot.content.devices if item.sensor_sn == request.sensor_sn), None
        )
        if device is None:
            raise HTTPException(422, "Select a registered device")
        setup = None
        if request.orientation_session_id:
            setup = await controller.orientation.finish(
                request.orientation_session_id, request.sensor_sn, request.directions
            )
        experiment = store.create(request, device.model_dump(mode="json"))
        if setup:
            experiment["orientation_setup"] = setup
            store.save(experiment)
        return experiment

    @app.get("/api/v1/calibration-experiments/{experiment_id}")
    async def detail(experiment_id: str):
        return read(experiment_id)

    @app.post("/api/v1/calibration-experiments/{experiment_id}/start")
    async def start(experiment_id: str):
        read(experiment_id)
        try:
            return await controller.start(experiment_id)
        except (ValueError, RuntimeError) as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/v1/calibration-experiments/{experiment_id}/stop")
    async def stop(experiment_id: str):
        read(experiment_id)
        try:
            return await controller.stop(experiment_id)
        except (ValueError, RuntimeError) as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/v1/calibration-experiments/{experiment_id}/trials")
    async def trial(experiment_id: str, request: TrialCreate):
        read(experiment_id)
        try:
            return await controller.start_trial(experiment_id, request)
        except (ValueError, RuntimeError) as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/v1/calibration-experiments/{experiment_id}/rotation-finished")
    async def rotation_finished(experiment_id: str):
        if (
            controller.active_id != experiment_id
            or controller.phase != "measure"
            or controller.deadline is not None
        ):
            raise HTTPException(409, "No rotation is currently running")
        controller.rotation_done.set()
        return read(experiment_id)

    @app.post("/api/v1/calibration-experiments/{experiment_id}/cancel-trial")
    async def cancel_trial(experiment_id: str):
        if controller.active_id != experiment_id:
            raise HTTPException(409, "Experiment is not active")
        return await controller.cancel_trial(experiment_id)

    @app.put("/api/v1/calibration-experiments/{experiment_id}/trials/{trial_id}/exclusion")
    async def exclusion(experiment_id: str, trial_id: str, request: TrialExclusion):
        idle(experiment_id)
        experiment = store.read(experiment_id)
        trial = next((item for item in experiment["trials"] if item["trial_id"] == trial_id), None)
        if trial is None:
            raise HTTPException(404, "Trial not found")
        trial.update(excluded=request.excluded, exclusion_reason=request.reason)
        store.save(experiment)
        return read(experiment_id)

    @app.get("/api/v1/calibration-experiments/{experiment_id}/preview")
    async def preview(experiment_id: str):
        experiment = read(experiment_id)
        if controller.active_id == experiment_id and coordinator.writer:
            coordinator.writer.handle.flush()
        return store.analyze(experiment)

    @app.post("/api/v1/calibration-experiments/{experiment_id}/report")
    async def report(experiment_id: str):
        idle(experiment_id)
        try:
            result, path = store.snapshot(experiment_id)
        except (ValueError, OSError) as error:
            raise HTTPException(409, str(error)) from error
        return {"report": result, "sha256": path.stem.removeprefix("report-")}

    @app.get("/api/v1/calibration-experiments/{experiment_id}/reports/{digest}")
    async def download_report(experiment_id: str, digest: str):
        experiment = read(experiment_id)
        if not any(item["sha256"] == digest for item in experiment["reports"]):
            raise HTTPException(404, "Report not found")
        return FileResponse(
            store.directory(experiment_id) / f"report-{digest}.json",
            filename=f"{experiment_id}-{digest[:12]}.json",
        )

    def candidate_profile(experiment_id: str):
        idle(experiment_id)
        report, path = store.snapshot(experiment_id)
        digest = path.stem.removeprefix("report-")
        profile = {
            **report["candidate"],
            "evidence_sha256": digest,
            "evidence": [
                {
                    "recording_id": experiment_id,
                    "kind": "calibration_experiment",
                    "summary_zh": f"实验报告 SHA-256: {digest}",
                    "summary_en": f"Experiment report SHA-256: {digest}",
                }
            ],
        }
        profile["profile_id"] = si_profile_id(report["sensor_sn"], profile)
        return report, profile

    @app.get("/api/v1/calibration-experiments/{experiment_id}/candidate.yaml")
    async def export(experiment_id: str):
        report, profile = candidate_profile(experiment_id)
        payload = {
            "sensor_sn": report["sensor_sn"],
            "allowed_data_tiers": ["test"],
            "si_profile": profile,
        }
        return Response(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            media_type="application/yaml",
            headers={
                "Content-Disposition": f'attachment; filename="{experiment_id}.candidate.yaml"'
            },
        )

    @app.post("/api/v1/calibration-experiments/{experiment_id}/copy-to-workspace")
    async def copy(experiment_id: str):
        report, profile = candidate_profile(experiment_id)
        workspace = configuration_manager.workspace().model_dump(mode="json")
        device = next(
            (
                item
                for item in workspace["content"]["devices"]
                if item["sensor_sn"] == report["sensor_sn"]
            ),
            None,
        )
        if device is None:
            raise HTTPException(409, "Device is absent from the local configuration workspace")
        device.update(si_profile=profile, allowed_data_tiers=["test"])
        configuration_manager.save_workspace(
            ConfigurationSnapshotSubmission.model_validate(workspace)
        )
        candidate_store.save(
            report["sensor_sn"],
            {
                **{
                    key: profile[key]
                    for key in (
                        "accel_counts_per_g",
                        "gyro_counts_per_dps",
                        "accel_bias_counts",
                        "gyro_bias_counts",
                        "raw_axis_order",
                        "axis_signs",
                    )
                },
                "evidence_status": f"unverified:calibration:{profile['evidence_sha256']}",
            },
        )
        return {"saved": True, "verified": False, "production_authority": False}

    @app.post("/api/v1/calibration-experiments/{experiment_id}/upload", status_code=202)
    async def upload(experiment_id: str):
        from imu_data_collector.calibration_cloud import upload_experiment

        idle(experiment_id)
        if (
            experiment_id in controller.upload_tasks
            and not controller.upload_tasks[experiment_id].done()
        ):
            return controller.uploads[experiment_id]
        report, path = store.snapshot(experiment_id)
        experiment = store.read(experiment_id)
        status = {"state": "uploading", "completed_bytes": 0, "total_bytes": 0}
        controller.uploads[experiment_id] = status

        async def run():
            try:
                result = await upload_experiment(
                    coordinator.settings,
                    coordinator.cloud_auth,
                    store,
                    experiment,
                    report,
                    path,
                    status,
                )
                status.update(state="complete", **result)
            except Exception as error:
                status.update(state="failed", error=str(error))

        controller.upload_tasks[experiment_id] = asyncio.create_task(run())
        return status

    return controller
