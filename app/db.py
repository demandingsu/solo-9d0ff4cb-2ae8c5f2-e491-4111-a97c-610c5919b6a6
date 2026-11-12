"""SQLite persistence for integrity-check runs and complete snapshots.

A complete run is written in a single transaction: the run row, both catalog
passes, the fetched list items, all review flags and the snapshot (with its
source URLs) either all become visible together or none of them do.  A
non-complete run is also written atomically, but never creates a snapshot.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "integrity.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_url     TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT    NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN ('complete', 'flagged', 'failed')),
    message         TEXT    NOT NULL DEFAULT '',
    page_count      INTEGER NOT NULL DEFAULT 0,
    item_count      INTEGER NOT NULL DEFAULT 0,
    pass1_pages     INTEGER NOT NULL DEFAULT 0,
    pass1_items     INTEGER NOT NULL DEFAULT 0,
    pass2_pages     INTEGER NOT NULL DEFAULT 0,
    pass2_items     INTEGER NOT NULL DEFAULT 0,
    snapshot_id     INTEGER,
    needs_review    INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
);

CREATE TABLE IF NOT EXISTS run_pages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    pass_no         INTEGER NOT NULL,
    page_no         INTEGER NOT NULL,
    url             TEXT    NOT NULL,
    item_count      INTEGER NOT NULL,
    ids_json        TEXT    NOT NULL,
    UNIQUE (run_id, pass_no, page_no)
);

CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    page_no         INTEGER NOT NULL,
    position        INTEGER NOT NULL,
    announcement_id TEXT    NOT NULL,
    detail_url      TEXT    NOT NULL,
    catalog_page_url TEXT   NOT NULL,
    title           TEXT    NOT NULL DEFAULT '',
    detail_id       TEXT    NOT NULL DEFAULT '',
    detail_status   TEXT    NOT NULL DEFAULT 'unknown',
    detail_error    TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS anomalies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind            TEXT    NOT NULL,
    page_no         INTEGER,
    url             TEXT    NOT NULL DEFAULT '',
    announcement_id TEXT    NOT NULL DEFAULT '',
    message         TEXT    NOT NULL,
    pass_no         INTEGER,
    is_reviewed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_url     TEXT    NOT NULL,
    collected_at    TEXT    NOT NULL,
    run_id          INTEGER NOT NULL REFERENCES runs(id),
    page_count      INTEGER NOT NULL,
    item_count      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    page_no         INTEGER NOT NULL,
    announcement_id TEXT    NOT NULL,
    detail_url      TEXT    NOT NULL,
    catalog_page_url TEXT   NOT NULL,
    title           TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_runs_catalog ON runs(catalog_url, id);
CREATE INDEX IF NOT EXISTS idx_snapshots_catalog ON snapshots(catalog_url, id);
CREATE INDEX IF NOT EXISTS idx_items_run ON items(run_id, position);
CREATE INDEX IF NOT EXISTS idx_anomalies_run ON anomalies(run_id, id);
CREATE INDEX IF NOT EXISTS idx_snapshot_items_snapshot ON snapshot_items(snapshot_id, position);
"""


def db_path() -> str:
    """Database location, overridable for tests and Docker."""
    return os.environ.get("INTEGRITY_DB_PATH") or str(DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    path = db_path()
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db() -> None:
    with closing(connect()) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


@contextmanager
def _transaction():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def persist_outcome(outcome: Any) -> int:
    """Atomically persist one verification outcome and return the run id.

    ``outcome`` is a :class:`app.crawler.RunOutcome`.  When its status is
    ``complete`` the complete list is written as a new snapshot; otherwise the
    run only records diagnostics and the last complete snapshot is untouched.
    """
    pass1_pages = outcome.pass1_pages
    pass2_pages = outcome.pass2_pages
    items = outcome.items
    anomalies = outcome.anomalies
    page_count = len(pass1_pages)
    item_count = len(items)

    with _transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO runs (
                catalog_url, started_at, finished_at, status, message,
                page_count, item_count, pass1_pages, pass1_items,
                pass2_pages, pass2_items, needs_review
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outcome.catalog_url,
                outcome.started_at,
                outcome.finished_at,
                outcome.status,
                outcome.message,
                page_count,
                item_count,
                len(pass1_pages),
                sum(len(p.items) for p in pass1_pages),
                len(pass2_pages),
                sum(len(p.items) for p in pass2_pages),
                0 if outcome.status == "complete" else 1,
            ),
        )
        run_id = int(cur.lastrowid)

        for pass_no, pages in ((1, pass1_pages), (2, pass2_pages)):
            for p_index, page in enumerate(pages):
                conn.execute(
                    """
                    INSERT INTO run_pages
                        (run_id, pass_no, page_no, url, item_count, ids_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        pass_no,
                        p_index + 1,
                        page.url,
                        len(page.items),
                        json.dumps(
                            [item.announcement_id for item in page.items],
                            ensure_ascii=False,
                        ),
                    ),
                )

        for position, item in enumerate(items, start=1):
            conn.execute(
                """
                INSERT INTO items (
                    run_id, page_no, position, announcement_id, detail_url,
                    catalog_page_url, title, detail_id, detail_status, detail_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    item.page_no,
                    position,
                    item.announcement_id,
                    item.detail_url,
                    item.catalog_page_url,
                    item.title,
                    item.detail_id,
                    item.detail_status,
                    item.detail_error,
                ),
            )

        for anomaly in anomalies:
            conn.execute(
                """
                INSERT INTO anomalies
                    (run_id, kind, page_no, url, announcement_id, message, pass_no)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    anomaly.kind,
                    anomaly.page_no,
                    anomaly.url or "",
                    anomaly.announcement_id or "",
                    anomaly.message,
                    anomaly.pass_no,
                ),
            )

        snapshot_id = None
        if outcome.status == "complete":
            cur = conn.execute(
                """
                INSERT INTO snapshots
                    (catalog_url, collected_at, run_id, page_count, item_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    outcome.catalog_url,
                    outcome.finished_at,
                    run_id,
                    page_count,
                    item_count,
                ),
            )
            snapshot_id = int(cur.lastrowid)
            for position, item in enumerate(items, start=1):
                conn.execute(
                    """
                    INSERT INTO snapshot_items (
                        snapshot_id, position, page_no, announcement_id,
                        detail_url, catalog_page_url, title
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        position,
                        item.page_no,
                        item.announcement_id,
                        item.detail_url,
                        item.catalog_page_url,
                        item.title,
                    ),
                )
            conn.execute(
                "UPDATE runs SET snapshot_id = ? WHERE id = ?",
                (snapshot_id, run_id),
            )

        return run_id


# ---------------------------------------------------------------------------
# Read helpers


def get_run(run_id: int) -> dict[str, Any] | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_run_items(run_id: int) -> list[dict[str, Any]]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE run_id = ? ORDER BY position", (run_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_run_pages(run_id: int) -> list[dict[str, Any]]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM run_pages WHERE run_id = ? ORDER BY pass_no, page_no",
            (run_id,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["ids"] = json.loads(item.pop("ids_json") or "[]")
        result.append(item)
    return result


def get_run_anomalies(run_id: int) -> list[dict[str, Any]]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM anomalies WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_snapshot(snapshot_id: int) -> dict[str, Any] | None:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if not row:
            return None
        snapshot = dict(row)
        items = conn.execute(
            "SELECT * FROM snapshot_items WHERE snapshot_id = ? ORDER BY position",
            (snapshot_id,),
        ).fetchall()
    snapshot["items"] = [dict(item) for item in items]
    return snapshot


def get_latest_snapshot(catalog_url: str) -> dict[str, Any] | None:
    """Latest complete snapshot for a catalog URL (never the current run)."""
    with closing(connect()) as conn:
        row = conn.execute(
            """
            SELECT * FROM snapshots
            WHERE catalog_url = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (catalog_url,),
        ).fetchone()
        if not row:
            return None
        snapshot = dict(row)
        items = conn.execute(
            "SELECT * FROM snapshot_items WHERE snapshot_id = ? ORDER BY position",
            (snapshot["id"],),
        ).fetchall()
    snapshot["items"] = [dict(item) for item in items]
    return snapshot


def get_latest_snapshot_any() -> dict[str, Any] | None:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return get_snapshot(int(row["id"]))


def compact_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    return {
        "id": snapshot["id"],
        "catalog_url": snapshot["catalog_url"],
        "collected_at": snapshot["collected_at"],
        "page_count": snapshot["page_count"],
        "item_count": snapshot["item_count"],
        "run_id": snapshot["run_id"],
        "items": snapshot.get("items", []),
    }
