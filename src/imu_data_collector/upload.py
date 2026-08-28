"""与云存储厂商无关的 rclone 复制与恢复操作。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from imu_data_collector.config import UploadSettings
from imu_data_collector.host import background_subprocess_kwargs


class RcloneRemoteStore:
    def __init__(self, settings: UploadSettings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.enabled and self.settings.remote_root)

    def remote_recording_dir(self, collection_id: str, recording_id: str) -> str:
        if not self.settings.remote_root:
            raise RuntimeError("upload.remote_root is not configured")
        root = self.settings.remote_root.strip("/")
        return f"{self.settings.remote}:{root}/{collection_id}/{recording_id}"

    async def _run(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "rclone",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **background_subprocess_kwargs(),
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace").strip())
        return stdout.decode(errors="replace")

    async def upload_pair(
        self, collection_id: str, recording_id: str, h5_path: Path, mkv_path: Path
    ) -> None:
        remote_dir = self.remote_recording_dir(collection_id, recording_id)
        for path in (h5_path, mkv_path):
            await self._run(
                "copyto",
                "--retries",
                "5",
                "--low-level-retries",
                "10",
                str(path),
                f"{remote_dir}/{path.name}",
            )
            await self._run(
                "check",
                "--one-way",
                "--include",
                path.name,
                str(path.parent),
                remote_dir,
            )

    async def restore_pair(
        self, collection_id: str, recording_id: str, target_dir: Path
    ) -> tuple[Path, Path]:
        remote_dir = self.remote_recording_dir(collection_id, recording_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        outputs: list[Path] = []
        for suffix in (".h5", ".mkv"):
            destination = target_dir / f"{recording_id}{suffix}"
            temporary = destination.with_suffix(destination.suffix + ".download")
            await self._run(
                "copyto", f"{remote_dir}/{destination.name}", str(temporary)
            )
            os.replace(temporary, destination)
            outputs.append(destination)
        return outputs[0], outputs[1]
