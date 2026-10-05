"""Request/response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class PoolCreate(BaseModel):
    film_id: str = Field(min_length=1, max_length=128, examples=["film-autumn-river"])
    max_sessions: int = Field(
        ge=1, le=1_000_000,
        description="影片级跨场馆共享放映次数上限（正整数）",
        examples=[10],
    )
    note: Optional[str] = Field(default=None, max_length=500)


class PoolOut(BaseModel):
    id: int
    film_id: str
    max_sessions: int
    used_sessions: int
    remaining_sessions: int
    bound_licenses: int = Field(description="当前绑定该池的授权份数")
    note: Optional[str]
    created_at: str


class LicenseCreate(BaseModel):
    film_id: str = Field(min_length=1, max_length=128, examples=["film-autumn-river"])
    venue_id: str = Field(min_length=1, max_length=128, examples=["hall-1"])
    valid_from: datetime = Field(examples=["2026-10-01T00:00:00Z"])
    valid_to: datetime = Field(examples=["2026-10-31T23:59:59Z"])
    max_sessions: int = Field(ge=1, le=1_000_000, examples=[5])
    pool_id: Optional[int] = Field(
        default=None,
        description="可选：绑定影片级共享额度池；绑定后不可修改。池不存在或影片不符返回 422。",
        examples=[1],
    )
    note: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check_window(self) -> "LicenseCreate":
        if self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class PoolBrief(BaseModel):
    id: int
    film_id: str
    max_sessions: int
    used_sessions: int
    remaining_sessions: int


class RevocationOut(BaseModel):
    reason: str
    revoked_at: str


class LicenseOut(BaseModel):
    id: int
    film_id: str
    venue_id: str
    valid_from: str
    valid_to: str
    max_sessions: int
    used_sessions: int
    remaining_sessions: int
    pool_id: Optional[int] = Field(description="所绑共享额度池 id；未绑定为 null")
    pool: Optional[PoolBrief] = Field(
        description="所绑池的实时已用/剩余快照；未绑定为 null"
    )
    revoked: bool
    revocation: Optional[RevocationOut]
    note: Optional[str]
    created_at: str


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500, examples=["版权方临时撤片"])


class RevokeOut(BaseModel):
    id: int
    license_id: int
    reason: str
    revoked_at: str


class SuspensionCreate(BaseModel):
    suspend_from: datetime = Field(
        description="暂停起（含），ISO-8601 且必须带时区偏移，如 2026-10-05T00:00:00+08:00",
        examples=["2026-10-05T00:00:00+08:00"],
    )
    suspend_to: datetime = Field(
        description="暂停止（不含），ISO-8601 且必须带时区偏移，须严格晚于 suspend_from",
        examples=["2026-10-06T00:00:00+08:00"],
    )
    reason: str = Field(min_length=1, max_length=500, examples=["版权方临时通知暂停放映"])

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value

    @model_validator(mode="after")
    def _check_window(self) -> "SuspensionCreate":
        if self.suspend_from.tzinfo is None or self.suspend_to.tzinfo is None:
            raise ValueError("suspend_from and suspend_to must include a timezone offset")
        if self.suspend_to <= self.suspend_from:
            raise ValueError("suspend_to must be later than suspend_from")
        return self


class SuspensionOut(BaseModel):
    id: int = Field(description="暂停编号")
    license_id: int
    suspend_from: str = Field(description="暂停起（含），统一存储为 UTC")
    suspend_to: str = Field(description="暂停止（不含），统一存储为 UTC")
    reason: str
    created_at: str


class VerifyRequest(BaseModel):
    film_id: str = Field(min_length=1, max_length=128)
    venue_id: str = Field(min_length=1, max_length=128)
    start_time: datetime = Field(
        description="场次开始时间，ISO-8601；不带时区时按 UTC 处理",
        examples=["2026-10-05T19:30:00Z"],
    )
    idempotency_key: str = Field(
        min_length=1, max_length=128,
        description="业务幂等键；重复提交返回原结果，内容变化则 409",
        examples=["order-20261005-0001"],
    )


class CancellationOut(BaseModel):
    reason: str
    cancelled_at: str


class VerificationOut(BaseModel):
    id: int
    idempotency_key: str
    film_id: str
    venue_id: str
    start_time: str
    decision: str = Field(description="approved 或 rejected；取消不回写，保持首次决定")
    license_id: Optional[int] = Field(description="命中并消耗额度的授权 id")
    pool_id: Optional[int] = Field(
        description="本次核准同时消耗的共享池 id；授权未绑池或被拒绝时为 null"
    )
    reject_reason: Optional[str] = Field(
        description="拒绝原因：NO_LICENSE / TIME_OUT_OF_WINDOW / LICENSE_REVOKED / "
                    "LICENSE_SUSPENDED / QUOTA_EXHAUSTED / POOL_EXHAUSTED"
    )
    remaining_quota: Optional[int] = Field(description="本次消耗后该授权自身的剩余额度（快照，取消不回写）")
    pool_remaining_quota: Optional[int] = Field(
        description="本次消耗后所绑共享池的剩余额度（快照，取消不回写）；授权未绑池或被拒绝时为 null"
    )
    verified_at: str
    replay: bool = Field(description="true 表示本次为幂等重放，未重新扣减额度")
    cancelled: bool = Field(description="该流水是否已登记场次取消")
    cancellation: Optional[CancellationOut] = Field(
        description="取消详情（原因与取消时间）；未取消为 null"
    )


class CancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500, examples=["放映设备故障，场次取消"])

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class CancelOut(BaseModel):
    id: int = Field(description="取消记录编号")
    idempotency_key: str = Field(description="原核验幂等键")
    verification_id: int = Field(description="被取消的核验流水 id")
    license_id: int = Field(description="释放一次已用次数的授权 id")
    pool_id: Optional[int] = Field(description="同步释放一次的共享池 id；授权未绑池为 null")
    reason: str
    cancelled_at: str = Field(description="取消登记时间（UTC）")
    replay: bool = Field(
        description="true 表示重复提交同一取消，返回首次记录，未重复释放额度"
    )


class OpeningCodeOut(BaseModel):
    idempotency_key: str = Field(description="原核验幂等键")
    verification_id: int = Field(description="对应核验流水 id")
    film_id: str
    venue_id: str = Field(description="核准场次的场馆编号；核销场馆必须与之相同")
    start_time: str = Field(description="计划开场时刻（UTC）；核销窗口为其后 30 分钟")
    window_open_at: str = Field(description="核销窗口开启时间（=计划开场时刻，UTC）")
    window_close_at: str = Field(description="核销窗口关闭时间（计划开场 +30 分钟，含，UTC）")
    status: str = Field(
        description="issued（已领取待核销）/ redeemed（已核销）/ void（场次取消，码已作废）"
    )
    code: Optional[str] = Field(
        description="一次性开场码原码；仅首次领取返回，重复领取与查询为 null"
    )
    issued_at: str = Field(description="首次领取时间（UTC）")
    redeemed_at: Optional[str] = Field(description="实际核销时间（UTC）；未核销为 null")
    replay: bool = Field(description="true 表示重复领取，仅回状态、不回原码")


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=128, examples=["OC-9f8c2a1b7e4d4f60a3b5c7d9e2f1a0b3"])
    venue_id: str = Field(min_length=1, max_length=128, examples=["hall-1"])


class RedemptionOut(BaseModel):
    id: int = Field(description="核销回执编号")
    idempotency_key: str = Field(description="场次原核验幂等键")
    verification_id: int = Field(description="对应核验流水 id")
    film_id: str
    venue_id: str = Field(description="实际核销场馆（等于场次核准场馆）")
    start_time: str = Field(description="计划开场时刻（UTC）")
    redeemed_at: str = Field(description="实际核销时间（UTC）")
    license_id: int = Field(description="关联授权 id（原核验命中的授权）")
    pool_id: Optional[int] = Field(description="关联共享池 id；授权未绑池为 null")
    replay: bool = Field(
        description="true 表示同一码重试核销，返回首次回执，不重复记场次"
    )
