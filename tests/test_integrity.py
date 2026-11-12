"""End-to-end tests against a real local uvicorn server.

The application crawls itself over HTTP, exactly like it will in Docker.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import tempfile
import threading
import time

import pytest
import requests
import uvicorn


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def db_file() -> str:
    handle, path = tempfile.mkstemp(prefix="integrity-test-", suffix=".db")
    os.close(handle)
    os.unlink(path)
    yield path
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass


@pytest.fixture(scope="session")
def server(db_file: str):
    os.environ["INTEGRITY_DB_PATH"] = db_file

    from app.main import app  # imported after the env var is set

    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", lifespan="on"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while time.time() < deadline:
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError("test server exited early")
        time.sleep(0.05)
    else:
        raise RuntimeError("test server did not start")

    base = f"http://127.0.0.1:{port}"
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _use(base: str, scenario: str) -> None:
    requests.get(f"{base}/demo/use", params={"scenario": scenario}, timeout=10)


def _check(base: str) -> dict:
    response = requests.post(
        f"{base}/api/check",
        json={"catalog_url": f"{base}/demo/catalog"},
        timeout=120,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _snapshot_count(db_file: str) -> int:
    with sqlite3.connect(db_file) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])


def _run_row(db_file: str, run_id: int) -> dict:
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else {}


def _anomaly_kinds(result: dict) -> set[str]:
    return {item["kind"] for item in result["anomalies"]}


def test_stable_catalog_is_saved_atomically(server, db_file):
    _use(server, "stable")
    before = _snapshot_count(db_file)
    result = _check(server)

    assert result["status"] == "complete"
    assert result["page_count"] == 3
    assert result["item_count"] == 12
    assert result["anomalies"] == []
    snapshot = result["snapshot"]
    assert snapshot is not None
    assert snapshot["page_count"] == 3
    assert snapshot["item_count"] == 12
    assert len(snapshot["items"]) == 12
    # Every saved entry keeps its source URLs.
    assert all(item["catalog_page_url"] for item in snapshot["items"])
    assert all(item["detail_url"] for item in snapshot["items"])
    assert _snapshot_count(db_file) == before + 1

    run = _run_row(db_file, result["run_id"])
    assert run["status"] == "complete"
    assert run["needs_review"] == 0
    assert run["snapshot_id"] is not None


def test_change_between_passes_is_flagged_and_old_snapshot_kept(server, db_file):
    _use(server, "stable")
    stable = _check(server)
    assert stable["status"] == "complete"
    previous_snapshot_id = stable["snapshot"]["id"]

    snapshots_before = _snapshot_count(db_file)
    _use(server, "shift")
    shifted = _check(server)

    assert shifted["status"] == "flagged"
    assert "catalog_changed" in _anomaly_kinds(shifted)
    assert shifted["snapshot"] is None
    assert _snapshot_count(db_file) == snapshots_before  # nothing overwritten

    previous = shifted["previous_snapshot"]
    assert previous is not None
    assert previous["id"] == previous_snapshot_id
    assert previous["collected_at"]
    grab = requests.get(f"{server}/runs/{shifted['run_id']}", timeout=10)
    assert grab.status_code == 200
    assert "注意：以下为上次完整清单，不是本轮结果" in grab.text
    assert "待复查" in grab.text


def test_duplicate_id_across_pages_is_flagged(server, db_file):
    _use(server, "duplicate")
    result = _check(server)
    assert result["status"] == "flagged"
    assert "duplicate_id" in _anomaly_kinds(result)
    duplicate = [a for a in result["anomalies"] if a["kind"] == "duplicate_id"][0]
    assert "SZ-2026-0003" in duplicate["announcement_id"]
    assert "第 1 页、第 2 页" in duplicate["message"]
    assert result["snapshot"] is None
    assert _run_row(db_file, result["run_id"])["needs_review"] == 1


def test_detail_id_mismatch_is_flagged(server, db_file):
    _use(server, "mismatch")
    result = _check(server)
    assert result["status"] == "flagged"
    assert "detail_mismatch" in _anomaly_kinds(result)
    mismatch = [a for a in result["anomalies"] if a["kind"] == "detail_mismatch"][0]
    assert mismatch["announcement_id"] == "SZ-2026-0002"
    assert "SZ-2026-8888" in mismatch["message"]


def test_detail_fetch_failure_is_flagged(server, db_file):
    _use(server, "detail_fail")
    result = _check(server)
    assert result["status"] == "flagged"
    assert "detail_fetch_failed" in _anomaly_kinds(result)
    failure = [a for a in result["anomalies"] if a["kind"] == "detail_fetch_failed"][0]
    assert failure["announcement_id"] == "SZ-2026-0007"
    assert "HTTP 404" in failure["message"]


def test_form_submission_redirects_to_result(server):
    _use(server, "stable")
    response = requests.post(
        f"{server}/check",
        data={"catalog_url": f"{server}/demo/catalog"},
        allow_redirects=False,
        timeout=120,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/runs/")
    page = requests.get(f"{server}{location}", timeout=10)
    assert page.status_code == 200
    assert "本轮完整清单（已保存）" in page.text


def test_demo_pages_are_offline_and_self_contained(server):
    for path in ("/", "/demo/", "/demo/catalog?scenario=stable"):
        response = requests.get(f"{server}{path}", timeout=10)
        assert response.status_code == 200
        # CSS is inlined and no remote hosts are referenced: works without internet.
        assert '<link rel="stylesheet"' not in response.text
        assert "<script src=" not in response.text
        assert "https://" not in response.text
        for line in response.text.splitlines():
            if "http://" in line:
                assert "127.0.0.1" in line or "localhost" in line
