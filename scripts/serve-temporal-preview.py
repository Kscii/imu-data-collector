"""Serve the existing annotation UI locally, with cloud writes scoped to one dev pilot."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.config import load_settings
from imu_data_collector.storage import GcsObjectStore, LocalFilesystemStore
from imu_data_collector.synthetic_temporal_contract import IDENTIFIER


class PilotStore:
    """Production motion inputs are readable; only this pilot's dev prefix is writable."""

    def __init__(self, store, run_id):
        self.store = store
        self.prefix = f"synthetic-motion/dev/{run_id}/v1/"
        self.index = f"synthetic-motion/temporal/run-index/{run_id}.json"

    def __getattr__(self, name):
        if name in {"read_json", "read_bytes", "download_file", "stat", "signed_download_url"}:
            return getattr(self.store, name)
        raise AttributeError(name)

    def list(self, prefix):
        if prefix == "synthetic-motion/temporal/run-index/":
            value = self.store.stat(self.index)
            return [value] if value else []
        return self.store.list(prefix)

    def list_after(self, prefix, after_key):
        return self.store.list_after(prefix, after_key)

    def writable(self, key):
        if not key.startswith(self.prefix):
            raise ValueError("This local preview can write only its isolated pilot namespace")

    def write_json(self, key, payload, *, if_generation_match):
        self.writable(key)
        return self.store.write_json(key, payload, if_generation_match=if_generation_match)

    def put_file(self, source, key, **kwargs):
        self.writable(key)
        return self.store.put_file(source, key, **kwargs)

    def delete(self, key, **kwargs):
        raise ValueError("The temporal preview does not delete cloud objects")

    def copy(self, source_key, destination_key, **kwargs):
        self.writable(destination_key)
        return self.store.copy(source_key, destination_key, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bucket")
    source.add_argument("--local-store", type=Path)
    parser.add_argument("--project")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--actor", default="xfan0282")
    args = parser.parse_args()
    if not IDENTIFIER.fullmatch(args.run_id):
        raise ValueError("Invalid pilot run ID")
    root = args.state_dir.resolve()
    settings = load_settings()
    settings.storage.backend = "local"
    settings.storage.root = root / "local-objects"
    settings.storage.cache_root = root / "cache"
    settings.annotation.catalog_path = root / "catalog.sqlite3"
    settings.annotation.catalog_refresh_interval_s = 0
    settings.annotation.synthetic_target = "prod"
    settings.annotation.synthetic_run_id = None
    settings.auth.mode = "local"
    settings.auth.local_actor_id = args.actor
    backing = (
        LocalFilesystemStore(args.local_store)
        if args.local_store
        else GcsObjectStore(args.bucket, args.project)
    )
    store = PilotStore(backing, args.run_id)
    app = create_annotation_app(settings, synthetic_store_override=store)
    # Local authentication is restricted to loopback; never bind this preview to a LAN address.
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
