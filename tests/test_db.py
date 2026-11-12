"""SQLite persistence tests: atomic writes and snapshot retention."""

from __future__ import annotations

import os

import pytest

from app import db
from app.crawler import Anomaly, CatalogItem, CatalogPage, RunOutcome


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "test-integrity.db"
    monkeypatch.setenv("INTEGRITY_DB_PATH", str(path))
    db.init_db()
    return str(path)


def _page(page_no: int, ids: list[str]) -> CatalogPage:
    return CatalogPage(
        page_no=page_no,
        url=f"http://example.test/catalog?page={page_no}",
        items=[
            CatalogItem(
                announcement_id=item_id,
                detail_url=f"http://example.test/n/{item_id}",
                title=f"公告 {item_id}",
                page_no=page_no,
                catalog_page_url=f"http://example.test/catalog?page={page_no}",
            )
            for item_id in ids
        ],
    )


def _outcome(status: str, catalog_url: str = "http://example.test/catalog") -> RunOutcome:
    pages = [_page(1, ["A-1", "A-2"]), _page(2, ["A-3"])]
    items = [item for page in pages for item in page.items]
    anomalies = []
    if status != "complete":
        anomalies = [
            Anomaly(
                kind="catalog_changed",
                message="目录发生变化",
                page_no=1,
                url=pages[0].url,
            )
        ]
    return RunOutcome(
        catalog_url=catalog_url,
        started_at="2026-11-12T01:00:00+00:00",
        finished_at="2026-11-12T01:00:05+00:00",
        status=status,
        message="测试轮次",
        pass1_pages=pages,
        pass2_pages=[_page(1, ["A-1", "A-2"]), _page(2, ["A-3"])],
        items=items,
        anomalies=anomalies,
    )


def test_complete_run_creates_snapshot_with_sources(isolated_db):
    run_id = db.persist_outcome(_outcome("complete"))
    run = db.get_run(run_id)
    assert run["status"] == "complete"
    assert run["needs_review"] == 0
    assert run["snapshot_id"] is not None

    snapshot = db.get_snapshot(int(run["snapshot_id"]))
    assert snapshot["item_count"] == 3
    assert [item["announcement_id"] for item in snapshot["items"]] == [
        "A-1",
        "A-2",
        "A-3",
    ]
    # Sources are saved with every item.
    assert all(item["detail_url"].startswith("http://example.test/") for item in snapshot["items"])
    assert all(item["catalog_page_url"] for item in snapshot["items"])


def test_flagged_run_keeps_previous_snapshot(isolated_db):
    first = db.persist_outcome(_outcome("complete"))
    previous = db.get_snapshot(int(db.get_run(first)["snapshot_id"]))

    flagged = db.persist_outcome(_outcome("flagged"))
    flagged_run = db.get_run(flagged)
    assert flagged_run["status"] == "flagged"
    assert flagged_run["needs_review"] == 1
    assert flagged_run["snapshot_id"] is None

    retained = db.get_latest_snapshot("http://example.test/catalog")
    assert retained is not None
    assert retained["id"] == previous["id"]
    assert len(retained["items"]) == 3


def test_snapshots_are_keyed_per_catalog_url(isolated_db):
    db.persist_outcome(_outcome("complete", "http://example.test/catalog"))
    assert db.get_latest_snapshot("http://other.test/catalog") is None
    assert db.get_latest_snapshot("http://example.test/catalog") is not None


def test_anomalies_are_persisted_and_marked_for_review(isolated_db):
    run_id = db.persist_outcome(_outcome("flagged"))
    anomalies = db.get_run_anomalies(run_id)
    assert len(anomalies) == 1
    assert anomalies[0]["kind"] == "catalog_changed"
    assert anomalies[0]["is_reviewed"] == 0


def test_run_pages_keep_both_passes(isolated_db):
    run_id = db.persist_outcome(_outcome("complete"))
    pages = db.get_run_pages(run_id)
    assert [(page["pass_no"], page["page_no"]) for page in pages] == [
        (1, 1),
        (1, 2),
        (2, 1),
        (2, 2),
    ]
    first_page = pages[0]
    assert first_page["ids"] == ["A-1", "A-2"]


def test_persist_is_atomic_when_write_fails(isolated_db):
    import sqlite3
    from contextlib import closing

    outcome = _outcome("complete")
    original = db.connect

    class FailingConnection:
        def __init__(self, *args, **kwargs):
            self._inner = original()

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def commit(self):
            raise sqlite3.OperationalError("simulated disk failure")

    db.connect = FailingConnection
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.persist_outcome(outcome)
    finally:
        db.connect = original

    # Nothing from the failed transaction is visible.
    assert db.get_latest_snapshot("http://example.test/catalog") is None
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
