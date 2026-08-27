"""把面向 WebUI 的 HTTP 错误包装成稳定、可本地化的结构。"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

_KNOWN_CODES = {
    "请求缺少可信登录身份": "authentication_required",
    "该操作仅限管理员": "admin_required",
    "找不到该录制": "recording_not_found",
    "找不到该校准证据": "calibration_evidence_not_found",
    "找不到该训练快照": "training_snapshot_not_found",
    "找不到视频": "video_not_found",
    "只支持单个 bytes Range": "single_range_required",
    "视频 Range 越界": "video_range_out_of_bounds",
    "训练快照 Range 越界": "snapshot_range_out_of_bounds",
    "test 数据永久禁止下载训练 H5": "test_training_export_forbidden",
    "预览通道尚未建立或已经释放": "preview_stream_unavailable",
}


def error_detail(code: str, message: str, **params: Any) -> dict[str, Any]:
    detail: dict[str, Any] = {"code": code, "message": message}
    if params:
        detail["params"] = params
    return detail


def _code_for(detail: Any, status_code: int) -> str:
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        return detail["code"]
    if isinstance(detail, str):
        if detail in _KNOWN_CODES:
            return _KNOWN_CODES[detail]
        normalized = re.sub(r"[^a-z0-9]+", "_", detail.lower()).strip("_")
        if normalized and not re.search(r"[\u3400-\u9fff]", normalized):
            return normalized[:80]
    return f"http_{status_code}"


async def structured_http_error_handler(
    _request: Request, error: HTTPException
) -> JSONResponse:
    if isinstance(error.detail, dict) and "code" in error.detail:
        detail = error.detail
    else:
        detail = error_detail(
            _code_for(error.detail, error.status_code),
            str(error.detail),
        )
    return JSONResponse(
        status_code=error.status_code,
        content={"detail": detail},
        headers=error.headers,
    )
