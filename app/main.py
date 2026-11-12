"""FastAPI entry point: integrity console, JSON API and demo site."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import db, demo
from .crawler import (
    MAX_CATALOG_PAGES,
    Anomaly,
    RunOutcome,
    USER_AGENT,
    verify_catalog,
)
from .html_ui import error_page, result_page, verify_page

FETCH_TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "20"))
CONNECT_TIMEOUT = min(8.0, FETCH_TIMEOUT)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _default_catalog_url(request: Request) -> str:
    return f"{_base_url(request)}/demo/catalog"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="市政招标公告完整性核对",
    description="两次遍历分页目录并核对详情编号，完整清单原子保存到 SQLite。",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(demo.router)


async def _execute_check(catalog_url: str) -> tuple[RunOutcome, int]:
    timeout = httpx.Timeout(FETCH_TIMEOUT, connect=CONNECT_TIMEOUT)
    headers = {"User-Agent": USER_AGENT}
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=headers
        ) as client:
            outcome = await verify_catalog(
                catalog_url, client=client, max_pages=MAX_CATALOG_PAGES
            )
    except Exception as exc:  # keep the run record even if the crawler explodes
        outcome = RunOutcome(
            catalog_url=catalog_url,
            started_at=_now(),
            finished_at=_now(),
            status="failed",
            message=f"核对过程发生内部错误：{exc.__class__.__name__}: {exc}",
            anomalies=[
                Anomaly(
                    kind="internal_error",
                    message=f"核对过程发生内部错误：{exc.__class__.__name__}: {exc}",
                    url=catalog_url,
                )
            ],
        )
    run_id = await asyncio.to_thread(db.persist_outcome, outcome)
    return outcome, run_id


def _run_payload(run_id: int) -> dict:
    run = db.get_run(run_id)
    if run is None:
        return {}
    items = db.get_run_items(run_id)
    anomalies = db.get_run_anomalies(run_id)
    current_snapshot = None
    previous_snapshot = None
    if run["status"] == "complete" and run.get("snapshot_id"):
        current_snapshot = db.get_snapshot(int(run["snapshot_id"]))
    else:
        previous_snapshot = db.get_latest_snapshot(run["catalog_url"])
    return {
        "run": run,
        "items": items,
        "anomalies": anomalies,
        "snapshot": db.compact_snapshot(current_snapshot),
        "previous_snapshot": db.compact_snapshot(previous_snapshot),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, url: str | None = None) -> HTMLResponse:
    default_url = (url or "").strip() or _default_catalog_url(request)
    return HTMLResponse(
        verify_page(
            default_url=default_url,
            demo_url=f"{_base_url(request)}/demo/",
        )
    )


@app.post("/check")
async def check_form(request: Request):
    try:
        body = await request.body()
        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type:
            fields = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
            catalog_url = fields.get("catalog_url", [""])[0].strip()
        else:
            # The page's own form is urlencoded; accept a simple plain body too.
            catalog_url = body.decode("utf-8", errors="replace").strip()
    except Exception:
        return HTMLResponse(
            verify_page(
                default_url=_default_catalog_url(request),
                demo_url=f"{_base_url(request)}/demo/",
                error="无法读取表单内容",
            ),
            status_code=400,
        )
    if not catalog_url:
        return HTMLResponse(
            verify_page(
                default_url=_default_catalog_url(request),
                demo_url=f"{_base_url(request)}/demo/",
                error="请输入目录 URL",
            ),
            status_code=400,
        )
    _outcome, run_id = await _execute_check(catalog_url)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_result(run_id: int) -> HTMLResponse:
    run = db.get_run(run_id)
    if run is None:
        return HTMLResponse(error_page("找不到该轮次", f"轮次 #{run_id} 不存在"), status_code=404)

    items = db.get_run_items(run_id)
    anomalies = db.get_run_anomalies(run_id)
    current_snapshot = None
    previous_snapshot = None
    if run["status"] == "complete" and run.get("snapshot_id"):
        current_snapshot = db.get_snapshot(int(run["snapshot_id"]))
    else:
        previous_snapshot = db.get_latest_snapshot(run["catalog_url"])
    return HTMLResponse(
        result_page(
            run=run,
            items=items,
            anomalies=anomalies,
            current_snapshot=current_snapshot,
            previous_snapshot=previous_snapshot,
        )
    )


@app.post("/api/check")
async def api_check(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是 JSON，例如 {\"catalog_url\": \"...\"}"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "请求体必须是 JSON 对象"}, status_code=400)
    catalog_url = str(payload.get("catalog_url", "")).strip()
    if not catalog_url:
        return JSONResponse({"error": "缺少 catalog_url"}, status_code=400)

    outcome, run_id = await _execute_check(catalog_url)
    data = _run_payload(run_id)
    return JSONResponse(
        {
            "run_id": run_id,
            "status": outcome.status,
            "message": outcome.message,
            "page_count": len(outcome.pass1_pages),
            "item_count": len(outcome.items),
            "snapshot": data.get("snapshot"),
            "previous_snapshot": data.get("previous_snapshot"),
            "anomalies": data.get("anomalies", []),
            "items": data.get("items", []),
        }
    )


@app.get("/api/runs/{run_id}")
async def api_run(run_id: int) -> JSONResponse:
    payload = _run_payload(run_id)
    if not payload:
        return JSONResponse({"error": f"轮次 #{run_id} 不存在"}, status_code=404)
    return JSONResponse(payload)


@app.get("/api/snapshots/latest")
async def api_latest_snapshot(catalog_url: str) -> JSONResponse:
    snapshot = db.get_latest_snapshot(catalog_url.strip())
    if snapshot is None:
        return JSONResponse({"error": "没有该目录的完整清单"}, status_code=404)
    return JSONResponse(db.compact_snapshot(snapshot))


@app.get("/api/snapshots/{snapshot_id}")
async def api_snapshot(snapshot_id: int) -> JSONResponse:
    snapshot = db.get_snapshot(snapshot_id)
    if snapshot is None:
        return JSONResponse({"error": f"快照 #{snapshot_id} 不存在"}, status_code=404)
    return JSONResponse(db.compact_snapshot(snapshot))


@app.get("/healthz")
async def healthz() -> JSONResponse:
    try:
        db.init_db()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True})
