"""标的全集 API schema：检索 / 按需解析 / 目录状态。有 response_model 才能让
前端 api.generated.ts 拿到真实形状（Dict[str, Any] 端点只会生成 unknown）。"""

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

SecurityType = Literal["stock", "etf", "fund", "reit", "adr", "pref", "gdr", "unknown"]
ListStatus = Literal["listed", "delisted", "unknown"]
Origin = Literal["holding", "watchlist", "history"]


class SecuritySearchItem(BaseModel):
    symbol: str
    market: str
    name: Optional[str] = None
    name_en: Optional[str] = None
    name_trad: Optional[str] = None
    pinyin: Optional[str] = None
    currency: Optional[str] = None
    security_type: SecurityType = "unknown"
    board: Optional[str] = None
    exchange: Optional[str] = None
    list_status: ListStatus = "unknown"
    in_catalog: bool = False
    origins: List[Origin] = []  # 纯目录行为空；账本行标 holding/watchlist/history
    last_used: Optional[date] = None  # 账本最近交易日；目录行 None


class CatalogHealth(BaseModel):
    ready: bool  # False ⇒ 前端提示「标的目录尚未同步，仅显示账本内标的」
    stale: bool
    last_success_at: Optional[datetime] = None
    failing_sources: List[str] = []
    capabilities: Dict[str, bool] = {}  # {"pinyin": 拼音兜底库可用, "simplified": 繁简转换可用}


class SecuritySearchResponse(BaseModel):
    items: List[SecuritySearchItem]
    catalog: CatalogHealth


class SecurityResolveResponse(BaseModel):
    symbol: str  # 归一化后的代码
    market: str
    name: Optional[str] = None  # 不可解析时显式 null
    name_en: Optional[str] = None
    currency: Optional[str] = None
    security_type: SecurityType = "unknown"
    list_status: ListStatus = "unknown"
    in_catalog: bool = False
    resolved_from: Optional[Literal["catalog", "tencent-quote"]] = None
    error: Optional[str] = None


class CatalogSourceStatus(BaseModel):
    source: str
    markets: List[str]
    status: Literal["ok", "failed", "skipped", "running", "never"]
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    rows_seen: int = 0
    rows_upserted: int = 0
    error: Optional[str] = None
    detail: Dict[str, Any] = {}


class CatalogStatusResponse(BaseModel):
    health: CatalogHealth
    sources: List[CatalogSourceStatus]  # LOADERS 顺序，未跑过的源 status=never
    total_rows: int
    by_market: Dict[str, int]
    coverage_notes: List[str]


class CatalogSyncAccepted(BaseModel):
    started: bool
    sources: List[str]
