"""采集与标注应用共享的制品存储合同。"""

from __future__ import annotations

import errno
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    key: str
    size_bytes: int
    generation: int
    content_type: str | None
    metadata: dict[str, str]
    updated_at_utc: datetime | None = None


class ObjectConflictError(RuntimeError):
    """对象已存在或 generation 前置条件不成立。"""


class ObjectStore(Protocol):
    def put_file(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: dict[str, str] | None = None,
        if_absent: bool = True,
    ) -> ObjectInfo: ...

    def download_file(self, key: str, destination: Path) -> ObjectInfo: ...

    def read_bytes(self, key: str, start: int | None = None, end: int | None = None) -> bytes: ...

    def read_json(self, key: str) -> tuple[dict[str, Any], int]: ...

    def write_json(
        self,
        key: str,
        payload: dict[str, Any],
        *,
        if_generation_match: int | None,
    ) -> ObjectInfo: ...

    def stat(self, key: str) -> ObjectInfo | None: ...

    def list(self, prefix: str) -> list[ObjectInfo]: ...

    def delete(self, key: str, *, if_generation_match: int | None) -> bool: ...

    def copy(
        self,
        source_key: str,
        destination_key: str,
        *,
        if_source_generation_match: int,
    ) -> ObjectInfo: ...


def _safe_key(key: str) -> Path:
    pure = PurePosixPath(key)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ValueError("对象键必须是安全的相对路径")
    return Path(*pure.parts)


class LocalFilesystemStore:
    """本地开发使用硬链接优先、跨文件系统原子复制的制品存储。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, key: str) -> Path:
        path = (self.root / _safe_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("对象键越出存储根目录")
        return path

    def _metadata_path(self, key: str) -> Path:
        return self.root / ".object_metadata" / _safe_key(f"{key}.json")

    def put(self, source: Path, key: str) -> Path:
        self.put_file(
            source,
            key,
            content_type="application/octet-stream",
        )
        return self.resolve(key)

    def exists(self, key: str) -> bool:
        return self.resolve(key).is_file()

    def put_file(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: dict[str, str] | None = None,
        if_absent: bool = True,
    ) -> ObjectInfo:
        target = self.resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if if_absent:
                raise ObjectConflictError(f"对象已存在：{key}")
            target.unlink()
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.partial"
        )
        temporary.unlink(missing_ok=True)
        try:
            try:
                os.link(source, temporary)
            except OSError as error:
                if error.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES}:
                    raise
                shutil.copy2(source, temporary)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        metadata_path = self._metadata_path(key)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(
                {"content_type": content_type, "metadata": metadata or {}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return self._info(key, target)

    def download_file(self, key: str, destination: Path) -> ObjectInfo:
        source = self.resolve(key)
        if not source.is_file():
            raise FileNotFoundError(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.partial"
        )
        try:
            shutil.copy2(source, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return self._info(key, source)

    def read_bytes(
        self, key: str, start: int | None = None, end: int | None = None
    ) -> bytes:
        with self.resolve(key).open("rb") as handle:
            if start is not None:
                handle.seek(start)
            return handle.read(None if end is None else end - (start or 0) + 1)

    def read_json(self, key: str) -> tuple[dict[str, Any], int]:
        path = self.resolve(key)
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle), path.stat().st_mtime_ns

    def write_json(
        self,
        key: str,
        payload: dict[str, Any],
        *,
        if_generation_match: int | None,
    ) -> ObjectInfo:
        path = self.resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        current = path.stat().st_mtime_ns if path.exists() else 0
        if if_generation_match is not None and current != if_generation_match:
            raise ObjectConflictError("对象 generation 已更新")
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.partial"
        )
        try:
            temporary.write_bytes(
                (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                )
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return self._info(key, path)

    def stat(self, key: str) -> ObjectInfo | None:
        path = self.resolve(key)
        return self._info(key, path) if path.is_file() else None

    def list(self, prefix: str) -> list[ObjectInfo]:
        root = self.resolve(prefix)
        if not root.exists():
            return []
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        return [
            self._info(path.relative_to(self.root).as_posix(), path)
            for path in paths
            if path.is_file()
        ]

    def delete(self, key: str, *, if_generation_match: int | None) -> bool:
        path = self.resolve(key)
        if not path.is_file():
            return False
        current = path.stat().st_mtime_ns
        if if_generation_match is not None and current != if_generation_match:
            raise ObjectConflictError("对象 generation 已更新")
        path.unlink()
        self._metadata_path(key).unlink(missing_ok=True)
        return True

    def copy(
        self,
        source_key: str,
        destination_key: str,
        *,
        if_source_generation_match: int,
    ) -> ObjectInfo:
        source = self.resolve(source_key)
        if not source.is_file():
            raise FileNotFoundError(source_key)
        if source.stat().st_mtime_ns != if_source_generation_match:
            raise ObjectConflictError("源对象 generation 已更新")
        destination = self.resolve(destination_key)
        if destination.exists():
            raise ObjectConflictError(f"对象已存在：{destination_key}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
        source_metadata = self._metadata_path(source_key)
        destination_metadata = self._metadata_path(destination_key)
        if source_metadata.is_file():
            destination_metadata.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_metadata, destination_metadata)
        return self._info(destination_key, destination)

    def _info(self, key: str, path: Path) -> ObjectInfo:
        stat = path.stat()
        metadata_path = self._metadata_path(key)
        payload: dict[str, Any] = {}
        if metadata_path.is_file():
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        return ObjectInfo(
            key,
            stat.st_size,
            stat.st_mtime_ns,
            payload.get("content_type"),
            payload.get("metadata", {}),
            datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )


class GcsObjectStore:
    """使用 ADC 与 GCS generation 前置条件的生产制品存储。"""

    def __init__(self, bucket: str, project: str | None = None) -> None:
        self.client = storage.Client(project=project)
        self.bucket = self.client.bucket(bucket.removeprefix("gs://"))

    @staticmethod
    def _info(blob: storage.Blob) -> ObjectInfo:
        return ObjectInfo(
            key=blob.name,
            size_bytes=int(blob.size or 0),
            generation=int(blob.generation or 0),
            content_type=blob.content_type,
            metadata=dict(blob.metadata or {}),
            updated_at_utc=blob.updated,
        )

    def put_file(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: dict[str, str] | None = None,
        if_absent: bool = True,
    ) -> ObjectInfo:
        _safe_key(key)
        blob = self.bucket.blob(key)
        blob.metadata = metadata or {}
        try:
            blob.upload_from_filename(
                source,
                content_type=content_type,
                if_generation_match=0 if if_absent else None,
                timeout=900,
            )
        except PreconditionFailed as error:
            raise ObjectConflictError(f"对象已存在：{key}") from error
        blob.reload()
        return self._info(blob)

    def download_file(self, key: str, destination: Path) -> ObjectInfo:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.partial"
        )
        blob = self.bucket.blob(key)
        try:
            blob.download_to_filename(temporary, timeout=900)
            temporary.replace(destination)
            blob.reload()
        except NotFound as error:
            raise FileNotFoundError(key) from error
        finally:
            temporary.unlink(missing_ok=True)
        return self._info(blob)

    def read_bytes(
        self, key: str, start: int | None = None, end: int | None = None
    ) -> bytes:
        try:
            return self.bucket.blob(key).download_as_bytes(
                start=start,
                end=end,
                timeout=120,
            )
        except NotFound as error:
            raise FileNotFoundError(key) from error

    def read_json(self, key: str) -> tuple[dict[str, Any], int]:
        blob = self.bucket.blob(key)
        try:
            payload = json.loads(blob.download_as_text(encoding="utf-8"))
            blob.reload()
        except NotFound as error:
            raise FileNotFoundError(key) from error
        return payload, int(blob.generation or 0)

    def write_json(
        self,
        key: str,
        payload: dict[str, Any],
        *,
        if_generation_match: int | None,
    ) -> ObjectInfo:
        blob = self.bucket.blob(key)
        try:
            blob.upload_from_string(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                content_type="application/json; charset=utf-8",
                if_generation_match=if_generation_match,
            )
        except PreconditionFailed as error:
            raise ObjectConflictError("对象 generation 已更新") from error
        blob.reload()
        return self._info(blob)

    def stat(self, key: str) -> ObjectInfo | None:
        blob = self.bucket.blob(key)
        try:
            blob.reload()
        except NotFound:
            return None
        return self._info(blob)

    def list(self, prefix: str) -> list[ObjectInfo]:
        return [self._info(blob) for blob in self.client.list_blobs(self.bucket, prefix=prefix)]

    def delete(self, key: str, *, if_generation_match: int | None) -> bool:
        _safe_key(key)
        try:
            self.bucket.blob(key).delete(
                if_generation_match=if_generation_match,
                timeout=120,
            )
        except NotFound:
            return False
        except PreconditionFailed as error:
            raise ObjectConflictError("对象 generation 已更新") from error
        return True

    def copy(
        self,
        source_key: str,
        destination_key: str,
        *,
        if_source_generation_match: int,
    ) -> ObjectInfo:
        _safe_key(source_key)
        _safe_key(destination_key)
        try:
            copied = self.bucket.copy_blob(
                self.bucket.blob(source_key, generation=if_source_generation_match),
                self.bucket,
                destination_key,
                source_generation=if_source_generation_match,
                if_generation_match=0,
                timeout=900,
            )
        except NotFound as error:
            raise FileNotFoundError(source_key) from error
        except PreconditionFailed as error:
            raise ObjectConflictError(f"对象已存在或源已更新：{destination_key}") from error
        copied.reload()
        return self._info(copied)


def create_object_store(
    backend: str,
    root: Path,
    bucket: str | None,
    project: str | None,
) -> ObjectStore:
    if backend == "local":
        return LocalFilesystemStore(root)
    if backend == "gcs" and bucket:
        return GcsObjectStore(bucket, project)
    raise ValueError("storage.backend 必须为 local，或同时配置 gcs bucket")
