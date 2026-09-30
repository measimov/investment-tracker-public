"""官方公告 API schema（#306）：分组事件 + 组内文件。有 response_model 才能生成前端类型。"""

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

Importance = Literal["major", "normal", "minor"]
ImportanceFilter = Literal["major", "normal", "all"]


class AnnouncementDocument(BaseModel):
    title: str
    url: str
    published_at: datetime
    importance: Importance
    category: str


class AnnouncementGroup(BaseModel):
    group_key: str = Field(description="symbol|market|公告日|类别：同日同类的文件合为一组")
    symbol: str
    market: str
    name: Optional[str] = Field(default=None, description="用户录入的名称，缺省取来源的证券简称")
    ann_date: date
    category: str
    category_label: str
    importance: Importance = Field(description="组内最高重要性")
    title: str = Field(description="代表标题（交易所原文）")
    url: str
    source: str = Field(description="cninfo / hkexnews / edgar")
    document_count: int
    first_seen_at: datetime
    latest_published_at: datetime
    documents: List[AnnouncementDocument]


class SecurityAnnouncementsResponse(BaseModel):
    symbol: str
    market: str
    sync_status: Literal["synced", "unsupported", "pending"] = Field(
        description="pending = 尚未同步（只同步持仓∪自选标的，或首次回溯未轮到）"
    )
    last_synced: Optional[date] = None
    unsupported_reason: Optional[str] = None
    groups: List[AnnouncementGroup]
    has_more: bool = False


class RecentAnnouncementsResponse(BaseModel):
    days: int
    importance: ImportanceFilter
    groups: List[AnnouncementGroup]
