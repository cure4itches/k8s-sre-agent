"""HTTP API: POST /query, POST /resume. Slack과 자체 평가는 모두 이 API를 거친다."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request

from src.agent import MODEL_USAGE, CopilotRuntime
from src.config import get_policy
from src.schemas import QueryRequest, QueryResponse, ResumeRequest


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """서버 시작 시 MCP 세션과 그래프를 준비하고, 종료 시 정리한다."""
    runtime = CopilotRuntime()
    await runtime.start()
    app.state.runtime = runtime
    app.state.locks = defaultdict(asyncio.Lock)  # thread별 중복 실행 방지
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(title="K8s 장애 대응 Copilot", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """헬스체크. 모델별 성공 호출 수(쿼터 초과 시 로테이션 확인용)도 돌려준다."""
    return {"status": "ok", "llm_usage": dict(MODEL_USAGE)}


@app.post("/query", response_model=QueryResponse)
async def query(body: QueryRequest, request: Request) -> QueryResponse:
    """질문을 처리한다. `thread_id`가 없으면 새 대화를 시작한다. 승인이 필요하면 승인 대기 안내와 `approval` step을 반환한다."""
    runtime: CopilotRuntime = request.app.state.runtime
    if body.thread_id:
        async with request.app.state.locks[body.thread_id]:
            return await runtime.query(body.question, body.thread_id)
    return await runtime.query(body.question)


@app.post("/resume", response_model=QueryResponse)
async def resume(body: ResumeRequest, request: Request) -> QueryResponse:
    """승인 대기 중인 thread를 재개한다 (approve | edit | reject).

    승인자(`approver`)가 `policy.yaml`에 등록된 Slack User ID가 아니면 승인 대기를 소비하지 않고 403으로 거부한다 (R1).
    """
    runtime: CopilotRuntime = request.app.state.runtime
    if body.approver not in get_policy().approval.user_ids:
        raise HTTPException(status_code=403, detail="승인 권한이 없는 사용자다")
    async with request.app.state.locks[body.thread_id]:
        if not await runtime.pending_approval(body.thread_id):
            raise HTTPException(status_code=409, detail="승인 대기 중인 요청이 없다 (이미 처리되었거나 thread_id가 올바르지 않다)")
        return await runtime.resume(body.thread_id, body.decision, body.params, body.approver)
