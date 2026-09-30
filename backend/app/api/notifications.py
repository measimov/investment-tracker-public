"""系统告警（管理员）：告警列表、测试通知、立即检查。

推送渠道与检查器见 services/notification_service.py、alert_checks.py；所有端点只返回
脱敏后的渠道地址。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..core.deps import get_current_admin_user
from ..database import get_db
from ..models.user import User
from ..schemas.notification import (
    AlertListResponse,
    NotificationEventItem,
    NotificationEventListResponse,
    NotifyResult,
)
from ..services import alert_checks, alert_service, event_notifications, notification_service

router = APIRouter(prefix="/api/notifications", tags=["Notifications"])


def build_alert_list(db: Session) -> AlertListResponse:
    listing = alert_service.list_alerts(db)
    return AlertListResponse(
        check_enabled=settings.alert_check_enabled,
        check_interval_minutes=alert_checks.PERIODIC_INTERVAL_SECONDS // 60,
        reminder_hours=settings.notify_reminder_hours,
        channels=notification_service.channel_summary(),
        **listing,
    )


@router.get("/alerts", response_model=AlertListResponse)
def list_alerts(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin_user),
):
    return build_alert_list(db)


@router.post("/test", response_model=NotifyResult)
def send_test_notification(_admin: User = Depends(get_current_admin_user)):
    return notification_service.send_test()


@router.post("/check", response_model=AlertListResponse)
def run_checks_now(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin_user),
):
    """立即跑一轮告警检查（与周期任务同一套状态机：该推送的照常推送）。"""
    alert_checks.run_checks(db)
    return build_alert_list(db)


@router.get("/events", response_model=NotificationEventListResponse)
def list_notification_events(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin_user),
):
    """最近的事件提醒（新分红建议、除净日临近、价格异动、重大公告），新的在前。"""
    rows = event_notifications.list_events(db, limit=limit)
    return NotificationEventListResponse(
        enabled=settings.event_notifications_enabled,
        price_move_pct=settings.notify_price_move_pct,
        ex_date_days_ahead=settings.notify_ex_date_days_ahead,
        announcement_notify_enabled=settings.announcement_notify_enabled,
        items=[NotificationEventItem.model_validate(row, from_attributes=True) for row in rows],
    )
