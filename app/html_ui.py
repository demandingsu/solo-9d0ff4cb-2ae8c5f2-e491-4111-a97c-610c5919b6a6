"""Small, dependency-free HTML renderer for the integrity console.

The container is intentionally offline-capable: no external fonts, scripts or
stylesheets are referenced.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Any, Iterable

STATUS_LABELS = {
    "complete": "完整",
    "flagged": "待复查",
    "failed": "采集失败",
}

ANOMALY_LABELS = {
    "catalog_fetch_failed": "目录读取失败",
    "catalog_parse_failed": "目录解析失败",
    "pagination_loop": "分页死循环",
    "max_pages_exceeded": "超过页数上限",
    "detail_fetch_failed": "详情读取失败",
    "detail_id_missing": "详情缺少编号",
    "detail_mismatch": "详情编号错配",
    "duplicate_id": "跨页编号重复",
    "catalog_changed": "两次目录不一致",
    "catalog_link_changed": "详情链接变化",
    "invalid_url": "目录地址无效",
    "internal_error": "内部错误",
}

DETAIL_STATUS_LABELS = {
    "ok": "一致",
    "mismatch": "错配",
    "missing": "缺少编号",
    "error": "读取失败",
    "unknown": "未核对",
}

_CSS = """
:root {
  color-scheme: light;
  --bg: #f4f6f8;
  --panel: #ffffff;
  --ink: #17212b;
  --muted: #5b6773;
  --line: #d8dee5;
  --blue: #0b5cad;
  --blue-soft: #e8f1fb;
  --green: #17663a;
  --green-soft: #e6f5ec;
  --amber: #8a5300;
  --amber-soft: #fff1d6;
  --red: #a61b1b;
  --red-soft: #fde9e9;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.6 "Noto Sans SC", "Microsoft YaHei", "PingFang SC",
        "Hiragino Sans GB", "Source Han Sans SC", system-ui, sans-serif;
}
a { color: var(--blue); }
.topbar {
  background: #12324f;
  color: #fff;
  box-shadow: 0 1px 4px rgba(0,0,0,.18);
}
.topbar .wrap {
  display: flex; align-items: center; justify-content: space-between;
  padding: 13px 0; gap: 18px;
}
.brand { font-weight: 700; letter-spacing: .02em; color: #fff; }
.topbar nav a {
  color: #d8e7f6; text-decoration: none; margin-left: 18px;
  padding: 5px 9px; border-radius: 6px;
}
.topbar nav a.active, .topbar nav a:hover { background: #214c72; color: #fff; }
.wrap { width: min(1120px, calc(100% - 36px)); margin: 0 auto; }
main { padding: 26px 0 42px; }
h1 { font-size: 26px; line-height: 1.3; margin: 0 0 8px; }
h2 { font-size: 19px; margin: 26px 0 12px; }
h3 { font-size: 16px; margin: 18px 0 8px; }
p { margin: 8px 0; }
.lead { color: var(--muted); }
.panel {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 22px; margin: 16px 0;
  box-shadow: 0 2px 8px rgba(20,40,60,.04);
}
.banner {
  border-radius: 9px; padding: 14px 16px; margin: 14px 0;
  border: 1px solid transparent;
}
.banner.green { background: var(--green-soft); border-color: #b9e1c8; color: var(--green); }
.banner.amber { background: var(--amber-soft); border-color: #f0d39b; color: var(--amber); }
.banner.red { background: var(--red-soft); border-color: #efbcbc; color: var(--red); }
.banner strong { display: block; margin-bottom: 3px; }
.pill {
  display: inline-block; border-radius: 999px; padding: 2px 11px;
  font-size: 13px; font-weight: 700; vertical-align: 2px;
}
.pill.complete { background: var(--green-soft); color: var(--green); border: 1px solid #b9e1c8; }
.pill.flagged { background: var(--amber-soft); color: var(--amber); border: 1px solid #f0d39b; }
.pill.failed { background: var(--red-soft); color: var(--red); border: 1px solid #efbcbc; }
.pill.review { background: var(--amber-soft); color: var(--amber); border: 1px solid #f0d39b; }
form.inline { display: flex; gap: 10px; flex-wrap: wrap; align-items: stretch; }
form.inline input[type=url], form.inline input[type=text] {
  flex: 1 1 460px; min-width: 260px; padding: 11px 13px;
  border: 1px solid #aab6c2; border-radius: 7px; font: inherit; background: #fff;
}
button {
  padding: 11px 22px; border: 0; border-radius: 7px; background: var(--blue);
  color: #fff; font: inherit; font-weight: 700; cursor: pointer;
}
button:hover { background: #084b8d; }
button.secondary { background: #e8edf2; color: #20364b; }
button.secondary:hover { background: #dbe3ea; }
.hint { color: var(--muted); font-size: 13.5px; }
code {
  background: #eef2f6; border: 1px solid #dfe6ed; border-radius: 5px;
  padding: 1px 5px; font-size: 13px; word-break: break-all;
}
.meta-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 10px 22px; margin: 12px 0 0;
}
.meta-grid div { min-width: 0; }
.meta-grid dt { color: var(--muted); font-size: 12.5px; margin: 0; }
.meta-grid dd { margin: 1px 0 0; font-weight: 600; word-break: break-all; }
table { width: 100%; border-collapse: collapse; margin: 8px 0 4px; }
th, td { border-bottom: 1px solid var(--line); padding: 8px 9px; text-align: left; vertical-align: top; }
th { background: #f1f4f7; color: #33485c; font-size: 13px; white-space: nowrap; }
td.small, th.small { font-size: 12.5px; color: var(--muted); }
td.wrap-url { word-break: break-all; max-width: 380px; }
details { margin: 10px 0; }
summary { cursor: pointer; font-weight: 600; color: #28455f; }
.empty { color: var(--muted); padding: 10px 0; }
.steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 16px 0 2px; }
.step { background: #f7f9fb; border: 1px solid var(--line); border-radius: 8px; padding: 13px 14px; }
.step b { display: block; color: var(--blue); margin-bottom: 3px; }
.demo-card { border: 1px solid var(--line); border-radius: 9px; padding: 16px; background: #fff; }
.demo-card h3 { margin-top: 0; }
.demo-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
.tag { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 999px; background: var(--blue-soft); color: var(--blue); margin-bottom: 7px; }
footer { color: var(--muted); font-size: 12.5px; padding: 0 0 30px; }
"""


def esc(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def format_ts(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return value


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def anomaly_label(kind: str) -> str:
    return ANOMALY_LABELS.get(kind, kind)


def detail_status_label(status: str) -> str:
    return DETAIL_STATUS_LABELS.get(status, status)


def layout(title: str, body: str, active: str = "") -> str:
    verify_class = "active" if active == "verify" else ""
    demo_class = "active" if active == "demo" else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 市政招标公告完整性核对</title>
<style>{_CSS}</style>
</head>
<body>
<header class="topbar">
  <div class="wrap">
    <span class="brand">市政招标公告完整性核对</span>
    <nav>
      <a class="{verify_class}" href="/">完整性核对</a>
      <a class="{demo_class}" href="/demo/">本地示例站</a>
    </nav>
  </div>
</header>
<main class="wrap">
{body}
</main>
<footer class="wrap">本地运行 · 清单保存在 SQLite · 无外部网络资源</footer>
</body>
</html>
"""


def verify_page(
    *,
    default_url: str,
    demo_url: str,
    error: str = "",
) -> str:
    error_html = (
        f'<div class="banner red"><strong>无法开始核对</strong>{esc(error)}</div>'
        if error
        else ""
    )
    body = f"""
<h1>公告目录完整性核对</h1>
<p class="lead">输入分页目录地址。系统会先沿分页读取公告编号和详情链接，抓完全部详情后再次遍历目录；
只有两轮各页编号及顺序完全一致、列表编号与详情编号一致时，才把本轮完整清单及来源原子写入 SQLite。</p>
{error_html}
<section class="panel">
  <form class="inline" method="post" action="/check">
    <label for="catalog_url" class="hint" style="flex-basis:100%;margin:0">目录 URL</label>
    <input id="catalog_url" name="catalog_url" type="url" required
           placeholder="http://127.0.0.1:8000/demo/catalog"
           value="{esc(default_url)}">
    <button type="submit">开始核对</button>
  </form>
  <p class="hint">示例目录：<code>{esc(demo_url)}</code>
    请先到 <a href="/demo/">本地示例站</a> 选择场景并重置计数，再回到本页发起核对。</p>
</section>

<section class="panel">
  <h2 style="margin-top:0">核对规则</h2>
  <div class="steps">
    <div class="step"><b>1 · 第一轮目录</b>沿“下一页”读取每一页，记录公告编号、详情链接和出现顺序。</div>
    <div class="step"><b>2 · 抓取详情</b>逐条打开详情页，读出详情中的公告编号并核对。</div>
    <div class="step"><b>3 · 第二轮目录</b>重新访问全部分页；任一页编号或顺序变化都会触发待复查。</div>
    <div class="step"><b>4 · 原子保存</b>全部通过才保存完整清单；否则保留上次完整清单并明确标注“不是本轮结果”。</div>
  </div>
  <p class="hint" style="margin-top:14px">核对过程中会访问输入地址及其分页、详情链接，耗时取决于站点速度。
  跨页重复编号、目录变化、详情错配、详情缺失或读取失败都会列为异常来源并标记为“待复查”。</p>
</section>
"""
    return layout("完整性核对", body, active="verify")


def _item_rows(items: Iterable[dict[str, Any]], *, with_detail: bool) -> str:
    rows: list[str] = []
    for item in items:
        detail_cell = ""
        if with_detail:
            status = item.get("detail_status", "unknown")
            detail_cell = (
                f'<td class="small">{esc(detail_status_label(status))}'
                + (
                    f'<br><span class="hint">{esc(item.get("detail_error", ""))}</span>'
                    if item.get("detail_error")
                    else ""
                )
                + "</td>"
            )
        detail_id = item.get("detail_id") or "—"
        title = item.get("title") or "—"
        rows.append(
            "<tr>"
            f"<td>{esc(item.get('page_no', '—'))}</td>"
            f"<td>{esc(item.get('position', '—'))}</td>"
            f"<td><strong>{esc(item.get('announcement_id', ''))}</strong></td>"
            f"<td>{esc(title)}</td>"
            f"<td>{esc(detail_id)}</td>"
            f'<td class="small wrap-url"><a href="{esc(item.get("catalog_page_url", ""))}" '
            'target="_blank" rel="noopener">目录页</a></td>'
            f'<td class="small wrap-url"><a href="{esc(item.get("detail_url", ""))}" '
            'target="_blank" rel="noopener">详情页</a></td>'
            f"{detail_cell}"
            "</tr>"
        )
    if not rows:
        span = 8 if with_detail else 7
        rows.append(f'<tr><td colspan="{span}" class="empty">（无条目）</td></tr>')
    head = (
        "<tr><th>页码</th><th>序号</th><th>公告编号</th><th>标题</th><th>详情编号</th>"
        "<th>来源目录页</th><th>详情链接</th>"
        + ("<th>详情核对</th>" if with_detail else "")
        + "</tr>"
    )
    return f"<table>{head}{''.join(rows)}</table>"


def _anomaly_table(anomalies: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for anomaly in anomalies:
        rows.append(
            "<tr>"
            f'<td><span class="pill review">待复查</span></td>'
            f"<td>{esc(anomaly_label(anomaly.get('kind', '')))}</td>"
            f"<td>{esc(anomaly.get('page_no') if anomaly.get('page_no') is not None else '—')}</td>"
            f"<td>{esc(anomaly.get('announcement_id') or '—')}</td>"
            f'<td class="small wrap-url">'
            + (
                f'<a href="{esc(anomaly.get("url"))}" target="_blank" rel="noopener">{esc(anomaly.get("url"))}</a>'
                if anomaly.get("url")
                else "—"
            )
            + "</td>"
            f"<td>{esc(anomaly.get('message', ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="6" class="empty">本轮没有异常。</td></tr>')
    return (
        "<table>"
        "<tr><th>处理状态</th><th>异常类型</th><th>页码</th><th>公告编号</th>"
        "<th>异常来源</th><th>说明</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _snapshot_section(
    snapshot: dict[str, Any],
    *,
    heading: str,
    note: str,
    note_class: str,
) -> str:
    items = snapshot.get("items", [])
    return f"""
<section class="panel">
  <h2 style="margin-top:0">{esc(heading)}</h2>
  <div class="banner {note_class}"><strong>{esc(note)}</strong>
    采集时间：{esc(format_ts(snapshot.get('collected_at')))}
    · 共 {esc(snapshot.get('page_count', 0))} 页 / {esc(snapshot.get('item_count', 0))} 条
    · 清单编号 #{esc(snapshot.get('id'))}（采集轮次 #{esc(snapshot.get('run_id'))}）
  </div>
  {_item_rows(items, with_detail=False)}
</section>
"""


def result_page(
    *,
    run: dict[str, Any],
    items: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
    current_snapshot: dict[str, Any] | None,
    previous_snapshot: dict[str, Any] | None,
) -> str:
    status = run.get("status", "failed")
    pill = f'<span class="pill {esc(status)}">{esc(status_label(status))}</span>'
    parts: list[str] = [
        f'<p style="margin:0"><a href="/">← 返回核对页</a></p>',
        f"<h1>核对结果 {pill}</h1>",
        f'<p class="lead">{esc(run.get("message", ""))}</p>',
    ]

    if status == "complete":
        parts.append(
            '<div class="banner green"><strong>核对通过，本轮完整清单已原子保存。</strong>'
            "两轮目录页数与各页编号顺序一致；列表编号与详情编号逐条一致，"
            "本轮清单及来源已作为新快照写入 SQLite。</div>"
        )
    else:
        parts.append(
            '<div class="banner amber"><strong>本轮结果不完整，标记为待复查。</strong>'
            "本轮没有写入新的完整清单；以下异常来源均需人工复查。"
            "若存在上次完整清单，它会在下方单独标出，<u>不是本轮结果</u>。</div>"
        )

    parts.append(
        f"""
<section class="panel">
  <h2 style="margin-top:0">本轮运行信息</h2>
  <dl class="meta-grid">
    <div><dt>轮次编号</dt><dd>#{esc(run.get('id'))}</dd></div>
    <div><dt>目录 URL</dt><dd><code>{esc(run.get('catalog_url'))}</code></dd></div>
    <div><dt>开始时间</dt><dd>{esc(format_ts(run.get('started_at')))}</dd></div>
    <div><dt>结束时间</dt><dd>{esc(format_ts(run.get('finished_at')))}</dd></div>
    <div><dt>第一轮</dt><dd>{esc(run.get('pass1_pages'))} 页 / {esc(run.get('pass1_items'))} 条</dd></div>
    <div><dt>第二轮</dt><dd>{esc(run.get('pass2_pages'))} 页 / {esc(run.get('pass2_items'))} 条</dd></div>
    <div><dt>本轮清单</dt><dd>{esc(run.get('page_count'))} 页 / {esc(run.get('item_count'))} 条</dd></div>
    <div><dt>异常数量</dt><dd>{len(anomalies)}</dd></div>
  </dl>
</section>
"""
    )

    if anomalies:
        parts.append(
            f"""
<section class="panel">
  <h2 style="margin-top:0">异常来源（全部待复查）</h2>
  {_anomaly_table(anomalies)}
</section>
"""
        )

    if current_snapshot:
        parts.append(
            _snapshot_section(
                current_snapshot,
                heading="本轮完整清单（已保存）",
                note="以下就是本轮核对通过并保存的清单。",
                note_class="green",
            )
        )
    else:
        if previous_snapshot:
            parts.append(
                _snapshot_section(
                    previous_snapshot,
                    heading="上次完整清单",
                    note="注意：以下为上次完整清单，不是本轮结果。",
                    note_class="amber",
                )
            )
        else:
            parts.append(
                '<section class="panel"><h2 style="margin-top:0">上次完整清单</h2>'
                '<p class="empty">没有找到该目录的上次完整清单。'
                "本轮结果未保存为完整清单。</p></section>"
            )

    parts.append(
        f"""
<section class="panel">
  <h2 style="margin-top:0">本轮读取到的条目</h2>
  <p class="hint">用于排查问题，仅供参考；{('本轮已保存为上面的完整清单。' if status == 'complete' else '它未被当作完整清单保存。')}</p>
  {_item_rows(items, with_detail=True)}
</section>
"""
    )
    return layout(f"核对结果 #{run.get('id')}", "".join(parts), active="verify")


def error_page(title: str, message: str) -> str:
    body = f"""
<h1>{esc(title)}</h1>
<div class="banner red"><strong>请求未完成</strong>{esc(message)}</div>
<p><a href="/">返回核对页</a></p>
"""
    return layout(title, body, active="verify")
