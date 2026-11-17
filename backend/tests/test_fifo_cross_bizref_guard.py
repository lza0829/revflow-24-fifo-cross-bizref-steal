"""跨业务单抢占 / 非 FIFO 乱序占用 回归测试。

基线缺陷：冲正可占用**异业务单/异账户**的正向敞口，且按入账时间**倒序**
挑单（跳号占用），部分冲正也不进隔离。修复后要求：
- 只能占用同账户、同 biz_ref 且仍有敞口的正向；
- 严格按 (booked_at, id) 升序 FIFO；
- 显式原单若跳过更早未闭口正向，整笔失败闭（不落占用、不动余额）；
- 失败闭的冲正落证明与隔离，证明带失败原因。
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
from app.db import connect  # noqa: E402

RUN_IDS: list[int] = []


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _run(client) -> dict:
    body = client.post("/api/settle-runs").json()
    RUN_IDS.append(body["id"])
    return body


def _entry(client, kind: str, key: str) -> dict:
    rows = client.get("/api/entries", params={"kind": kind}).json()
    return next(r for r in rows if r["entry_key"] == key)


def _add_entry(*, kind, entry_key, amount_cents, biz_ref, booked_at, target=None, account_id=1):
    """测试夹具：直接插入一笔待归并流水（服务层无写入接口）。"""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ledger_entries(
              entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '待归并')
            """,
            (entry_key, account_id, kind, amount_cents, biz_ref, target, booked_at),
        )
        conn.commit()


def _insert_reversal(client, *, entry_key, amount_cents, biz_ref, booked_at, target=None):
    _add_entry(
        kind="冲正",
        entry_key=entry_key,
        amount_cents=amount_cents,
        biz_ref=biz_ref,
        booked_at=booked_at,
        target=target,
    )


def test_reversal_takes_earliest_open_forward_first(client):
    _run(client)
    rev = _entry(client, "冲正", "REV-BIZ-001")
    keys = [a["forward_entry_key"] for a in rev["allocations"]]
    assert keys == ["FWD-BIZ-001", "FWD-BIZ-002"], keys
    # 占用必须严格按入账时间升序
    times = [a["forward_booked_at"] for a in rev["allocations"]]
    assert times == sorted(times)


def test_no_cross_biz_ref_steal(client):
    """异业务单正向（ORD-B02 的 FWD-BIZ-004，更早入账）不得被 ORD-B01 占用。"""
    _run(client)
    f4 = _entry(client, "正向", "FWD-BIZ-004")
    assert f4["allocated_cents"] == 0


def test_cross_bizref_reversal_fails_closed_without_allocation(client):
    """冲正 biz_ref 在账户内无正向时，不得改占他单正向；失败闭 + 隔离 + 证明失败原因。"""
    before_balance = sum(a["balance_cents"] for a in client.get("/api/accounts").json())
    before_alloc = client.get("/api/match-allocations").json()
    _insert_reversal(
        client,
        entry_key="REV-XBIZ-001",
        amount_cents=3000,
        biz_ref="ORD-NOPE",
        booked_at="2026-11-16T19:00:00Z",
    )
    run = _run(client)

    assert client.get("/api/match-allocations").json() == before_alloc
    rev = _entry(client, "冲正", "REV-XBIZ-001")
    assert rev["status"] == "已隔离"
    assert rev["allocated_cents"] == 0
    q = client.get("/api/quarantine").json()
    item = next(x for x in q if x["entry_key"] == "REV-XBIZ-001")
    assert item["reason"] == "冲正无原单"
    # 余额不得因跨单抢占而变化
    assert sum(a["balance_cents"] for a in client.get("/api/accounts").json()) == before_balance

    proof = client.get("/api/allocation-proofs", params={"settle_run_id": run["id"]}).json()
    p = next(x for x in proof if x["冲正流水键"] == "REV-XBIZ-001")
    assert p["结论"] == "失败"
    assert p["失败原因"] == "冲正无原单"
    assert p["占用"] == []


def test_explicit_target_allowed_when_it_is_earliest_open(client):
    """显式原单不构成插队权，但若它正是最早未闭口正向，则正常占用。"""
    _add_entry(
        kind="正向",
        entry_key="FWD-ORDT-001",
        amount_cents=2000,
        biz_ref="ORD-T01",
        booked_at="2026-11-16T05:00:00Z",
    )
    _add_entry(
        kind="正向",
        entry_key="FWD-ORDT-002",
        amount_cents=2000,
        biz_ref="ORD-T01",
        booked_at="2026-11-16T06:00:00Z",
    )
    _insert_reversal(
        client,
        entry_key="REV-ORDT-001",
        amount_cents=2000,
        biz_ref="ORD-T01",
        booked_at="2026-11-16T19:30:00Z",
        target="FWD-ORDT-001",
    )
    run = _run(client)
    rev = _entry(client, "冲正", "REV-ORDT-001")
    assert rev["allocated_cents"] == 2000
    assert [a["forward_entry_key"] for a in rev["allocations"]] == ["FWD-ORDT-001"]
    assert _entry(client, "正向", "FWD-ORDT-002")["allocated_cents"] == 0

    proof = client.get("/api/allocation-proofs", params={"settle_run_id": run["id"]}).json()
    p = next(x for x in proof if x["冲正流水键"] == "REV-ORDT-001")
    assert p["结论"] == "占用"
    assert [s["FIFO 序"] for s in p["占用"]] == [1]
    assert p["占用"][0]["有序正向键"].endswith("#000001") or "#" in p["占用"][0]["有序正向键"]


def test_out_of_order_reversal_rejected_when_earlier_forward_open(client):
    """构造：同单存在更早未闭口正向时，指向较晚正向的冲正必须失败闭。"""
    _add_entry(
        kind="正向",
        entry_key="FWD-ORDQ-001",
        amount_cents=2000,
        biz_ref="ORD-Q01",
        booked_at="2026-11-16T05:00:00Z",
    )
    _add_entry(
        kind="正向",
        entry_key="FWD-ORDQ-002",
        amount_cents=2000,
        biz_ref="ORD-Q01",
        booked_at="2026-11-16T06:00:00Z",
    )
    _add_entry(
        kind="冲正",
        entry_key="REV-ORDQ-SKIP",
        amount_cents=2000,
        biz_ref="ORD-Q01",
        booked_at="2026-11-16T20:00:00Z",
        target="FWD-ORDQ-002",
    )
    before_alloc = client.get("/api/match-allocations").json()
    run = _run(client)

    assert client.get("/api/match-allocations").json() == before_alloc
    rev = _entry(client, "冲正", "REV-ORDQ-SKIP")
    assert rev["status"] == "已隔离"
    assert rev["allocated_cents"] == 0
    q = next(
        x for x in client.get("/api/quarantine").json() if x["entry_key"] == "REV-ORDQ-SKIP"
    )
    assert q["reason"] == "FIFO 乱序占用"

    proof = client.get("/api/allocation-proofs", params={"settle_run_id": run["id"]}).json()
    p = next(x for x in proof if x["冲正流水键"] == "REV-ORDQ-SKIP")
    assert p["结论"] == "失败"
    assert p["失败原因"] == "FIFO 乱序占用"


def test_allocation_proofs_queryable_by_run_and_same_source(client):
    """证明按归并运行 id 可查，且与占用表、敞口、余额同源。"""
    run_id = RUN_IDS[0]
    proofs = client.get("/api/allocation-proofs", params={"settle_run_id": run_id}).json()
    assert proofs, "任何一轮归并都必须留下占用证明"
    assert all(p["归并运行#"] == run_id for p in proofs)

    allocs = client.get("/api/match-allocations", params={"settle_run_id": run_id}).json()
    proof_total = sum(p["占用分"] for p in proofs)
    assert proof_total == sum(a["allocated_cents"] for a in allocs)

    for p in proofs:
        for s in p["占用"]:
            famt = sum(
                a["allocated_cents"]
                for a in allocs
                if a["reversal_entry_key"] == p["冲正流水键"]
                and a["forward_entry_key"] == s["正向流水键"]
            )
            assert famt == s["占用分"]
            assert "#" in s["有序正向键"]  # 形如 2026-11-16T08:00:00Z#000001
            assert s["FIFO 序"] >= 1

    # 流水页每笔冲正带证明，明细同源
    rev = _entry(client, "冲正", "REV-BIZ-002")
    assert rev["allocation_proof"]["失败原因"] == "FIFO 不足"
    assert [
        (s["正向流水键"], s["占用分"]) for s in rev["allocation_proof"]["占用"]
    ] == [(a["forward_entry_key"], a["allocated_cents"]) for a in rev["allocations"]]


def test_verify_checks_include_proof_and_fifo_guard(client):
    run_id = RUN_IDS[0]
    report = client.post("/api/verify-reports", params={"settle_run_id": run_id}).json()
    assert report["consistent"] == 1, report["detail"]["校验"]
    checks = report["detail"]["校验"]
    assert checks["占用证明=占用表"] is True
    assert checks["占用证明同单同户"] is True
    assert checks["占用严格 FIFO"] is True
    assert report["detail"]["占用证明"]


def test_rerun_keeps_proof_and_allocation_stable(client):
    before = client.get("/api/allocation-proofs").json()
    _run(client)
    after = client.get("/api/allocation-proofs").json()
    # 第二轮不重算已归并冲正，首轮证明不漂移
    first_run_before = [p for p in before if p["归并运行#"] == RUN_IDS[0]]
    first_run_after = [p for p in after if p["归并运行#"] == RUN_IDS[0]]
    assert [
        (p["冲正流水键"], p["占用分"], p["失败原因"]) for p in first_run_before
    ] == [(p["冲正流水键"], p["占用分"], p["失败原因"]) for p in first_run_after]


def test_later_reversal_cannot_skip_earlier_open_forward(client):
    """第二笔冲正在同单更早正向仍未闭口时，不得跳号占用较晚正向。"""
    _add_entry(
        kind="正向",
        entry_key="FWD-ORDJ-001",
        amount_cents=3000,
        biz_ref="ORD-J01",
        booked_at="2026-11-16T05:00:00Z",
    )
    _add_entry(
        kind="正向",
        entry_key="FWD-ORDJ-002",
        amount_cents=3000,
        biz_ref="ORD-J01",
        booked_at="2026-11-16T06:00:00Z",
    )
    _insert_reversal(
        client,
        entry_key="REV-ORDJ-001",
        amount_cents=1000,
        biz_ref="ORD-J01",
        booked_at="2026-11-16T20:00:00Z",
    )
    _insert_reversal(
        client,
        entry_key="REV-ORDJ-002",
        amount_cents=1000,
        biz_ref="ORD-J01",
        booked_at="2026-11-16T21:00:00Z",
    )
    run = _run(client)

    a1 = [x["forward_entry_key"] for x in _entry(client, "冲正", "REV-ORDJ-001")["allocations"]]
    a2 = [x["forward_entry_key"] for x in _entry(client, "冲正", "REV-ORDJ-002")["allocations"]]
    assert a1 == ["FWD-ORDJ-001"]
    # 第二笔接续占用同一最早正向的余量，而非跳过它去占 FWD-ORDJ-002
    assert a2 == ["FWD-ORDJ-001"]
    assert _entry(client, "正向", "FWD-ORDJ-002")["allocated_cents"] == 0

    proofs = client.get("/api/allocation-proofs", params={"settle_run_id": run["id"]}).json()
    p2 = next(x for x in proofs if x["冲正流水键"] == "REV-ORDJ-002")
    assert p2["占用"][0]["正向流水键"] == "FWD-ORDJ-001"
    assert p2["占用"][0]["FIFO 序"] == 1
