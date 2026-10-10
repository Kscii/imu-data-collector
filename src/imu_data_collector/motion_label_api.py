"""Production routes; experimental routes remain a separate admin-only domain."""

from fastapi import HTTPException, Request

from .motion_label_batch import configure_batch
from .motion_label_models import active_model, list_models, promote_model
from .motion_label_previews import MotionLabelPreviews
from .motion_label_service import MotionLabelService
from .storage import ObjectConflictError
from .synthetic_temporal_contract import (
    STANDING_TAXONOMY_VERSION as CURRENT_TAXONOMY_VERSION,
    AnnotationIssues,
    activity_rules,
    document_sha256,
)


def register_motion_label_routes(app, source, current_actor):
    service = MotionLabelService(source)
    app.state.motion_label_service = service
    root = "/api/v1/synthetic/motions"

    def invoke(call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except ObjectConflictError as error:
            raise HTTPException(409, str(error)) from error
        except FileNotFoundError as error:
            raise HTTPException(404, "找不到动捕或标注记录") from error
        except AnnotationIssues as error:
            raise HTTPException(
                422,
                {"code": "annotation_issues", "message": str(error), "issues": error.issues},
            ) from error
        except (ValueError, KeyError, TypeError) as error:
            raise HTTPException(422, str(error)) from error

    def admin(request):
        actor = current_actor(request)
        if not actor.is_admin:
            raise HTTPException(403, "仅管理员可管理自动标注模型")
        return actor

    previews = MotionLabelPreviews(service)

    @app.get(root + "/activity-rules")
    def production_activity_rules(request: Request, version: str = CURRENT_TAXONOMY_VERSION):
        current_actor(request)
        return invoke(activity_rules, version)

    @app.get(root + "/previews")
    def preview_list(request: Request):
        admin(request)
        return invoke(previews.previews)

    @app.get(root + "/previews/{preview_id}")
    def preview_cases(preview_id: str, request: Request):
        admin(request)
        return invoke(previews.listing, preview_id)

    @app.get(root + "/previews/{preview_id}/{mid}")
    def preview_case(preview_id: str, mid: str, request: Request):
        admin(request)
        return invoke(previews.case, preview_id, mid)

    @app.get(root)
    def listing(
        request: Request, offset: int = 0, limit: int = 30, status: str = "all", search: str = ""
    ):
        current_actor(request)
        return invoke(service.listing, offset=offset, limit=limit, status=status, search=search)

    @app.get(root + "/models")
    def models(request: Request):
        admin(request)
        active = invoke(active_model, service)
        with service.db() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='inference_jobs'"
            ).fetchone()
            jobs = (
                [
                    dict(r)
                    for r in db.execute(
                        "SELECT state,count(*) AS count FROM inference_jobs GROUP BY state"
                    )
                ]
                if exists
                else []
            )
            discovery_errors = (
                db.execute("SELECT count(*) FROM discovery_errors").fetchone()[0]
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='discovery_errors'"
                ).fetchone()
                else 0
            )
            timing = (
                dict(
                    db.execute(
                        "SELECT count(*) AS completed,coalesce(sum(seconds),0) AS seconds,"
                        "coalesce(avg(seconds),0) AS mean_seconds "
                        "FROM inference_jobs WHERE state='complete'"
                    ).fetchone()
                )
                if exists
                else {"completed": 0, "seconds": 0, "mean_seconds": 0}
            )
        control = service.optional(service.root + "/batch-control.json")[0]
        return {
            "models": invoke(list_models, service),
            "batch_control": control,
            "batch_control_sha256": document_sha256(control) if control else None,
            "active": active,
            "active_sha256": document_sha256(active) if active else None,
            "jobs": jobs,
            "timing": timing,
            "discovery_errors": discovery_errors,
        }

    @app.get(root + "/summary")
    def summary(request: Request):
        current_actor(request)
        clause, args = service.visible_clause()
        with service.db() as db:
            counts = dict(
                db.execute(
                    "SELECT status,count(*) FROM motions WHERE " + clause + " GROUP BY status", args
                ).fetchall()
            )
            eligible = db.execute(
                "SELECT count(*) FROM motions WHERE "
                + clause
                + " AND status='accepted' AND accepted_usable_ns>0",
                args,
            ).fetchone()[0]
        return {
            "published": sum(counts.values()),
            "passed": counts.get("accepted", 0),
            "unreviewed": counts.get("pending_review", 0),
            "rejected": counts.get("rejected", 0),
            "label_pending": counts.get("inference_pending", 0),
            "snapshot_eligible": eligible,
            "latest_published_at_utc": None,
        }

    @app.post(root + "/models/retry-failed")
    def retry_failed(request: Request):
        admin(request)
        with service.db() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='inference_jobs'").fetchone():
                db.execute(
                    "UPDATE inference_jobs SET state='queued',attempts=0,not_before=0 "
                    "WHERE state='failed'"
                )
        return {"queued": True}

    @app.post(root + "/models/{model_id}/activate")
    def activate(model_id: str, body: dict, request: Request):
        actor = admin(request)
        return invoke(
            promote_model, service, model_id, actor.unikey, body.get("expected_active_sha256")
        )

    @app.post(root + "/batch")
    def update_batch(body: dict, request: Request):
        actor = admin(request)
        return invoke(
            configure_batch,
            service,
            body["control"],
            body.get("expected_control_sha256"),
            actor.unikey,
        )

    @app.get(root + "/configurations")
    def configurations(request: Request):
        current_actor(request)
        with service.db() as db:
            import json

            values = [json.loads(r[0]) for r in db.execute("SELECT value FROM variants")]
        unique = {v["configuration_id"]: v["configuration"] for v in values}
        return {"configurations": [{"configuration_id": k, **v} for k, v in sorted(unique.items())]}

    @app.get(root + "/snapshots")
    def snapshots(request: Request):
        current_actor(request)
        result = []
        for item in service.store.list(source.prefix + "/snapshots/requests/"):
            sid = item.key.rsplit("/", 1)[-1].removesuffix(".json")
            value = source.snapshot(sid)
            result.append(
                {
                    "snapshot_id": sid,
                    "candidate_count": len(value["request"]["entries"]),
                    "state": value["result"].get("state", "complete")
                    if value["result"]
                    else "queued",
                    "result": value["result"],
                }
            )
        return {"snapshots": result}

    @app.post(root + "/snapshots")
    def snapshot(body: dict, request: Request):
        actor = current_actor(request)
        return invoke(service.create_snapshot, actor.unikey, body.get("configuration_id"))

    @app.get(root + "/lookup/{cid}/{vid}")
    def lookup(cid: str, vid: str, request: Request):
        current_actor(request)
        with service.db() as db:
            row = db.execute(
                "SELECT motion FROM variants WHERE id=?", (cid + "/" + vid,)
            ).fetchone()
        if row is None:
            raise HTTPException(404, "该 IMU 版本尚未关联动捕标注，请等待后台登记")
        invoke(service.check_holdout, row[0])
        return {"motion_sha256": row[0]}

    @app.get(root + "/{mid}")
    def detail(mid: str, request: Request):
        current_actor(request)
        return invoke(service.case, mid)

    @app.post(root + "/{mid}/claim")
    def claim(mid: str, body: dict, request: Request):
        actor = current_actor(request)
        return invoke(service.claim, mid, actor.unikey, body.get("token"))

    @app.post(root + "/{mid}/release")
    def release(mid: str, body: dict, request: Request):
        actor = current_actor(request)
        return invoke(service.release, mid, actor.unikey, body.get("token"))

    variant_path = root + "/{mid}/variants/{cid}/{vid}"

    @app.get(variant_path)
    def variant(mid: str, cid: str, vid: str, request: Request):
        current_actor(request)
        value = invoke(service.variant, mid, cid, vid)
        if value is None:
            raise HTTPException(404, "找不到 IMU 版本")
        return invoke(service.case, mid, value)

    @app.post(variant_path + "/draft")
    def draft(mid: str, cid: str, vid: str, body: dict, request: Request):
        actor = current_actor(request)
        return invoke(service.edit, mid, cid, vid, body, actor.unikey)

    @app.post(variant_path + "/review")
    def review(mid: str, cid: str, vid: str, body: dict, request: Request):
        actor = current_actor(request)
        if body.get("decision") not in ("pass", "reject"):
            raise HTTPException(422, "请明确选择通过或拒绝")
        return invoke(
            service.edit, mid, cid, vid, body, actor.unikey, decision=body.get("decision")
        )
