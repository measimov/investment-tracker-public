from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

VALID_CADENCES = {"off", "weekly", "monthly"}


class LlmReportListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    trigger_source: str
    model: str
    total_tokens: Optional[int]
    created_at: datetime


class LlmReportMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    created_at: datetime


class LlmReportDetail(LlmReportListItem):
    content: str
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    messages: List[LlmReportMessageResponse] = []


class LlmReportAskRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class LlmReportAskResponse(BaseModel):
    question: LlmReportMessageResponse
    answer: LlmReportMessageResponse


class LlmReportScheduleUpdate(BaseModel):
    cadence: str


class LlmReportScheduleResponse(BaseModel):
    cadence: str
