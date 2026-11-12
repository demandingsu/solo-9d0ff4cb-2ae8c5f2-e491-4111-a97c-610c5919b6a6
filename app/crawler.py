"""Catalog crawling and completeness verification.

The verifier walks a paginated announcement catalog twice.  After the first
walk it fetches every detail page.  The run is complete only when:

* both walks expose exactly the same announcement ids, in the same order, on
  every page;
* the ids read from the catalog match the ids printed on their detail pages;
* there are no cross-page duplicate ids and no fetch/parse failures.

Anything else is reported as an anomaly (待复查) and no new snapshot is saved.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urljoin, urldefrag, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# Tunables (all safely overridable through the environment)

MAX_CATALOG_PAGES = 200
DETAIL_CONCURRENCY = 6
USER_AGENT = (
    "MunicipalNoticeIntegrityChecker/1.0 "
    "(local completeness verification)"
)

_NEXT_TEXTS = {
    "下一页",
    "下页",
    "下一頁",
    "后一页",
    "後一頁",
    "next",
    "next page",
    "›",
    "»",
    ">",
    ">>",
}
_PREV_TEXTS = {"上一页", "上页", "上一頁", "前一页", "prev", "previous", "‹", "«"}
_NAV_TEXTS = _NEXT_TEXTS | _PREV_TEXTS | {
    "首页",
    "首頁",
    "尾页",
    "末页",
    "最后一页",
    "第一页",
    "返回",
    "登录",
    "注册",
    "更多",
}
_PAGINATION_HINTS = (
    "pagination",
    "pager",
    "paging",
    "page-nav",
    "pagebar",
    "page-list",
    "fenye",
    "paginate",
)
_DATA_ATTRS = (
    "data-notice-id",
    "data-noticeid",
    "data-announcement-id",
    "data-announcementid",
    "data-id",
    "data-no",
    "data-code",
    "data-bid-no",
    "data-project-code",
    "data-project-no",
)

_LABEL_WORD = (
    r"(?:项目编号|招标编号|公告编号|采购编号|标段编号|文件编号|"
    r"项目编码|招标项目编号|公告号|项目号|标号|编号)"
)
_ID_LABEL_RE = re.compile(
    _LABEL_WORD + r"\s*(?:[：:]|为|是)\s*([^\s，,；;。、|｜<>《》\n\r\t]+)"
)
_ID_LABEL_SPACE_RE = re.compile(
    _LABEL_WORD + r"\s+([^\s，,；;。、|｜<>《》\n\r\t]+)"
)
_ID_BRACKET_RE = re.compile(r"【([^】]{3,60})】")
_ID_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/\-]{2,60}")
_DATE_RE = re.compile(r"[年月日]|^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$")


# ---------------------------------------------------------------------------
# Data structures


@dataclass
class Anomaly:
    kind: str
    message: str
    page_no: int | None = None
    url: str = ""
    announcement_id: str = ""
    pass_no: int | None = None


@dataclass
class CatalogItem:
    announcement_id: str
    detail_url: str
    title: str = ""
    page_no: int = 0
    catalog_page_url: str = ""
    detail_id: str = ""
    detail_status: str = "unknown"  # ok / mismatch / missing / error
    detail_error: str = ""


@dataclass
class CatalogPage:
    page_no: int
    url: str
    items: list[CatalogItem] = field(default_factory=list)


@dataclass
class RunOutcome:
    catalog_url: str
    started_at: str
    finished_at: str
    status: str
    message: str
    pass1_pages: list[CatalogPage] = field(default_factory=list)
    pass2_pages: list[CatalogPage] = field(default_factory=list)
    items: list[CatalogItem] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)


class CrawlError(Exception):
    pass


# ---------------------------------------------------------------------------
# Small helpers


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", unescape(value)).strip()


def normalize_id(value: str | None) -> str:
    if value is None:
        return ""
    text = unescape(str(value)).replace("　", " ").strip()
    text = text.strip("：: 　")
    text = text.strip("()（）[]【】<>《》\"'“”‘’")
    text = re.sub(r"\s+", "", text)
    text = text.rstrip("，,；;。.、")
    return text


def _id_key(value: str | None) -> str:
    return normalize_id(value).casefold()


def _looks_like_id(value: str) -> bool:
    value = normalize_id(value)
    if not value or not (3 <= len(value) <= 60):
        return False
    if not re.search(r"\d", value):
        return False
    if _DATE_RE.search(value):
        return False
    # Reject obvious prose fragments and labels.
    if re.search(r"[，。；：!?！？]", value):
        return False
    if value.lower() in {"编号", "项目编号", "公告编号", "none", "null"}:
        return False
    return True


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _absolute_url(base: str, href: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith("#"):
        return ""
    if href.lower().startswith(("javascript:", "mailto:", "tel:")):
        return ""
    absolute, _fragment = urldefrag(urljoin(base, href))
    return absolute


def _class_id_text(tag: Tag) -> str:
    classes = " ".join(tag.get("class", []) or [])
    element_id = tag.get("id", "") or ""
    return f"{classes} {element_id}".lower()


def _attr_id(tag: Tag) -> str:
    for attr in _DATA_ATTRS:
        value = tag.get(attr)
        if value and _looks_like_id(str(value)):
            return normalize_id(str(value))
    return ""


def _label_id(text: str) -> str:
    for pattern in (_ID_LABEL_RE, _ID_LABEL_SPACE_RE):
        match = pattern.search(text)
        if match:
            candidate = normalize_id(match.group(1))
            if _looks_like_id(candidate):
                return candidate
    return ""


def _extract_element_id(tag: Tag) -> str:
    """Find an announcement number attached to a catalog/detail element."""
    direct = _attr_id(tag)
    if direct:
        return direct

    # data attributes on immediate children (e.g. <span data-id=...>)
    for child in tag.find_all(True, limit=12):
        child_id = _attr_id(child)
        if child_id:
            return child_id

    text = _clean_text(tag.get_text(" ", strip=True))[:1200]
    labelled = _label_id(text)
    if labelled:
        return labelled

    bracketed = _ID_BRACKET_RE.search(text)
    if bracketed and _looks_like_id(bracketed.group(1)):
        return normalize_id(bracketed.group(1))
    return ""


def _in_pagination_container(tag: Tag) -> bool:
    current: Tag | None = tag
    for _ in range(4):
        if current is None:
            break
        haystack = _class_id_text(current)
        if any(hint in haystack for hint in _PAGINATION_HINTS):
            return True
        current = current.parent if isinstance(current.parent, Tag) else None
    return False


def _is_pagination_anchor(anchor: Tag, current_url: str) -> bool:
    if getattr(anchor, "name", None) != "a":
        return False
    href = (anchor.get("href") or "").strip()
    if not href or href.startswith("#") or href.lower().startswith("javascript:"):
        return True
    rel = " ".join(anchor.get("rel", []) or []).lower()
    if "next" in rel or "prev" in rel:
        return True
    text = _clean_text(anchor.get_text(" ", strip=True)).lower()
    if text in _NAV_TEXTS or "下一页" in text or "上一页" in text:
        return True
    if (
        re.fullmatch(r"\d{1,4}", text)
        and len(anchor.get_text(strip=True)) <= 4
        and _in_pagination_container(anchor)
    ):
        return True
    if "页" in text and len(text) <= 8:
        return True
    aria = (anchor.get("aria-label") or "").lower()
    if any(word in aria for word in ("next", "prev", "下一页", "上一页")):
        return True
    haystack = _class_id_text(anchor)
    if any(hint in haystack for hint in _PAGINATION_HINTS):
        return True
    if _in_pagination_container(anchor):
        return True
    return False


# ---------------------------------------------------------------------------
# Catalog parsing


def _choose_detail_anchor(container: Tag, current_url: str) -> Tag | None:
    anchors = [
        a
        for a in container.find_all("a", href=True)
        if not _is_pagination_anchor(a, current_url)
        and _absolute_url(current_url, a.get("href", ""))
    ]
    if not anchors:
        return None

    def score(anchor: Tag) -> int:
        haystack = _class_id_text(anchor)
        href = (anchor.get("href") or "").lower()
        value = 0
        if any(word in haystack for word in ("detail", "notice", "title", "content", "bid")):
            value += 3
        if any(word in href for word in ("/notice", "/detail", "/content", "/article", ".html")):
            value += 2
        if len(_clean_text(anchor.get_text(" ", strip=True))) >= 4:
            value += 1
        return value

    return max(anchors, key=score)


def _items_from_element(container: Tag, current_url: str, page_url: str) -> CatalogItem | None:
    if _is_pagination_anchor(container, current_url):
        return None
    announcement_id = _extract_element_id(container)
    if not announcement_id:
        return None
    anchor = _choose_detail_anchor(container, current_url)
    if anchor is None:
        return None
    detail_url = _absolute_url(current_url, anchor.get("href", ""))
    if not detail_url:
        return None
    title = _clean_text(anchor.get_text(" ", strip=True))
    if not title or _looks_like_id(title):
        title = _clean_text(anchor.get("title") or "")
    return CatalogItem(
        announcement_id=announcement_id,
        detail_url=detail_url,
        title=title,
        catalog_page_url=page_url,
    )


def parse_catalog(html: str, page_url: str) -> list[CatalogItem]:
    """Extract (announcement id, detail link) pairs in document order."""
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup(["script", "style", "noscript", "template"]):
        junk.decompose()

    def collect(containers: list[Tag]) -> list[CatalogItem]:
        result: list[CatalogItem] = []
        seen_elements: set[int] = set()
        for container in containers:
            if id(container) in seen_elements:
                continue
            seen_elements.add(id(container))
            item = _items_from_element(container, page_url, page_url)
            if item is not None:
                result.append(item)
        return result

    result = collect(soup.find_all(["li", "tr"]))
    if result:
        return result

    # Fall back to list-ish divs/articles, only if they are small enough that
    # they are unlikely to swallow several announcements.
    candidates: list[Tag] = []
    for tag in soup.find_all(["div", "article", "section"]):
        haystack = _class_id_text(tag)
        if not any(
            word in haystack
            for word in ("notice", "announcement", "item", "result", "list", "公告", "招标")
        ):
            continue
        anchors = tag.find_all("a", href=True)
        if not anchors or len(anchors) > 3:
            continue
        if len(_clean_text(tag.get_text(" ", strip=True))) > 800:
            continue
        candidates.append(tag)
    result = collect(candidates)
    if result:
        return result

    # Last resort: anchors that themselves carry a labelled id.
    result = []
    for anchor in soup.find_all("a", href=True):
        if _is_pagination_anchor(anchor, page_url):
            continue
        parent = anchor.parent if isinstance(anchor.parent, Tag) else anchor
        announcement_id = _extract_element_id(parent) or _extract_element_id(anchor)
        if not announcement_id:
            continue
        detail_url = _absolute_url(page_url, anchor.get("href", ""))
        if not detail_url:
            continue
        result.append(
            CatalogItem(
                announcement_id=announcement_id,
                detail_url=detail_url,
                title=_clean_text(anchor.get_text(" ", strip=True)),
                catalog_page_url=page_url,
            )
        )
    return result


def _current_page_number(url: str) -> int:
    query = parse_qs(urlparse(url).query)
    for key in ("page", "p", "pageNo", "pageNum", "currentPage", "pn", "PageNo"):
        if key in query:
            try:
                return int(query[key][0])
            except (TypeError, ValueError):
                pass
    return 1


def _numeric_next_url(soup: BeautifulSoup, current_url: str) -> str:
    current = _current_page_number(current_url)
    parsed_current = urlparse(current_url)
    best: str = ""
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        absolute = _absolute_url(current_url, href)
        if not absolute:
            continue
        parsed = urlparse(absolute)
        if parsed.path != parsed_current.path:
            continue
        query = parse_qs(parsed.query)
        for key in ("page", "p", "pageNo", "pageNum", "currentPage", "pn", "PageNo"):
            if key not in query:
                continue
            try:
                number = int(query[key][0])
            except (TypeError, ValueError):
                continue
            if number == current + 1:
                return absolute
    return best


def find_next_url(soup: BeautifulSoup, current_url: str) -> str:
    scored: list[tuple[int, str]] = []
    for anchor in soup.find_all("a", href=True):
        absolute = _absolute_url(current_url, anchor.get("href", ""))
        if not absolute:
            continue
        text = _clean_text(anchor.get_text(" ", strip=True)).lower()
        rel = " ".join(anchor.get("rel", []) or []).lower()
        haystack = _class_id_text(anchor)
        aria = (anchor.get("aria-label") or "").lower()
        score = 0
        if "next" in rel:
            score = max(score, 6)
        if text in _NEXT_TEXTS or "下一页" in text or "下页" in text:
            score = max(score, 5)
        if "next" in haystack or "page-next" in haystack:
            score = max(score, 4)
        if "next" in aria or "下一页" in aria:
            score = max(score, 3)
        if score:
            scored.append((score, absolute))
    if scored:
        return max(scored, key=lambda pair: pair[0])[1]

    numeric = _numeric_next_url(soup, current_url)
    if numeric:
        return numeric
    return ""


# ---------------------------------------------------------------------------
# Detail parsing


def _detail_scope(soup: BeautifulSoup) -> Tag:
    for selector in (
        "main",
        "article",
        "[class*=detail]",
        "[class*=content]",
        "#content",
        ".article",
        ".notice",
        "body",
    ):
        found = soup.select_one(selector)
        if isinstance(found, Tag):
            return found
    return soup


def extract_detail_id(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup(["script", "style", "noscript", "template"]):
        junk.decompose()

    # Explicit metadata beats prose.
    for tag in soup.find_all(
        [
            "body",
            "main",
            "article",
            "section",
            "h1",
            "h2",
            "h3",
            "div",
            "span",
            "p",
            "dl",
            "dt",
            "dd",
            "td",
            "li",
            "strong",
        ]
    ):
        value = _attr_id(tag)
        if value:
            return value

    for meta in soup.find_all("meta"):
        name = (meta.get("name") or meta.get("property") or "").lower()
        if any(word in name for word in ("notice", "announcement", "project", "bid")):
            content = meta.get("content")
            if content and _looks_like_id(str(content)):
                return normalize_id(str(content))

    scope = _detail_scope(soup)
    text = _clean_text(scope.get_text(" ", strip=True))[:6000]
    labelled = _label_id(text)
    if labelled:
        return labelled

    bracketed = _ID_BRACKET_RE.search(text)
    if bracketed and _looks_like_id(bracketed.group(1)):
        return normalize_id(bracketed.group(1))
    return ""


def extract_detail_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for selector in ("main h1", "article h1", "h1", "title"):
        node = soup.select_one(selector)
        if node:
            text = _clean_text(node.get_text(" ", strip=True))
            if text:
                return text
    return ""


# ---------------------------------------------------------------------------
# Network


async def _fetch(
    client: httpx.AsyncClient, url: str
) -> tuple[str | None, str | None, str]:
    """Return (html, error, final_url)."""
    try:
        response = await client.get(
            url,
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
        final_url = str(response.url)
        if response.status_code >= 400:
            return None, f"HTTP {response.status_code}", final_url
        content_type = response.headers.get("content-type", "")
        if content_type and not any(
            kind in content_type for kind in ("html", "xml", "text/plain", "text")
        ):
            # Still accept bodies that look like HTML even with a poor header.
            body = response.text[:200].lstrip().lower()
            if not body.startswith(("<", "<?xml")):
                return None, f"不是网页内容（{content_type}）", final_url
        return response.text, None, final_url
    except httpx.TimeoutException:
        return None, "请求超时", url
    except httpx.HTTPError as exc:
        return None, f"网络错误：{exc.__class__.__name__}", url
    except Exception as exc:  # defensive: never let one page abort the run
        return None, f"请求失败：{exc.__class__.__name__}: {exc}", url


async def _traverse_catalog(
    client: httpx.AsyncClient,
    start_url: str,
    pass_no: int,
    max_pages: int,
) -> tuple[list[CatalogPage], list[Anomaly]]:
    pages: list[CatalogPage] = []
    anomalies: list[Anomaly] = []
    current = start_url
    visited: set[str] = set()
    page_no = 0

    while current:
        if page_no >= max_pages:
            anomalies.append(
                Anomaly(
                    kind="max_pages_exceeded",
                    message=f"目录超过 {max_pages} 页的安全上限，已停止遍历",
                    page_no=page_no + 1,
                    url=current,
                    pass_no=pass_no,
                )
            )
            break
        if current in visited:
            anomalies.append(
                Anomaly(
                    kind="pagination_loop",
                    message="分页链接指回已访问的页面，疑似分页死循环，已停止遍历",
                    page_no=page_no + 1,
                    url=current,
                    pass_no=pass_no,
                )
            )
            break
        visited.add(current)
        page_no += 1

        html, error, final_url = await _fetch(client, current)
        if error is not None:
            anomalies.append(
                Anomaly(
                    kind="catalog_fetch_failed",
                    message=f"目录第 {page_no} 页读取失败：{error}",
                    page_no=page_no,
                    url=current,
                    pass_no=pass_no,
                )
            )
            break
        assert html is not None

        try:
            items = parse_catalog(html, final_url or current)
        except Exception as exc:
            anomalies.append(
                Anomaly(
                    kind="catalog_parse_failed",
                    message=f"目录第 {page_no} 页解析失败：{exc.__class__.__name__}: {exc}",
                    page_no=page_no,
                    url=current,
                    pass_no=pass_no,
                )
            )
            break

        for item in items:
            item.page_no = page_no
            item.catalog_page_url = final_url or current
        pages.append(CatalogPage(page_no=page_no, url=final_url or current, items=items))

        try:
            soup = BeautifulSoup(html, "html.parser")
            next_url = find_next_url(soup, final_url or current)
        except Exception:
            next_url = ""
        if not next_url:
            break
        if next_url in visited:
            anomalies.append(
                Anomaly(
                    kind="pagination_loop",
                    message="分页链接指回已访问的页面，疑似分页死循环，已停止遍历",
                    page_no=page_no,
                    url=next_url,
                    pass_no=pass_no,
                )
            )
            break
        current = next_url

    return pages, anomalies


async def _fetch_details(
    client: httpx.AsyncClient,
    items: list[CatalogItem],
) -> list[Anomaly]:
    anomalies: list[Anomaly] = []
    cache: dict[str, tuple[str, str, str, str]] = {}  # url -> (html, error, final, title)
    semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def load(url: str) -> tuple[str | None, str | None, str]:
        async with semaphore:
            return await _fetch(client, url)

    # Detail pages may repeat (duplicate catalog rows); fetch each URL once.
    urls = sorted({item.detail_url for item in items})
    results: dict[str, tuple[str | None, str | None, str]] = {}
    gathered = await asyncio.gather(*(load(url) for url in urls), return_exceptions=True)
    for url, result in zip(urls, gathered):
        if isinstance(result, Exception):
            results[url] = (None, f"请求失败：{result.__class__.__name__}: {result}", url)
        else:
            results[url] = result

    for item in items:
        html, error, final_url = results[item.detail_url]
        if error is not None:
            item.detail_status = "error"
            item.detail_error = error
            anomalies.append(
                Anomaly(
                    kind="detail_fetch_failed",
                    message=f"公告 {item.announcement_id} 的详情页读取失败：{error}",
                    page_no=item.page_no,
                    url=item.detail_url,
                    announcement_id=item.announcement_id,
                    pass_no=1,
                )
            )
            continue

        assert html is not None
        detail_id = extract_detail_id(html)
        if not detail_id:
            item.detail_status = "missing"
            item.detail_error = "详情页中未找到公告编号"
            anomalies.append(
                Anomaly(
                    kind="detail_id_missing",
                    message=f"公告 {item.announcement_id} 的详情页未找到公告编号，无法核对",
                    page_no=item.page_no,
                    url=item.detail_url,
                    announcement_id=item.announcement_id,
                    pass_no=1,
                )
            )
            continue

        item.detail_id = detail_id
        if _id_key(detail_id) != _id_key(item.announcement_id):
            item.detail_status = "mismatch"
            item.detail_error = f"列表编号 {item.announcement_id} / 详情编号 {detail_id}"
            anomalies.append(
                Anomaly(
                    kind="detail_mismatch",
                    message=(
                        f"列表编号 {item.announcement_id} 与详情页编号 "
                        f"{detail_id} 不一致"
                    ),
                    page_no=item.page_no,
                    url=item.detail_url,
                    announcement_id=item.announcement_id,
                    pass_no=1,
                )
            )
        else:
            item.detail_status = "ok"
            if not item.title:
                item.title = extract_detail_title(html)
    return anomalies


# ---------------------------------------------------------------------------
# Comparison


def _duplicate_anomalies(pages: list[CatalogPage], pass_no: int) -> list[Anomaly]:
    occurrences: dict[str, list[CatalogItem]] = {}
    for page in pages:
        for item in page.items:
            occurrences.setdefault(_id_key(item.announcement_id), []).append(item)

    anomalies: list[Anomaly] = []
    for key, rows in occurrences.items():
        if len(rows) <= 1:
            continue
        page_numbers = sorted({row.page_no for row in rows})
        pages_text = "、".join(f"第 {number} 页" for number in page_numbers)
        cross_page = len(page_numbers) > 1
        kind = "duplicate_id"
        label = "跨页编号重复" if cross_page else "同一页内编号重复"
        anomalies.append(
            Anomaly(
                kind=kind,
                message=(
                    f"{label}：编号 {rows[0].announcement_id} 在 {pages_text}"
                    f"共出现 {len(rows)} 次"
                ),
                page_no=page_numbers[0],
                url=rows[0].catalog_page_url,
                announcement_id=rows[0].announcement_id,
                pass_no=pass_no,
            )
        )
    return anomalies


def _short_ids(items: list[CatalogItem], limit: int = 12) -> str:
    values = [item.announcement_id for item in items]
    shown = values[:limit]
    text = "、".join(shown) if shown else "（空）"
    if len(values) > limit:
        text += f" …（共 {len(values)} 条）"
    return text


def _compare_passes(
    pass1: list[CatalogPage], pass2: list[CatalogPage]
) -> list[Anomaly]:
    anomalies: list[Anomaly] = []
    if not pass2:
        anomalies.append(
            Anomaly(
                kind="catalog_changed",
                message="第二次目录遍历没有取得任何页面，无法确认目录未发生变化",
                pass_no=2,
            )
        )
        return anomalies

    if len(pass1) != len(pass2):
        anomalies.append(
            Anomaly(
                kind="catalog_changed",
                message=(
                    f"两次目录遍历的页数不一致：第一次 {len(pass1)} 页，"
                    f"第二次 {len(pass2)} 页"
                ),
                page_no=min(len(pass1), len(pass2)) + 1,
                url=(
                    pass1[len(pass1) - 1].url
                    if len(pass1) >= len(pass2)
                    else pass2[len(pass2) - 1].url
                ),
                pass_no=2,
            )
        )

    for index in range(min(len(pass1), len(pass2))):
        page1 = pass1[index]
        page2 = pass2[index]
        ids1 = [_id_key(item.announcement_id) for item in page1.items]
        ids2 = [_id_key(item.announcement_id) for item in page2.items]
        if ids1 != ids2:
            first_diff = next(
                (
                    position
                    for position, (left, right) in enumerate(zip(ids1, ids2), start=1)
                    if left != right
                ),
                min(len(ids1), len(ids2)) + 1,
            )
            anomalies.append(
                Anomaly(
                    kind="catalog_changed",
                    message=(
                        f"第 {page1.page_no} 页目录在两次遍历中不一致"
                        f"（首个差异在第 {first_diff} 条）：第一次【"
                        f"{_short_ids(page1.items)}】；第二次【"
                        f"{_short_ids(page2.items)}】"
                    ),
                    page_no=page1.page_no,
                    url=page1.url,
                    pass_no=2,
                )
            )
            continue

        # Ids and order match; a changed link still needs a human look because
        # the first pass fetched details from the old link.
        for item1, item2 in zip(page1.items, page2.items):
            if item1.detail_url != item2.detail_url:
                anomalies.append(
                    Anomaly(
                        kind="catalog_link_changed",
                        message=(
                            f"公告 {item1.announcement_id} 的详情链接在两次遍历中不一致："
                            f"第一次 {item1.detail_url}；第二次 {item2.detail_url}"
                        ),
                        page_no=page1.page_no,
                        url=item1.catalog_page_url,
                        announcement_id=item1.announcement_id,
                        pass_no=2,
                    )
                )
    return anomalies


# ---------------------------------------------------------------------------
# Public entry point


async def verify_catalog(
    catalog_url: str,
    *,
    client: httpx.AsyncClient,
    max_pages: int = MAX_CATALOG_PAGES,
) -> RunOutcome:
    """Run the whole two-pass verification.  Persisting is done by the caller."""
    started_at = _now()
    anomalies: list[Anomaly] = []

    catalog_url = (catalog_url or "").strip()
    if not _is_http_url(catalog_url):
        return RunOutcome(
            catalog_url=catalog_url,
            started_at=started_at,
            finished_at=_now(),
            status="failed",
            message="目录 URL 无效，请输入 http:// 或 https:// 开头的完整地址",
            anomalies=[
                Anomaly(
                    kind="invalid_url",
                    message="目录 URL 无效，请输入 http:// 或 https:// 开头的完整地址",
                    url=catalog_url,
                )
            ],
        )

    pass1, issues1 = await _traverse_catalog(client, catalog_url, 1, max_pages)
    anomalies.extend(issues1)

    detail_issues = await _fetch_details(client, [item for page in pass1 for item in page.items])
    anomalies.extend(detail_issues)

    # Second traversal starts from scratch after all details have been read.
    if pass1:
        pass2, issues2 = await _traverse_catalog(client, catalog_url, 2, max_pages)
        anomalies.extend(issues2)
    else:
        pass2 = []

    # Duplicate ids are checked in both walks so a duplicate that only appears
    # in the second walk is still reported.
    seen_duplicate_keys: set[tuple[str, tuple[int, ...], str]] = set()
    for pages, pass_no in ((pass1, 1), (pass2, 2)):
        for anomaly in _duplicate_anomalies(pages, pass_no):
            key = (
                anomaly.kind,
                tuple(
                    sorted(
                        {
                            item.page_no
                            for page in pages
                            for item in page.items
                            if _id_key(item.announcement_id)
                            == _id_key(anomaly.announcement_id)
                        }
                    )
                ),
                _id_key(anomaly.announcement_id),
            )
            if key in seen_duplicate_keys:
                continue
            seen_duplicate_keys.add(key)
            anomalies.append(anomaly)

    if pass1:
        anomalies.extend(_compare_passes(pass1, pass2))
    elif not anomalies:
        anomalies.append(
            Anomaly(
                kind="catalog_fetch_failed",
                message="目录没有任何页面被成功读取",
                pass_no=1,
            )
        )

    items = [item for page in pass1 for item in page.items]

    # Deduplicate byte-identical anomaly rows (same source + same message).
    deduped: list[Anomaly] = []
    seen_rows: set[tuple[Any, ...]] = set()
    for anomaly in anomalies:
        row_key = (
            anomaly.kind,
            anomaly.page_no,
            anomaly.url,
            anomaly.announcement_id,
            anomaly.message,
            anomaly.pass_no,
        )
        if row_key in seen_rows:
            continue
        seen_rows.add(row_key)
        deduped.append(anomaly)
    anomalies = deduped

    status = "complete" if not anomalies else "flagged"
    if status == "complete":
        message = (
            f"核对通过：两次遍历的 {len(pass1)} 页目录编号及顺序一致，"
            f"{len(items)} 条列表编号与详情一致，完整清单已原子保存"
        )
    else:
        message = (
            f"发现 {len(anomalies)} 项异常，本轮结果标记为待复查，"
            "未保存为本轮完整清单"
        )

    return RunOutcome(
        catalog_url=catalog_url,
        started_at=started_at,
        finished_at=_now(),
        status=status,
        message=message,
        pass1_pages=pass1,
        pass2_pages=pass2,
        items=items,
        anomalies=anomalies,
    )
