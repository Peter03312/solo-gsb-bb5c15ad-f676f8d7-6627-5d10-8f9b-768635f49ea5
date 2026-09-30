"""冷链货物追责服务 —— FastAPI 入口。

启动（开发）: uvicorn app.main:app --reload
一键启动    : docker compose up --build  （见 README）
"""
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import chain, crud
from app.database import Base, engine, get_db
from app.models import Cargo, Event
from app.schemas import (
    CargoCreate,
    CargoOut,
    ChainReport,
    EventCreate,
    EventOut,
    Excursion,
    IdempotentReplay,
    ResponsibilitySegment,
)
from app.timeutil import ensure_utc


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 生产中由 db/init.sql 建表；此处确保直接 uvicorn / sqlite 测试也可用
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="冷链货物追责服务",
    description=(
        "记录冷链货物的测温与交接事件，按发生时间重建保管链，"
        "计算越温区间并拆分各保管方责任。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(crud.ApiError)
async def api_error_handler(request: Request, exc: crud.ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


# ---------------------------------------------------------------- 货物


@app.post("/cargo", response_model=CargoOut, status_code=201, tags=["货物"])
def create_cargo(payload: CargoCreate, db: Session = Depends(get_db)) -> Cargo:
    payload.started_at = ensure_utc(payload.started_at)
    return crud.create_cargo(db, payload)


@app.get("/cargo", response_model=list[CargoOut], tags=["货物"])
def list_cargo(db: Session = Depends(get_db)) -> list[Cargo]:
    return list(db.scalars(select(Cargo).order_by(Cargo.cargo_id)).all())


@app.get("/cargo/{cargo_id}", response_model=CargoOut, tags=["货物"])
def get_cargo(cargo_id: str, db: Session = Depends(get_db)) -> Cargo:
    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    return cargo


# ---------------------------------------------------------------- 事件


@app.post(
    "/events",
    response_model=EventOut,
    status_code=201,
    tags=["事件"],
    responses={
        200: {
            "model": IdempotentReplay,
            "description": "同 ID 同内容重传：不重复写入",
        }
    },
)
def create_event(payload: EventCreate, db: Session = Depends(get_db)):
    payload.occurred_at = ensure_utc(payload.occurred_at)
    result = crud.create_event(db, payload)
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=IdempotentReplay(
                event_id=payload.event_id,
                message="相同内容的事件已存在，重传被忽略（幂等）",
            ).model_dump(),
        )
    return result.event


@app.get(
    "/cargo/{cargo_id}/events",
    response_model=list[EventOut],
    tags=["事件"],
)
def list_events(cargo_id: str, db: Session = Depends(get_db)) -> list[Event]:
    if db.get(Cargo, cargo_id) is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    stmt = (
        select(Event)
        .where(Event.cargo_id == cargo_id)
        .order_by(Event.occurred_at, Event.event_id)
    )
    events = list(db.scalars(stmt).all())
    for e in events:
        e.occurred_at = ensure_utc(e.occurred_at)
    return events


# ---------------------------------------------------------------- 追责查询


@app.get(
    "/cargo/{cargo_id}/report",
    response_model=ChainReport,
    tags=["追责查询"],
)
def cargo_report(cargo_id: str, db: Session = Depends(get_db)) -> ChainReport:
    """按时间返回越温区间（起止）与各责任方片段；未闭合区间标记未结束。"""
    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    cargo.started_at = ensure_utc(cargo.started_at)

    stmt = select(Event).where(Event.cargo_id == cargo_id)
    events = list(db.scalars(stmt).all())
    for e in events:
        e.occurred_at = ensure_utc(e.occurred_at)

    result = chain.rebuild_chain(
        cargo.initial_holder, Decimal(cargo.temp_upper_limit), events
    )

    excursions = [
        Excursion(
            start=ex.start,
            end=ex.end,
            is_open=ex.is_open,
            segments=[
                ResponsibilitySegment(
                    start=seg.start, end=seg.end, responsible_party=seg.party
                )
                for seg in ex.segments
            ],
        )
        for ex in result.excursions
    ]
    return ChainReport(
        cargo_id=cargo.cargo_id,
        temp_upper_limit=Decimal(cargo.temp_upper_limit),
        current_holder=result.current_holder,
        excursions=excursions,
    )


@app.get("/health", tags=["运维"])
def health() -> dict:
    return {"status": "ok"}
