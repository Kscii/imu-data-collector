#!/usr/bin/env python3
"""保守判断一组 Git 路径是否会影响桌面安装包。"""

from __future__ import annotations

import argparse
import fnmatch
import sys
from collections.abc import Iterable

# 只有能够明确证明不进入采集端安装包的路径才能加入这里。未知路径、共享
# models/config/storage 以及共享 App.tsx 一律触发桌面构建，避免漏发采集端变更。
ANNOTATION_ONLY_PATTERNS = (
    ".github/**",
    "configs/server.annotation.example.yaml",
    "docs/**",
    "frontend/src/annotationShortcuts.*",
    "frontend/src/annotationTimeline.*",
    "scripts/deploy/**",
    "scripts/desktop_ci_scope.py",
    "src/imu_data_collector/annotation_*.py",
    "src/imu_data_collector/dataset_catalog.py",
    "src/imu_data_collector/model_catalog.py",
    "src/imu_data_collector/taxonomy_store.py",
    "tests/test_annotation*.py",
    "tests/test_calibration_evidence.py",
    "tests/test_dataset_catalog.py",
    "tests/test_desktop_ci_scope.py",
    "tests/test_model_catalog.py",
)


def is_annotation_only_path(path: str) -> bool:
    """路径必须安全且命中明确的标注端专属白名单。"""

    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized.startswith("/"):
        return False
    if ".." in normalized.split("/"):
        return False
    return any(
        fnmatch.fnmatchcase(normalized, pattern)
        for pattern in ANNOTATION_ONLY_PATTERNS
    )


def desktop_build_required(paths: Iterable[str]) -> bool:
    """空集合和任何未知/共享路径都保守地要求构建桌面安装包。"""

    changed = tuple(paths)
    return not changed or any(not is_annotation_only_path(path) for path in changed)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--null",
        action="store_true",
        help="从标准输入读取以 NUL 分隔的 Git 路径",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    payload = sys.stdin.buffer.read()
    separator = b"\0" if args.null else b"\n"
    paths = [
        item.decode("utf-8", errors="surrogateescape")
        for item in payload.split(separator)
        if item
    ]
    print("true" if desktop_build_required(paths) else "false")


if __name__ == "__main__":
    main()
