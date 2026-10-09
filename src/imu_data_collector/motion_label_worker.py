"""Single CPU worker with durable jobs, content-addressed scores and CAS publication."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import signal
import time
from pathlib import Path

import numpy as np

from .motion_annotations import primary_prediction as proposal
from .motion_annotations import require
from .motion_label_batch import batch_policy
from .motion_label_models import active_model, validate_model
from .motion_label_runtime import (
    SCORE_RUNTIME_VERSION,
    SEQUENCE_RUNTIME_VERSION,
    cpu_session,
    impact_candidates,
    predict_outputs,
    reject,
    sequence_decode,
)
from .motion_label_service import MotionLabelService, now
from .production_gate import validated_capabilities
from .storage import ObjectConflictError, create_object_store
from .synthetic_motion import SyntheticReviewService
from .synthetic_temporal_contract import (
    CURRENT_PRIMARY,
    FALLS,
    PRIMARY,
    PRODUCTION_PRIMARY,
    document_sha256,
)


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
        held_out = self.service.holdout_groups()
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
                if feature.get("group_key") in held_out:
                    continue
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
            tuple(metadata["labels"]) in (PRIMARY, PRODUCTION_PRIMARY, CURRENT_PRIMARY),
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
                "score_runtime": SCORE_RUNTIME_VERSION,
            }
        )
        score_key = self.service.root + f"/score-manifests/{score_id}.json"
        cached, _ = self.service.optional(score_key)
        score_path = self.cache / "scores" / (score_id + ".npz")
        if cached:
            checked_download(self.service, cached, score_path)
            with np.load(score_path, allow_pickle=False) as saved:
                output = {k: saved[k] for k in saved.files}
        else:
            session = cpu_session(str(onnx))
            output = predict_outputs(session, joints, valid, metadata)
            score_path.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(score_path, **output)
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
        scores = output["scores"]
        require(
            scores.shape == (feature["sample_count"], len(metadata["labels"]))
            and np.isfinite(scores).all(),
            "Invalid cached scores",
        )
        quality = {}
        if metadata.get("decoder", {}).get("kind") == SEQUENCE_RUNTIME_VERSION:
            decoded_id = document_sha256(
                {
                    "scores": score_id,
                    "metadata": model["metadata"]["sha256"],
                    "runtime_sha256": sha(Path(__file__).with_name("motion_label_runtime.py")),
                }
            )
            decoded_key = self.service.root + f"/sequence-manifests/{decoded_id}.json"
            saved, _ = self.service.optional(decoded_key)
            decoded_path = self.cache / "sequences" / (decoded_id + ".npz")
            if saved:
                checked_download(self.service, saved, decoded_path)
                with np.load(decoded_path, allow_pickle=False) as archive:
                    decoded = {key: archive[key] for key in archive.files}
            else:
                decoded = sequence_decode(
                    scores,
                    valid,
                    metadata,
                    outside_scores=output.get("outside_scores")
                    if metadata.get("outside_supported")
                    else None,
                )
                decoded_path.parent.mkdir(parents=True, exist_ok=True)
                np.savez_compressed(decoded_path, **decoded)
                object_key = self.service.root + f"/sequences/{decoded_id}.npz"
                self.service.store.put_file(
                    decoded_path,
                    object_key,
                    content_type="application/octet-stream",
                    metadata={"sha256": sha(decoded_path)},
                )
                self.service.immutable(
                    decoded_key,
                    {
                        "object_key": object_key,
                        "sha256": sha(decoded_path),
                        "byte_length": decoded_path.stat().st_size,
                        "raw_scores_key": score_key,
                        "metadata_sha256": model["metadata"]["sha256"],
                    },
                )
            indices = decoded["class_indices"]
            impact_scores = (
                output.get("impact_scores") if metadata.get("impact_supported") else None
            )
            impacts = impact_candidates(
                indices, impact_scores, metadata["labels"], metadata["decoder"]["impact_threshold"]
            )
            capabilities = validated_capabilities(model.get("validation") or {})
            fall_validated = capabilities["fall"]["passed"]
            high = decoded["high_confidence_indices"].copy()
            if not fall_validated:
                high[
                    np.isin(high, [i for i, code in enumerate(metadata["labels"]) if code in FALLS])
                ] = -1
            high_impacts = impact_candidates(
                high, impact_scores, metadata["labels"], metadata["decoder"]["impact_threshold"]
            )
            high_doc = proposal(
                feature["template"],
                metadata["labels"],
                high,
                model["model_id"],
                impacts=high_impacts,
            )
            doc = proposal(
                feature["template"], metadata["labels"], indices, model["model_id"], impacts=impacts
            )
            intervals = []
            for s in doc["segments"]:
                a, b = s["start_ns"] // 40_000_000, s["stop_ns"] // 40_000_000
                fraction = float((high[a:b] >= 0).mean())
                intervals.append(
                    {
                        "start_ns": s["start_ns"],
                        "stop_ns": s["stop_ns"],
                        "primary_code": s["primary_code"],
                        "score": float(decoded["reliability"][a:b].mean()),
                        "high_confidence_fraction": fraction,
                        "boundary_reliable": False,
                        "reason": "experimental_fall"
                        if s["primary_code"] == "fall" and not fall_validated
                        else "boundary_not_validated"
                        if fraction == 1
                        else "low_confidence_candidate",
                    }
                )
            quality = {
                "schema": "imu.motion_prediction_quality.v2",
                "intervals": intervals,
                "capabilities": capabilities,
                "calibration": metadata["decoder"].get("calibration"),
                "high_confidence_document": high_doc,
                "decoder_sha256": document_sha256(metadata["decoder"]),
                "runtime_version": SEQUENCE_RUNTIME_VERSION,
                "sequence_scores_key": decoded_key,
            }
        else:
            indices = reject(scores, joints, valid, metadata)
            doc = proposal(feature["template"], metadata["labels"], indices, model["model_id"])
        return {
            "schema": "imu.motion_prediction.v1",
            "document": doc,
            "model": model,
            "feature_id": feature["feature_id"],
            "scores_key": score_key,
            **({"quality": quality} if quality else {}),
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

    def step(self, *, policy=None):
        if policy and policy.get("deadline_unix", float("inf")) <= time.time():
            return None
        clause, args = "", []
        if policy and policy.get("motion_sha256"):
            allowed = policy["motion_sha256"]
            clause = " AND motion IN (" + ",".join("?" for _ in allowed) + ")"
            args.extend(allowed)
        with self.service.db() as db:
            row = db.execute(
                "SELECT * FROM inference_jobs WHERE state='queued' AND not_before<=? "
                + clause
                + " ORDER BY created DESC LIMIT 1",
                (time.time(), *args),
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
            elif feature.get("group_key") in self.service.holdout_groups():
                state = "protected"
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
            release = active_model(service)
            try:
                policy = batch_policy(service, release)
            except ValueError as exc:
                policy = None
                print(
                    json.dumps({"state": "paused", "reason": str(exc)}, ensure_ascii=False),
                    flush=True,
                )
            # SIGALRM ends this worker even inside native inference or network I/O.
            # Durable running jobs return to queued on the next authorized run.
            deadline = policy.get("deadline_unix") if policy else None
            signal.setitimer(
                signal.ITIMER_REAL, max(0.001, deadline - time.time()) if deadline else 0
            )
            if time.monotonic() - scanned >= 10:
                # Data discovery remains available while inference is paused.
                service.refresh(force=True)
                if policy:
                    worker.enqueue()
                scanned = time.monotonic()
            if not policy:
                if args.once:
                    break
                time.sleep(5)
                continue
            result = worker.step(policy=policy)
            if result:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.once:
                break
            if result is None:
                time.sleep(2)


if __name__ == "__main__":
    main()
