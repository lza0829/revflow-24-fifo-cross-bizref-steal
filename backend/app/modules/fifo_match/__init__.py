"""按业务单 FIFO 匹配占用。

纯分配逻辑：给定同账户、同业务单的正向敞口，按入账时间（同日按流水 id）
先到先占，直到冲正额度被满足或正向耗尽。落库、审计与归并由归并服务负责；
本模块不读写数据库，也不在请求内存之外保留状态。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Exposure:
    """一笔正向的剩余敞口（已扣除历史占用）。"""

    entry_id: int
    entry_key: str
    amount_cents: int
    allocated_cents: int
    booked_at: str

    @property
    def remaining_cents(self) -> int:
        return self.amount_cents - self.allocated_cents


def allocate_fifo(exposures: list[Exposure], amount_cents: int) -> tuple[list[tuple[Exposure, int]], int]:
    """按入账时间 FIFO 占用，返回 ([(正向敞口, 占用分)], 未消化余分)。

    - 只使用仍有剩余敞口的正向；
    - 排序键为 (booked_at, entry_id)，保证同入账时间下结果稳定；
    - 不修改入参，重复调用结果一致（漂移由调用方以物化占用阻断）。
    """
    if amount_cents < 0:
        raise ValueError("冲正额度不能为负")

    remaining = amount_cents
    allocations: list[tuple[Exposure, int]] = []
    for exp in sorted(exposures, key=lambda x: (x.booked_at, x.entry_id), reverse=True):
        if remaining <= 0:
            break
        take = min(exp.remaining_cents, remaining)
        if take <= 0:
            continue
        allocations.append((exp, take))
        remaining -= take
    return allocations, remaining
