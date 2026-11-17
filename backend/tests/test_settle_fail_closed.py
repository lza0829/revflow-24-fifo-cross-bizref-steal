"""归并失败闭：匹配、占用、隔离、余额写回要么全成，要么全退。"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

SEED_FILE = "seed_fifo_bizref.json"

from app.main import app  # noqa: E402
from app.services import settle as svc  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_settle_rolls_back_everything_on_failure(client, monkeypatch):
    before_entries = client.get("/api/entries").json()
    before_accounts = client.get("/api/accounts").json()
    before_q = client.get("/api/quarantine").json()
    before_alloc = client.get("/api/match-allocations").json()
    before_runs = client.get("/api/settle-runs").json()

    def boom(conn, run_id):
        raise RuntimeError("模拟余额写回失败")

    monkeypatch.setattr(svc, "_recompute_balances", boom)
    with pytest.raises(RuntimeError):
        client.post("/api/settle-runs")
    monkeypatch.undo()

    # 占用、隔离、流水状态、余额、归并运行均不得残留
    assert client.get("/api/match-allocations").json() == before_alloc
    assert client.get("/api/quarantine").json() == before_q
    assert client.get("/api/settle-runs").json() == before_runs
    after_entries = client.get("/api/entries").json()
    assert [(e["entry_key"], e["status"], e.get("settle_run_id")) for e in after_entries] == [
        (e["entry_key"], e["status"], e.get("settle_run_id")) for e in before_entries
    ]
    assert [(a["code"], a["balance_cents"]) for a in client.get("/api/accounts").json()] == [
        (a["code"], a["balance_cents"]) for a in before_accounts
    ]


def test_settle_succeeds_after_failure(client):
    run = client.post("/api/settle-runs").json()
    assert run["status"] == "完成"
    assert len(client.get("/api/match-allocations").json()) == 4
