"""Platform-authorized browsing of archived external history; no browser upstream access."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from imu_data_collector import external_device_insights as insights
from imu_data_collector.auth import Actor
from imu_data_collector.config import Settings
from imu_data_collector.external_device_domain import PREFIX, now, timestamp, utc
from imu_data_collector.external_device_runtime import ExternalDeviceRuntime
from imu_data_collector.http_download import object_download_response
from imu_data_collector.storage import ObjectStore


class ExportRequest(BaseModel):
    scope: Literal["device", "kind"] | None = None
    device_id: str | None = None
    kind: Literal["radar-watch", "mattress"] | None = None
    start: str | None = None
    end: str | None = None


class HistorySettingsRequest(BaseModel):
    history_start: str


class RebuildRequest(BaseModel):
    confirmed: bool = False
    history_start: str


class HistoryOriginRequest(BaseModel):
    start: str
    confirmed_by_provider: bool


def interval(start: str, end: str) -> tuple[str, str]:
    try:
        start, end = utc(timestamp(start)), utc(timestamp(end))
        if start >= end:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(422, "请输入带时区且开始早于结束的时间范围") from None
    return start, end


def register_external_devices(
    app: FastAPI, settings: Settings, store: ObjectStore, current_actor: Callable[[Request], Actor]
) -> None:
    if not settings.external_devices.enabled:
        return
    service = ExternalDeviceRuntime(settings, store)
    app.state.external_device_service = service

    @app.middleware("http")
    async def pin_external_generation(request, call_next):
        if not request.url.path.startswith("/api/v1/external-devices"):
            return await call_next(request)
        token = service.bound.set(service.service(service.generation))
        try:
            return await call_next(request)
        finally:
            service.bound.reset(token)

    def viewer(request: Request) -> Actor:
        actor = current_actor(request)
        if not actor.is_admin and settings.external_devices.access != "members":
            raise HTTPException(403, "外部设备数据暂时仅向管理员开放")
        return actor

    def admin(request: Request) -> Actor:
        actor = viewer(request)
        if not actor.is_admin:
            raise HTTPException(403, "同步管理仅限管理员")
        return actor

    def device(identifier: str) -> dict:
        try:
            return service.catalog.device(identifier)
        except KeyError:
            raise HTTPException(404, "找不到该外部设备") from None

    def public_export(job: dict) -> dict:
        result = {
            key: job.get(key)
            for key in (
                "id",
                "device_id",
                "start",
                "end",
                "state",
                "created_at",
                "size_bytes",
                "error",
            )
        }
        result["scope"] = json.loads(job["scope_json"]) if job.get("scope_json") else None
        return result

    router = APIRouter(prefix="/api/v1/external-devices", dependencies=[Depends(viewer)])

    @router.get("/status")
    def status():
        return service.status()

    @router.get("/devices")
    def devices():
        return {"devices": service.devices(), "generation": service.current.generation}

    @router.get("/devices/{identifier}/calendar")
    def calendar(
        identifier: str, year: int = Query(ge=1000, le=9998), month: int = Query(ge=1, le=12)
    ):
        device(identifier)
        return service.calendar(identifier, year, month)

    @router.get("/devices/{identifier}/records")
    def records(
        identifier: str,
        start: str,
        end: str,
        limit: int = Query(100, ge=1, le=500),
        cursor: str | None = None,
        message: str | None = None,
    ):
        device(identifier)
        start, end = interval(start, end)
        before = None
        if cursor:
            try:
                before = json.loads(base64.urlsafe_b64decode(cursor))
                if (
                    not isinstance(before, list)
                    or len(before) != 2
                    or not isinstance(before[0], str)
                    or not isinstance(before[1], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", before[1])
                ):
                    raise ValueError
                before[0] = utc(timestamp(before[0]))
            except (ValueError, TypeError, KeyError, OverflowError):
                raise HTTPException(422, "无效分页游标") from None
        rows = service.catalog.records(
            identifier,
            start,
            end,
            limit=limit + 1,
            before=tuple(before) if before else None,
            message=message,
        )
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps([rows[-1]["received_at"], rows[-1]["id"]]).encode()
            ).decode()
            if more
            else None
        )
        counts = service.catalog.rows(
            """SELECT message_type,COUNT(*) AS count,SUM(synthetic) AS synthetic
            FROM records WHERE device_id=? AND received_at>=? AND received_at<?
            GROUP BY message_type""",
            (identifier, start, end),
        )
        additions_ready = insights.preparation(service.catalog, identifier)["ready"]
        return {
            "records": [
                service.public_record(row, additions_ready=additions_ready) for row in rows
            ],
            "next_cursor": next_cursor,
            "types": counts,
            "total": sum(row["count"] for row in counts),
            "coverage": service.catalog.coverage(identifier, start, end),
        }

    @router.get("/devices/{identifier}/records/{rid}")
    def record(identifier: str, rid: str):
        device(identifier)
        rows = service.catalog.rows(
            "SELECT * FROM records WHERE device_id=? AND id=?", (identifier, rid)
        )
        if not rows:
            raise HTTPException(404, "找不到该记录")
        try:
            original = service.original(rows[0])
        except (OSError, ValueError, KeyError):
            raise HTTPException(503, "原始归档暂不可用或校验失败") from None
        return {
            "record_json": json.dumps(original, ensure_ascii=False, indent=2),
            "raw_payload": original.get("rawPayload"),
            "versions": service.catalog.rows(
                "SELECT hash,fetched_at FROM versions WHERE record_id=? ORDER BY fetched_at", (rid,)
            ),
        }

    @router.get("/devices/{identifier}/series")
    def series(
        identifier: str,
        start: str,
        end: str,
        metric: str | None = None,
        point_budget: int = Query(800, ge=100, le=2000),
    ):
        device(identifier)
        start, end = interval(start, end)
        if metric is not None:
            if metric not in {
                item["key"] for item in service.metric_metadata(identifier)["metrics"]
            }:
                raise HTTPException(422, "该设备没有此指标")
            try:
                return service.aggregate_series(identifier, metric, start, end, point_budget)
            except RuntimeError:
                raise HTTPException(503, "正在准备统计索引，请稍后重试") from None
        return service.series(identifier, start, end)

    @router.get("/devices/{identifier}/metrics")
    def metrics(identifier: str):
        device(identifier)
        return service.metric_metadata(identifier)

    @router.get("/devices/{identifier}/fields")
    def fields(identifier: str):
        device(identifier)
        return insights.fields(service.catalog, identifier)

    def insight_page(identifier, category, start, end, limit, cursor, valid=None):
        device(identifier)
        start, end = interval(start, end)
        before = None
        if cursor:
            try:
                before = json.loads(base64.urlsafe_b64decode(cursor))
                if (
                    not isinstance(before, list)
                    or len(before) != 2
                    or not isinstance(before[1], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", before[1])
                ):
                    raise ValueError
                before[0] = utc(timestamp(before[0]))
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                raise HTTPException(422, "无效分页游标") from None
        result = insights.entries(
            service.catalog, identifier, category, start, end, limit, before, valid
        )
        next_page = result.pop("next")
        result["next_cursor"] = (
            base64.urlsafe_b64encode(json.dumps(next_page).encode()).decode() if next_page else None
        )
        return result

    @router.get("/devices/{identifier}/reports")
    def reports(
        identifier: str,
        start: str = "1000-01-01T00:00:00.000Z",
        end: str = "9998-01-01T00:00:00.000Z",
        limit: int = Query(30, ge=1, le=100),
        cursor: str | None = None,
        valid: bool | None = None,
    ):
        return insight_page(identifier, "reports", start, end, limit, cursor, valid)

    @router.get("/devices/{identifier}/reports/{rid}")
    def report(identifier: str, rid: str):
        device(identifier)
        if not insights.preparation(service.catalog, identifier)["ready"]:
            raise HTTPException(503, "正在准备报告索引")
        rows = service.catalog.rows(
            "SELECT * FROM insight_reports WHERE device_id=? AND id=?", (identifier, rid)
        )
        if not rows:
            raise HTTPException(404, "找不到该睡眠报告")
        return {**rows[0], "summary": json.loads(rows[0]["summary"])}

    @router.get("/devices/{identifier}/events")
    def events(
        identifier: str, start: str, end: str, point_budget: int = Query(800, ge=100, le=2000)
    ):
        device(identifier)
        start, end = interval(start, end)
        return insights.events(service.catalog, identifier, start, end, point_budget)

    @router.get("/devices/{identifier}/events/records")
    def event_records(
        identifier: str,
        start: str,
        end: str,
        limit: int = Query(100, ge=1, le=500),
        cursor: str | None = None,
    ):
        return insight_page(identifier, "events", start, end, limit, cursor)

    def enqueue(mode, expected_start=None):
        try:
            return service.public_task(
                service.request_task(mode, require_online=True, expected_start=expected_start)
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from None

    @router.post("/sync", status_code=202, dependencies=[Depends(admin)])
    def sync():
        return enqueue("sync")

    @router.post("/rebuild", status_code=202, dependencies=[Depends(admin)])
    def rebuild(body: RebuildRequest):
        if not body.confirmed:
            raise HTTPException(422, "请确认重建范围和保留旧库备份后再执行")
        start, _ = interval(body.history_start, now())
        if start != service.history_start():
            raise HTTPException(409, "历史起点已改变，请重新确认范围")
        return enqueue("rebuild", expected_start=start)

    @router.put("/settings", dependencies=[Depends(admin)])
    def settings_update(body: HistorySettingsRequest):
        start, _ = interval(body.history_start, now())
        try:
            return service.set_history_start(start)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @router.post("/history", status_code=202, dependencies=[Depends(admin)])
    def history(body: HistoryOriginRequest):
        if not body.confirmed_by_provider:
            raise HTTPException(422, "历史起点需要由接口方确认")
        start, _ = interval(body.start, now())
        try:
            service.set_history_start(start, source="provider")
            service.catalog.confirm_history(start)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return {"state": "queued", "history_start": start}

    @router.post("/exports", status_code=202)
    def create_export(body: ExportRequest):
        if body.scope:
            if body.start is not None or body.end is not None:
                raise HTTPException(422, "全量下载不接受时间范围")
            if body.scope == "device" and body.device_id and body.kind is None:
                device(body.device_id)
                return public_export(service.catalog.full_export_job(device=body.device_id))
            if body.scope == "kind" and body.kind and body.device_id is None:
                try:
                    return public_export(service.catalog.full_export_job(kind=body.kind))
                except KeyError:
                    raise HTTPException(404, "该类型没有已保存设备") from None
            raise HTTPException(422, "请选择单个设备或设备类型")
        if not body.device_id or not body.start or not body.end or body.kind:
            raise HTTPException(422, "时间范围下载需要设备及起止时间")
        device(body.device_id)
        start, end = interval(body.start, body.end)
        return public_export(service.catalog.export_job(body.device_id, start, end))

    @router.get("/exports/{identifier}")
    def export_status(identifier: str):
        try:
            _, job = service.export_job(identifier)
        except KeyError:
            raise HTTPException(404, "找不到下载任务") from None
        return public_export(job)

    @router.get("/exports")
    def exports():
        return {
            "exports": [
                public_export(row)
                for _, row in sorted(
                    service.export_rows(), key=lambda pair: pair[1]["created_at"], reverse=True
                )[:30]
            ]
        }

    @router.get("/exports/{identifier}/download")
    def download(identifier: str, request: Request):
        try:
            _, job = service.export_job(identifier)
        except KeyError:
            raise HTTPException(404, "找不到下载任务") from None
        if job["state"] != "ready":
            raise HTTPException(409, "下载包尚未准备好")
        expected = f"{PREFIX}/exports/{identifier}.zip"
        if job["object_key"] != expected:
            raise HTTPException(503, "下载对象校验失败")
        info = store.stat(expected)
        if (
            not info
            or info.size_bytes != job["size_bytes"]
            or info.metadata.get("sha256") != job["sha256"]
        ):
            raise HTTPException(503, "下载对象校验失败")
        return object_download_response(
            store=store,
            info=info,
            filename=f"external-data-{identifier}.zip",
            media_type="application/zip",
            sha256=job["sha256"],
            range_header=request.headers.get("range"),
        )

    app.include_router(router)
