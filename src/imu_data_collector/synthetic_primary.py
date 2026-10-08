"""Primary learning jobs freeze development reviews; independent references never train."""

import hashlib

from fastapi import HTTPException, Request

from .storage import ObjectConflictError
from .synthetic_temporal_contract import IDENTIFIER, document_sha256, run_root


def register_primary_routes(app, service, current_actor):
    from .http_download import object_download_response
    from .synthetic_temporal import ReviewBody, now, primary_run

    base = "/api/v1/synthetic/temporal/runs/{run_id}"

    def invoke(function, *args):
        try:
            return function(*args)
        except FileNotFoundError as exc:
            raise HTTPException(404, "找不到记录") from exc
        except ObjectConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    def locate(run_id):
        run = service.get_run(run_id)
        if not primary_run(run):
            raise ValueError("This endpoint requires a primary-only run")
        return run

    def jobs(run):
        root = run_root(run) + "/training/"
        values = []
        for item in service.store.list(root + "requests/"):
            request = service.read(item.key)
            result = service.optional(root + "results/" + request["job_id"] + ".json")[0]
            running = service.optional(root + "running/" + request["job_id"] + ".json")[0]
            if result and result.get("request_sha256") != document_sha256(request):
                raise ValueError("Training result binding mismatch")
            values.append(
                {
                    "request": request,
                    "result": result,
                    "state": result["state"] if result else "running" if running else "queued",
                }
            )
        return sorted(values, key=lambda x: x["request"]["created_at_utc"], reverse=True)

    @app.post(base + "/cases/{cid}/{vid}/correction")
    def correct(run_id: str, cid: str, vid: str, body: ReviewBody, request: Request):
        actor = current_actor(request)
        run = invoke(locate, run_id)
        if body.corrected_document is None or body.decision != "pass":
            raise HTTPException(422, "请明确保存纠正后的主活动区间")
        entry = invoke(service.entry, run, cid, vid)
        return invoke(service.review, run, entry, body, actor.unikey)

    @app.get(base + "/training")
    def training(run_id: str, request: Request):
        current_actor(request)
        run = invoke(locate, run_id)
        ready = []
        for entry in run["candidates"]:
            pointer = service.pointer(run, entry)[0]
            if (
                entry["reference_role"] == "development"
                and pointer
                and pointer["decision"] == "pass"
            ):
                ready.append(pointer["revision_sha256"])
        result = invoke(jobs, run)
        used = {
            r["review_sha256"]
            for j in result
            if j["state"] == "complete"
            for r in j["request"]["reviews"]
        }
        return {
            "eligible_reviews": len(ready),
            "new_reviews": len(set(ready) - used),
            "jobs": result,
        }

    @app.post(base + "/training")
    def start_training(run_id: str, request: Request):
        actor = current_actor(request)
        if not actor.is_admin:
            raise HTTPException(403, "仅管理员可启动训练")
        run = invoke(locate, run_id)
        existing = invoke(jobs, run)
        pending = [j for j in existing if j["state"] in ("queued", "running")]
        if pending:
            return pending[0]["request"]
        reviews = []
        for entry in run["candidates"]:
            pointer = service.pointer(run, entry)[0]
            if (
                entry["reference_role"] == "development"
                and pointer
                and pointer["decision"] == "pass"
            ):
                reviews.append(
                    {
                        "candidate_id": entry["candidate"]["candidate_id"],
                        "review_key": pointer["revision_key"],
                        "review_sha256": pointer["revision_sha256"],
                    }
                )
        if not reviews:
            raise HTTPException(422, "先完成至少一个开发片段的人工确认或纠正")
        binding = {
            "run_sha256": document_sha256(run),
            "reviews": reviews,
            "recipe": "primary-tcn-v1",
        }
        same = [j for j in existing if all(j["request"].get(k) == v for k, v in binding.items())]
        completed = [j for j in same if j["state"] == "complete"]
        if completed:
            return completed[0]["request"]
        attempt = len(same)
        job_id = "train-" + document_sha256({**binding, "attempt": attempt})[:24]
        key = run_root(run) + "/training/requests/" + job_id + ".json"
        previous = service.optional(key)[0]
        value = previous or {
            "schema": "imu.primary_training_request.v1",
            "job_id": job_id,
            **binding,
            "attempt": attempt,
            "requested_by": actor.unikey,
            "created_at_utc": now(),
        }
        invoke(service.immutable, key, value)
        return value

    @app.get(base + "/models")
    def models(run_id: str, request: Request):
        current_actor(request)
        run = invoke(locate, run_id)
        return {
            "models": [
                service.read(i.key)
                for i in service.store.list(run_root(run) + "/models/")
                if i.key.endswith("/descriptor.json")
            ]
        }

    @app.get(base + "/models/{model_id}/{filename}")
    def download(run_id: str, model_id: str, filename: str, request: Request):
        current_actor(request)
        run = invoke(locate, run_id)
        if not IDENTIFIER.fullmatch(model_id) or filename not in (
            "model.onnx",
            "model-bundle.zip",
            "metadata.json",
        ):
            raise HTTPException(404, "找不到模型文件")
        root = run_root(run) + "/models/" + model_id
        descriptor = invoke(service.read, root + "/descriptor.json")
        file = descriptor["files"].get(filename)
        if not file or file["object_key"] != root + "/" + filename:
            raise HTTPException(404, "模型文件不匹配")
        info = service.store.stat(file["object_key"])
        if (
            info is None
            or info.size_bytes != file["byte_length"]
            or info.metadata.get("sha256") not in (None, file["sha256"])
        ):
            raise HTTPException(404, "模型文件缺失或已经变化")
        # Simulator local objects are immutable files without collector metadata sidecars.
        if info.metadata.get("sha256") is None:
            digest = hashlib.sha256()
            for start in range(0, info.size_bytes, 8 * 1024 * 1024):
                digest.update(
                    service.store.read_bytes(
                        info.key, start, min(info.size_bytes - 1, start + 8 * 1024 * 1024 - 1)
                    )
                )
            if digest.hexdigest() != file["sha256"]:
                raise HTTPException(404, "模型文件内容校验失败")
        return object_download_response(
            store=service.store,
            info=info,
            filename=filename,
            media_type="application/octet-stream",
            range_header=request.headers.get("range"),
            sha256=file["sha256"],
            label="骨架主活动模型",
        )
