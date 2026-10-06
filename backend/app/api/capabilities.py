"""只读本地配置与全局可读历史，不探活、不初始化采集器状态。"""

from fastapi import APIRouter, Depends
from sqlalchemy import exists
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..database import get_db
from ..models.security_opinion import SecurityOpinionSummary
from ..models.user import User
from ..models.xueqiu_collector import XueqiuArchiverUtterance, XueqiuSymbolPost
from ..schemas.capabilities import CapabilitiesResponse, EntryCapability
from ..services.xueqiu_source import is_configured

router = APIRouter(prefix="/api/capabilities", tags=["Capabilities"])


@router.get("", response_model=CapabilitiesResponse)
def get_capabilities(
    _user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    if is_configured():
        configured = EntryCapability(available=True, reason="configured")
        return CapabilitiesResponse(opinions=configured, xueqiu_symbol_feed=configured)

    # 两类内容沿现有全局只读权限；成功的空ScanRun不代表存在可读内容。
    utterances, summaries, posts = db.query(
        exists().where(XueqiuArchiverUtterance.utterance_key.is_not(None)),
        exists().where(SecurityOpinionSummary.id.is_not(None)),
        exists().where(XueqiuSymbolPost.kind.in_(["announcement", "discussion"])),
    ).one()
    opinions = bool(utterances or summaries)
    return CapabilitiesResponse(
        opinions=EntryCapability(
            available=opinions, reason="history" if opinions else "unconfigured"
        ),
        xueqiu_symbol_feed=EntryCapability(
            available=bool(posts), reason="history" if posts else "unconfigured"
        ),
    )
