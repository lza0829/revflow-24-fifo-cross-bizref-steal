"""按业务单 FIFO 匹配占用。

纯分配逻辑：给定同账户、同业务单的正向敞口，按入账时间（同日按流水 id）
先到先占，直到冲正额度被满足或正向耗尽。落库、审计与归并由归并服务负责；
本模块不读写数据库，也不在请求内存之外保留状态。

占用规则（回归修复后）：
- 冲正只能占用**同账户且同 ``biz_ref``**、仍有剩余敞口的正向；
- 占用顺序严格按 ``(booked_at, entry_id)`` FIFO，不允许跳过更早的未闭口正向；
- 显式原单键只作为校验：原单必须同账户同业务单，且必须是当前最早未闭口正向，
  否则整笔冲正失败闭（不落任何占用）；
- 跨业务单、跨账户、跳号一律失败闭，返回失败原因而不退化为「挑一笔能占的」。
"""
from __future__ import annotations

from dataclasses import dataclass

# 失败原因（同一套文案贯穿占用、隔离、证明与互证）
REASON_ACCOUNT_MISMATCH = "冲正账户与原单不一致"
REASON_CROSS_BIZ = "跨业务单占用"
REASON_OUT_OF_ORDER = "FIFO 乱序占用"
REASON_NO_ORIGINAL = "冲正无原单"
REASON_EXCESS = "冲正超额"
REASON_FIFO_SHORT = "FIFO 不足"


@dataclass(frozen=True)
class Exposure:
    """一笔正向的剩余敞口（已扣除历史占用）。"""

    entry_id: int
    entry_key: str
    amount_cents: int
    allocated_cents: int
    booked_at: str
    account_id: int = 0
    biz_ref: str = ""

    @property
    def remaining_cents(self) -> int:
        return self.amount_cents - self.allocated_cents

    @property
    def ordered_key(self) -> str:
        """有序正向键：入账时间 + 流水 id，可稳定排序且可直接展示。"""
        return f"{self.booked_at}#{self.entry_id:06d}"


@dataclass(frozen=True)
class Pick:
    """占用计划中的一步：某笔正向被占用多少分。"""

    forward_entry_id: int
    forward_entry_key: str
    forward_biz_ref: str
    ordered_key: str
    fifo_rank: int
    cents: int


@dataclass(frozen=True)
class AllocationPlan:
    """一笔冲正的有序占用计划；落库时只能照此落，计划外禁止落账。"""

    picks: tuple[Pick, ...]
    allocated_cents: int
    residual_cents: int
    failure_reason: str

    @property
    def ok(self) -> bool:
        return not self.failure_reason


def allocate_fifo(
    exposures: list[Exposure], amount_cents: int
) -> tuple[list[tuple[Exposure, int]], int]:
    """对给定敞口按 ``(booked_at, entry_id)`` 正向 FIFO 占用。

    返回 ([(正向敞口, 占用分)], 未消化余分)。调用方必须保证 exposures
    已按业务单与账户筛过；本函数只做时间序占用，不跨业务单挑单。
    """
    if amount_cents < 0:
        raise ValueError("冲正额度不能为负")

    remaining = amount_cents
    allocations: list[tuple[Exposure, int]] = []
    for exp in sorted(exposures, key=lambda x: (x.booked_at, x.entry_id)):
        if remaining <= 0:
            break
        take = min(exp.remaining_cents, remaining)
        if take <= 0:
            continue
        allocations.append((exp, take))
        remaining -= take
    return allocations, remaining


def plan_bizref_fifo(
    *,
    reversal_account_id: int,
    reversal_biz_ref: str,
    amount_cents: int,
    target_entry_key: str | None,
    exposures: list[Exposure],
) -> AllocationPlan:
    """生成一笔冲正的有序占用计划（纯函数，不读写库）。

    只有同账户、同 biz_ref、仍有敞口的正向可进入计划；显式原单不构成
    「插队权」，它必须就是最早未闭口正向，否则整笔失败闭。
    """
    if amount_cents <= 0:
        raise ValueError("冲正额度必须为正")

    biz = reversal_biz_ref or ""
    by_key = {e.entry_key: e for e in exposures}
    target = by_key.get(target_entry_key) if target_entry_key else None

    def fail(reason: str) -> AllocationPlan:
        return AllocationPlan((), 0, amount_cents, reason)

    # 显式原单键指向不存在的流水：失败闭，不退化为按业务单挑单
    if target_entry_key and target is None:
        return fail(REASON_NO_ORIGINAL)
    if not biz and target is None:
        return fail(REASON_NO_ORIGINAL)

    # 冲正只带显式原单、不带业务单时：业务单以原单为准
    if not biz and target is not None:
        biz = target.biz_ref or ""

    # 显式原单：账户、业务单必须一致，且不得跳过更早的未闭口正向
    if target is not None:
        if target.account_id != reversal_account_id:
            return fail(REASON_ACCOUNT_MISMATCH)
        if (target.biz_ref or "") != biz:
            return fail(REASON_CROSS_BIZ)
        if target.remaining_cents > 0:
            eligible_now = sorted(
                (
                    e
                    for e in exposures
                    if e.account_id == reversal_account_id
                    and (e.biz_ref or "") == biz
                    and e.remaining_cents > 0
                ),
                key=lambda e: (e.booked_at, e.entry_id),
            )
            if eligible_now and eligible_now[0].entry_id != target.entry_id:
                return fail(REASON_OUT_OF_ORDER)

    eligible = sorted(
        (
            e
            for e in exposures
            if e.account_id == reversal_account_id
            and (e.biz_ref or "") == biz
            and e.remaining_cents > 0
        ),
        key=lambda e: (e.booked_at, e.entry_id),
    )

    if not eligible:
        if target is not None:
            # 原单仍在同账户同业务单内，但已无剩余敞口可占
            return fail(REASON_EXCESS)
        return fail(_no_exposure_reason(exposures, reversal_account_id, biz))

    picks: list[Pick] = []
    ranked = sorted(eligible, key=lambda e: (e.booked_at, e.entry_id))
    rank_of = {e.entry_id: i for i, e in enumerate(ranked, start=1)}
    allocations, remaining = allocate_fifo(eligible, amount_cents)
    for exp, take in allocations:
        picks.append(
            Pick(
                forward_entry_id=exp.entry_id,
                forward_entry_key=exp.entry_key,
                forward_biz_ref=exp.biz_ref or "",
                ordered_key=exp.ordered_key,
                fifo_rank=rank_of[exp.entry_id],
                cents=take,
            )
        )

    allocated = amount_cents - remaining
    if remaining > 0:
        return AllocationPlan(tuple(picks), allocated, remaining, REASON_FIFO_SHORT)
    return AllocationPlan(tuple(picks), allocated, 0, "")


def _no_exposure_reason(
    exposures: list[Exposure], account_id: int, biz_ref: str
) -> str:
    """无可占用敞口时给出失败原因：本户已闭口 > 他户同单 > 无原单。"""
    if not biz_ref:
        return REASON_NO_ORIGINAL
    same_biz = [e for e in exposures if (e.biz_ref or "") == biz_ref]
    if not same_biz:
        return REASON_NO_ORIGINAL
    if any(e.account_id == account_id for e in same_biz):
        # 同账户同业务单的正向已全部闭口，剩余额度无处可占
        return REASON_FIFO_SHORT
    return REASON_ACCOUNT_MISMATCH
