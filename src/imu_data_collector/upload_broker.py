"""为桌面采集端签发录制范围内的 GCS resumable upload 会话。

代理是唯一持有 GCS 服务账号权限的组件。桌面端只提交经过 Google 签名的短期
ID token；代理校验邮箱白名单、对象键、大小与 SHA-256，最后才原子写 manifest。
"""

import argparse
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import storage
from google.oauth2 import id_token as google_id_token

from imu_data_collector.broker_models import (
    BrokerArtifactSession,
    BrokerUploadCompleteRequest,
    BrokerUploadCompleteResponse,
    BrokerUploadStartRequest,
    BrokerUploadStartResponse,
)
from imu_data_collector.config import Settings, load_settings
from imu_data_collector.constants import ANNOTATION_ACCEPTED_CAPTURE_SCHEMA_VERSIONS
from imu_data_collector.models import CaptureManifestV2


def _expected_key(recording_id: str, role: str) -> str:
    suffixes = {
        "capture_h5": "capture.h5",
        "video_mkv": "video.mkv",
        "preview_mp4": "preview.mp4",
    }
    return f"captures/{recording_id}/{suffixes[role]}"


def _verify_manifest_keys(manifest: CaptureManifestV2) -> None:
    for artifact in manifest.artifacts:
        if artifact.object_key != _expected_key(manifest.recording_id, artifact.role):
            raise HTTPException(status_code=422, detail="manifest 对象键不是代理允许的稳定键")


def _sha256_blob(blob: storage.Blob) -> str:
    digest = hashlib.sha256()
    with blob.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def create_upload_broker_app(settings: Settings | None = None) -> FastAPI:
    active = settings or load_settings()
    if active.storage.backend != "gcs" or not active.storage.bucket:
        raise RuntimeError("上传代理必须配置 storage.backend=gcs 和 storage.bucket")
    if not active.cloud.google_oauth_client_id:
        raise RuntimeError("上传代理缺少 cloud.google_oauth_client_id")
    client = storage.Client(project=active.storage.project)
    bucket = client.bucket(active.storage.bucket.removeprefix("gs://"))
    app = FastAPI(title="IMU 上传代理", version="1.0.0")

    def actor(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, str]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="缺少 Google ID token")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            claims = google_id_token.verify_oauth2_token(
                token,
                GoogleAuthRequest(),
                active.cloud.google_oauth_client_id,
            )
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=401, detail="Google ID token 无效") from error
        email = str(claims.get("email") or "").strip().lower()
        if not claims.get("email_verified") or email not in active.identity.email_to_unikey:
            raise HTTPException(status_code=403, detail="Google 账号不在团队上传白名单")
        return {"email": email, "unikey": active.identity.email_to_unikey[email]}

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "application": "upload-broker"}

    @app.get("/v1/capabilities")
    def capabilities() -> dict[str, Any]:
        return {
            "accepted_manifest_schema_versions": ["2.1.0"],
            "accepted_capture_h5_schema_versions": list(
                ANNOTATION_ACCEPTED_CAPTURE_SCHEMA_VERSIONS
            ),
            "direct_to_bucket_resumable": True,
            "server_verifies_sha256_before_manifest": True,
        }

    @app.post("/v1/uploads", response_model=BrokerUploadStartResponse)
    def start_upload(
        body: BrokerUploadStartRequest,
        current: Annotated[dict[str, str], Depends(actor)],
    ) -> BrokerUploadStartResponse:
        manifest = body.manifest
        _verify_manifest_keys(manifest)
        if (
            manifest.source_h5_schema_version
            not in ANNOTATION_ACCEPTED_CAPTURE_SCHEMA_VERSIONS
        ):
            raise HTTPException(status_code=422, detail="标注端不接受该 capture H5 schema")
        manifest_blob = bucket.blob(f"captures/{manifest.recording_id}/manifest.json")
        if manifest_blob.exists(client):
            existing = CaptureManifestV2.model_validate(
                json.loads(manifest_blob.download_as_bytes(client=client))
            )
            if existing != manifest:
                raise HTTPException(status_code=409, detail="该录制的 manifest 内容冲突")

        sessions: list[BrokerArtifactSession] = []
        for artifact in manifest.artifacts:
            blob = bucket.blob(artifact.object_key)
            if blob.exists(client):
                blob.reload(client)
                metadata = dict(blob.metadata or {})
                if (
                    int(blob.size or -1) != artifact.size_bytes
                    or metadata.get("sha256") != artifact.sha256
                    or metadata.get("role") != artifact.role
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=f"对象已存在但身份不一致：{artifact.object_key}",
                    )
                sessions.append(
                    BrokerArtifactSession(
                        role=artifact.role,
                        object_key=artifact.object_key,
                        already_present=True,
                    )
                )
                continue
            blob.content_type = artifact.content_type
            blob.metadata = {
                "recording_id": manifest.recording_id,
                "role": artifact.role,
                "sha256": artifact.sha256,
                "uploader": current["unikey"],
            }
            session_url = blob.create_resumable_upload_session(
                content_type=artifact.content_type,
                size=artifact.size_bytes,
                if_generation_match=0,
                checksum="auto",
            )
            sessions.append(
                BrokerArtifactSession(
                    role=artifact.role,
                    object_key=artifact.object_key,
                    session_url=session_url,
                )
            )

        upload_id = uuid.uuid4().hex
        plan = {
            "schema_version": "1.0.0",
            "upload_id": upload_id,
            "actor": current,
            "expires_at_utc": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
            "manifest": manifest.model_dump(mode="json"),
        }
        bucket.blob(f"_upload_sessions/{upload_id}.json").upload_from_string(
            json.dumps(plan, ensure_ascii=False),
            content_type="application/json",
            if_generation_match=0,
        )
        return BrokerUploadStartResponse(upload_id=upload_id, sessions=sessions)

    @app.get("/v1/recordings/{recording_id}/receipt")
    def read_receipt(
        recording_id: str,
        _current: Annotated[dict[str, str], Depends(actor)],
    ) -> dict[str, Any]:
        blob = bucket.blob(f"index-receipts/{recording_id}.json")
        if not blob.exists(client):
            raise HTTPException(status_code=404, detail="标注端尚未写入索引回执")
        value = json.loads(blob.download_as_bytes(client=client))
        if not isinstance(value, dict):
            raise HTTPException(status_code=500, detail="索引回执不是 JSON 对象")
        return value

    @app.post(
        "/v1/uploads/complete",
        response_model=BrokerUploadCompleteResponse,
    )
    def complete_upload(
        body: BrokerUploadCompleteRequest,
        current: Annotated[dict[str, str], Depends(actor)],
    ) -> BrokerUploadCompleteResponse:
        plan_blob = bucket.blob(f"_upload_sessions/{body.upload_id}.json")
        if not plan_blob.exists(client):
            raise HTTPException(status_code=404, detail="上传会话不存在或已经失效")
        plan = json.loads(plan_blob.download_as_bytes(client=client))
        if plan.get("actor") != current:
            raise HTTPException(status_code=403, detail="上传会话不属于当前用户")
        if datetime.fromisoformat(plan["expires_at_utc"]) < datetime.now(UTC):
            raise HTTPException(status_code=410, detail="上传会话已经过期")
        planned = CaptureManifestV2.model_validate(plan["manifest"])
        if planned != body.manifest:
            raise HTTPException(status_code=409, detail="完成请求与上传计划不一致")

        for artifact in planned.artifacts:
            blob = bucket.blob(artifact.object_key)
            if not blob.exists(client):
                raise HTTPException(status_code=409, detail=f"制品尚未上传：{artifact.role}")
            blob.reload(client)
            if int(blob.size or -1) != artifact.size_bytes:
                raise HTTPException(status_code=409, detail=f"制品大小不一致：{artifact.role}")
            if _sha256_blob(blob) != artifact.sha256:
                raise HTTPException(status_code=409, detail=f"制品 SHA-256 不一致：{artifact.role}")

        manifest_key = f"captures/{planned.recording_id}/manifest.json"
        manifest_blob = bucket.blob(manifest_key)
        payload = json.dumps(
            planned.model_dump(mode="json"), ensure_ascii=False, indent=2
        ).encode()
        if manifest_blob.exists(client):
            existing = CaptureManifestV2.model_validate(
                json.loads(manifest_blob.download_as_bytes(client=client))
            )
            if existing != planned:
                raise HTTPException(status_code=409, detail="远端 manifest 内容冲突")
            manifest_blob.reload(client)
        else:
            manifest_blob.metadata = {
                "recording_id": planned.recording_id,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            manifest_blob.upload_from_string(
                payload,
                content_type="application/json",
                if_generation_match=0,
            )
            manifest_blob.reload(client)
        return BrokerUploadCompleteResponse(
            recording_id=planned.recording_id,
            manifest_generation=int(manifest_blob.generation or 0),
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="imu-upload-broker")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    settings = load_settings(args.config)
    uvicorn.run(
        create_upload_broker_app(settings),
        host=settings.cloud.broker_server_host,
        port=settings.cloud.broker_server_port,
    )
