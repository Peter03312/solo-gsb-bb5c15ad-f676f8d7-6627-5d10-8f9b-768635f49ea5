"""业务服务层：在事务内校验、写入并按需重建保管链。

任何 DomainError 都会导致当前事务回滚——“写入整体拒绝，既有结果保持不变”。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from psycopg import errors as pg_errors

from . import db
from .domain import (
    Cargo, Event, DomainError,
    MEASUREMENT, HANDOVER,
    events_semantically_equal, validate_and_replay, reconstruct_chain,
)

EVENT_COLUMNS = (
    "event_id, cargo_no, kind, occurred_at, temperature, "
    "from_party, to_party, created_at"
)


def _row_to_cargo(row) -> Cargo:
    return Cargo(
        cargo_no=row["cargo_no"],
        start_time=row["start_time"],
        temp_upper_limit=row["temp_upper_limit"],
        initial_holder=row["initial_holder"],
    )


def _row_to_event(row) -> Event:
    return Event(
        event_id=row["event_id"],
        cargo_no=row["cargo_no"],
        kind=row["kind"],
        occurred_at=row["occurred_at"],
        temperature=row["temperature"],
        from_party=row["from_party"],
        to_party=row["to_party"],
    )


def create_cargo(data) -> dict:
    params = (data.cargo_no, data.start_time, data.temp_upper_limit,
              data.initial_holder)
    with db.transaction() as conn:
        try:
            row = conn.execute(
                """
                INSERT INTO cargo (cargo_no, start_time, temp_upper_limit,
                                   initial_holder)
                VALUES (%s, %s, %s, %s)
                RETURNING cargo_no, start_time, temp_upper_limit,
                          initial_holder, created_at
                """,
                params,
            ).fetchone()
        except pg_errors.UniqueViolation:
            raise DomainError(
                "cargo_exists", f"货物编号 {data.cargo_no} 已存在")
        return dict(row)


def _get_cargo_for_update(conn, cargo_no: str) -> Cargo:
    # 锁定货物行：同一货物的并发写入串行化，避免并发绕过校验。
    row = conn.execute(
        "SELECT * FROM cargo WHERE cargo_no = %s FOR UPDATE",
        (cargo_no,),
    ).fetchone()
    if row is None:
        raise DomainError("cargo_not_found", f"货物 {cargo_no} 不存在")
    return _row_to_cargo(row)


def _get_cargo(conn, cargo_no: str) -> Tuple[Cargo, dict]:
    row = conn.execute(
        "SELECT * FROM cargo WHERE cargo_no = %s", (cargo_no,)
    ).fetchone()
    if row is None:
        raise DomainError("cargo_not_found", f"货物 {cargo_no} 不存在")
    return _row_to_cargo(row), dict(row)


def _existing_event_rows(conn, cargo_no: str) -> List[dict]:
    rows = conn.execute(
        f"SELECT {EVENT_COLUMNS} FROM events WHERE cargo_no = %s "
        f"ORDER BY occurred_at, event_id",
        (cargo_no,),
    ).fetchall()
    return [dict(r) for r in rows]


def submit_event(candidate: Event) -> Tuple[dict, bool]:
    """写入一笔测温 / 交接事件。

    返回 (事件行, 是否为重传去重)。校验失败抛 DomainError，事务整体回滚。
    """
    with db.transaction() as conn:
        cargo = _get_cargo_for_update(conn, candidate.cargo_no)

        # 1) 同 event_id 重传：全局唯一，同内容幂等、异内容拒绝。
        same_id = conn.execute(
            f"SELECT {EVENT_COLUMNS} FROM events WHERE event_id = %s",
            (candidate.event_id,),
        ).fetchone()
        if same_id is not None:
            old = _row_to_event(same_id)
            if events_semantically_equal(old, candidate):
                return dict(same_id), True
            raise DomainError(
                "event_id_content_mismatch",
                f"事件 ID {candidate.event_id} 已存在且内容不同，重传被拒绝",
                event_id=candidate.event_id,
            )

        # 2) 取出该货物全部既有事件，连同新事件一起全量重放校验。
        #    晚到记录由此被纳入重建；任何既有交接因此失效也会整体拒绝。
        rows = _existing_event_rows(conn, candidate.cargo_no)
        existing = [_row_to_event(r) for r in rows]
        validate_and_replay(cargo, existing + [candidate])

        # 3) 落库。货物行锁已串行化同货写入，这里主要兜底跨货物的
        #    并发同 event_id 插入；部分唯一索引再防一次同时刻冲突。
        try:
            row = conn.execute(
                """
                INSERT INTO events (event_id, cargo_no, kind, occurred_at,
                                    temperature, from_party, to_party)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING
            """ + EVENT_COLUMNS,
                (candidate.event_id, candidate.cargo_no, candidate.kind,
                 candidate.occurred_at, candidate.temperature,
                 candidate.from_party, candidate.to_party),
            ).fetchone()
        except pg_errors.UniqueViolation as e:
            constraint = (e.diag.constraint_name or "") if e.diag else ""
            if constraint.startswith("uq_events_cargo_time"):
                raise DomainError(
                    "same_time_conflict",
                    f"货物 {candidate.cargo_no} 在 "
                    f"{candidate.occurred_at.isoformat()} 已存在同类事件，"
                    f"同一时刻不得有多笔测温或交接",
                    event_id=candidate.event_id,
                )
            # events_pkey：event_id 被并发事务抢先插入
            raise DomainError(
                "event_id_content_mismatch",
                f"事件 ID {candidate.event_id} 已被并发写入占用，写入被拒绝",
                event_id=candidate.event_id,
            )
        return dict(row), False


def list_cargoes() -> List[dict]:
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT cargo_no, start_time, temp_upper_limit, initial_holder, "
            "created_at FROM cargo ORDER BY created_at, cargo_no"
        ).fetchall()
        return [dict(r) for r in rows]


def get_cargo(cargo_no: str) -> dict:
    with db.transaction() as conn:
        _, row = _get_cargo(conn, cargo_no)
        return row


def list_events(cargo_no: Optional[str] = None) -> List[dict]:
    with db.transaction() as conn:
        if cargo_no is not None:
            _, _ = _get_cargo(conn, cargo_no)
            rows = conn.execute(
                f"SELECT {EVENT_COLUMNS} FROM events WHERE cargo_no = %s "
                f"ORDER BY occurred_at, event_id",
                (cargo_no,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {EVENT_COLUMNS} FROM events "
                f"ORDER BY occurred_at, event_id"
            ).fetchall()
        return [dict(r) for r in rows]


def get_violation_report(cargo_no: str) -> dict:
    """按发生时间重建并返回越温区间与各责任方片段。"""
    with db.transaction() as conn:
        cargo, cargo_row = _get_cargo(conn, cargo_no)
        rows = _existing_event_rows(conn, cargo_no)
        events = [_row_to_event(r) for r in rows]
        chain = reconstruct_chain(cargo, events)

    custody_chain = [
        {"start": s, "end": e, "holder": holder}
        for s, e, holder in chain["custody"]
    ]
    return {
        "cargo_no": cargo.cargo_no,
        "temp_upper_limit": cargo.temp_upper_limit,
        "start_time": cargo.start_time,
        "current_holder": chain["custody"][-1][2],
        "custody_chain": custody_chain,
        "violations": chain["violations"],
    }
