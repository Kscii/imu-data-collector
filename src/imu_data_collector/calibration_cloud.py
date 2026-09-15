"""Small immutable experiment catalog using the existing broker and object store."""

import asyncio
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException
from google.api_core.exceptions import PreconditionFailed
from pydantic import BaseModel, ConfigDict, Field, model_validator

from imu_data_collector.broker_client import _broker_post, _put_resumable
from imu_data_collector.calibration_experiments import ID_PATTERN, SCHEMA, json_bytes
from imu_data_collector.http_download import object_download_response

PREFIX = "calibration-experiments/v1/"
SHA = r"^[0-9a-f]{64}$"


class CalibrationArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["raw_h5", "report"]
    sha256: str = Field(pattern=SHA)
    size_bytes: int = Field(gt=0)

    @property
    def file_id(self) -> str:
        return "report.json" if self.role == "report" else f"raw-{self.sha256}.h5"


class CalibrationUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment_id: str = Field(pattern=ID_PATTERN)
    sensor_sn: str = Field(pattern=r"^IMU-[0-9]{4}-R[0-9]{2}$")
    report_sha256: str = Field(pattern=SHA)
    artifacts: list[CalibrationArtifact] = Field(min_length=1)

    @model_validator(mode="after")
    def check_report(self):
        reports = [item for item in self.artifacts if item.role == "report"]
        if len(reports) != 1 or reports[0].sha256 != self.report_sha256:
            raise ValueError("Exactly one matching report is required")
        if reports[0].size_bytes > 32 * 1024 * 1024:
            raise ValueError("Experiment report exceeds 32 MiB")
        if len({item.file_id for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("Duplicate artifact IDs")
        return self

    @property
    def prefix(self) -> str:
        return f"{PREFIX}{self.sensor_sn}/{self.experiment_id}/{self.report_sha256}"

    def files(self) -> list[dict]:
        return [
            {
                **item.model_dump(),
                "file_id": item.file_id,
                "object_key": f"{self.prefix}/{item.file_id}",
                "content_type": "application/json"
                if item.role == "report"
                else "application/x-hdf5",
            }
            for item in self.artifacts
        ]


class CalibrationComplete(BaseModel):
    upload_id: str = Field(pattern=r"^[0-9a-f]{32}$")


def check_uploaded_report(report: dict, upload: CalibrationUpload) -> None:
    if (
        report.get("schema_version") != SCHEMA
        or report.get("experiment_id") != upload.experiment_id
        or report.get("sensor_sn") != upload.sensor_sn
        or report.get("training_eligible") is not False
        or report.get("data_tier") != "test"
        or report.get("candidate", {}).get("verified") is not False
    ):
        raise ValueError("Report identity or calibration-only status does not match the upload")
    referenced = {(item["sha256"], item["size_bytes"]) for item in report["sources"]}
    uploaded = {
        (item.sha256, item.size_bytes) for item in upload.artifacts if item.role == "raw_h5"
    }
    if referenced != uploaded:
        raise ValueError("Report raw sources do not match uploaded evidence")


def register_calibration_broker(app: FastAPI, bucket, client, actor, sha256_blob) -> None:
    @app.post("/v1/calibration-uploads")
    def start(body: CalibrationUpload, current: Annotated[dict[str, str], Depends(actor)]):
        files = body.files()
        sessions = []
        for artifact in files:
            blob = bucket.blob(artifact["object_key"])
            session = {
                "file_id": artifact["file_id"],
                "already_present": False,
                "session_url": None,
            }
            if blob.exists(client):
                blob.reload(client)
                if (
                    int(blob.size or -1) != artifact["size_bytes"]
                    or (blob.metadata or {}).get("sha256") != artifact["sha256"]
                ):
                    raise HTTPException(409, "Existing evidence conflicts with this upload")
                session["already_present"] = True
            else:
                blob.metadata = {
                    "sha256": artifact["sha256"],
                    "uploader": current["unikey"],
                    "experiment_id": body.experiment_id,
                }
                session["session_url"] = blob.create_resumable_upload_session(
                    content_type=artifact["content_type"],
                    size=artifact["size_bytes"],
                    if_generation_match=0,
                    checksum="auto",
                )
            sessions.append(session)
        upload_id = uuid.uuid4().hex
        plan = {
            "actor": current,
            "upload": body.model_dump(mode="json"),
            "expires_at_utc": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
        }
        bucket.blob(f"_upload_sessions/calibration/{upload_id}.json").upload_from_string(
            json_bytes(plan),
            content_type="application/json",
            if_generation_match=0,
        )
        return {"upload_id": upload_id, "sessions": sessions}

    @app.post("/v1/calibration-uploads/complete")
    def complete(body: CalibrationComplete, current: Annotated[dict[str, str], Depends(actor)]):
        blob = bucket.blob(f"_upload_sessions/calibration/{body.upload_id}.json")
        if not blob.exists(client):
            raise HTTPException(404, "Upload session not found")
        plan = json.loads(blob.download_as_bytes(client=client))
        if plan["actor"] != current:
            raise HTTPException(403, "Upload belongs to another user")
        if datetime.fromisoformat(plan["expires_at_utc"]) < datetime.now(UTC):
            raise HTTPException(410, "Upload session expired; retry the upload")
        upload = CalibrationUpload.model_validate(plan["upload"])
        files = upload.files()
        for artifact in files:
            source = bucket.blob(artifact["object_key"])
            if not source.exists(client):
                raise HTTPException(409, "Evidence file has not finished uploading")
            source.reload(client)
            if (
                int(source.size or -1) != artifact["size_bytes"]
                or sha256_blob(source) != artifact["sha256"]
            ):
                raise HTTPException(409, "Evidence size or SHA-256 mismatch")
        report_blob = bucket.blob(f"{upload.prefix}/report.json")
        try:
            report = json.loads(report_blob.download_as_bytes(client=client))
            check_uploaded_report(report, upload)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise HTTPException(422, str(error)) from error
        manifest = {
            "schema_version": SCHEMA,
            "experiment_id": upload.experiment_id,
            "sensor_sn": upload.sensor_sn,
            "report_sha256": upload.report_sha256,
            "operator_id": report["operator_id"],
            "uploader": current["unikey"],
            "created_at_utc": report["created_at_utc"],
            "uploaded_at_utc": datetime.now(UTC).isoformat(),
            "files": files,
            "training_eligible": False,
            "data_tier": "test",
        }
        marker = bucket.blob(f"{upload.prefix}/manifest.json")
        if not marker.exists(client):
            try:
                marker.upload_from_string(
                    json_bytes(manifest), content_type="application/json", if_generation_match=0
                )
            except PreconditionFailed:
                pass
        existing = json.loads(marker.download_as_bytes(client=client))
        if existing["files"] != files or existing["report_sha256"] != upload.report_sha256:
            raise HTTPException(409, "Immutable experiment manifest conflicts")
        return {
            "experiment_id": upload.experiment_id,
            "report_sha256": upload.report_sha256,
            "verified_sha256": True,
            "manifest_object": f"{upload.prefix}/manifest.json",
        }


async def upload_experiment(
    settings, auth, store, experiment, report, report_path: Path, status: dict
) -> dict:
    if not settings.cloud.broker_url:
        raise ValueError("Cloud upload is not configured")
    paths = {"report.json": report_path}
    artifacts = [
        {
            "role": "report",
            "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "size_bytes": report_path.stat().st_size,
        }
    ]
    for source in experiment["sources"]:
        file_id = f"raw-{source['sha256']}.h5"
        if file_id not in paths:
            paths[file_id] = store.source_path(source)
            artifacts.append(
                {"role": "raw_h5", "sha256": source["sha256"], "size_bytes": source["size_bytes"]}
            )
    request = CalibrationUpload(
        experiment_id=experiment["experiment_id"],
        sensor_sn=experiment["sensor_sn"],
        report_sha256=artifacts[0]["sha256"],
        artifacts=artifacts,
    )
    check_uploaded_report(report, request)
    token = await asyncio.to_thread(auth.id_token)
    base = settings.cloud.broker_url.rstrip("/")
    started = await asyncio.to_thread(
        _broker_post, f"{base}/v1/calibration-uploads", token, request.model_dump(mode="json")
    )
    status["total_bytes"] = sum(item.size_bytes for item in request.artifacts)
    completed = 0
    for session in started["sessions"]:
        path = paths[session["file_id"]]
        if not session["already_present"]:
            await asyncio.to_thread(
                _put_resumable,
                session["session_url"],
                path,
                progress=lambda value, _total, offset=completed: status.update(
                    completed_bytes=offset + value
                ),
            )
        completed += path.stat().st_size
        status["completed_bytes"] = completed
    token = await asyncio.to_thread(auth.id_token)
    result = await asyncio.to_thread(
        _broker_post,
        f"{base}/v1/calibration-uploads/complete",
        token,
        {"upload_id": started["upload_id"]},
        timeout=1800,
    )
    if not result.get("verified_sha256") or result.get("report_sha256") != request.report_sha256:
        raise ValueError("Broker did not confirm this experiment's evidence hash")
    return result


def register_calibration_catalog(app: FastAPI, store) -> None:
    def manifest(experiment_id: str, digest: str) -> dict:
        if not re.fullmatch(ID_PATTERN, experiment_id) or not re.fullmatch(SHA, digest):
            raise HTTPException(404, "Experiment report not found")
        # The team catalog is small; completed manifests are its only index.
        suffix = f"/{experiment_id}/{digest}/manifest.json"
        for item in store.list(PREFIX):
            if item.key.endswith(suffix):
                return store.read_json(item.key)[0]
        raise HTTPException(404, "Experiment report not found")

    @app.get("/api/v1/calibration-experiments")
    def experiments():
        return [
            store.read_json(item.key)[0]
            for item in store.list(PREFIX)
            if item.key.endswith("/manifest.json")
        ]

    @app.get("/api/v1/calibration-experiments/{experiment_id}/reports/{digest}")
    def report(experiment_id: str, digest: str):
        entry = manifest(experiment_id, digest)
        artifact = next(item for item in entry["files"] if item["role"] == "report")
        raw = store.read_bytes(artifact["object_key"])
        if hashlib.sha256(raw).hexdigest() != digest:
            raise HTTPException(409, "Stored report failed SHA-256 verification")
        return json.loads(raw)

    @app.get("/api/v1/calibration-experiments/{experiment_id}/reports/{digest}/files/{file_id}")
    def download(experiment_id: str, digest: str, file_id: str):
        entry = manifest(experiment_id, digest)
        artifact = next((item for item in entry["files"] if item["file_id"] == file_id), None)
        if artifact is None:
            raise HTTPException(404, "Experiment file not found")
        info = store.stat(artifact["object_key"])
        if info is None or info.size_bytes != artifact["size_bytes"]:
            raise HTTPException(409, "Experiment evidence is unavailable")
        return object_download_response(
            store=store,
            info=info,
            filename=file_id,
            media_type=artifact["content_type"],
            range_header=None,
            sha256=artifact["sha256"],
        )
