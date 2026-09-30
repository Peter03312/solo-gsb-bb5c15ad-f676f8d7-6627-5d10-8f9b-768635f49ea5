"""冷链货物追责服务入口。

默认监听 0.0.0.0:8000，API 前缀 /api，交互文档 /docs（Swagger）与 /redoc。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from . import db, services
from .domain import (
    DomainError, Event, MEASUREMENT, HANDOVER,
)
from .schemas import (
    CargoCreate, CargoOut,
    MeasurementCreate, HandoverCreate, EventOut, EventAccepted,
    ViolationReport,
)

# 业务错误码 -> HTTP 状态码
STATUS_BY_CODE = {
    "cargo_not_found": status.HTTP_404_NOT_FOUND,
    "event_before_start": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "invalid_custodian": status.HTTP_409_CONFLICT,
    "same_time_conflict": status.HTTP_409_CONFLICT,
    "event_id_content_mismatch": status.HTTP_409_CONFLICT,
    "cargo_exists": status.HTTP_409_CONFLICT,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 连接池建连（内含数据库就绪等待与 DDL 幂等执行）
    db.init_pool()
    try:
        yield
    finally:
        db.close_pool()


def create_app() -> FastAPI:
    app = FastAPI(
        title="冷链货物追责服务",
        version="1.0.0",
        lifespan=lifespan,
        description=(
            "冷链货物测温与交接事件溯源：按发生时间重建保管链，"
            "计算越温区间并在交接时刻拆分责任方。\n\n"
            "- 测温温度 > 上限开启越温区间，其后首笔不超温测温关闭；\n"
            "- 区间跨交接时，交接时刻起归接收方；未闭合区间 end 为 null；\n"
            "- 晚到记录自动参与重算；同 ID 同内容重传幂等，异内容拒绝；\n"
            "- 交出方不等于当时保管方的写入、同一货物同一时刻同类事件冲突，"
            "整体拒绝。"
        ),
    )

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
            content=exc.to_dict(),
        )

    @app.get("/", tags=["meta"])
    def root():
        return {
            "service": "cold-chain-accountability",
            "docs": "/docs",
            "api": {
                "create_cargo": "POST /api/cargo",
                "report_measurement": "POST /api/events/measurements",
                "report_handover": "POST /api/events/handovers",
                "list_cargoes": "GET /api/cargo",
                "get_cargo": "GET /api/cargo/{cargo_no}",
                "list_events": "GET /api/events",
                "violation_report": "GET /api/cargo/{cargo_no}/violations",
            },
        }

    @app.get("/health", tags=["meta"])
    def health():
        # 简单探活：能取到连接即健康
        with db.get_pool().connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    # ---- 货物 ----

    @app.post("/api/cargo", response_model=CargoOut,
              status_code=status.HTTP_201_CREATED, tags=["cargo"])
    def create_cargo(payload: CargoCreate):
        return services.create_cargo(payload)

    @app.get("/api/cargo", response_model=list[CargoOut], tags=["cargo"])
    def list_cargoes():
        return services.list_cargoes()

    @app.get("/api/cargo/{cargo_no}", response_model=CargoOut, tags=["cargo"])
    def get_cargo(cargo_no: str):
        return services.get_cargo(cargo_no)

    # ---- 事件 ----

    @app.post("/api/events/measurements", response_model=EventAccepted,
              status_code=status.HTTP_201_CREATED, tags=["events"])
    def report_measurement(payload: MeasurementCreate):
        candidate = Event(
            event_id=payload.event_id,
            cargo_no=payload.cargo_no,
            kind=MEASUREMENT,
            occurred_at=payload.occurred_at,
            temperature=payload.temperature,
        )
        row, dedup = services.submit_event(candidate)
        # 同内容重传返回 200 而非 201
        return _accepted_response(row, dedup)

    @app.post("/api/events/handovers", response_model=EventAccepted,
              status_code=status.HTTP_201_CREATED, tags=["events"])
    def report_handover(payload: HandoverCreate):
        candidate = Event(
            event_id=payload.event_id,
            cargo_no=payload.cargo_no,
            kind=HANDOVER,
            occurred_at=payload.occurred_at,
            from_party=payload.from_party,
            to_party=payload.to_party,
        )
        row, dedup = services.submit_event(candidate)
        return _accepted_response(row, dedup)

    @app.get("/api/events", response_model=list[EventOut], tags=["events"])
    def list_events(cargo_no: Optional[str] = None):
        return services.list_events(cargo_no)

    # ---- 追责查询 ----

    @app.get("/api/cargo/{cargo_no}/violations",
             response_model=ViolationReport, tags=["accountability"])
    def violation_report(cargo_no: str):
        return services.get_violation_report(cargo_no)

    return app


def _accepted_response(row: dict, dedup: bool):
    """构造事件受理响应；幂等重传使用 200。"""
    body = EventAccepted(deduplicated=dedup, event=EventOut.model_validate(row))
    code = status.HTTP_200_OK if dedup else status.HTTP_201_CREATED
    return JSONResponse(status_code=code, content=body.model_dump(mode="json"))


app = create_app()
