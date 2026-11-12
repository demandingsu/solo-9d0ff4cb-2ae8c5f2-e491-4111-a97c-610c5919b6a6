"""Unit tests for parsing and two-pass comparison logic (no network)."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from app.crawler import (
    CatalogItem,
    CatalogPage,
    _compare_passes,
    _duplicate_anomalies,
    extract_detail_id,
    find_next_url,
    normalize_id,
    parse_catalog,
)

TABLE_HTML = """
<html><body>
<table class="list-table">
<tr><th>标题</th><th>编号</th></tr>
<tr><td><a href="/notice/n1">道路工程招标</a></td>
    <td>项目编号:GC-2026-001</td><td>2026-11-01</td></tr>
<tr><td><a href="/notice/n2">排水工程招标</a></td>
    <td>公告编号：GC-2026-002</td></tr>
</table>
<div class="pagination">
  <a href="/list?page=1">1</a>
  <a class="next" href="/list?page=2" rel="next">下一页</a>
</div>
</body></html>
"""

LIST_HTML = """
<ul class="notice-list">
  <li data-notice-id="AB-2026-10"><a href="/n/10">公告十</a><span>2026-10-10</span></li>
  <li data-notice-id="AB-2026-11"><a href="/n/11">公告十一</a><span>2026-11-11</span></li>
</ul>
<a href="/list?page=2" class="page-next" aria-label="下一页">下一页</a>
"""


def test_normalize_id_strips_punctuation_and_whitespace():
    assert normalize_id("  GC-2026-001 ") == "GC-2026-001"
    assert normalize_id("【ZC-2026-X9】") == "ZC-2026-X9"
    assert normalize_id("编号： AB 2026 001") == "编号：AB2026001" or normalize_id(
        "编号： AB 2026 001"
    ).endswith("AB2026001")


def test_parse_table_layout_preserves_order():
    items = parse_catalog(TABLE_HTML, "http://example.test/list?page=1")
    assert [item.announcement_id for item in items] == [
        "GC-2026-001",
        "GC-2026-002",
    ]
    assert items[0].detail_url == "http://example.test/notice/n1"
    assert items[0].title == "道路工程招标"


def test_parse_list_layout_with_data_attributes():
    items = parse_catalog(LIST_HTML, "http://example.test/list")
    assert [item.announcement_id for item in items] == ["AB-2026-10", "AB-2026-11"]
    assert items[1].detail_url == "http://example.test/n/11"


def test_pagination_link_is_not_treated_as_notice():
    items = parse_catalog(TABLE_HTML, "http://example.test/list?page=1")
    assert all(item.detail_url != "http://example.test/list?page=2" for item in items)


def test_find_next_url_forms():
    soup = BeautifulSoup(TABLE_HTML, "html.parser")
    assert (
        find_next_url(soup, "http://example.test/list?page=1")
        == "http://example.test/list?page=2"
    )
    soup2 = BeautifulSoup(LIST_HTML, "html.parser")
    assert (
        find_next_url(soup2, "http://example.test/list")
        == "http://example.test/list?page=2"
    )


def test_find_next_url_uses_numeric_query_param():
    html = '<div class="pagination"><a href="/catalog?p=1">1</a><a href="/catalog?p=2">2</a></div>'
    soup = BeautifulSoup(html, "html.parser")
    assert (
        find_next_url(soup, "http://example.test/catalog?p=1")
        == "http://example.test/catalog?p=2"
    )


def test_find_next_url_empty_on_last_page():
    html = '<div><a href="/catalog?page=1">首页</a></div>'
    soup = BeautifulSoup(html, "html.parser")
    assert find_next_url(soup, "http://example.test/catalog?page=2") == ""


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<p>项目编号：GC-2026-001</p>", "GC-2026-001"),
        ('<dd data-notice-id="ZC-2026-X9">ZC-2026-X9</dd>', "ZC-2026-X9"),
        ("<span>【招标编号 ZC-2026-X9】</span>", "ZC-2026-X9"),
        ("<meta name='notice-id' content='GG-2026-77'>", "GG-2026-77"),
    ],
)
def test_extract_detail_id_variants(html, expected):
    assert extract_detail_id(html) == expected


def _page(page_no: int, ids: list[str]) -> CatalogPage:
    return CatalogPage(
        page_no=page_no,
        url=f"http://example.test/list?page={page_no}",
        items=[
            CatalogItem(
                announcement_id=item_id,
                detail_url=f"http://example.test/n/{item_id}",
                page_no=page_no,
            )
            for item_id in ids
        ],
    )


def test_compare_passes_detects_order_and_content_change():
    first = [_page(1, ["A", "B"]), _page(2, ["C", "D"])]
    second = [_page(1, ["X", "A"]), _page(2, ["C", "D"])]
    anomalies = _compare_passes(first, second)
    assert len(anomalies) == 1
    assert anomalies[0].kind == "catalog_changed"
    assert anomalies[0].page_no == 1


def test_compare_passes_detects_page_count_change():
    first = [_page(1, ["A"]), _page(2, ["B"])]
    second = [_page(1, ["A"]), _page(2, ["B"]), _page(3, ["C"])]
    anomalies = _compare_passes(first, second)
    assert any(item.kind == "catalog_changed" for item in anomalies)


def test_compare_passes_clean_when_identical():
    first = [_page(1, ["A", "B"])]
    second = [_page(1, ["A", "B"])]
    assert _compare_passes(first, second) == []


def test_compare_passes_flags_changed_detail_link():
    first = [
        CatalogPage(
            page_no=1,
            url="u1",
            items=[
                CatalogItem("A", "http://example.test/old", page_no=1),
            ],
        )
    ]
    second = [
        CatalogPage(
            page_no=1,
            url="u1",
            items=[
                CatalogItem("A", "http://example.test/new", page_no=1),
            ],
        )
    ]
    anomalies = _compare_passes(first, second)
    assert len(anomalies) == 1
    assert anomalies[0].kind == "catalog_link_changed"


def test_duplicate_detection_across_pages():
    pages = [_page(1, ["A", "B"]), _page(2, ["B", "C"])]
    anomalies = _duplicate_anomalies(pages, pass_no=1)
    assert len(anomalies) == 1
    assert anomalies[0].kind == "duplicate_id"
    assert anomalies[0].announcement_id == "B"
    assert "跨页编号重复" in anomalies[0].message


def test_duplicate_detection_within_same_page():
    pages = [_page(1, ["A", "A", "B"])]
    anomalies = _duplicate_anomalies(pages, pass_no=1)
    assert len(anomalies) == 1
    assert "同一页内编号重复" in anomalies[0].message
