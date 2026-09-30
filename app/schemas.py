"""Pydantic 请求 / 响应模型。"""
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models import EventType


class CargoCreate(BaseModel):
    cargo_id: str = Field(..., min_length=1, max_length=64, description="货物编号")
    started_at: datetime = Field(..., description="起始时间（ISO 8601，建议带时区）")
    temp_upper_limit: Decimal = Field(..., description="温度上限（摄氏度）")
    initial_holder: str = Field(
        ..., min_length=1, max_length=128, description="起始保管方"
    )


class EventCreate(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=64, description="事件ID")
    cargo_id: str = Field(..., min_length=1, max_length=64, description="货物编号")
    occurred_at: datetime = Field(..., description="发生时间（ISO 8601，建议带时区）")

    # 测温
    temperature: Decimal | None = Field(
        default=None, description="测温温度；测温事件必填且事件只能含此字段"
    )
    # 交接
    giver: str | None = Field(default=None, max_length=128, description="交出方")
    receiver: str | None = Field(default=None, max_length=128, description="接收方")

    def event_type(self) -> EventType:
        is_measurement = self.temperature is not None
        is_handover = self.giver is not None or self.receiver is not None
        if is_measurement and is_handover:
            raise ValueError("事件不能同时包含温度与交接双方")
        if not is_measurement and not is_handover:
            raise ValueError("事件必须包含 temperature，或同时包含 giver 与 receiver")
        if is_handover and not (self.giver and self.receiver):
            raise ValueError("交接事件必须同时提供交出方与接收方")
        if is_handover and self.giver == self.receiver:
            raise ValueError("交出方与接收方不能相同")
        return EventType.measurement if is_measurement else EventType.handover


class CargoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cargo_id: str
    started_at: datetime
    temp_upper_limit: Decimal
    initial_holder: str
    created_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    cargo_id: str
    event_type: EventType
    occurred_at: datetime
    temperature: Decimal | None
    giver: str | None
    receiver: str | None


class ResponsibilitySegment(BaseModel):
    """越温区间内按交接时刻切分出的责任片段。"""

    start: datetime = Field(..., description="片段起点（含）")
    end: datetime | None = Field(
        ..., description="片段终点（不含）；null 表示区间未闭合（未结束）"
    )
    responsible_party: str = Field(..., description="该片段责任方（保管方）")


class Excursion(BaseModel):
    """一次越温区间及其责任拆分。"""

    start: datetime = Field(..., description="越温开始（首次超限测温时刻，含）")
    end: datetime | None = Field(
        ..., description="越温结束（首次不超限测温时刻，不含）；null=未闭合"
    )
    is_open: bool = Field(..., description="是否尚未闭合（未结束）")
    segments: list[ResponsibilitySegment] = Field(..., description="责任方片段")


class ChainReport(BaseModel):
    cargo_id: str
    temp_upper_limit: Decimal
    current_holder: str = Field(..., description="按事件重建出的当前保管方")
    excursions: list[Excursion]


class IdempotentReplay(BaseModel):
    """同 ID 同内容重传时的响应：不重复写入。"""

    replayed: bool = True
    event_id: str
    message: str
