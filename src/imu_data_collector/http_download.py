"""HTTP byte-range responses for immutable object-store artifacts."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse

from imu_data_collector.storage import ObjectInfo, ObjectStore

DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024
DOWNLOAD_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int
    status_code: int


def parse_byte_range(value: str | None, size: int, *, label: str) -> ByteRange:
    """Parse one RFC 9110 byte range, including open and suffix forms."""

    if size <= 0:
        raise ValueError("下载对象必须非空")
    if value is None:
        return ByteRange(0, size - 1, 200)
    raw = value.strip()
    if not raw.startswith("bytes=") or "," in raw:
        raise HTTPException(
            status_code=416,
            detail=f"{label}只支持单个 bytes Range",
            headers={"Content-Range": f"bytes */{size}"},
        )
    bounds = raw.removeprefix("bytes=").split("-", 1)
    if len(bounds) != 2 or not any(bounds):
        raise HTTPException(
            status_code=416,
            detail=f"{label} Range 无效",
            headers={"Content-Range": f"bytes */{size}"},
        )
    start_text, end_text = bounds
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_text)
            end = min(size - 1, int(end_text)) if end_text else size - 1
            if start < 0 or start >= size or start > end:
                raise ValueError
    except ValueError as error:
        raise HTTPException(
            status_code=416,
            detail=f"{label} Range 越界",
            headers={"Content-Range": f"bytes */{size}"},
        ) from error
    return ByteRange(start, end, 206)


def _read_chunk(
    store: ObjectStore,
    key: str,
    start: int,
    end: int,
    *,
    attempts: int = DOWNLOAD_ATTEMPTS,
) -> bytes:
    expected = end - start + 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            payload = store.read_bytes(key, start, end)
            if len(payload) != expected:
                raise OSError(
                    f"对象分块提前结束：期望 {expected} 字节，实际 {len(payload)} 字节"
                )
            return payload
        except FileNotFoundError:
            raise
        except Exception as error:  # a streaming retry must cover transport-specific errors
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(0.25 * (2**attempt))
    assert last_error is not None
    raise last_error


def _chunks(
    store: ObjectStore,
    key: str,
    byte_range: ByteRange,
    *,
    chunk_bytes: int = DOWNLOAD_CHUNK_BYTES,
) -> Iterator[bytes]:
    cursor = byte_range.start
    while cursor <= byte_range.end:
        chunk_end = min(byte_range.end, cursor + chunk_bytes - 1)
        yield _read_chunk(store, key, cursor, chunk_end)
        cursor = chunk_end + 1


def object_download_response(
    *,
    store: ObjectStore,
    info: ObjectInfo,
    filename: str,
    media_type: str,
    range_header: str | None,
    sha256: str | None,
    cache_control: str = "private, no-store",
    head: bool = False,
    label: str = "文件",
) -> Response:
    byte_range = parse_byte_range(range_header, info.size_bytes, label=label)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(byte_range.end - byte_range.start + 1),
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": cache_control,
    }
    if sha256:
        headers["ETag"] = f'"sha256-{sha256}"'
        headers["X-Content-SHA256"] = sha256
    if byte_range.status_code == 206:
        headers["Content-Range"] = (
            f"bytes {byte_range.start}-{byte_range.end}/{info.size_bytes}"
        )
    if head:
        return Response(
            status_code=byte_range.status_code,
            media_type=media_type,
            headers=headers,
        )
    return StreamingResponse(
        _chunks(store, info.key, byte_range),
        status_code=byte_range.status_code,
        media_type=media_type,
        headers=headers,
    )
