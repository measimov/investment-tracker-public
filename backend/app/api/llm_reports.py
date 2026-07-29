"""LLM 复盘报告：生成（后台任务）、查看、追问对话、定期节奏配置。"""

from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..database import get_db
from ..models.llm_report import LlmReport, LlmReportMessage, LlmReportSchedule
from ..models.user import User
from ..schemas.llm_report import (
    VALID_CADENCES,
    LlmReportAskRequest,
    LlmReportAskResponse,
    LlmReportDetail,
    LlmReportListItem,
    LlmReportMessageResponse,
    LlmReportScheduleResponse,
    LlmReportScheduleUpdate,
)
from ..services.llm_client import (
    LLMClientError,
    LLMNotConfiguredError,
    chat_completion,
    is_llm_configured,
)
from ..services.llm_report_jobs import (
    get_llm_report_job,
    run_llm_report_job,
    start_llm_report_job,
)
from ..services.llm_report_prompts import build_chat_messages
from ._ownership import get_owned_record

router = APIRouter()

MAX_MESSAGES_PER_REPORT = 40
CHAT_HISTORY_LIMIT = 12


def _require_llm_configured() -> None:
    if not is_llm_configured():
        raise HTTPException(
            status_code=409,
            detail="未配置 LLM API Key（llm_report_api_key），无法使用 AI 复盘功能",
        )


@router.get("", response_model=List[LlmReportListItem])
def list_reports(
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(LlmReport)
        .filter(LlmReport.user_id == current_user.id)
        .order_by(LlmReport.created_at.desc(), LlmReport.id.desc())
        .limit(limit)
        .all()
    )


@router.post("/generate")
def generate_report(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
):
    _require_llm_configured()
    job = start_llm_report_job(current_user.id, trigger="manual")
    if job["status"] == "queued":
        background_tasks.add_task(run_llm_report_job, job["id"])
    return job


@router.get("/jobs/{job_id}")
def get_report_job(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
):
    job = get_llm_report_job(job_id, current_user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.get("/schedule", response_model=LlmReportScheduleResponse)
def get_schedule(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    row = (
        db.query(LlmReportSchedule)
        .filter(LlmReportSchedule.user_id == current_user.id)
        .first()
    )
    return LlmReportScheduleResponse(cadence=row.cadence if row else "off")


@router.put("/schedule", response_model=LlmReportScheduleResponse)
def update_schedule(
    payload: LlmReportScheduleUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    cadence = payload.cadence.strip()
    if cadence not in VALID_CADENCES:
        raise HTTPException(status_code=422, detail=f"未知节奏: {cadence}")
    row = (
        db.query(LlmReportSchedule)
        .filter(LlmReportSchedule.user_id == current_user.id)
        .first()
    )
    if row is None:
        row = LlmReportSchedule(user_id=current_user.id, cadence=cadence)
        db.add(row)
    else:
        row.cadence = cadence
    db.commit()
    return LlmReportScheduleResponse(cadence=cadence)


@router.get("/{report_id}", response_model=LlmReportDetail)
def get_report(
    report_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    report = get_owned_record(db, LlmReport, report_id, current_user.id, "报告不存在")
    messages = (
        db.query(LlmReportMessage)
        .filter(LlmReportMessage.report_id == report.id)
        .order_by(LlmReportMessage.id)
        .all()
    )
    detail = LlmReportDetail.model_validate(report)
    detail.messages = [
        LlmReportMessageResponse.model_validate(message) for message in messages
    ]
    return detail


@router.delete("/{report_id}", status_code=204)
def delete_report(
    report_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    report = get_owned_record(db, LlmReport, report_id, current_user.id, "报告不存在")
    db.delete(report)
    db.commit()


@router.post("/{report_id}/messages", response_model=LlmReportAskResponse)
def ask_report(
    report_id: int,
    payload: LlmReportAskRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """追问对话：同步调 LLM；仅成功后一次性落用户+助手两条消息（失败零残留）。"""
    _require_llm_configured()
    report = get_owned_record(db, LlmReport, report_id, current_user.id, "报告不存在")

    history = (
        db.query(LlmReportMessage)
        .filter(LlmReportMessage.report_id == report.id)
        .order_by(LlmReportMessage.id)
        .all()
    )
    if len(history) >= MAX_MESSAGES_PER_REPORT:
        raise HTTPException(status_code=409, detail="本报告追问已达上限，请生成新报告继续讨论")

    messages = build_chat_messages(
        report.content,
        report.input_payload,
        [{"role": m.role, "content": m.content} for m in history[-CHAT_HISTORY_LIMIT:]],
        payload.content,
    )
    try:
        completion = chat_completion(messages)
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except LLMClientError as exc:
        raise HTTPException(status_code=502, detail=f"LLM 调用失败：{exc}")

    question = LlmReportMessage(
        report_id=report.id, user_id=current_user.id, role="user", content=payload.content
    )
    usage = completion.get("usage") or {}
    answer = LlmReportMessage(
        report_id=report.id,
        user_id=current_user.id,
        role="assistant",
        content=completion["content"],
        total_tokens=usage.get("total_tokens"),
    )
    db.add_all([question, answer])
    db.commit()
    db.refresh(question)
    db.refresh(answer)
    return LlmReportAskResponse(
        question=LlmReportMessageResponse.model_validate(question),
        answer=LlmReportMessageResponse.model_validate(answer),
    )
