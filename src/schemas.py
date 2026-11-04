"""Pydantic 모델: 진단, 조치안, API 요청·응답."""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    """`remediate`가 지원하는 조치 유형 4종."""

    RESTART = "restart"
    SCALE = "scale"
    ROLLBACK = "rollback"
    SET_LIMITS = "set_limits"


class Hypothesis(BaseModel):
    """원인 가설 1건."""

    cause: str = Field(description="원인 가설")
    confidence: Literal["high", "medium", "low"] = Field(description="확신도")
    evidence: list[str] = Field(description="근거 목록. 메트릭 수치, 로그·이벤트 발췌, Runbook doc_id 출처를 포함")


class Diagnosis(BaseModel):
    """진단 결과. 근거가 부족하면 confidence를 low로 두고 확신 불가를 명시한다."""

    hypotheses: list[Hypothesis]


class RemediationPlan(BaseModel):
    """조치안. 승인 요청의 내용이 된다."""

    target: str = Field(description="대상 Deployment 이름")
    action_type: ActionType
    change: str = Field(description="변경 내용을 사람이 읽을 수 있게 설명 (변경 전 → 후)")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description='정책 검증과 실행에 쓰는 파라미터. 예: {"replicas": 4}, '
        '{"container": "server", "limits": {"memory": "60Mi"}}, {"to_revision": 2}',
    )
    impact: str = Field(description="예상 영향")
    rollback: str = Field(description="되돌리는 방법")
    evidence: list[str] = Field(description="근거 목록 (출처 포함)")


class QueryRequest(BaseModel):
    """POST /query 요청."""

    question: str
    thread_id: str | None = Field(default=None, description="이어갈 대화 thread. 없으면 새로 만든다")


class ContextItem(BaseModel):
    """응답에 포함되는 검색 문서."""

    doc_id: str
    text: str


class TraceStep(BaseModel):
    """실행 추적 1단계."""

    step: str
    input: str
    output: str


class QueryResponse(BaseModel):
    """POST /query, POST /resume 응답."""

    answer: str
    contexts: list[ContextItem]
    trace: list[TraceStep]
    thread_id: str | None = None


class ResumeRequest(BaseModel):
    """POST /resume 요청. 승인 대기 중인 thread를 재개한다."""

    thread_id: str
    decision: Literal["approve", "edit", "reject"]
    params: dict[str, Any] = Field(default_factory=dict, description="edit일 때 수정할 파라미터")
    approver: str
