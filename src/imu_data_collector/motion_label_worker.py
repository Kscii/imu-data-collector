"""Single CPU worker with durable jobs, content-addressed scores and CAS publication."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import time
from pathlib import Path

import numpy as np

from .motion_annotations import primary_prediction as proposal
from .motion_annotations import require
from .motion_label_models import active_model, validate_model
from .motion_label_runtime import cpu_session, predict_scores, reject
from .motion_label_service import MotionLabelService, now
from .storage import ObjectConflictError, create_object_store
from .synthetic_motion import SyntheticReviewService
from .synthetic_temporal_contract import PRIMARY, document_sha256


def sha(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def checked_download(service, descriptor, path):
    path = Path(path)
    if (
        path.exists()
        and path.stat().st_size == descriptor["byte_length"]
        and sha(path) == descriptor["sha256"]
    ):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    service.store.download_file(descriptor["object_key"], temporary)
    require(
        temporary.stat().st_size == descriptor["byte_length"]
        and sha(temporary) == descriptor["sha256"],
        "Downloaded inference artifact hash differs",
    )
    temporary.replace(path)
    return path


class MotionLabelWorker:
    def __init__(self, service, cache):
        self.service, self.cache = service, Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        with service.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inference_jobs(
                    id TEXT PRIMARY KEY, motion TEXT, release_json TEXT, feature_json TEXT,
                    state TEXT, attempts INTEGER DEFAULT 0, not_before REAL DEFAULT 0,
                    created REAL, error TEXT, seconds REAL);
                CREATE TABLE IF NOT EXISTS enqueue_cursor(
                    release TEXT PRIMARY KEY, position INTEGER);
                CREATE INDEX IF NOT EXISTS inference_queue
                    ON inference_jobs(state,not_before,created);
            """)

    def recover(self):
        with self.service.db() as db:
            db.execute("UPDATE inference_jobs SET state='queued' WHERE state='running'")

    def enqueue(self):
        release = active_model(self.service)
        if not release:
            return 0
        validate_model(self.service, release["model"])
        with self.service.db() as db:
            release_id = document_sha256(release)
            position = db.execute(
                "SELECT position FROM enqueue_cursor WHERE release=?", (release_id,)
            ).fetchone()
            features = db.execute(
                "SELECT rowid AS position,id,feature FROM motions "
                "WHERE rowid>? ORDER BY rowid LIMIT 1000",
                (position[0] if position else 0,),
            ).fetchall()
            count = 0
            for row in features:
                feature = json.loads(row["feature"])
                if feature["binding"]["feature_version"] != release["model"]["feature_version"]:
                    continue
                key = document_sha256({"release": release, "feature": feature["feature_id"]})
                cursor = db.execute(
                    "INSERT OR IGNORE INTO inference_jobs"
                    "(id,motion,release_json,feature_json,state,created) VALUES(?,?,?,?,?,?)",
                    (key, row["id"], json.dumps(release), row["feature"], "queued", time.time()),
                )
                count += cursor.rowcount
            if features:
                db.execute(
                    "INSERT OR REPLACE INTO enqueue_cursor VALUES(?,?)",
                    (release_id, features[-1]["position"]),
                )
        return count

    def infer(self, feature, model):
        data = checked_download(
            self.service, feature, self.cache / "features" / (feature["feature_id"] + ".npz")
        )
        model_dir = self.cache / "models" / document_sha256(model)
        onnx = checked_download(self.service, model["onnx"], model_dir / "model.onnx")
        metadata = json.loads(
            checked_download(
                self.service, model["metadata"], model_dir / "metadata.json"
            ).read_text()
        )
        require(
            len(metadata["labels"]) == 24 and set(metadata["labels"]) == set(PRIMARY),
            "Model labels differ",
        )
        with np.load(data, allow_pickle=False) as saved:
            require(
                json.loads(str(saved["binding"])) == feature["binding"],
                "Feature payload binding differs",
            )
            joints, valid = saved["joints"], saved["valid"]
        score_id = document_sha256(
            {
                "features": feature["sha256"],
                "model": model["onnx"]["sha256"],
                "labels": metadata["labels"],
            }
        )
        score_key = self.service.root + f"/score-manifests/{score_id}.json"
        cached, _ = self.service.optional(score_key)
        score_path = self.cache / "scores" / (score_id + ".npz")
        if cached:
            checked_download(self.service, cached, score_path)
            with np.load(score_path, allow_pickle=False) as saved:
                scores = saved["scores"]
        else:
            session = cpu_session(str(onnx))
            scores = predict_scores(
                lambda x, mask: session.run(None, {"joints_world_m": x, "valid": mask})[0],
                joints,
                valid,
            )
            score_path.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(score_path, scores=scores)
            score_object = self.service.root + f"/scores/{score_id}.npz"
            self.service.store.put_file(
                score_path,
                score_object,
                content_type="application/octet-stream",
                metadata={"sha256": sha(score_path)},
            )
            self.service.immutable(
                score_key,
                {
                    "object_key": score_object,
                    "sha256": sha(score_path),
                    "byte_length": score_path.stat().st_size,
                },
            )
        require(
            scores.shape == (feature["sample_count"], 24) and np.isfinite(scores).all(),
            "Invalid cached scores",
        )
        indices = reject(scores, joints, valid, metadata)
        doc = proposal(feature["template"], metadata["labels"], indices, model["model_id"])
        return {
            "schema": "imu.motion_prediction.v1",
            "document": doc,
            "model": model,
            "feature_id": feature["feature_id"],
            "scores_key": score_key,
        }

    def publish(self, mid, prediction, release):
        with self.service.publication_lock():
            return self._publish(mid, prediction, release)

    def _publish(self, mid, prediction, release):
        reference = self.service.reference("predictions", prediction)
        for _ in range(4):
            if active_model(self.service) != release:
                return "superseded"
            state, generation = self.service.state(mid)
            lease = state["lease"]
            if (
                lease
                and lease["expires_at"] > time.time()
                and not state["draft"]
                and not state["accepted"]
            ):
                # Freeze the visible automatic draft while a human is editing it.
                old = self.service.base_document(mid, state)
                state["draft"] = self.service.reference(
                    "drafts",
                    {
                        "schema": "imu.motion_draft.v1",
                        "document": old,
                        "author": "system-edit-protection",
                        "motion_sha256": mid,
                        "created_at_utc": now(),
                        "previous": None,
                    },
                )
            state["prediction"] = reference
            try:
                self.service.write_state(mid, state, generation)
                return "complete"
            except ObjectConflictError:
                continue
        raise ObjectConflictError("Motion review changed during prediction publication")

    def step(self):
        with self.service.db() as db:
            row = db.execute(
                "SELECT * FROM inference_jobs WHERE state='queued' AND not_before<=? "
                "ORDER BY created DESC LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE inference_jobs SET state='running',attempts=attempts+1 WHERE id=?",
                (row["id"],),
            )
        started = time.monotonic()
        error = None
        try:
            release, feature = json.loads(row["release_json"]), json.loads(row["feature_json"])
            if active_model(self.service) != release:
                state = "superseded"
            else:
                prediction = self.infer(feature, release["model"])
                state = self.publish(row["motion"], prediction, release)
        except Exception as exc:
            error = str(exc)
            state = "failed" if row["attempts"] + 1 >= 5 else "queued"
        with self.service.db() as db:
            db.execute(
                "UPDATE inference_jobs SET state=?,error=?,seconds=?,not_before=? WHERE id=?",
                (
                    state,
                    error,
                    time.monotonic() - started,
                    time.time() + min(300, 10 * 2 ** row["attempts"]) if error else 0,
                    row["id"],
                ),
            )
        return {"job_id": row["id"], "state": state, "error": error}


def main():
    from .config import load_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = load_settings(args.config)
    storage = settings.storage
    store = create_object_store(
        "gcs" if settings.annotation.synthetic_bucket else storage.backend,
        storage.root,
        settings.annotation.synthetic_bucket or storage.bucket,
        storage.project,
    )
    source = SyntheticReviewService(
        store,
        settings.annotation.synthetic_run_id,
        catalog_path=settings.annotation.catalog_path.with_name("synthetic.sqlite3"),
        target=settings.annotation.synthetic_target,
    )
    service = MotionLabelService(source)
    worker = MotionLabelWorker(service, settings.storage.cache_root / "motion-inference")
    with (worker.cache / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker.recover()
        scanned = 0.0
        while True:
            if time.monotonic() - scanned >= 10:
                service.refresh(force=True)
                worker.enqueue()
                scanned = time.monotonic()
            result = worker.step()
            if result:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.once:
                break
            if result is None:
                time.sleep(2)


if __name__ == "__main__":
    main()
