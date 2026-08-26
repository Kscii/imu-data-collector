"""可替换的大文件存储边界；当前实现只访问本地文件系统。"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath
from typing import Protocol


class ObjectStore(Protocol):
    def put(self, source: Path, key: str) -> Path: ...

    def resolve(self, key: str) -> Path: ...

    def exists(self, key: str) -> bool: ...


def _safe_key(key: str) -> Path:
    pure = PurePosixPath(key)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ValueError("对象键必须是安全的相对路径")
    return Path(*pure.parts)


class LocalFilesystemStore:
    """将对象键映射到一个固定根目录，便于以后替换为 GCS。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, key: str) -> Path:
        path = (self.root / _safe_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("对象键越出存储根目录")
        return path

    def put(self, source: Path, key: str) -> Path:
        target = self.resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.partial")
        shutil.copy2(source, temporary)
        temporary.replace(target)
        return target

    def exists(self, key: str) -> bool:
        return self.resolve(key).is_file()
