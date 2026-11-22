"""归并差异底稿路由：建底稿、复核裁定、确认、发布与回溯核对。

所有接口围绕 data/merge_worksheets 与 data/merge_published 工作，
不提供任何修改 data/exports 源导出的入口。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.merge_worksheet import (
    CONFLICT_POLICIES,
    PENDING_DECISIONS,
    worksheet_service,
)

router = APIRouter(prefix="/api/merge-worksheets", tags=["归并差异底稿"])


class CreateWorksheetPayload(BaseModel):
    primary_export_id: int
    secondary_export_id: int
    merge_key: str
    rule_id: int | None = None


class ResolveCellPayload(BaseModel):
    pair_id: str
    field: str
    resolution: str
    manual_value: str | None = None


class PolicyPayload(BaseModel):
    policy: str


class PendingPayload(BaseModel):
    pending_id: str
    decision: str


class ConfirmPayload(BaseModel):
    reviewer: str = "复核员"


def _error(ex: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(ex))


@router.get("")
def list_worksheets() -> dict[str, Any]:
    """底稿台账：每条带输入对指纹与重开漂移检查。"""
    items = worksheet_service.list_worksheets()
    return {"items": items, "total": len(items)}


@router.post("")
def create_worksheet(payload: CreateWorksheetPayload) -> dict[str, Any]:
    try:
        return worksheet_service.create_from_exports(
            primary_export_id=payload.primary_export_id,
            secondary_export_id=payload.secondary_export_id,
            merge_key=payload.merge_key,
            rule_id=payload.rule_id,
        )
    except ValueError as ex:
        raise _error(ex) from ex


@router.get("/{worksheet_id}")
def get_worksheet(worksheet_id: int) -> dict[str, Any]:
    try:
        return worksheet_service.get_worksheet(worksheet_id)
    except KeyError as ex:
        raise HTTPException(status_code=404, detail=str(ex)) from ex


@router.post("/{worksheet_id}/resolve-cell")
def resolve_cell(worksheet_id: int, payload: ResolveCellPayload) -> dict[str, Any]:
    try:
        return worksheet_service.resolve_cell(
            worksheet_id, payload.pair_id, payload.field,
            payload.resolution, payload.manual_value,
        )
    except (KeyError, ValueError) as ex:
        raise _error(ex) from ex


@router.post("/{worksheet_id}/apply-policy")
def apply_policy(worksheet_id: int, payload: PolicyPayload) -> dict[str, Any]:
    try:
        return worksheet_service.apply_conflict_policy(worksheet_id, payload.policy)
    except ValueError as ex:
        raise _error(ex) from ex


@router.post("/{worksheet_id}/pending-decision")
def pending_decision(worksheet_id: int, payload: PendingPayload) -> dict[str, Any]:
    try:
        return worksheet_service.decide_pending_row(
            worksheet_id, payload.pending_id, payload.decision)
    except (KeyError, ValueError) as ex:
        raise _error(ex) from ex


@router.post("/{worksheet_id}/confirm")
def confirm_worksheet(worksheet_id: int, payload: ConfirmPayload) -> dict[str, Any]:
    try:
        return worksheet_service.confirm_worksheet(worksheet_id, payload.reviewer)
    except (KeyError, ValueError) as ex:
        raise _error(ex) from ex


@router.post("/{worksheet_id}/publish")
def publish_merge(worksheet_id: int) -> dict[str, Any]:
    try:
        return worksheet_service.publish_merge(worksheet_id)
    except (KeyError, ValueError) as ex:
        raise _error(ex) from ex


@router.get("/published/{published_id}/verify")
def verify_published(published_id: int) -> dict[str, Any]:
    try:
        return worksheet_service.verify_published(published_id)
    except KeyError as ex:
        raise HTTPException(status_code=404, detail=str(ex)) from ex


@router.get("/meta/options")
def policy_options() -> dict[str, Any]:
    """前端下拉选项：冲突策略与待确认原行处置方式。"""
    return {
        "conflict_policies": list(CONFLICT_POLICIES),
        "pending_decisions": list(PENDING_DECISIONS),
    }
