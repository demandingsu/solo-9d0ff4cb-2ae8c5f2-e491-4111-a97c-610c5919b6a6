"""Offline, reproducible example municipal-procurement site.

It lives in the same container as the checker so ``docker compose up`` gives a
fully offline demo.  Every scenario is deterministic; the "目录在两轮之间
更新" scenario flips after the first full walk (three catalog page requests).
"""

from __future__ import annotations

import math
import threading
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .html_ui import esc, layout

router = APIRouter(prefix="/demo", tags=["demo"])

PAGE_SIZE = 5

BASE_NOTICES: list[tuple[str, str, str]] = [
    ("SZ-2026-0001", "滨江路道路改造工程招标公告", "2026-11-01"),
    ("SZ-2026-0002", "老城区雨污分流管网施工招标公告", "2026-11-02"),
    ("SZ-2026-0003", "人民广场周边照明提升工程招标公告", "2026-11-03"),
    ("SZ-2026-0004", "高铁站北广场绿化工程招标公告", "2026-11-04"),
    ("SZ-2026-0005", "学府路慢行系统改造招标公告", "2026-11-05"),
    ("SZ-2026-0006", "第三污水处理厂设备采购招标公告", "2026-11-06"),
    ("SZ-2026-0007", "市政道路桥梁检测服务招标公告", "2026-11-07"),
    ("SZ-2026-0008", "城东片区排水泵站新建工程招标公告", "2026-11-08"),
    ("SZ-2026-0009", "中心城区公厕提升改造招标公告", "2026-11-09"),
    ("SZ-2026-0010", "沿江步道护栏更新工程招标公告", "2026-11-10"),
    ("SZ-2026-0011", "工业园区道路标线工程招标公告", "2026-11-11"),
    ("SZ-2026-0012", "城市道路窨井盖专项整治招标公告", "2026-11-12"),
]

SHIFT_NEW_NOTICE = (
    "SZ-2026-0100",
    "老城区排水管网应急抢修工程招标公告",
    "2026-11-12",
)

NOTICE_MAP: dict[str, tuple[str, str]] = {
    notice_id: (title, published) for notice_id, title, published in BASE_NOTICES
}
NOTICE_MAP[SHIFT_NEW_NOTICE[0]] = (SHIFT_NEW_NOTICE[1], SHIFT_NEW_NOTICE[2])

SCENARIOS: dict[str, dict[str, str]] = {
    "stable": {
        "name": "稳定目录",
        "tag": "正常",
        "description": "3 页共 12 条公告，两轮遍历完全一致，详情编号也一致。预期核对通过并保存完整清单。",
    },
    "shift": {
        "name": "翻页期间更新",
        "tag": "目录变化",
        "description": "第一轮读完 3 页后目录头部插入 1 条新公告，第二轮内容整体后移。预期两次目录不一致、待复查。",
    },
    "duplicate": {
        "name": "跨页重复编号",
        "tag": "重复",
        "description": "编号 SZ-2026-0003 同时出现在第 1 页和第 2 页。预期报告跨页重复、待复查。",
    },
    "mismatch": {
        "name": "详情编号错配",
        "tag": "详情异常",
        "description": "列表中的 SZ-2026-0002 打开详情后显示为 SZ-2026-8888。预期报告详情错配、待复查。",
    },
    "detail_fail": {
        "name": "详情读取失败",
        "tag": "抓取失败",
        "description": "公告 SZ-2026-0007 的详情页返回 404。预期报告详情读取失败、待复查。",
    },
}

SHIFT_AFTER_CATALOG_REQUESTS = math.ceil(len(BASE_NOTICES) / PAGE_SIZE)

_lock = threading.Lock()
_state: dict[str, Any] = {
    "scenario": "stable",
    "counters": {name: 0 for name in SCENARIOS},
    "shift_generation": "old",
}


def _reset_state(scenario: str | None = None) -> None:
    with _lock:
        if scenario in SCENARIOS:
            _state["scenario"] = scenario
        _state["counters"] = {name: 0 for name in SCENARIOS}
        _state["shift_generation"] = "old"


def _current_scenario(requested: str | None) -> str:
    with _lock:
        if requested in SCENARIOS:
            return requested
        return _state["scenario"]


def _shift_generation_for_request(scenario: str) -> str:
    """Deterministically flip after the first complete catalog walk."""
    if scenario != "shift":
        return "old"
    with _lock:
        count = _state["counters"].get("shift", 0) + 1
        _state["counters"]["shift"] = count
        generation = "new" if count > SHIFT_AFTER_CATALOG_REQUESTS else "old"
        _state["shift_generation"] = generation
        return generation


def _notices_for(scenario: str, generation: str) -> list[tuple[str, str, str]]:
    if scenario == "shift" and generation == "new":
        return [SHIFT_NEW_NOTICE, *BASE_NOTICES]
    if scenario == "duplicate":
        # Put the duplicate at the top of page 2, so the same id spans pages.
        return [*BASE_NOTICES[:PAGE_SIZE], BASE_NOTICES[2], *BASE_NOTICES[PAGE_SIZE:]]
    return list(BASE_NOTICES)


def _page_count(total: int) -> int:
    return max(1, math.ceil(total / PAGE_SIZE))


def _page_notices(
    notices: list[tuple[str, str, str]], page: int
) -> list[tuple[str, str, str]]:
    start = (page - 1) * PAGE_SIZE
    return notices[start : start + PAGE_SIZE]


def _catalog_url(base: str, scenario: str, page: int) -> str:
    return f"{base}/demo/catalog?" + urlencode({"page": page, "scenario": scenario})


def _catalog_start_url(base: str) -> str:
    """Stable verification entry point (scenario lives in demo state)."""
    return f"{base}/demo/catalog"


def _demo_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _detail_href(notice_id: str, scenario: str) -> str:
    return f"/demo/notice/{notice_id}?" + urlencode({"scenario": scenario})


def _catalog_html(
    *,
    request: Request,
    scenario: str,
    page: int,
    notices: list[tuple[str, str, str]],
    total_pages: int,
) -> str:
    base = _demo_base(request)
    meta = SCENARIOS[scenario]
    rows: list[str] = []
    for notice_id, title, published in notices:
        rows.append(
            f"""
<li class="notice-item" data-notice-id="{esc(notice_id)}">
  <a class="notice-link" href="{esc(_detail_href(notice_id, scenario))}">{esc(title)}</a>
  <span class="notice-meta">项目编号：{esc(notice_id)} · 发布日期：{esc(published)}</span>
</li>"""
        )
    if not rows:
        rows.append('<li class="empty">本页没有公告。</li>')

    pagination: list[str] = []
    if page > 1:
        pagination.append(
            f'<a class="page-prev" href="{esc(_catalog_url(base, scenario, page - 1))}">上一页</a>'
        )
    for number in range(1, total_pages + 1):
        if number == page:
            pagination.append(f'<span class="current">{number}</span>')
        else:
            pagination.append(
                f'<a class="page-number" href="{esc(_catalog_url(base, scenario, number))}">{number}</a>'
            )
    if page < total_pages:
        pagination.append(
            f'<a class="next" rel="next" href="{esc(_catalog_url(base, scenario, page + 1))}">下一页</a>'
        )
    else:
        pagination.append('<span class="disabled">已是最后一页</span>')

    body = f"""
<section class="panel" style="margin-top:0">
  <p style="margin:0 0 4px"><a href="/demo/">← 返回示例站场景控制</a></p>
  <h1 style="margin-bottom:4px">某市市政工程招标公告</h1>
  <p class="lead" style="margin-top:0">
    本地示例站 · 场景：<strong>{esc(meta['name'])}</strong>
    · 第 {page} / {total_pages} 页
  </p>
  <div class="banner amber" style="margin-top:12px">
    <strong>这是离线示例目录，不是真实公告。</strong>
    目录由本机生成；场景“{esc(meta['name'])}”的行为：{esc(meta['description'])}
  </div>
  <ul class="notice-list">{''.join(rows)}</ul>
  <nav class="pagination"> {' '.join(pagination)} </nav>
</section>
"""
    headings = """
<style>
.notice-list { list-style: none; margin: 8px 0 18px; padding: 0; border-top: 1px solid var(--line); }
.notice-list li { padding: 12px 4px; border-bottom: 1px solid var(--line); }
.notice-list .notice-link { font-weight: 600; text-decoration: none; }
.notice-list .notice-meta { display: block; margin-top: 3px; color: var(--muted); font-size: 13px; }
.pagination { display: flex; gap: 7px; flex-wrap: wrap; align-items: center; }
.pagination a, .pagination span { padding: 5px 10px; border: 1px solid var(--line); border-radius: 6px; }
.pagination .current { background: var(--blue); color: #fff; border-color: var(--blue); font-weight: 700; }
.pagination .disabled { color: var(--muted); }
</style>
"""
    return layout(f"示例目录 · 第 {page} 页", body + headings, active="demo")


def _notice_html(
    *, request: Request, scenario: str, notice_id: str, title: str, published: str, detail_id: str
) -> str:
    body = f"""
<section class="panel" style="margin-top:0">
  <p style="margin:0 0 4px"><a href="{esc(_catalog_url(_demo_base(request), scenario, 1))}">← 返回目录</a></p>
  <h1>{esc(title)}</h1>
  <dl class="meta-grid">
    <div><dt>项目编号</dt><dd data-notice-id="{esc(detail_id)}">{esc(detail_id)}</dd></div>
    <div><dt>发布日期</dt><dd>{esc(published)}</dd></div>
    <div><dt>招标人</dt><dd>某市市政工程管理中心（示例）</dd></div>
    <div><dt>公告类型</dt><dd>招标公告（示例）</dd></div>
  </dl>
  <div class="banner amber" style="margin-top:16px">
    <strong>示例详情页</strong>本文本仅用于本地完整性核对演示，不代表真实采购信息。
  </div>
  <h2>项目概况</h2>
  <p>本项目建设内容包括道路、排水、照明及附属设施施工，具体范围以招标文件为准。</p>
  <p>投标人应具备相应施工资质，并按公告要求提交投标文件。</p>
</section>
"""
    return layout(f"示例公告 {notice_id}", body, active="demo")


@router.get("/", response_class=HTMLResponse)
async def demo_home(request: Request, scenario: str | None = None) -> HTMLResponse:
    resolved = _current_scenario(scenario)
    with _lock:
        shift_count = _state["counters"].get("shift", 0)
        shift_generation = _state["shift_generation"]
    base = _demo_base(request)
    catalog = _catalog_start_url(base)
    preview = _catalog_url(base, resolved, 1)
    verify_url = "/?" + urlencode({"url": catalog})

    cards: list[str] = []
    for key, meta in SCENARIOS.items():
        selected = "（当前）" if key == resolved else ""
        cards.append(
            f"""
<div class="demo-card">
  <span class="tag">{esc(meta['tag'])}</span>
  <h3>{esc(meta['name'])} {selected}</h3>
  <p>{esc(meta['description'])}</p>
  <p class="hint">核对入口（固定 URL）：<code>{esc(catalog)}</code><br>
  预览目录：<code>{esc(_catalog_url(base, key, 1))}</code></p>
  <p><a href="/demo/use?{urlencode({'scenario': key, 'go': 1})}">使用此场景并去核对</a>
     · <a href="/demo/use?{urlencode({'scenario': key})}">仅切换场景并重置计数</a>
     · <a href="{esc(_catalog_url(base, key, 1))}" target="_blank" rel="noopener">预览目录</a></p>
</div>
"""
        )

    body = f"""
<h1>本地示例站</h1>
<p class="lead">用于复现“翻页期间目录更新导致漏采”等情况的离线招标公告站点。所有页面由本机生成，
不访问任何外部地址。请先选择场景并重置计数，再到核对页发起采集。</p>

<section class="panel">
  <div class="banner amber">
    <strong>当前场景：{esc(SCENARIOS[resolved]['name'])}</strong>
    核对入口（固定 URL，切换场景后仍保持同一地址）：<code>{esc(catalog)}</code>
    · <a href="{esc(verify_url)}">打开核对页并预填此目录</a>
    · <a href="{esc(preview)}" target="_blank" rel="noopener">预览当前目录</a>
  </div>
  <dl class="meta-grid">
    <div><dt>目录页数</dt><dd>3 页（每页 5 条）</dd></div>
    <div><dt>“翻页期间更新”场景计数</dt><dd>{shift_count} 次目录请求（第 {SHIFT_AFTER_CATALOG_REQUESTS + 1} 次起切换）</dd></div>
    <div><dt>当前目录代次</dt><dd>{esc(shift_generation)}</dd></div>
  </dl>
  <p class="hint">“翻页期间更新”场景在前 {SHIFT_AFTER_CATALOG_REQUESTS} 次目录请求中返回旧目录，
  第 {SHIFT_AFTER_CATALOG_REQUESTS + 1} 次起返回在头部插入新公告后的目录。核对器顺序遍历第一轮 3 页，
  因此第二轮会看到变化。若手动预览过目录，请先点击“重置计数”。</p>
  <p><a href="/demo/reset?{urlencode({'scenario': resolved})}">重置当前场景计数</a></p>
</section>

<h2>可复现场景</h2>
<div class="demo-grid">{''.join(cards)}</div>

<section class="panel">
  <h2 style="margin-top:0">操作建议</h2>
  <p>1. 在本页选择场景（会自动重置计数）。</p>
  <p>2. 点击“去核对”，核对页已预填该场景的目录 URL。</p>
  <p>3. 点击“开始核对”，查看异常来源与上次完整清单。先跑“稳定目录”产生一份完整清单，
  再跑异常场景，可以验证旧清单被保留并明确标注为“不是本轮结果”。</p>
</section>
"""
    return HTMLResponse(layout("本地示例站", body, active="demo"))


@router.get("/use")
async def demo_use(
    request: Request, scenario: str = "stable", go: int = 0
) -> RedirectResponse:
    if scenario not in SCENARIOS:
        scenario = "stable"
    _reset_state(scenario)
    if go:
        target = "/?" + urlencode({"url": _catalog_start_url(_demo_base(request))})
        return RedirectResponse(target, status_code=303)
    return RedirectResponse("/demo/", status_code=303)


@router.get("/reset")
async def demo_reset(
    request: Request, scenario: str = "stable", go: int = 0
) -> RedirectResponse:
    if scenario not in SCENARIOS:
        scenario = "stable"
    _reset_state(scenario)
    if go:
        target = "/?" + urlencode({"url": _catalog_start_url(_demo_base(request))})
        return RedirectResponse(target, status_code=303)
    return RedirectResponse("/demo/", status_code=303)


@router.get("/catalog", response_class=HTMLResponse)
async def demo_catalog(
    request: Request, page: int = 1, scenario: str | None = None
) -> HTMLResponse:
    resolved = _current_scenario(scenario)
    generation = _shift_generation_for_request(resolved)
    notices = _notices_for(resolved, generation)
    total_pages = _page_count(len(notices))
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages
    page_items = _page_notices(notices, page)
    response = HTMLResponse(
        _catalog_html(
            request=request,
            scenario=resolved,
            page=page,
            notices=page_items,
            total_pages=total_pages,
        )
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@router.get("/notice/{notice_id}", response_class=HTMLResponse)
async def demo_notice(
    request: Request, notice_id: str, scenario: str | None = None
) -> HTMLResponse:
    resolved = _current_scenario(scenario)
    if resolved == "detail_fail" and notice_id == "SZ-2026-0007":
        return HTMLResponse(
            layout(
                "公告不存在",
                '<section class="panel"><h1>404 · 公告不存在</h1>'
                "<p>该公告的详情页在示例站中被配置为读取失败。</p>"
                '<p><a href="/demo/">返回示例站</a></p></section>',
                active="demo",
            ),
            status_code=404,
        )
    notice = NOTICE_MAP.get(notice_id)
    if notice is None:
        return HTMLResponse(
            layout(
                "公告不存在",
                '<section class="panel"><h1>404 · 公告不存在</h1>'
                "<p>示例站没有这个公告编号。</p>"
                '<p><a href="/demo/">返回示例站</a></p></section>',
                active="demo",
            ),
            status_code=404,
        )
    title, published = notice
    detail_id = notice_id
    if resolved == "mismatch" and notice_id == "SZ-2026-0002":
        detail_id = "SZ-2026-8888"
    response = HTMLResponse(
        _notice_html(
            request=request,
            scenario=resolved,
            notice_id=notice_id,
            title=title,
            published=published,
            detail_id=detail_id,
        )
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response
