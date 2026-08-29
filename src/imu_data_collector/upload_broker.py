"""为桌面采集端代理 OAuth token exchange 并签发 GCS 上传会话。

代理是唯一持有 Google client secret 和 GCS 服务账号权限的组件。桌面端上传时只提交经过
Google 签名的短期 ID token；代理校验邮箱白名单、对象键、大小与 SHA-256，最后才原子写
manifest。
"""

import argparse
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import requests
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from google.api_core.exceptions import PreconditionFailed
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import storage
from google.oauth2 import id_token as google_id_token

from imu_data_collector.broker_models import (
    BrokerArtifactSession,
    BrokerOAuthTokenRequest,
    BrokerOAuthTokenResponse,
    BrokerUploadCompleteRequest,
    BrokerUploadCompleteResponse,
    BrokerUploadStartRequest,
    BrokerUploadStartResponse,
    ModelArtifactSession,
    ModelPublicationRestoreRequest,
    ModelUploadCompleteRequest,
    ModelUploadCompleteResponse,
    ModelUploadStartRequest,
    ModelUploadStartResponse,
)
from imu_data_collector.config import Settings, load_settings
from imu_data_collector.constants import ANNOTATION_ACCEPTED_CAPTURE_SCHEMA_VERSIONS
from imu_data_collector.models import CaptureManifestV2

EXPERIMENT_PUBLICATION_SCHEMA = "imu_benchmark_result_manifest_v2"
PACKAGE_PUBLICATION_SCHEMA = "imu_model_package_publication_v1"
MODEL_STATE_SCHEMA = "imu_model_publication_state_v1"
EXPERIMENT_PREFIXES = {
    "formal_cv": "benchmark-results/temporal-core",
    "engineering": "benchmark-results/engineering",
}
PACKAGE_PREFIX = "benchmark-models/packages"
MODEL_CONTENT_TYPES = {
    "application/gzip",
    "application/json",
    "application/json; charset=utf-8",
    "application/octet-stream",
    "text/csv",
    "text/markdown",
}


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


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


def _safe_object_key(value: object) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="模型制品对象键无效")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise HTTPException(status_code=422, detail="模型制品对象键无效")
    return value


def _content_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".json":
        return "application/json"
    if suffix == ".csv":
        return "text/csv"
    if suffix == ".md":
        return "text/markdown"
    return "application/octet-stream"


def _artifact(
    file_id: str,
    object_key: str,
    payload: dict[str, Any],
    *,
    content_type: str,
) -> dict[str, Any]:
    size = payload.get("size_bytes")
    digest = payload.get("sha256")
    if (
        not isinstance(size, int)
        or size <= 0
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        raise HTTPException(status_code=422, detail="模型发布制品身份无效")
    return {
        "file_id": file_id,
        "object_key": _safe_object_key(object_key),
        "size_bytes": size,
        "sha256": digest,
        "content_type": content_type,
    }


def _expected_model_publication(
    body: ModelUploadStartRequest,
) -> tuple[str, str, list[dict[str, Any]]]:
    """Derive every permitted object key from the immutable publication marker."""

    marker = body.marker
    publication_id = body.publication_id
    expected: list[dict[str, Any]] = []
    if body.publication_kind == "experiment":
        if (
            marker.get("schema_version") != EXPERIMENT_PUBLICATION_SCHEMA
            or marker.get("run_id") != publication_id
            or marker.get("evidence_level") not in EXPERIMENT_PREFIXES
        ):
            raise HTTPException(status_code=422, detail="实验发布 manifest 无效")
        prefix = f"{EXPERIMENT_PREFIXES[marker['evidence_level']]}/{publication_id}"
        marker_key = f"{prefix}/manifest.json"
        bundle = marker.get("bundle")
        if not isinstance(bundle, dict) or bundle.get("filename") != "run.tar.gz":
            raise HTTPException(status_code=422, detail="实验发布 bundle 无效")
        expected.append(
            _artifact(
                "bundle",
                f"{prefix}/run.tar.gz",
                bundle,
                content_type="application/gzip",
            )
        )
        quick_files = marker.get("quick_files")
        if not isinstance(quick_files, list):
            raise HTTPException(status_code=422, detail="实验发布 quick_files 无效")
        for item in quick_files:
            if not isinstance(item, dict):
                raise HTTPException(status_code=422, detail="实验发布 quick_files 无效")
            filename = item.get("path")
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise HTTPException(status_code=422, detail="实验快捷文件名无效")
            expected.append(
                _artifact(
                    f"quick-{filename.replace('.', '-')}",
                    f"{prefix}/files/{filename}",
                    item,
                    content_type=_content_type(filename),
                )
            )
        direct_files = marker.get("direct_files")
        if not isinstance(direct_files, list) or not direct_files:
            raise HTTPException(status_code=422, detail="实验直接下载文件无效")
        for item in direct_files:
            if not isinstance(item, dict):
                raise HTTPException(status_code=422, detail="实验直接下载文件无效")
            file_id = item.get("file_id")
            object_key = item.get("object_key")
            content_type = item.get("content_type")
            if (
                not isinstance(file_id, str)
                or not isinstance(object_key, str)
                or not object_key.startswith(f"{prefix}/models/")
                or content_type not in MODEL_CONTENT_TYPES
            ):
                raise HTTPException(status_code=422, detail="实验直接下载对象无效")
            expected.append(
                _artifact(file_id, object_key, item, content_type=content_type)
            )
    else:
        if (
            marker.get("schema_version") != PACKAGE_PUBLICATION_SCHEMA
            or marker.get("package_id") != publication_id
        ):
            raise HTTPException(status_code=422, detail="模型包 publication 无效")
        prefix = f"{PACKAGE_PREFIX}/{publication_id}"
        marker_key = f"{prefix}/publication.json"
        bundle = marker.get("bundle")
        if not isinstance(bundle, dict) or bundle.get("filename") != "package.tar.gz":
            raise HTTPException(status_code=422, detail="模型包 bundle 无效")
        expected.append(
            _artifact(
                "bundle",
                f"{prefix}/package.tar.gz",
                bundle,
                content_type="application/gzip",
            )
        )
        files = marker.get("files")
        if not isinstance(files, list) or not files:
            raise HTTPException(status_code=422, detail="模型包文件列表无效")
        for item in files:
            if not isinstance(item, dict):
                raise HTTPException(status_code=422, detail="模型包文件列表无效")
            file_id = item.get("file_id")
            object_key = item.get("object_key")
            content_type = item.get("content_type")
            if (
                not isinstance(file_id, str)
                or not isinstance(object_key, str)
                or not object_key.startswith(f"{prefix}/files/")
                or content_type not in MODEL_CONTENT_TYPES
            ):
                raise HTTPException(status_code=422, detail="模型包对象无效")
            expected.append(
                _artifact(file_id, object_key, item, content_type=content_type)
            )
    actual = [item.model_dump(mode="json") for item in body.artifacts]
    if actual != expected:
        raise HTTPException(status_code=422, detail="上传制品与发布标记推导结果不一致")
    if len({item["file_id"] for item in expected}) != len(expected):
        raise HTTPException(status_code=422, detail="上传制品 file_id 重复")
    return prefix, marker_key, expected


def _sha256_blob(blob: storage.Blob) -> str:
    digest = hashlib.sha256()
    with blob.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_loopback_redirect_uri(value: str) -> None:
    """只允许采集端固定的 loopback OAuth 回调，禁止代理交换任意重定向授权码。"""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise HTTPException(status_code=422, detail="OAuth redirect_uri 无效") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or not port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/api/v1/cloud/oauth/callback"
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=422, detail="OAuth redirect_uri 不是允许的本机回调")


def _safe_oauth_field(value: object, fallback: str, limit: int) -> str:
    return " ".join(str(value or fallback).split())[:limit]


def _oauth_error_response(response: requests.Response) -> JSONResponse:
    try:
        payload = response.json()
    except (requests.JSONDecodeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    error = _safe_oauth_field(payload.get("error"), "upstream_error", 80)
    descriptions = {
        "invalid_grant": "授权码或 refresh token 无效、已过期或已经使用",
        "invalid_request": "Google OAuth 请求参数无效",
        "unauthorized_client": "Google OAuth 客户端未被允许执行该请求",
        "access_denied": "Google 账号拒绝了授权请求",
    }
    # 不透传上游自由文本，避免它意外回显授权码、verifier、refresh token 或 client secret。
    description = descriptions.get(error, "Google OAuth token endpoint 拒绝请求")
    status = response.status_code if 400 <= response.status_code < 500 else 502
    return JSONResponse(
        status_code=status,
        content={"error": error, "error_description": description},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def create_upload_broker_app(settings: Settings | None = None) -> FastAPI:
    active = settings or load_settings()
    if active.storage.backend != "gcs" or not active.storage.bucket:
        raise RuntimeError("上传代理必须配置 storage.backend=gcs 和 storage.bucket")
    if not active.cloud.google_oauth_client_id:
        raise RuntimeError("上传代理缺少 cloud.google_oauth_client_id")
    if not active.cloud.google_oauth_client_secret:
        raise RuntimeError("上传代理缺少服务器私有的 cloud.google_oauth_client_secret")
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

    def model_actor(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, str]:
        """Accept only a Google-signed gcloud identity token for a whitelisted member."""

        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="缺少 Google ID token")
        token = authorization.removeprefix("Bearer ").strip()
        claims: dict[str, Any] | None = None
        for audience in active.cloud.model_publish_google_audiences:
            try:
                claims = google_id_token.verify_oauth2_token(
                    token,
                    GoogleAuthRequest(),
                    audience,
                )
                break
            except (ValueError, TypeError):
                continue
        if claims is None:
            raise HTTPException(status_code=401, detail="模型发布 Google ID token 无效")
        email = str(claims.get("email") or "").strip().lower()
        if not claims.get("email_verified") or email not in active.identity.email_to_unikey:
            raise HTTPException(status_code=403, detail="Google 账号不在团队发布白名单")
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
            "accepted_model_publication_schema_versions": [
                EXPERIMENT_PUBLICATION_SCHEMA,
                PACKAGE_PUBLICATION_SCHEMA,
            ],
            "model_publication_lifecycle": ["available", "deprecated"],
            "direct_to_bucket_resumable": True,
            "server_verifies_sha256_before_manifest": True,
        }

    @app.post("/v1/oauth/token", response_model=BrokerOAuthTokenResponse)
    def exchange_oauth_token(
        body: BrokerOAuthTokenRequest,
        response: Response,
    ) -> BrokerOAuthTokenResponse | JSONResponse:
        """使用服务器私有 client secret 代理 Google token exchange。"""

        form: dict[str, str] = {
            "client_id": active.cloud.google_oauth_client_id,
            "client_secret": active.cloud.google_oauth_client_secret,
            "grant_type": body.grant_type,
        }
        if body.grant_type == "authorization_code":
            assert body.code and body.code_verifier and body.redirect_uri
            _validate_loopback_redirect_uri(body.redirect_uri)
            form.update(
                {
                    "code": body.code,
                    "code_verifier": body.code_verifier,
                    "redirect_uri": body.redirect_uri,
                }
            )
        else:
            assert body.refresh_token
            form["refresh_token"] = body.refresh_token
        try:
            upstream = requests.post(
                active.cloud.token_endpoint,
                data=form,
                timeout=20,
            )
        except requests.RequestException:
            return JSONResponse(
                status_code=502,
                content={
                    "error": "temporarily_unavailable",
                    "error_description": "上传代理暂时无法连接 Google OAuth",
                },
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        if not upstream.ok:
            return _oauth_error_response(upstream)
        try:
            payload = upstream.json()
        except (requests.JSONDecodeError, ValueError):
            payload = None
        if not isinstance(payload, dict) or not payload.get("id_token"):
            return JSONResponse(
                status_code=502,
                content={
                    "error": "invalid_response",
                    "error_description": "Google OAuth 响应缺少 ID token",
                },
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return BrokerOAuthTokenResponse(
            id_token=str(payload["id_token"]),
            refresh_token=(
                str(payload["refresh_token"]) if payload.get("refresh_token") else None
            ),
            expires_in=int(payload.get("expires_in", 3600)),
        )

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

    @app.post("/v1/model-uploads", response_model=ModelUploadStartResponse)
    def start_model_upload(
        body: ModelUploadStartRequest,
        current: Annotated[dict[str, str], Depends(model_actor)],
    ) -> ModelUploadStartResponse:
        prefix, marker_key, artifacts = _expected_model_publication(body)
        marker_blob = bucket.blob(marker_key)
        marker_payload = _json_bytes(body.marker)
        if marker_blob.exists(client):
            existing = marker_blob.download_as_bytes(client=client)
            if existing != marker_payload:
                raise HTTPException(status_code=409, detail="该发布 ID 已存在且内容不同")

        sessions: list[ModelArtifactSession] = []
        for artifact in artifacts:
            blob = bucket.blob(artifact["object_key"])
            if blob.exists(client):
                blob.reload(client)
                metadata = dict(blob.metadata or {})
                if (
                    int(blob.size or -1) != artifact["size_bytes"]
                    or metadata.get("sha256") != artifact["sha256"]
                    or metadata.get("file_id") != artifact["file_id"]
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=f"模型对象已存在但身份不一致：{artifact['object_key']}",
                    )
                sessions.append(
                    ModelArtifactSession(
                        file_id=artifact["file_id"],
                        object_key=artifact["object_key"],
                        already_present=True,
                    )
                )
                continue
            blob.content_type = artifact["content_type"]
            blob.metadata = {
                "publication_kind": body.publication_kind,
                "publication_id": body.publication_id,
                "file_id": artifact["file_id"],
                "sha256": artifact["sha256"],
                "uploader": current["unikey"],
            }
            session_url = blob.create_resumable_upload_session(
                content_type=artifact["content_type"],
                size=artifact["size_bytes"],
                if_generation_match=0,
                checksum="auto",
            )
            sessions.append(
                ModelArtifactSession(
                    file_id=artifact["file_id"],
                    object_key=artifact["object_key"],
                    session_url=session_url,
                )
            )

        upload_id = uuid.uuid4().hex
        plan = {
            "schema_version": "imu_model_upload_plan_v1",
            "upload_id": upload_id,
            "actor": current,
            "expires_at_utc": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
            "publication_kind": body.publication_kind,
            "publication_id": body.publication_id,
            "prefix": prefix,
            "marker_key": marker_key,
            "marker": body.marker,
            "artifacts": artifacts,
        }
        bucket.blob(f"_upload_sessions/models/{upload_id}.json").upload_from_string(
            _json_bytes(plan),
            content_type="application/json",
            if_generation_match=0,
        )
        return ModelUploadStartResponse(upload_id=upload_id, sessions=sessions)

    @app.post(
        "/v1/model-uploads/complete",
        response_model=ModelUploadCompleteResponse,
    )
    def complete_model_upload(
        body: ModelUploadCompleteRequest,
        current: Annotated[dict[str, str], Depends(model_actor)],
    ) -> ModelUploadCompleteResponse:
        plan_blob = bucket.blob(f"_upload_sessions/models/{body.upload_id}.json")
        if not plan_blob.exists(client):
            raise HTTPException(status_code=404, detail="模型上传会话不存在或已经失效")
        plan = json.loads(plan_blob.download_as_bytes(client=client))
        if plan.get("actor") != current:
            raise HTTPException(status_code=403, detail="模型上传会话不属于当前用户")
        if datetime.fromisoformat(plan["expires_at_utc"]) < datetime.now(UTC):
            raise HTTPException(status_code=410, detail="模型上传会话已经过期")
        for artifact in plan["artifacts"]:
            blob = bucket.blob(artifact["object_key"])
            if not blob.exists(client):
                raise HTTPException(
                    status_code=409,
                    detail=f"模型制品尚未上传：{artifact['file_id']}",
                )
            blob.reload(client)
            if int(blob.size or -1) != artifact["size_bytes"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"模型制品大小不一致：{artifact['file_id']}",
                )
            if _sha256_blob(blob) != artifact["sha256"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"模型制品 SHA-256 不一致：{artifact['file_id']}",
                )

        timestamp = datetime.now(UTC).isoformat()
        state_key = f"{plan['prefix']}/state.json"
        state_blob = bucket.blob(state_key)
        if not state_blob.exists(client):
            state_blob.metadata = {
                "publication_kind": plan["publication_kind"],
                "publication_id": plan["publication_id"],
            }
            state_blob.upload_from_string(
                _json_bytes(
                    {
                        "schema_version": MODEL_STATE_SCHEMA,
                        "kind": plan["publication_kind"],
                        "publication_id": plan["publication_id"],
                        "status": "available",
                        "updated_at_utc": timestamp,
                        "updated_by": current["unikey"],
                        "history": [],
                    }
                ),
                content_type="application/json",
                if_generation_match=0,
            )

        marker_blob = bucket.blob(plan["marker_key"])
        marker_payload = _json_bytes(plan["marker"])
        if marker_blob.exists(client):
            if marker_blob.download_as_bytes(client=client) != marker_payload:
                raise HTTPException(status_code=409, detail="远端发布标记内容冲突")
            marker_blob.reload(client)
        else:
            marker_blob.metadata = {
                "publication_kind": plan["publication_kind"],
                "publication_id": plan["publication_id"],
                "sha256": hashlib.sha256(marker_payload).hexdigest(),
                "publisher": current["unikey"],
            }
            marker_blob.upload_from_string(
                marker_payload,
                content_type="application/json",
                if_generation_match=0,
            )
            marker_blob.reload(client)
        return ModelUploadCompleteResponse(
            publication_kind=plan["publication_kind"],
            publication_id=plan["publication_id"],
            marker_object=plan["marker_key"],
            marker_generation=int(marker_blob.generation or 0),
        )

    def model_state_blob(
        publication_kind: Literal["experiment", "package"], publication_id: str
    ) -> storage.Blob:
        if publication_kind == "package":
            candidates = [f"{PACKAGE_PREFIX}/{publication_id}/state.json"]
        else:
            candidates = [
                f"{root}/{publication_id}/state.json"
                for root in EXPERIMENT_PREFIXES.values()
            ]
        for key in candidates:
            blob = bucket.blob(key)
            if blob.exists(client):
                blob.reload(client)
                return blob
        raise HTTPException(status_code=404, detail="找不到模型发布状态")

    @app.post("/v1/model-publications/{publication_kind}/{publication_id}/restore")
    def restore_model_publication(
        publication_kind: Literal["experiment", "package"],
        publication_id: str,
        body: ModelPublicationRestoreRequest,
        current: Annotated[dict[str, str], Depends(model_actor)],
    ) -> dict[str, Any]:
        if current["unikey"] not in active.identity.admins:
            raise HTTPException(status_code=403, detail="恢复模型发布仅限管理员")
        blob = model_state_blob(publication_kind, publication_id)
        if int(blob.generation or 0) != body.expected_generation:
            raise HTTPException(status_code=409, detail="模型发布状态已经变化")
        state = json.loads(blob.download_as_bytes(client=client))
        if (
            state.get("schema_version") != MODEL_STATE_SCHEMA
            or state.get("kind") != publication_kind
            or state.get("publication_id") != publication_id
        ):
            raise HTTPException(status_code=422, detail="模型发布状态无效")
        timestamp = datetime.now(UTC).isoformat()
        previous = state.get("status")
        history = list(state.get("history") or [])
        history.append(
            {
                "action": "restore",
                "from": previous,
                "to": "available",
                "actor": current["unikey"],
                "at_utc": timestamp,
            }
        )
        state.update(
            {
                "status": "available",
                "updated_at_utc": timestamp,
                "updated_by": current["unikey"],
                "history": history,
            }
        )
        try:
            blob.upload_from_string(
                _json_bytes(state),
                content_type="application/json",
                if_generation_match=body.expected_generation,
            )
        except PreconditionFailed as error:
            raise HTTPException(status_code=409, detail="模型发布状态已经变化") from error
        blob.reload(client)
        return {**state, "generation": int(blob.generation or 0)}

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
