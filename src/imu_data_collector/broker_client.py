"""桌面端通过上传代理把大制品直传 GCS。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import requests

from imu_data_collector.broker_models import (
    BrokerUploadCompleteRequest,
    BrokerUploadCompleteResponse,
    BrokerUploadStartRequest,
    BrokerUploadStartResponse,
)
from imu_data_collector.config import Settings
from imu_data_collector.desktop_auth import DesktopOAuthManager
from imu_data_collector.models import CaptureManifestV2, RecordingSummary
from imu_data_collector.publisher import prepare_publication


def _put_resumable(
    session_url: str,
    path: Path,
    *,
    chunk_size: int = 8 * 1024 * 1024,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """按 GCS resumable upload 协议发送固定大小分块。"""

    total = path.stat().st_size
    if total <= 0:
        raise RuntimeError(f"禁止上传空制品：{path.name}")
    offset = 0
    with path.open("rb") as handle:
        while offset < total:
            chunk = handle.read(min(chunk_size, total - offset))
            end = offset + len(chunk) - 1
            response = requests.put(
                session_url,
                data=chunk,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{total}",
                },
                timeout=180,
            )
            if response.status_code not in {200, 201, 308}:
                response.raise_for_status()
                raise RuntimeError(f"GCS resumable upload 返回 {response.status_code}")
            offset = end + 1
            if progress is not None:
                progress(offset, total)
            if response.status_code in {200, 201} and offset != total:
                raise RuntimeError("GCS 在全部分块发送前提前结束上传")
    if offset != total:
        raise RuntimeError("GCS resumable upload 没有覆盖完整文件")


def _broker_post(
    url: str,
    token: str,
    payload: dict,
    *,
    timeout: int = 60,
) -> dict:
    response = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("上传代理返回了无效 JSON")
    return value


async def publish_recording_via_broker(
    summary: RecordingSummary,
    settings: Settings,
    auth: DesktopOAuthManager,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[CaptureManifestV2, int]:
    manifest, paths = await prepare_publication(summary, settings)
    if not settings.cloud.broker_url:
        raise RuntimeError("尚未配置上传代理 URL")
    token = await asyncio.to_thread(auth.id_token)
    base = settings.cloud.broker_url.rstrip("/")
    started_payload = await asyncio.to_thread(
        _broker_post,
        f"{base}/v1/uploads",
        token,
        BrokerUploadStartRequest(manifest=manifest).model_dump(mode="json"),
    )
    started = BrokerUploadStartResponse.model_validate(started_payload)
    total_bytes = sum(artifact.size_bytes for artifact in manifest.artifacts)
    completed_bytes = 0
    for session in started.sessions:
        artifact = next(
            item for item in manifest.artifacts if item.role == session.role
        )
        if session.already_present:
            completed_bytes += artifact.size_bytes
            if progress is not None:
                progress(completed_bytes, total_bytes, session.role)
            continue
        if not session.session_url:
            raise RuntimeError(f"上传代理没有为 {session.role} 返回会话 URL")
        await asyncio.to_thread(
            _put_resumable,
            session.session_url,
            paths[session.role],
            progress=(
                lambda current, _total, base=completed_bytes, role=session.role: progress(
                    base + current, total_bytes, role
                )
                if progress is not None
                else None
            ),
        )
        completed_bytes += artifact.size_bytes
    completed_payload = await asyncio.to_thread(
        _broker_post,
        f"{base}/v1/uploads/complete",
        token,
        BrokerUploadCompleteRequest(
            upload_id=started.upload_id,
            manifest=manifest,
        ).model_dump(mode="json"),
        timeout=1800,
    )
    completed = BrokerUploadCompleteResponse.model_validate(completed_payload)
    if completed.recording_id != manifest.recording_id or not completed.verified_sha256:
        raise RuntimeError("上传代理没有确认当前录制的 SHA-256")
    return manifest, completed.manifest_generation


async def read_index_receipt_via_broker(
    recording_id: str,
    settings: Settings,
    auth: DesktopOAuthManager,
) -> dict:
    if not settings.cloud.broker_url:
        raise RuntimeError("尚未配置上传代理 URL")
    token = await asyncio.to_thread(auth.id_token)
    response = await asyncio.to_thread(
        requests.get,
        f"{settings.cloud.broker_url.rstrip('/')}/v1/recordings/{recording_id}/receipt",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if response.status_code == 404:
        raise FileNotFoundError(recording_id)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("上传代理返回了无效索引回执")
    return payload
