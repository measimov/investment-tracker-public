"""行业分类响应 schema（GET /api/securities/industries）。"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

IndustrySource = Literal["rule", "tushare", "edgar", "eastmoney"]


class SecurityIndustryItem(BaseModel):
    symbol: str
    market: str
    # 无任何来源时两者皆为 None（显式缺口，前端显示「—」而不是猜）
    industry: Optional[str] = None
    # rule=用户特例规则；tushare/edgar=官方；eastmoney=东方财富 F10（非官方补缺）
    source: Optional[IndustrySource] = None
    fetched_at: Optional[datetime] = None
