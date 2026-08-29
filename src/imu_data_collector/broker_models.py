"""桌面采集端与最小权限上传代理之间的私有合同。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from imu_data_collector.models import CaptureManifestV2


class BrokerOAuthTokenRequest(BaseModel):
    """桌面端提交给代理的最小 OAuth token exchange 参数。"""

    model_config = ConfigDict(extra="forbid")

    grant_type: Literal["authorization_code", "refresh_token"]
    code: str | None = Field(default=None, min_length=1, max_length=4096)
    code_verifier: str | None = Field(default=None, min_length=43, max_length=128)
    redirect_uri: str | None = Field(default=None, min_length=1, max_length=512)
    refresh_token: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_grant_fields(self) -> BrokerOAuthTokenRequest:
        if self.grant_type == "authorization_code":
            if not self.code or not self.code_verifier or not self.redirect_uri:
                raise ValueError("authorization_code 缺少 code、code_verifier 或 redirect_uri")
            if self.refresh_token is not None:
                raise ValueError("authorization_code 禁止携带 refresh_token")
        elif not self.refresh_token:
            raise ValueError("refresh_token grant 缺少 refresh_token")
        elif any((self.code, self.code_verifier, self.redirect_uri)):
            raise ValueError("refresh_token grant 禁止携带授权码字段")
        return self


class BrokerOAuthTokenResponse(BaseModel):
    """代理过滤后的 token 响应；不把无用 access token 交给桌面端。"""

    id_token: str = Field(min_length=1)
    refresh_token: str | None = None
    expires_in: int = Field(default=3600, ge=60, le=86_400)


class BrokerUploadStartRequest(BaseModel):
    manifest: CaptureManifestV2


class BrokerArtifactSession(BaseModel):
    role: Literal["capture_h5", "video_mkv", "preview_mp4"]
    object_key: str
    session_url: str | None = None
    already_present: bool = False


class BrokerUploadStartResponse(BaseModel):
    upload_id: str
    sessions: list[BrokerArtifactSession]


class BrokerUploadCompleteRequest(BaseModel):
    upload_id: str
    manifest: CaptureManifestV2


class BrokerUploadCompleteResponse(BaseModel):
    recording_id: str
    manifest_generation: int
    verified_sha256: bool = True


class ModelArtifactDescriptor(BaseModel):
    """Benchmark publisher requests only these immutable objects."""

    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
    object_key: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(min_length=1, max_length=128)


class ModelUploadStartRequest(BaseModel):
    """Immutable model/result payload and the marker written after verification."""

    model_config = ConfigDict(extra="forbid")

    publication_kind: Literal["result", "experiment", "model"]
    publication_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
    marker: dict[str, Any]
    artifacts: list[ModelArtifactDescriptor] = Field(min_length=1)


class ModelArtifactSession(BaseModel):
    file_id: str
    object_key: str
    session_url: str | None = None
    already_present: bool = False


class ModelUploadStartResponse(BaseModel):
    upload_id: str
    sessions: list[ModelArtifactSession]


class ModelUploadCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class ModelUploadCompleteResponse(BaseModel):
    publication_kind: Literal["result", "experiment", "model"]
    publication_id: str
    marker_object: str
    marker_generation: int
    verified_sha256: bool = True


class ModelPublicationRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_generation: int = Field(gt=0)
