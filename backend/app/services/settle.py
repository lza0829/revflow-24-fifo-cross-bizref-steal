from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts
from app.modules.fifo_match import Exposure, allocate_fifo


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def dashboard() -> dict:
    with connect() as conn:
        accounts = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())
        fwd = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()["c"]
        rev = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '冲正'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE status = '待归并'"
        ).fetchone()["c"]
        q = conn.execute("SELECT COUNT(*) AS c FROM quarantine_items").fetchone()["c"]
        partial = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE status = '部分归并'"
        ).fetchone()["c"]
        bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        alloc = conn.execute(
            "SELECT COALESCE(SUM(allocated_cents), 0) AS s FROM match_allocations"
        ).fetchone()["s"]
        last = conn.execute("SELECT * FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "账户数": len(accounts),
            "正向笔数": fwd,
            "冲正笔数": rev,
            "待归并": pending,
            "部分归并": partial,
            "未平隔离": q,
            "余额合计分": bal,
            "占用合计分": alloc,
            "最近归并": dict(last) if last else None,
            "账户": accounts,
        }


def list_accounts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())


def _allocation_map(conn) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    """返回 (按冲正 id 分组, 按正向 id 分组) 的占用明细，两视图同源于 match_allocations。"""
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT ma.id, ma.reversal_entry_id, ma.reversal_entry_key,
                   ma.forward_entry_id, ma.forward_entry_key,
                   ma.allocated_cents, ma.settle_run_id,
                   f.booked_at AS forward_booked_at, f.biz_ref AS forward_biz_ref,
                   r.biz_ref AS reversal_biz_ref
            FROM match_allocations ma
            JOIN ledger_entries f ON f.id = ma.forward_entry_id
            JOIN ledger_entries r ON r.id = ma.reversal_entry_id
            ORDER BY f.booked_at, f.id
            """
        ).fetchall()
    )
    by_rev: dict[int, list[dict]] = {}
    by_fwd: dict[int, list[dict]] = {}
    for a in rows:
        by_rev.setdefault(a["reversal_entry_id"], []).append(a)
        by_fwd.setdefault(a["forward_entry_id"], []).append(a)
    return by_rev, by_fwd


def list_entries(kind: str | None = None) -> list[dict]:
    with connect() as conn:
        if kind:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                WHERE e.kind = ?
                ORDER BY e.id
                """,
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                ORDER BY e.id
                """
            ).fetchall()

        by_rev, by_fwd = _allocation_map(conn)
        quarantine = {
            r["entry_key"]: dict(r)
            for r in conn.execute("SELECT * FROM quarantine_items ORDER BY id").fetchall()
        }

        out: list[dict] = []
        for row in rows:
            d = dict(row)
            if d["kind"] == "正向":
                allocs = by_fwd.get(d["id"], [])
                d["allocations"] = allocs
                d["allocated_cents"] = sum(a["allocated_cents"] for a in allocs)
                d["remaining_cents"] = d["amount_cents"] - d["allocated_cents"]
            else:
                allocs = by_rev.get(d["id"], [])
                d["allocations"] = allocs
                d["allocated_cents"] = sum(a["allocated_cents"] for a in allocs)
                d["residual_cents"] = max(d["amount_cents"] - d["allocated_cents"], 0)
                q = quarantine.get(d["entry_key"])
                d["quarantine_reason"] = q["reason"] if q else None
                d["fifo_short"] = bool(q and q["reason"] == "FIFO 不足")
                d["quarantine_residual_cents"] = q["amount_cents"] if q else 0
            out.append(d)
        return out


def list_quarantine() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT q.*, a.code AS account_code
            FROM quarantine_items q
            JOIN accounts a ON a.id = q.account_id
            ORDER BY q.id DESC
            """
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            raw = d.pop("detail_json", "") or "{}"
            try:
                d["detail"] = json.loads(raw)
            except ValueError:
                d["detail"] = {}
            d["allocations"] = d["detail"].get("占用", [])
            out.append(d)
        return out


def list_settle_runs() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM settle_runs ORDER BY id DESC").fetchall()
        )


def list_match_allocations(settle_run_id: int | None = None) -> list[dict]:
    with connect() as conn:
        sql = """
            SELECT ma.*, a.code AS account_code,
                   f.booked_at AS forward_booked_at, f.biz_ref AS forward_biz_ref,
                   r.biz_ref AS reversal_biz_ref, r.booked_at AS reversal_booked_at
            FROM match_allocations ma
            JOIN ledger_entries f ON f.id = ma.forward_entry_id
            JOIN ledger_entries r ON r.id = ma.reversal_entry_id
            JOIN accounts a ON a.id = f.account_id
        """
        params: tuple = ()
        if settle_run_id is not None:
            sql += " WHERE ma.settle_run_id = ?"
            params = (settle_run_id,)
        sql += " ORDER BY ma.reversal_entry_id, f.booked_at, f.id"
        return rows_to_dicts(conn.execute(sql, params).fetchall())


def list_verify_reports() -> list[dict]:
    with connect() as conn:
        rows = rows_to_dicts(
            conn.execute("SELECT * FROM verify_reports ORDER BY id DESC").fetchall()
        )
        for d in rows:
            raw = d.pop("detail_json", "") or "{}"
            try:
                d["detail"] = json.loads(raw)
            except ValueError:
                d["detail"] = {}
        return rows


# ---------------------------------------------------------------------------
# 归并
# ---------------------------------------------------------------------------


def _insert_quarantine(
    conn,
    run_id: int,
    now: str,
    *,
    entry_key: str,
    account_id: int,
    reason: str,
    amount_cents: int,
    allocated_cents: int,
    detail: dict,
) -> None:
    """写入隔离项；同一冲正键只落一条，避免重跑产生重复隔离。"""
    exists = conn.execute(
        "SELECT id FROM quarantine_items WHERE entry_key = ? LIMIT 1", (entry_key,)
    ).fetchone()
    if exists:
        return
    conn.execute(
        """
        INSERT INTO quarantine_items(
          entry_key, account_id, reason, amount_cents, allocated_cents,
          detail_json, settle_run_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry_key,
            account_id,
            reason,
            amount_cents,
            allocated_cents,
            json.dumps(detail, ensure_ascii=False),
            run_id,
            now,
        ),
    )


def _recompute_balances(conn, run_id: int) -> int:
    """余额 = 正向量 − 占用总量（同源于 ledger_entries 与 match_allocations）。"""
    conn.execute(
        """
        UPDATE accounts
        SET balance_cents = COALESCE((
              SELECT SUM(f.amount_cents) FROM ledger_entries f
              WHERE f.kind = '正向' AND f.account_id = accounts.id
            ), 0) - COALESCE((
              SELECT SUM(ma.allocated_cents)
              FROM match_allocations ma
              JOIN ledger_entries f ON f.id = ma.forward_entry_id
              WHERE f.account_id = accounts.id
            ), 0)
        """
    )
    touched = conn.execute(
        """
        SELECT DISTINCT account_id FROM (
          SELECT account_id FROM ledger_entries WHERE settle_run_id = ?
          UNION
          SELECT f.account_id FROM match_allocations ma
          JOIN ledger_entries f ON f.id = ma.forward_entry_id
          WHERE ma.settle_run_id = ?
        )
        """,
        (run_id, run_id),
    ).fetchall()
    for row in touched:
        conn.execute(
            "UPDATE accounts SET last_settle_run_id = ? WHERE id = ?", (run_id, row["account_id"])
        )
    return conn.execute(
        "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts"
    ).fetchone()["s"]


def run_settle() -> dict:
    """跑一轮净额归并。

    匹配、占用落库、余额写回与隔离写入在同一事务内完成；任一步异常即整体回滚。
    冲正只认 match_allocations 中已物化的占用：已有占用直接复用，绝不重算挑单。
    """
    with connect() as conn:
        now = _now()
        cur = conn.execute(
            "INSERT INTO settle_runs(status, started_at) VALUES ('进行中', ?)", (now,)
        )
        run_id = cur.lastrowid

        try:
            forwards = rows_to_dicts(
                conn.execute("SELECT * FROM ledger_entries WHERE kind = '正向'").fetchall()
            )
            fwd_by_key = {f["entry_key"]: f for f in forwards}

            allocated: dict[int, int] = {f["id"]: 0 for f in forwards}
            for row in conn.execute(
                """
                SELECT forward_entry_id, SUM(allocated_cents) AS s
                FROM match_allocations GROUP BY forward_entry_id
                """
            ).fetchall():
                if row["forward_entry_id"] in allocated:
                    allocated[row["forward_entry_id"]] = row["s"]

            pending_forwards = [f for f in forwards if f["status"] == "待归并"]
            reversals = rows_to_dicts(
                conn.execute(
                    """
                    SELECT * FROM ledger_entries
                    WHERE kind = '冲正' AND status = '待归并'
                    ORDER BY booked_at, id
                    """
                ).fetchall()
            )

            settled = 0
            quarantined = 0
            partial = 0
            run_forward_cents = sum(f["amount_cents"] for f in pending_forwards)
            run_allocated_cents = 0

            for r in reversals:
                existing = rows_to_dicts(
                    conn.execute(
                        """
                        SELECT ma.forward_entry_id, ma.forward_entry_key,
                               ma.allocated_cents, f.booked_at AS forward_booked_at
                        FROM match_allocations ma
                        JOIN ledger_entries f ON f.id = ma.forward_entry_id
                        WHERE ma.reversal_entry_id = ?
                        ORDER BY f.booked_at, f.id
                        """,
                        (r["id"],),
                    ).fetchall()
                )

                picks: list[tuple[int, str, int]] = []  # (正向 id, 正向键, 分)
                frozen = bool(existing)
                if frozen:
                    picks = [
                        (a["forward_entry_id"], a["forward_entry_key"], a["allocated_cents"])
                        for a in existing
                    ]
                else:
                    target = None
                    if r["target_entry_key"]:
                        target = fwd_by_key.get(r["target_entry_key"])
                        if target and target["account_id"] != r["account_id"]:
                            _insert_quarantine(
                                conn,
                                run_id,
                                now,
                                entry_key=r["entry_key"],
                                account_id=r["account_id"],
                                reason="冲正账户与原单不一致",
                                amount_cents=r["amount_cents"],
                                allocated_cents=0,
                                detail={
                                    "冲正流水键": r["entry_key"],
                                    "冲正金额分": r["amount_cents"],
                                    "已占用分": 0,
                                    "未消化分": r["amount_cents"],
                                    "占用": [],
                                },
                            )
                            conn.execute(
                                "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
                                (run_id, r["id"]),
                            )
                            quarantined += 1
                            continue

                    need = r["amount_cents"]

                    # 显式原单优先，但同样受剩余敞口约束
                    if target:
                        exp = Exposure(
                            entry_id=target["id"],
                            entry_key=target["entry_key"],
                            amount_cents=target["amount_cents"],
                            allocated_cents=allocated.get(target["id"], 0),
                            booked_at=target["booked_at"],
                        )
                        take = min(exp.remaining_cents, need)
                        if take > 0:
                            picks.append((target["id"], target["entry_key"], take))
                            need -= take

                    # 缺口按同账户、同业务单 FIFO 补足
                    if need > 0:
                        refs = {r["biz_ref"]}
                        if target:
                            refs.add(target["biz_ref"])
                        refs.discard("")
                        exclude_id = target["id"] if target else None
                        cands = [
                            Exposure(
                                entry_id=f["id"],
                                entry_key=f["entry_key"],
                                amount_cents=f["amount_cents"],
                                allocated_cents=allocated.get(f["id"], 0),
                                booked_at=f["booked_at"],
                            )
                            for f in forwards
                            if f["id"] != exclude_id
                            and allocated.get(f["id"], 0) < f["amount_cents"]
                        ]
                        fifo_picks, _ = allocate_fifo(cands, need)
                        for exp, take in fifo_picks:
                            picks.append((exp.entry_id, exp.entry_key, take))

                matched = sum(p[2] for p in picks)
                residual = max(r["amount_cents"] - matched, 0)
                alloc_detail = [
                    {"正向流水键": key, "分摊分": cents} for _, key, cents in picks
                ]

                if not frozen:
                    for fwd_id, fwd_key, cents in picks:
                        conn.execute(
                            """
                            INSERT INTO match_allocations(
                              reversal_entry_id, reversal_entry_key,
                              forward_entry_id, forward_entry_key,
                              allocated_cents, settle_run_id, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (r["id"], r["entry_key"], fwd_id, fwd_key, cents, run_id, now),
                        )
                        allocated[fwd_id] = allocated.get(fwd_id, 0) + cents
                    run_allocated_cents += matched

                if matched > 0:
                    status = "已归并"
                    reason = None
                    settled += 1
                else:
                    status = "已隔离"
                    if target:
                        reason = "冲正超额"
                    else:
                        cross_account = bool(r["biz_ref"]) and any(
                            f["account_id"] != r["account_id"] and f["biz_ref"] == r["biz_ref"]
                            for f in forwards
                        )
                        reason = "冲正账户与原单不一致" if cross_account else "冲正无原单"

                if residual > 0 and matched <= 0:
                    before = conn.execute(
                        "SELECT COUNT(*) AS c FROM quarantine_items WHERE entry_key = ?",
                        (r["entry_key"],),
                    ).fetchone()["c"]
                    _insert_quarantine(
                        conn,
                        run_id,
                        now,
                        entry_key=r["entry_key"],
                        account_id=r["account_id"],
                        reason=reason,
                        amount_cents=residual,
                        allocated_cents=matched,
                        detail={
                            "冲正流水键": r["entry_key"],
                            "冲正金额分": r["amount_cents"],
                            "已占用分": matched,
                            "未消化分": residual,
                            "占用": alloc_detail,
                        },
                    )
                    after = conn.execute(
                        "SELECT COUNT(*) AS c FROM quarantine_items WHERE entry_key = ?",
                        (r["entry_key"],),
                    ).fetchone()["c"]
                    quarantined += after - before

                conn.execute(
                    "UPDATE ledger_entries SET status = ?, settle_run_id = ? WHERE id = ?",
                    (status if matched > 0 else status, run_id, r["id"]),
                )

            for f in pending_forwards:
                conn.execute(
                    "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                    (run_id, f["id"]),
                )
                settled += 1

            _recompute_balances(conn, run_id)
            run_net = run_forward_cents - run_allocated_cents
            finished = _now()
            conn.execute(
                """
                UPDATE settle_runs
                SET status = '完成',
                    forward_count = ?,
                    reversal_count = ?,
                    settled_count = ?,
                    quarantine_count = ?,
                    partial_count = ?,
                    allocated_cents = ?,
                    net_cents = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    len(pending_forwards),
                    len(reversals),
                    settled,
                    quarantined,
                    partial,
                    run_allocated_cents,
                    run_net,
                    finished,
                    run_id,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return get_settle_run(run_id)


def get_settle_run(run_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "归并运行不存在")
        return dict(row)


# ---------------------------------------------------------------------------
# 互证
# ---------------------------------------------------------------------------


def run_verify(settle_run_id: int | None = None) -> dict:
    """净额互证：占用表、敞口、余额与报告明细全部同源。"""
    with connect() as conn:
        if settle_run_id is None:
            last = conn.execute("SELECT id FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not last:
                raise HTTPException(400, "尚无归并运行，无法互证")
            settle_run_id = last["id"]
        run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (settle_run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")

        alloc_rows = rows_to_dicts(
            conn.execute(
                """
                SELECT ma.*, f.account_id AS forward_account, f.booked_at AS forward_booked_at,
                       f.biz_ref AS forward_biz_ref, r.account_id AS reversal_account,
                       r.biz_ref AS reversal_biz_ref, r.amount_cents AS reversal_amount_cents,
                       r.booked_at AS reversal_booked_at
                FROM match_allocations ma
                JOIN ledger_entries f ON f.id = ma.forward_entry_id
                JOIN ledger_entries r ON r.id = ma.reversal_entry_id
                ORDER BY ma.reversal_entry_id, f.booked_at, f.id
                """
            ).fetchall()
        )

        fwd_total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()["s"]
        alloc_total = conn.execute(
            "SELECT COALESCE(SUM(allocated_cents), 0) AS s FROM match_allocations"
        ).fetchone()["s"]
        balance_sum = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts"
        ).fetchone()["s"]

        forward_rows = rows_to_dicts(
            conn.execute(
                """
                SELECT f.id, f.entry_key, f.amount_cents, f.biz_ref, f.booked_at,
                       a.code AS account_code,
                       COALESCE(SUM(ma.allocated_cents), 0) AS allocated_cents
                FROM ledger_entries f
                JOIN accounts a ON a.id = f.account_id
                LEFT JOIN match_allocations ma ON ma.forward_entry_id = f.id
                WHERE f.kind = '正向'
                GROUP BY f.id
                ORDER BY f.account_id, f.booked_at, f.id
                """
            ).fetchall()
        )
        exposures = [
            {
                "正向流水键": f["entry_key"],
                "账户": f["account_code"],
                "金额分": f["amount_cents"],
                "已占用分": f["allocated_cents"],
                "剩余敞口分": f["amount_cents"] - f["allocated_cents"],
                "业务单": f["biz_ref"],
                "入账时间": f["booked_at"],
            }
            for f in forward_rows
        ]
        exposure_remaining = sum(e["剩余敞口分"] for e in exposures)

        run_reversals = rows_to_dicts(
            conn.execute(
                """
                SELECT r.*, a.code AS account_code
                FROM ledger_entries r
                JOIN accounts a ON a.id = r.account_id
                WHERE r.kind = '冲正' AND r.settle_run_id = ?
                ORDER BY r.booked_at, r.id
                """,
                (settle_run_id,),
            ).fetchall()
        )

        alloc_by_rev: dict[int, list[dict]] = {}
        for a in alloc_rows:
            alloc_by_rev.setdefault(a["reversal_entry_id"], []).append(a)

        q_by_entry = {
            r["entry_key"]: dict(r)
            for r in conn.execute("SELECT * FROM quarantine_items ORDER BY id").fetchall()
        }

        reversal_detail = []
        partial_closure_ok = True
        alloc_within_reversal_ok = True
        for r in run_reversals:
            allocs = alloc_by_rev.get(r["id"], [])
            allocated_amt = sum(a["allocated_cents"] for a in allocs)
            residual = r["amount_cents"] - allocated_amt
            q = q_by_entry.get(r["entry_key"])
            if allocated_amt > r["amount_cents"]:
                alloc_within_reversal_ok = False
            if 0 < allocated_amt < r["amount_cents"]:
                if not q or q["reason"] != "FIFO 不足" or q["amount_cents"] != residual:
                    partial_closure_ok = False
            reversal_detail.append(
                {
                    "冲正流水键": r["entry_key"],
                    "账户": r["account_code"],
                    "业务单": r["biz_ref"],
                    "冲正金额分": r["amount_cents"],
                    "已占用分": allocated_amt,
                    "未消化分": residual,
                    "状态": r["status"],
                    "隔离原因": q["reason"] if q else None,
                    "占用": [
                        {
                            "正向流水键": a["forward_entry_key"],
                            "分摊分": a["allocated_cents"],
                            "正向入账时间": a["forward_booked_at"],
                            "占用归并#": a["settle_run_id"],
                        }
                        for a in allocs
                    ],
                }
            )

        alloc_account_ok = all(a["forward_account"] == a["reversal_account"] for a in alloc_rows)
        report_alloc_total = sum(a["allocated_cents"] for a in alloc_rows)
        report_vs_table_ok = report_alloc_total == alloc_total
        balance_identity_ok = fwd_total - alloc_total == balance_sum
        exposure_identity_ok = fwd_total - alloc_total == exposure_remaining

        run_q_count = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["c"]

        checks = {
            "占用账户与冲正一致": alloc_account_ok,
            "占用不超冲正额度": alloc_within_reversal_ok,
            "余额=正向-占用": balance_identity_ok,
            "敞口=正向-占用": exposure_identity_ok,
            "部分冲正隔离闭环": partial_closure_ok,
            "报告占用=占用表": report_vs_table_ok,
        }
        consistent = 1 if all(checks.values()) else 0

        detail = {
            "全量正向分": fwd_total,
            "全量占用分": alloc_total,
            "全量流水净额分": fwd_total - alloc_total,
            "全量账户余额分": balance_sum,
            "全量剩余敞口分": exposure_remaining,
            "本轮冲正占用分": sum(
                a["allocated_cents"] for a in alloc_rows if a["settle_run_id"] == settle_run_id
            ),
            "本轮隔离数": run_q_count,
            "校验": checks,
            "每笔冲正占用": reversal_detail,
            "正向敞口": exposures,
        }
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO verify_reports(
              settle_run_id, ledger_net_cents, balance_sum_cents,
              quarantine_count, consistent, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settle_run_id,
                fwd_total - alloc_total,
                balance_sum,
                run_q_count,
                consistent,
                json.dumps(detail, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM verify_reports WHERE id = ?", (rid,)).fetchone()
        out = dict(row)
        out.pop("detail_json", None)
        out["detail"] = detail
        return out
