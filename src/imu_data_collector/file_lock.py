"""为 review、缓存等小型临界区提供跨平台进程锁。"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


def _lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                # LK_LOCK 只内置有限次数重试；缓存并发可能等待更久，因此由
                # 应用持续等待，并保留与 POSIX flock 相同的阻塞语义。
                time.sleep(0.05)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            # msvcrt.locking() 只能锁定已经存在的字节。先在文件为空时写入哨兵；
            # 并发首次创建即使多写一个字节，也只会锁定稳定的第 0 字节。
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
        _lock(handle)
        try:
            yield
        finally:
            _unlock(handle)
