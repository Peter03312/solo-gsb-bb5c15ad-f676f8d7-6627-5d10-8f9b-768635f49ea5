"""请求 / 响应模型。

所有时间字段要求带时区（ISO 8601，如 2026-01-01T08:00:00Z 或
2026-01-01T16:00:00+08:00），服务内部统一转 UTC 存储与比较。
温度以字符串传输（如 "6.20"）以避免浮点误差，响应同样返回字符串。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _require_aware_utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("时间必须带时区偏移，例如 2026-01-01T08:00:00Z")
    return v.astimezone(timezone.utc)


class CargoCreate(BaseModel):
    cargo_no: str = Field(..., min_length=1, max_length=64, description="货物编号（唯一）")
    start_time: datetime = Field(..., description="起始时间（带时区，ISO 8601）")
    temp_upper_limit: Decimal = Field(..., max_digits=12, decimal_places=4,
                                      description="温度上限；温度 > 上限即越温")
    initial_holder: str = Field(..., min_length=1, max_length=128, description="起始保管方")

    @field_validator("start_time")
    @classmethod
    def _utc_start(cls, v: datetime) -> datetime:
        return _require_aware_utc(v)


class MeasurementCreate(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=64, description="事件ID（全局唯一，用于重传去重）")
    cargo_no: str = Field(..., min_length=1, max_length=64)
    occurred_at: datetime = Field(..., description="测温发生时间（带时区）")
    temperature: Decimal = Field(..., max_digits=12, decimal_places=4, description="测得温度")

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_aware_utc(v)


class HandoverCreate(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=64, description="事件ID（全局唯一）")
    cargo_no: str = Field(..., min_length=1, max_length=64)
    occurred_at: datetime = Field(..., description="交接发生时间（带时区）")
    from_party: str = Field(..., min_length=1, max_length=128, description="交出方")
    to_party: str = Field(..., min_length=1, max_length=128, description="接收方")

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_aware_utc(v)


class CargoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    cargo_no: str
    start_time: datetime
    temp_upper_limit: Decimal
    initial_holder: str
    created_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    event_id: str
    cargo_no: str
    kind: Literal["measurement", "handover"]
    occurred_at: datetime
    temperature: Optional[Decimal] = None
    from_party: Optional[str] = None
    to_party: Optional[str] = None
    created_at: datetime


class EventAccepted(BaseModel):
    """事件写入响应。deduplicated=true 表示同 ID 同内容重传，未重复写入。"""
    deduplicated: bool
    event: EventOut


class CustodySegment(BaseModel):
    start: datetime
    end: Optional[datetime] = None
    holder: str


class LiabilitySegment(BaseModel):
    start: datetime
    end: Optional[datetime] = None
    party: str


class ViolationOut(BaseModel):
    start: datetime = Field(..., description="越温开始（首次超限测温时刻）")
    end: Optional[datetime] = Field(None, description="越温结束；null=未结束（未闭合区间）")
    segments: List[LiabilitySegment] = Field(..., description="按交接时刻拆分的各责任方片段")


class ViolationReport(BaseModel):
    cargo_no: str
    temp_upper_limit: Decimal
    start_time: datetime
    current_holder: str
    custody_chain: List[CustodySegment]
    violations: List[ViolationOut]
