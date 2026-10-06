"""入口可见能力，不代表外部来源健康或探活结果。"""

from typing import Literal

from pydantic import BaseModel


class EntryCapability(BaseModel):
    available: bool
    reason: Literal["configured", "history", "unconfigured"]


class CapabilitiesResponse(BaseModel):
    opinions: EntryCapability
    xueqiu_symbol_feed: EntryCapability
