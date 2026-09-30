"""冷链追责核心领域逻辑（不依赖 FastAPI / PostgreSQL，可独立测试）。

事件分两类：
- 测温事件 measurement：只影响越温区间的开合，不改变保管方；
- 交接事件 handover：只改变保管方，要求交出方 == 当前保管方。

越温区间：温度 > 温度上限时开启（含测温时刻），之后第一笔温度 <= 上限
的测温将其关闭（关闭时刻为该测温时刻）。区间跨交接时，以交接时刻为界
拆分责任，交接时刻起归接收方。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional


# 事件类型常量
MEASUREMENT = "measurement"
HANDOVER = "handover"


class DomainError(Exception):
    """业务规则错误。code 决定返回给客户端的 HTTP 状态码。"""

    def __init__(self, code: str, message: str, *, event_id: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.event_id = event_id

    def to_dict(self) -> dict:
        d = {"detail": self.message, "code": self.code}
        if self.event_id is not None:
            d["event_id"] = self.event_id
        return d


@dataclass(frozen=True)
class Cargo:
    cargo_no: str
    start_time: object  # datetime（UTC）
    temp_upper_limit: Decimal
    initial_holder: str


@dataclass(frozen=True)
class Event:
    event_id: str
    cargo_no: str
    kind: str
    occurred_at: object  # datetime（UTC）
    temperature: Optional[Decimal] = None
    from_party: Optional[str] = None
    to_party: Optional[str] = None


def ensure_not_before_start(events: List[Event], cargo: Cargo) -> None:
    """事件发生时间早于货物起始时间则拒绝（等于起始时间允许）。"""
    for e in events:
        if e.occurred_at < cargo.start_time:
            raise DomainError(
                "event_before_start",
                f"事件 {e.event_id} 发生时间 {e.occurred_at.isoformat()} 早于货物 "
                f"起始时间 {cargo.start_time.isoformat()}",
                event_id=e.event_id,
            )


def ensure_no_same_time_conflicts(events: List[Event]) -> None:
    """同一货物同一时刻不得存在多笔测温或多笔交接。

    同一时刻的一笔测温 + 一笔交接不冲突：交接时刻同时关闭测温区间的
    边界语义由半开区间自然处理（交接时刻起归接收方）。
    """
    seen = {}
    for e in events:
        key = (e.cargo_no, e.occurred_at, e.kind)
        if key in seen:
            raise DomainError(
                "same_time_conflict",
                f"货物 {e.cargo_no} 在 {e.occurred_at.isoformat()} 存在多笔"
                + ("测温" if e.kind == MEASUREMENT else "交接")
                + f"事件：{seen[key]} 与 {e.event_id}",
                event_id=e.event_id,
            )
        seen[key] = True


def _decimal_equal(a: object, b: object) -> bool:
    if a is None or b is None:
        return a is b
    return Decimal(a) == Decimal(b)


def find_replayed(existing: List[Event], candidate: Event) -> Optional[Event]:
    """同 event_id 的既有事件。"""
    for e in existing:
        if e.event_id == candidate.event_id:
            return e
    return None


def events_semantically_equal(a: Event, b: Event) -> bool:
    """同 event_id 下，内容是否完全一致（同内容重传幂等，异内容拒绝）。"""
    return (
        a.cargo_no == b.cargo_no
        and a.kind == b.kind
        and a.occurred_at == b.occurred_at
        and _decimal_equal(a.temperature, b.temperature)
        and a.from_party == b.from_party
        and a.to_party == b.to_party
    )


def ensure_replay_consistent(existing: List[Event], candidate: Event) -> Event:
    """校验同 ID 重传：同内容返回既有事件（幂等），异内容整体拒绝。"""
    old = find_replayed(existing, candidate)
    if old is not None and not events_semantically_equal(old, candidate):
        raise DomainError(
            "event_id_content_mismatch",
            f"事件 ID {candidate.event_id} 已存在但内容不同，重传被拒绝",
            event_id=candidate.event_id,
        )
    return old


def reconstruct_chain(cargo: Cargo, events: List[Event]) -> dict:
    """按发生时间重放事件，重建保管链与越温责任。

    返回:
        {
          "custody": [(start, end_or_None, holder)],   # 保管方片段（按时间）
          "violations": [                              # 越温区间
            {"start": dt, "end": dt|None,
             "segments": [{"start": dt, "end": dt, "party": holder}]}
          ],
        }

    保管方片段与越温片段均为左闭右开 [start, end)；end 为 None 表示仍持续。
    区间在交接时刻 t 拆分：[.., t) 归交出方，[t, ..) 归接收方。
    """
    # 交接按发生时间重放；测温按发生时间扫描越温开合。
    handovers = sorted((e for e in events if e.kind == HANDOVER),
                       key=lambda e: e.occurred_at)
    measurements = sorted((e for e in events if e.kind == MEASUREMENT),
                          key=lambda e: e.occurred_at)

    # 1) 保管链：从起始保管方开始，逐笔交接；交出方不等于当时保管方则拒绝。
    custody: List[list] = []  # [start, end, holder]
    current_holder = cargo.initial_holder
    seg_start = cargo.start_time
    for h in handovers:
        if h.from_party != current_holder:
            raise DomainError(
                "invalid_custodian",
                f"交接 {h.event_id}：交出方 {h.from_party} 不是 {h.occurred_at.isoformat()} "
                f"时刻的保管方 {current_holder}，写入整体拒绝",
                event_id=h.event_id,
            )
        # 交接时刻等于片段起点时（如交接恰在货物起始时刻）跳过零长度片段。
        if h.occurred_at > seg_start:
            custody.append([seg_start, h.occurred_at, current_holder])
        current_holder = h.to_party
        seg_start = h.occurred_at
    custody.append([seg_start, None, current_holder])

    # 2) 越温区间：> 上限开启，之后首笔 <= 上限的测温关闭。
    violations: List[dict] = []
    open_start = None
    for m in measurements:
        if m.temperature > cargo.temp_upper_limit:
            if open_start is None:
                open_start = m.occurred_at
        else:
            if open_start is not None:
                violations.append({"start": open_start, "end": m.occurred_at})
                open_start = None
    if open_start is not None:
        violations.append({"start": open_start, "end": None})

    # 3) 越温区间按保管链片段切分责任。
    result_violations = []
    for v in violations:
        segments = []
        for c_start, c_end, holder in custody:
            s = max(v["start"], c_start)
            v_end = v["end"]
            # 任一上端为 None（未结束）则取另一个；均为 None 则仍为 None。
            if v_end is None:
                e = c_end
            elif c_end is None:
                e = v_end
            else:
                e = min(v_end, c_end)
            # 半开区间要求 s < e；零长度片段（交接时刻恰好为区间起点/终点）不产生责任。
            if e is None or s < e:
                segments.append({"start": s, "end": e, "party": holder})
        result_violations.append({
            "start": v["start"],
            "end": v["end"],
            "segments": segments,
        })

    return {"custody": custody, "violations": result_violations}


def validate_and_replay(cargo: Cargo, events: List[Event]) -> dict:
    """一次写入的完整校验 + 重建（任何失败都抛出 DomainError，调用方整体回滚）。"""
    ensure_not_before_start(events, cargo)
    ensure_no_same_time_conflicts(events)
    return reconstruct_chain(cargo, events)
