"""FIFO 按业务单匹配占用测试。

用 fixtures/seed_fifo_bizref.json：冲正只带 biz_ref、不带原正向键。
正向敞口（同账户、ORD-B01）：FWD-BIZ-001 3000@08:00 → FWD-BIZ-002 5000@09:00
→ FWD-BIZ-003 2000@10:00，合计 10000。
冲正：REV-BIZ-001 6000@12:00、REV-BIZ-002 5000@13:00，合计 11000。
另有一笔 4000@07:00 的 ORD-B02，不得被 ORD-B01 的冲正占用。

期望：6000 → 3000+3000；5000 → 2000(002 余)+2000(003)+1000 无敞口可占，
故 REV-BIZ-002 部分归并、余 1000 进隔离「FIFO 不足」。
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

SEED_FILE = "seed_fifo_bizref.json"

from app.main import app  # noqa: E402

RUN_IDS: list[int] = []


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _run(client) -> dict:
    resp = client.post("/api/settle-runs")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    RUN_IDS.append(body["id"])
    return body


def _allocations(client, reversal_key: str) -> list[dict]:
    revs = client.get("/api/entries", params={"kind": "冲正"}).json()
    rev = next(r for r in revs if r["entry_key"] == reversal_key)
    return rev["allocations"]


def test_bizref_only_reversal_allocates_fifo_across_forwards(client):
    run = _run(client)
    assert run["status"] == "完成"

    a1 = _allocations(client, "REV-BIZ-001")
    assert [(x["forward_entry_key"], x["allocated_cents"]) for x in a1] == [
        ("FWD-BIZ-001", 3000),
        ("FWD-BIZ-002", 3000),
    ]

    a2 = _allocations(client, "REV-BIZ-002")
    assert [(x["forward_entry_key"], x["allocated_cents"]) for x in a2] == [
        ("FWD-BIZ-002", 2000),
        ("FWD-BIZ-003", 2000),
    ]

    # 异业务单的正向不得被占用
    fwds = client.get("/api/entries", params={"kind": "正向"}).json()
    f4 = next(f for f in fwds if f["entry_key"] == "FWD-BIZ-004")
    assert f4["allocated_cents"] == 0
    assert f4["remaining_cents"] == 4000


def test_partial_reversal_goes_to_quarantine_with_occupied_reference(client):
    revs = client.get("/api/entries", params={"kind": "冲正"}).json()
    r2 = next(r for r in revs if r["entry_key"] == "REV-BIZ-002")
    assert r2["status"] == "部分归并"
    assert r2["allocated_cents"] == 4000
    assert r2["residual_cents"] == 1000
    assert r2["fifo_short"] is True

    q = client.get("/api/quarantine").json()
    item = next(x for x in q if x["entry_key"] == "REV-BIZ-002")
    assert item["reason"] == "FIFO 不足"
    assert item["amount_cents"] == 1000       # 只隔离未消化部分
    assert item["allocated_cents"] == 4000    # 引用已占用部分
    assert [a["正向流水键"] for a in item["allocations"]] == ["FWD-BIZ-002", "FWD-BIZ-003"]

    # 全额消化的冲正不进隔离
    assert not any(x["entry_key"] == "REV-BIZ-001" for x in q)


def test_allocation_materialized_and_stable_across_reruns(client):
    before = client.get("/api/match-allocations").json()
    assert len(before) == 4

    # 第二轮：流水已归并，不得重算挑单或产生漂移
    run2 = _run(client)
    assert run2["status"] == "完成"
    after = client.get("/api/match-allocations").json()
    assert [(a["reversal_entry_key"], a["forward_entry_key"], a["allocated_cents"]) for a in after] == [
        (a["reversal_entry_key"], a["forward_entry_key"], a["allocated_cents"]) for a in before
    ]
    # 重复归并不得重复隔离
    q = client.get("/api/quarantine").json()
    assert len([x for x in q if x["entry_key"] == "REV-BIZ-002"]) == 1

    # 冲正页仍能看到原单键与分摊
    assert [(x["forward_entry_key"], x["allocated_cents"]) for x in _allocations(client, "REV-BIZ-001")] == [
        ("FWD-BIZ-001", 3000),
        ("FWD-BIZ-002", 3000),
    ]


def test_verify_report_lists_fifo_detail_and_matches_sources(client):
    report = client.post("/api/verify-reports", params={"settle_run_id": RUN_IDS[0]}).json()
    assert report["consistent"] == 1, report["detail"]
    detail = report["detail"]
    assert all(detail["校验"].values())

    table_total = sum(a["allocated_cents"] for a in client.get("/api/match-allocations").json())
    assert detail["全量占用分"] == table_total
    assert detail["全量流水净额分"] == detail["全量账户余额分"]
    assert detail["全量剩余敞口分"] == detail["全量正向分"] - detail["全量占用分"]

    by_key = {r["冲正流水键"]: r for r in detail["每笔冲正占用"]}
    # 每笔冲正都在报告里（包括无占用的），先确认本轮两笔都在
    assert {"REV-BIZ-001", "REV-BIZ-002"} <= set(by_key)
    assert [(x["正向流水键"], x["分摊分"]) for x in by_key["REV-BIZ-002"]["占用"]] == [
        ("FWD-BIZ-002", 2000),
        ("FWD-BIZ-003", 2000),
    ]
    assert by_key["REV-BIZ-002"]["未消化分"] == 1000
    assert by_key["REV-BIZ-002"]["隔离原因"] == "FIFO 不足"

    # 敞口视图同源
    exp = {e["正向流水键"]: e for e in detail["正向敞口"]}
    assert exp["FWD-BIZ-001"]["剩余敞口分"] == 0
    assert exp["FWD-BIZ-004"]["剩余敞口分"] == 4000

    # 余额 = 正向合计 − 占用合计
    accounts = client.get("/api/accounts").json()
    assert sum(a["balance_cents"] for a in accounts) == 10000 + 4000 - 10000


def test_balanced_statuses(client):
    revs = client.get("/api/entries", params={"kind": "冲正"}).json()
    assert next(r for r in revs if r["entry_key"] == "REV-BIZ-001")["status"] == "已归并"
