"""桌面采集端与最小权限上传代理之间的私有合同。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from imu_data_collector.models import CaptureManifestV2


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
