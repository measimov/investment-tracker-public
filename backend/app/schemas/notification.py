"""系统告警与通知渠道（管理员页「系统告警」）。"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["info", "warning", "critical"]


class NotifyChannel(BaseModel):
    kind: str = Field(description="bark / Apprise 的 scheme")
    channel: str = Field(description="脱敏后的渠道地址（不含设备 key / token）")
    valid: bool = Field(description="Apprise 能否识别该 URL（不外呼）")


class NotifyChannelSummary(BaseModel):
    configured: bool
    count: int
    valid_count: int
    apprise_available: bool
    min_severity: Severity
    channels: List[NotifyChannel]


class NotifyChannelResult(BaseModel):
    kind: str
    channel: str
    ok: bool
    error: Optional[str] = None


class NotifyResult(BaseModel):
    ok: bool
    status: Literal["unconfigured", "sent", "partial", "failed"]
    message: str
    configured: int
    sent: int
    channels: List[NotifyChannelResult]


class AlertItem(BaseModel):
    alert_key: str
    source: str
    severity: Severity
    status: Literal["active", "resolved"]
    title: str
    message: str
    first_seen_at: datetime
    last_seen_at: datetime
    last_notified_at: Optional[datetime] = None
    notify_count: int
    resolved_at: Optional[datetime] = None
    resolve_notified_at: Optional[datetime] = Field(
        default=None, description="「已恢复」推送送达时间；推送过的告警恢复后为空 = 恢复通知待重试"
    )
    last_notify: Optional[Dict[str, Any]] = Field(
        default=None, description="最近一次推送尝试：{at, action, status, message}"
    )
    payload: Dict[str, Any] = Field(default_factory=dict)


class AlertCounts(BaseModel):
    active: int
    info: int
    warning: int
    critical: int
    recent_resolved: int


class AlertListResponse(BaseModel):
    check_enabled: bool
    check_interval_minutes: int
    reminder_hours: float
    channels: NotifyChannelSummary
    counts: AlertCounts
    active: List[AlertItem]
    recent_resolved: List[AlertItem]
