import re
from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

class CollectorAuthorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    xueqiu_user_id: str
    display_name: str
    enabled: bool
    note: str
    created_at: datetime
    last_run_at: Optional[datetime] = None
    last_status: str
    last_message: str


class CollectorAuthorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    xueqiu_user_id: str = Field(..., description="雪球用户数字 ID（主页 URL xueqiu.com/u/<ID>）")
    display_name: str = Field("", max_length=100)
    note: str = Field("", max_length=500)
    enabled: bool = True

    @field_validator("xueqiu_user_id", mode="before")
    @classmethod
    def user_id_numeric(cls, value):
        text = str(value or "").strip()
        if not text.isascii() or not text.isdigit() or not 1 <= len(text) <= 20:
            raise ValueError("雪球用户 ID 必须是 1-20 位数字（主页链接 xueqiu.com/u/<ID>）")
        return text

    @field_validator("display_name", "note", mode="before")
    @classmethod
    def strip_text(cls, value):
        return str(value or "").strip()


class CollectorAuthorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(None, max_length=100)
    note: Optional[str] = Field(None, max_length=500)
    enabled: Optional[bool] = None


class CollectorScanRunResponse(BaseModel):
    run_id: int
    author_user_id: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: str
    candidate_count: int
    reply_count: int
    utterance_count: int
    stopped_early: bool
    waf_hit: bool
    error_message: str


class CollectorCookieStatus(BaseModel):
    level: str = Field(..., description="normal / warning / critical / unconfigured")
    message: str
    days_left: Optional[float] = None
    cookie: str = ""


class XueqiuCookiePrimaryFact(BaseModel):
    name: str = Field(..., description="主凭证 Cookie 名（xq_a_token / xqat）")
    present: bool
    expires_at: Optional[datetime] = None
    days_left: Optional[float] = Field(None, description="距到期天数；无 expirationDate 时为空")


class XueqiuCookieAdminStatus(BaseModel):
    """雪球 Cookie 的管理视图（仅管理员）。只有名称与到期事实，绝不含 Cookie 值。"""

    source: Literal["file", "inline", "none"] = Field(
        ..., description="file=XUEQIU_COOKIE_FILE / inline=XUEQIU_COOKIES / none=未配置"
    )
    file_path: Optional[str] = Field(None, description="容器内 Cookie 文件路径")
    file_exists: bool
    file_mtime: Optional[datetime] = None
    backup_exists: bool
    backup_mtime: Optional[datetime] = None
    writable: bool = Field(..., description="能否在界面更新（文件来源且目录对后端可写）")
    writable_reason: Optional[str] = Field(None, description="不能更新的原因与修复办法")
    level: str = Field(..., description="normal / warning / critical / unconfigured")
    message: str
    keys: List[str] = Field(default_factory=list, description="当前 Cookie 的名称（不含值）")
    primary: List[XueqiuCookiePrimaryFact]
    read_error: Optional[str] = None


class XueqiuCookieUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(
        ...,
        description="浏览器插件导出的 J2Team JSON、{name: value} JSON，或请求头 `a=b; c=d`",
    )
    probe: bool = Field(False, description="写入后发一次真实请求确认登录态（一次限速请求）")


class XueqiuCookieProbeResult(BaseModel):
    ok: bool
    detail: str


class XueqiuCookieUpdateResponse(BaseModel):
    status: XueqiuCookieAdminStatus
    backup_created: bool
    source_format: Literal["j2team", "json_list", "json_dict", "header"]
    notes: List[str] = Field(default_factory=list)
    probe: Optional[XueqiuCookieProbeResult] = None


class CollectorStatusResponse(BaseModel):
    enabled: bool = Field(..., description="Web 进程读到的 XUEQIU_COLLECTOR_ENABLED")
    alive: bool = Field(..., description="采集器进程心跳在健康阈值内")
    heartbeat_at: Optional[datetime] = None
    cycle_minutes: int
    last_cycle_started_at: Optional[datetime] = None
    last_cycle_finished_at: Optional[datetime] = None
    last_cycle_status: str
    last_cycle_message: str
    last_waf_at: Optional[datetime] = None
    waf_cooldown_until: Optional[datetime] = None
    run_requested_at: Optional[datetime] = None
    run_pending: bool
    cookie: CollectorCookieStatus
    recent_runs: List[CollectorScanRunResponse]
    authors: List[CollectorAuthorResponse]
    symbols: "CollectorSymbolsStatus"
    cubes: List["CollectorCubeResponse"]


# --------------------------------------------------------------------------- #
# 按标的监控（公告/讨论、组合调仓）
# --------------------------------------------------------------------------- #
class CollectorCubeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cube_id: str
    display_name: str
    enabled: bool
    note: str
    created_at: datetime
    last_run_at: Optional[datetime] = None
    last_status: str
    last_message: str


class CollectorCubeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cube_id: str = Field(..., description="雪球组合代号，如 ZH000001（组合页 xueqiu.com/P/<代号>）")
    display_name: str = Field("", max_length=100)
    note: str = Field("", max_length=500)
    enabled: bool = True

    @field_validator("cube_id", mode="before")
    @classmethod
    def cube_id_format(cls, value):
        text = str(value or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}[0-9]{1,20}", text):
            raise ValueError("组合代号必须是两位字母加数字，如 ZH000001")
        return text

    @field_validator("display_name", "note", mode="before")
    @classmethod
    def strip_text(cls, value):
        return str(value or "").strip()


class CollectorCubeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(None, max_length=100)
    note: Optional[str] = Field(None, max_length=500)
    enabled: Optional[bool] = None


class CollectorSymbolsStatus(BaseModel):
    enabled: bool = Field(..., description="XUEQIU_COLLECTOR_SYMBOLS_ENABLED（仍受总开关约束）")
    run_after: str = Field(..., description="每日一轮的起始时刻（业务时区 HH:MM）")
    last_started_at: Optional[datetime] = None
    last_finished_at: Optional[datetime] = None
    last_status: str
    last_message: str
    last_business_date: Optional[date] = None
    last_stats: Dict[str, Any] = Field(default_factory=dict)
    run_requested_at: Optional[datetime] = None
    run_pending: bool
    retry_pending: bool = Field(False, description="当天有待重试的失败项（业务日尚未记为已跑）")
    retry_attempts: int = Field(0, description="当天已尝试的轮数（含首轮）")
    retry_item_count: Optional[int] = Field(
        None, description="待重试项数；None 且 retry_pending = 整轮重跑（WAF/Cookie 不可用）"
    )


class XueqiuFeedPost(BaseModel):
    post_id: str
    created_at_ms: int
    title: str
    text: str
    author_id: str
    author_name: str
    url: str
    reply_count: Optional[int] = None
    like_count: Optional[int] = None
    links: List[str] = Field(default_factory=list, description="站外附件链接（公告原文等）")
    first_seen_at: datetime
    last_seen_at: datetime


class XueqiuSymbolFeedResponse(BaseModel):
    symbol: str
    market: str
    announcements: List[XueqiuFeedPost] = Field(default_factory=list)
    discussions: List[XueqiuFeedPost] = Field(default_factory=list)
    last_cycle_finished_at: Optional[datetime] = Field(
        None, description="上一轮按标的采集结束时间（判断数据新旧）"
    )


CollectorStatusResponse.model_rebuild()
