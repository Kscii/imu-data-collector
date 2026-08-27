"""生成可用于识别采集端前后端是否来自同一源码的短哈希。"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CAPTURE_API_SOURCES = ("capture_api.py", "coordinator.py", "models.py")


def _capture_api_build_id() -> str:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in _CAPTURE_API_SOURCES:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


# 在进程导入阶段冻结。即使服务运行期间磁盘上的源码改变，旧进程仍报告旧哈希。
CAPTURE_API_BUILD_ID = _capture_api_build_id()
