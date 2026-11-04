"""메인 그래프: Supervisor + 서브 Agent 4종, 승인(interrupt), 사후 검증, 실행 추적.

흐름: 조사 → 진단 → 조치안 → 승인 → 실행 → 검증.
- Supervisor는 서브 Agent를 도구로 호출해 위임한다 (metrics_analyst, k8s_inspector, runbook_researcher, remediator).
- remediator는 `remediate` 호출 직전에 ApprovalGate가 `interrupt()`로 멈춘다.
- 가드레일은 `src/guardrails.py`의 미들웨어로 붙이며 이 파일에는 섞지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import AsyncExitStack
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from botocore.config import Config as BotoConfig
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_aws import ChatBedrockConverse
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import LLMResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool, tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command, interrupt

from src.config import get_policy, get_settings, langfuse_enabled, resolve_path
from src.guardrails import (
    BudgetGuard,
    InputGuardrail,
    OutputGuardrail,
    PolicyGuardrail,
    ToolResultGuardrail,
    current_budget,
    emit_guardrail,
    mask_text,
    message_text,
    new_budget,
)
from src.prompts import (
    DIAGNOSIS_PROMPT,
    K8S_INSPECTOR_PROMPT,
    METRICS_ANALYST_PROMPT,
    PLAN_PROMPT,
    REMEDIATOR_PROMPT,
    RUNBOOK_RESEARCHER_PROMPT,
    SUPERVISOR_PROMPT,
)
from src.schemas import ContextItem, Diagnosis, PlanResult, QueryResponse, RemediationPlan, TraceStep
from src.tools import open_mcp_tools, role_tools

_TRACE_CLIP = 1500
_DELEGATION_TOOLS = {
    "ask_k8s_inspector",
    "ask_metrics_analyst",
    "ask_runbook_researcher",
    "make_diagnosis",
    "make_remediation_plan",
    "ask_remediator",
}
_FAILURE_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "RunContainerError"}

# 승인 게이트가 승인 요청에 영향·롤백 문구를 담기 위해 읽는 현재 조치안
current_plan: ContextVar[RemediationPlan | None] = ContextVar("current_plan", default=None)


# ---------------------------------------------------------------- LLM

def build_llm() -> BaseChatModel:
    """설정값으로 Bedrock LLM을 만든다. 타임아웃과 지수 백오프 재시도를 적용한다."""
    cfg = get_settings().llm
    boto_cfg = BotoConfig(
        read_timeout=cfg.timeout_sec,
        connect_timeout=cfg.timeout_sec,
        retries={"max_attempts": cfg.retries, "mode": "standard"},
    )
    return ChatBedrockConverse(
        model=cfg.model_id,
        region_name=cfg.region,
        temperature=cfg.temperature,
        max_tokens=cfg.max_output_tokens,
        config=boto_cfg,
    )


# ---------------------------------------------------------------- 실행 추적

def _clip(text: str, limit: int = _TRACE_CLIP) -> str:
    """trace에 넣을 문자열을 마스킹하고 길이를 제한한다 (R4: 트레이스에 시크릿을 남기지 않는다)."""
    masked, _ = mask_text(text)
    return masked if len(masked) <= limit else masked[:limit] + "…(생략)"


class TraceCollector(AsyncCallbackHandler):
    """중첩된 서브 Agent 호출까지 포함해 trace step, contexts, 토큰 사용량을 수집한다."""

    run_inline = True  # 이벤트 순서를 보존한다

    def __init__(self) -> None:
        self.steps: list[TraceStep] = []
        self.contexts: dict[str, str] = {}
        self._tools: dict[UUID, tuple[str, str]] = {}

    def _add(self, step: str, inp: str, out: str) -> None:
        self.steps.append(TraceStep(step=step, input=_clip(inp), output=_clip(out)))

    async def on_tool_start(self, serialized: dict[str, Any], input_str: str, *, run_id: UUID, **kwargs: Any) -> None:
        """도구 호출 시작을 기록해 둔다."""
        name = serialized.get("name") or kwargs.get("name") or "unknown"
        self._tools[run_id] = (name, str(kwargs.get("inputs") or input_str))

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        """도구 호출 종료 시 step을 남긴다. 검색 도구는 `retrieve` step과 contexts도 만든다."""
        name, inp = self._tools.pop(run_id, ("unknown", ""))
        content = output.content if isinstance(output, ToolMessage) else output
        text = message_text(content) if not isinstance(content, str) else content
        if name in _DELEGATION_TOOLS:
            self._add("supervisor", f"{name}: {inp}", text)
            return
        if name == "retrieve_runbooks":
            docs = getattr(output, "artifact", None) or []
            for d in docs:
                self.contexts[d.metadata["doc_id"]] = d.page_content
            self._add("retrieve", inp, ", ".join(d.metadata["doc_id"] for d in docs) or "관련 문서 없음")
        self._add(f"tool:{name}", inp, text)

    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """도구 오류도 step으로 남긴다. 승인 대기를 위한 interrupt는 오류가 아니므로 제외한다."""
        name, inp = self._tools.pop(run_id, ("unknown", ""))
        if type(error).__name__ in {"GraphInterrupt", "GraphBubbleUp"}:
            return
        self._add(f"tool:{name}", inp, f"오류: {type(error).__name__}: {error}")

    async def on_custom_event(self, name: str, data: Any, *, run_id: UUID, **kwargs: Any) -> None:
        """가드레일·승인·검증 이벤트를 step으로 남긴다."""
        if name == "guardrail":
            self._add("guardrail", f"{data['rule']}", f"{data['action']}: {data['detail']}")
        elif name in ("approval", "verify"):
            self._add(name, str(data.get("input", "")), str(data.get("output", "")))

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        """LLM 토큰 사용량을 요청 한도 객체에 더한다."""
        budget = current_budget.get()
        if not budget:
            return
        for gens in response.generations:
            for gen in gens:
                usage = getattr(getattr(gen, "message", None), "usage_metadata", None) or {}
                budget.add_usage(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)))


# ---------------------------------------------------------------- 사후 검증

def _json_of(tool_output: Any) -> Any:
    """MCP 도구 출력(문자열 또는 블록 목록)을 JSON으로 해석한다. 실패하면 None."""
    text = tool_output if isinstance(tool_output, str) else message_text(tool_output)
    try:
        return json.loads(text)
    except ValueError:
        return None


def evaluate_rollout(dep: dict[str, Any], pods: list[dict[str, Any]]) -> tuple[str, str]:
    """조치 후 Deployment와 Pod 상태를 판정한다. (`healthy`|`failing`|`progressing`, 근거 문장)을 반환한다."""
    revision = dep["revision"]
    current = [p for p in pods if p.get("revision") == revision and not p.get("terminating")]
    for p in current:
        for c in p["containers"]:
            last = (c.get("last_terminated") or {}).get("reason")
            if c.get("reason") in _FAILURE_WAITING:
                return "failing", f"{p['name']}의 {c['name']}가 {c['reason']} 상태다 (재시작 {c['restarts']}회)"
            if last == "OOMKilled" and c["restarts"] > 0:
                return "failing", f"{p['name']}의 {c['name']}가 OOMKilled로 재시작되었다 (재시작 {c['restarts']}회)"
    replicas = dep["replicas"]
    ready = replicas["ready"] == replicas["desired"] and replicas["unavailable"] == 0
    synced = dep["observed_generation"] >= dep["generation"]
    all_ready = bool(current) and all(p["ready"].split("/")[0] == p["ready"].split("/")[1] for p in current)
    if ready and synced and all_ready and len(current) == replicas["desired"]:
        return "healthy", f"revision {revision}의 Pod {len(current)}개가 모두 Ready이고 재시작 증가가 없다"
    return "progressing", f"롤아웃 진행 중 (ready {replicas['ready']}/{replicas['desired']}, 새 revision Pod {len(current)}개)"


async def verify_remediation(inspector: BaseTool, target: str, namespace: str) -> dict[str, Any]:
    """조치 실행 뒤 상태를 재조회해 `success | failure | uncertain`으로 판정한다. 추가 조치는 하지 않는다.

    실행 60초 뒤부터 30초 간격으로 조회하며 최대 5분까지 본다 (설정값).
    정상 상태가 연속으로 유지되면 success, 실패 신호(CrashLoopBackOff, OOMKilled 재발 등)가 보이면 즉시 failure다.
    """
    cfg = get_settings().verification
    started = time.monotonic()
    await asyncio.sleep(cfg.initial_delay_sec)
    stable, polls, last_reason = 0, 0, "상태를 확인하지 못했다"
    while True:
        polls += 1
        dep = _json_of(await inspector.ainvoke({"action": "deployment", "namespace": namespace, "name": target}))
        pods = _json_of(await inspector.ainvoke({"action": "pods", "namespace": namespace, "name": target}))
        if isinstance(dep, dict) and "replicas" in dep and isinstance(pods, list):
            verdict, last_reason = evaluate_rollout(dep, pods)
            await adispatch(("verify", {"input": f"{target} 재조회 #{polls}", "output": f"{verdict}: {last_reason}"}))
            if verdict == "failing":
                return {"verdict": "failure", "reason": last_reason, "polls": polls, "elapsed_sec": int(time.monotonic() - started)}
            stable = stable + 1 if verdict == "healthy" else 0
            if stable >= cfg.stable_polls:
                return {"verdict": "success", "reason": last_reason, "polls": polls, "elapsed_sec": int(time.monotonic() - started)}
        if time.monotonic() - started + cfg.interval_sec > cfg.max_wait_sec:
            return {"verdict": "uncertain", "reason": f"제한 시간 안에 안정 상태를 확인하지 못했다. 마지막 상태: {last_reason}", "polls": polls, "elapsed_sec": int(time.monotonic() - started)}
        await asyncio.sleep(cfg.interval_sec)


async def adispatch(event: tuple[str, dict[str, Any]]) -> None:
    """trace용 커스텀 이벤트를 보낸다 (approval, verify)."""
    try:
        await adispatch_custom_event(event[0], event[1])
    except Exception:
        pass


# ---------------------------------------------------------------- 승인 게이트

class ApprovalGate(AgentMiddleware):
    """`remediate` 실행 직전에 `interrupt()`로 멈추고 승인자의 결정을 기다린다 (R1).

    정책 가드레일(PolicyGuardrail)을 통과한 호출만 여기에 도달한다. 승인되기 전에는 실행하지 않으며,
    승인·수정 후 실행하면 사후 검증까지 수행해 결과에 붙인다.
    """

    def __init__(self, inspector: BaseTool) -> None:
        super().__init__()
        self._inspector = inspector

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """dry_run은 통과시키고, 실제 실행은 승인 결정에 따라 처리한다."""
        call = request.tool_call
        args = dict(call["args"])
        if call["name"] != "remediate" or args.get("dry_run"):
            return await handler(request)

        policy, settings = get_policy(), get_settings()
        dry = _json_of((await handler(request.override(tool_call={**call, "args": {**args, "dry_run": True}}))).content) or {}
        plan = current_plan.get()
        payload = {
            "type": "approval_request",
            "action": str(getattr(args.get("action_type"), "value", args.get("action_type"))),
            "target": args.get("target"),
            "namespace": args.get("namespace", policy.namespace),
            "params": args.get("params") or {},
            "before": dry.get("before"),
            "after": dry.get("after"),
            "change": plan.change if plan else None,
            "impact": plan.impact if plan else None,
            "rollback": plan.rollback if plan else None,
            "evidence": plan.evidence if plan else [],
            "approver_role": policy.approval.group,
            "requested_at": time.time(),
            "expires_in_min": settings.approval.wait_minutes,
        }
        decision: dict[str, Any] = interrupt(payload)  # 재개될 때까지 여기서 멈춘다

        approver = str(decision.get("approver", ""))
        if approver not in policy.approval.user_ids:  # R1: 등록된 승인자만
            await emit_guardrail("R1", "blocked", "등록되지 않은 승인자의 결정을 거부했다")
            return self._result(call, {"status": "denied", "executed": False, "message": "승인 권한이 없는 사용자의 결정이라 실행하지 않았다"})
        kind = decision.get("decision")
        await adispatch(("approval", {"input": f"{payload['action']} {payload['target']}", "output": f"{kind} by {approver}" + (" (만료되어 거절 처리)" if decision.get("expired") else "")}))
        if decision.get("expired"):
            return self._result(call, {"status": "expired", "executed": False, "message": f"승인 대기 {payload['expires_in_min']}분이 지나 만료(거절)되었다"})
        if kind == "reject":
            return self._result(call, {"status": "rejected_by_approver", "executed": False, "message": "승인자가 거절해 실행하지 않았다"})

        run_args = {**args, "approver": approver, "dry_run": False}
        if kind == "edit":
            run_args["params"] = {**(args.get("params") or {}), **(decision.get("params") or {})}
            check = _json_of((await handler(request.override(tool_call={**call, "args": {**run_args, "dry_run": True}}))).content) or {}
            if check.get("status") != "ok":
                await emit_guardrail("R2", "blocked", f"승인자가 수정한 파라미터가 정책을 위반했다: {check.get('message')}")
                msg = f"승인자가 수정한 값({json.dumps(decision.get('params'), ensure_ascii=False)})이 정책을 위반해 실행하지 않았다: {check.get('message')}"
                return self._result(call, {"status": "blocked", "executed": False, "message": msg, "alternative": check.get("alternative")})

        executed = await handler(request.override(tool_call={**call, "args": run_args}))
        result = _json_of(executed.content) or {"raw": message_text(executed.content)}
        if result.get("status") != "ok":
            return self._result(call, {"executed": False, **result})
        verification = await verify_remediation(self._inspector, str(args["target"]), payload["namespace"])
        return self._result(call, {"executed": True, "execution": result, "verification": verification})

    @staticmethod
    def _result(call: dict[str, Any], body: dict[str, Any]) -> ToolMessage:
        """도구 결과 메시지를 만든다."""
        return ToolMessage(content=json.dumps(body, ensure_ascii=False), tool_call_id=call["id"], name=call["name"])


# ---------------------------------------------------------------- 서브 Agent와 Supervisor

def _format_diagnosis(diag: Diagnosis) -> str:
    """진단 결과를 Supervisor가 읽기 쉬운 텍스트로 바꾼다."""
    lines = []
    for i, h in enumerate(diag.hypotheses, 1):
        lines.append(f"가설 {i} [{h.confidence}] {h.cause}")
        lines.extend(f"  - 근거: {e}" for e in h.evidence)
    return "\n".join(lines)


def build_supervisor(
    llm: BaseChatModel, roles: dict[str, list[BaseTool]], checkpointer: AsyncSqliteSaver
) -> Any:
    """서브 Agent 4종과 Supervisor 그래프를 만든다."""
    limits = get_settings().limits
    sub_recursion = 2 * limits.subagent_max_iterations + 1
    common: list[AgentMiddleware] = [BudgetGuard(), InputGuardrail(detect=False), ToolResultGuardrail()]

    def sub_agent(name: str, prompt: str, extra: list[AgentMiddleware] | None = None, persist: bool = False) -> Any:
        return create_agent(
            llm, roles[name], system_prompt=prompt, name=name,
            middleware=[*common, *(extra or [])], checkpointer=None if persist else False,
        )

    inspector = sub_agent("k8s_inspector", K8S_INSPECTOR_PROMPT)
    metrics = sub_agent("metrics_analyst", METRICS_ANALYST_PROMPT)
    researcher = sub_agent("runbook_researcher", RUNBOOK_RESEARCHER_PROMPT)
    # remediator는 승인 대기 상태를 체크포인트로 보존해야 하므로 상위 checkpointer를 상속한다
    remediator = sub_agent(
        "remediator", REMEDIATOR_PROMPT,
        extra=[PolicyGuardrail(), ApprovalGate(next(t for t in roles["k8s_inspector"] if t.name == "inspect_k8s"))],
        persist=True,
    )

    async def run_sub(agent: Any, request: str, config: RunnableConfig) -> str:
        """서브 Agent를 실행하고 마지막 응답 텍스트를 반환한다. 반복 한도에 닿으면 그 사실을 알린다."""
        try:
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=request)]}, config={**config, "recursion_limit": sub_recursion}
            )
        except GraphRecursionError:
            return f"서브 Agent 반복 한도({limits.subagent_max_iterations}회)에 도달해 중단했다. 확인된 내용만으로 판단해야 한다."
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and not msg.tool_calls:
                return message_text(msg.content)
        return "서브 Agent가 응답을 만들지 못했다."

    @tool
    async def ask_k8s_inspector(request: str, config: RunnableConfig) -> str:
        """k8s_inspector에게 클러스터 상태 조회를 맡긴다 (Pod·Deployment 상태, 이벤트, 로그, revision 이력, HTTP 에러율·지연).

        Args:
            request: 무엇을 확인할지 구체적으로 쓴다. 서브 Agent는 이전 대화를 모른다. 예: "recommendationservice의 Pod 상태와 마지막 종료 사유, 재시작 횟수, 현재 limit을 확인해 줘"
        """
        return await run_sub(inspector, request, config)

    @tool
    async def ask_metrics_analyst(request: str, config: RunnableConfig) -> str:
        """metrics_analyst에게 Prometheus 지표 조회를 맡긴다 (CPU·메모리·재시작·replica).

        Args:
            request: 확인할 서비스와 지표. 예: "frontend의 최근 30분 CPU 사용률(limit 대비)과 스로틀링을 확인해 줘"
        """
        return await run_sub(metrics, request, config)

    @tool
    async def ask_runbook_researcher(request: str, config: RunnableConfig) -> str:
        """runbook_researcher에게 Runbook·Postmortem 검색을 맡긴다.

        Args:
            request: 서비스명과 증상 키워드. 예: "cartservice ImagePullBackOff 이미지 태그 오류 대응 절차"
        """
        return await run_sub(researcher, request, config)

    @tool
    async def make_diagnosis(findings: str) -> str:
        """조사 결과를 모아 구조화된 진단(원인 가설, 확신도, 근거)을 만든다.

        Args:
            findings: 서브 Agent 보고 내용을 그대로 모은 조사 결과 (수치, 로그·이벤트 발췌, Runbook doc_id 포함)
        """
        diag = await llm.with_structured_output(Diagnosis).ainvoke(
            [SystemMessage(content=DIAGNOSIS_PROMPT), HumanMessage(content=findings)]
        )
        return _format_diagnosis(diag)

    @tool
    async def make_remediation_plan(findings: str, diagnosis: str) -> str:
        """진단으로 조치안 1건(대상, 조치 유형, 파라미터, 변경, 영향, 롤백, 근거)을 만든다. 4종 조치로 해결할 수 없으면 수동 조치 가이드를 반환한다.

        Args:
            findings: 서브 Agent 조사 결과 (현재 replica·limit·revision 값 포함)
            diagnosis: make_diagnosis의 결과
        """
        result = await llm.with_structured_output(PlanResult).ainvoke(
            [SystemMessage(content=PLAN_PROMPT), HumanMessage(content=f"## 진단\n{diagnosis}\n\n## 조사 결과\n{findings}")]
        )
        if result.plan is None:
            return f"조치안 없음 (4종 조치로 해결할 수 없거나 정보 부족)\n수동 조치 가이드: {result.manual_guide}"
        return "조치안:\n" + result.plan.model_dump_json(indent=2) + "\n실행을 요청받은 경우 이 값을 ask_remediator에 그대로 전달한다."

    async def _ask_remediator(config: RunnableConfig, **fields: Any) -> str:
        plan = RemediationPlan(**fields)
        token = current_plan.set(plan)
        try:
            request = f"다음 조치안을 remediate 도구로 실행한다.\n{plan.model_dump_json(indent=2)}"
            return await run_sub(remediator, request, config)
        finally:
            current_plan.reset(token)

    ask_remediator = StructuredTool.from_function(
        coroutine=_ask_remediator,
        name="ask_remediator",
        description=(
            "사용자가 클러스터 변경을 명시적으로 요청했을 때만 호출한다. 조치안으로 승인 요청을 만든다. "
            "승인자가 승인하기 전에는 실행되지 않으며, 정책 위반이면 승인 요청 없이 사유와 대안을 반환한다. "
            "인자는 make_remediation_plan이 만든 조치안 값이다."
        ),
        args_schema=RemediationPlan,
    )

    return create_agent(
        llm,
        [ask_k8s_inspector, ask_metrics_analyst, ask_runbook_researcher, make_diagnosis, make_remediation_plan, ask_remediator],
        system_prompt=SUPERVISOR_PROMPT,
        name="supervisor",
        middleware=[BudgetGuard(count_tools=False), InputGuardrail(classifier=llm), ToolResultGuardrail(), OutputGuardrail()],
        checkpointer=checkpointer,
    )


# ---------------------------------------------------------------- 실행기

class CopilotRuntime:
    """그래프, MCP 세션, 체크포인트를 묶어 질의와 승인 재개를 처리한다. API와 Slack이 이 객체를 쓴다."""

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._graph: Any = None
        self._saver: AsyncSqliteSaver | None = None

    async def start(self) -> None:
        """MCP 세션을 열고 체크포인트와 그래프를 준비한다."""
        settings = get_settings()
        mcp_tools = await open_mcp_tools(self._stack)
        db_path = str(resolve_path(settings.approval.checkpoint_db))
        self._saver = await self._stack.enter_async_context(AsyncSqliteSaver.from_conn_string(db_path))
        await self._prune_checkpoints()
        self._graph = build_supervisor(build_llm(), role_tools(mcp_tools), self._saver)

    async def stop(self) -> None:
        """MCP 세션과 체크포인트 연결을 닫는다."""
        await self._stack.aclose()

    async def _prune_checkpoints(self) -> None:
        """보존 기간(기본 7일)이 지난 thread의 체크포인트를 지운다."""
        assert self._saver is not None
        conn = self._saver.conn
        await conn.execute("CREATE TABLE IF NOT EXISTS thread_activity (thread_id TEXT PRIMARY KEY, updated_at REAL)")
        cutoff = time.time() - get_settings().approval.checkpoint_retention_days * 86400
        async with conn.execute("SELECT thread_id FROM thread_activity WHERE updated_at < ?", (cutoff,)) as cur:
            old = [r[0] for r in await cur.fetchall()]
        for tid in old:
            for table in ("checkpoints", "writes"):
                await conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (tid,))
            await conn.execute("DELETE FROM thread_activity WHERE thread_id = ?", (tid,))
        await conn.commit()

    async def _touch(self, thread_id: str) -> None:
        """thread의 마지막 사용 시각을 기록한다."""
        assert self._saver is not None
        await self._saver.conn.execute(
            "INSERT INTO thread_activity(thread_id, updated_at) VALUES(?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET updated_at = excluded.updated_at",
            (thread_id, time.time()),
        )
        await self._saver.conn.commit()

    def _config(self, thread_id: str, collector: TraceCollector) -> RunnableConfig:
        """thread와 콜백(추적, Langfuse)을 담은 실행 설정을 만든다."""
        limits = get_settings().limits
        callbacks: list[Any] = [collector]
        if langfuse_enabled():
            from langfuse.langchain import CallbackHandler

            callbacks.append(CallbackHandler())
        return {
            "configurable": {"thread_id": thread_id},
            "callbacks": callbacks,
            "recursion_limit": 2 * limits.max_tool_calls + 1,
            "metadata": {"langfuse_session_id": thread_id},
        }

    async def query(self, question: str, thread_id: str | None = None) -> QueryResponse:
        """질문을 처리한다. 승인이 필요하면 승인 대기 상태로 멈추고 안내를 반환한다."""
        thread_id = thread_id or uuid.uuid4().hex
        return await self._run({"messages": [HumanMessage(content=question)]}, thread_id, question,
                               get_settings().limits.request_timeout_sec)

    async def resume(self, thread_id: str, decision: str, params: dict[str, Any], approver: str) -> QueryResponse:
        """승인 대기 중인 thread를 재개한다. 만료되었으면 거절로 처리한다."""
        pending = await self.pending_approval(thread_id)
        expired = bool(pending) and time.time() > pending["requested_at"] + pending["expires_in_min"] * 60
        resume_value = {"decision": decision, "params": params, "approver": approver, "expired": expired}
        s = get_settings()
        timeout = s.limits.request_timeout_sec + s.verification.initial_delay_sec + s.verification.max_wait_sec
        return await self._run(Command(resume=resume_value), thread_id, f"resume:{decision}", timeout)

    async def pending_approval(self, thread_id: str) -> dict[str, Any] | None:
        """thread가 승인 대기 중이면 승인 요청 내용을, 아니면 None을 반환한다."""
        state = await self._graph.aget_state({"configurable": {"thread_id": thread_id}})
        for task in state.tasks:
            for intr in task.interrupts:
                if isinstance(intr.value, dict) and intr.value.get("type") == "approval_request":
                    return intr.value
        return None

    async def _run(self, graph_input: Any, thread_id: str, label: str, timeout: float) -> QueryResponse:
        """그래프를 실행하고 응답(answer, contexts, trace)을 만든다."""
        collector = TraceCollector()
        collector.steps.append(TraceStep(step="supervisor", input=_clip(label), output="요청 수신"))
        budget = new_budget()
        budget.deadline = time.monotonic() + timeout
        token = current_budget.set(budget)
        config = self._config(thread_id, collector)
        try:
            result = await asyncio.wait_for(self._graph.ainvoke(graph_input, config), timeout=timeout)
            answer = self._last_answer(result.get("messages", []))
        except asyncio.TimeoutError:
            answer = "처리 시간 한도를 넘어 중단했다. 질문을 나누어 다시 요청해 달라."
            collector.steps.append(TraceStep(step="guardrail", input="LIMIT", output="stopped: 요청 시간 초과"))
        except Exception as e:  # LLM·도구 오류가 서비스를 중단시키지 않도록 안내한다
            answer = f"처리 중 오류가 발생했다 ({type(e).__name__}). 잠시 후 다시 시도해 달라."
            collector.steps.append(TraceStep(step="supervisor", input="error", output=_clip(f"{type(e).__name__}: {e}")))
        finally:
            current_budget.reset(token)
        pending = await self.pending_approval(thread_id)
        if pending:
            answer = self._approval_answer(pending, thread_id)
            collector.steps.append(TraceStep(step="approval", input=_clip(json.dumps(pending, ensure_ascii=False)),
                                             output=f"승인 대기 (thread_id={thread_id}, 만료 {pending['expires_in_min']}분)"))
        else:
            collector.steps.append(TraceStep(step="supervisor", input="최종 응답", output=_clip(answer)))
        await self._touch(thread_id)
        return QueryResponse(
            answer=mask_text(answer)[0],
            contexts=[ContextItem(doc_id=k, text=v) for k, v in collector.contexts.items()],
            trace=collector.steps,
            thread_id=thread_id,
        )

    @staticmethod
    def _last_answer(messages: list[BaseMessage]) -> str:
        """마지막 AI 응답 텍스트를 반환한다."""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and not msg.tool_calls:
                return message_text(msg.content)
        return ""

    @staticmethod
    def _approval_answer(p: dict[str, Any], thread_id: str) -> str:
        """승인 대기 상태 안내문을 만든다. 승인 전에는 실행되지 않았음을 분명히 밝힌다."""
        lines = [
            "승인이 필요한 조치라 실행하지 않고 승인 대기 중이다. 승인 전에는 클러스터가 변경되지 않는다.",
            f"- 대상: {p['namespace']}/{p['target']}",
            f"- 조치: {p['action']} {json.dumps(p['params'], ensure_ascii=False)}",
            f"- 변경 내용: {p.get('change') or json.dumps({'before': p.get('before'), 'after': p.get('after')}, ensure_ascii=False)}",
            f"- 예상 영향: {p.get('impact') or '파드가 순차 교체되며 교체 중 일시적으로 처리 용량이 줄 수 있다'}",
            f"- 롤백 방법: {p.get('rollback') or '`kubectl rollout undo`로 직전 revision으로 되돌린다'}",
        ]
        if p.get("evidence"):
            lines.append("- 근거: " + "; ".join(p["evidence"]))
        lines.append(f"- 승인 권한: {p['approver_role']} (등록된 승인자). {p['expires_in_min']}분 안에 승인하지 않으면 만료(거절)된다.")
        lines.append(f"- 재개: thread_id={thread_id}")
        return "\n".join(lines)
