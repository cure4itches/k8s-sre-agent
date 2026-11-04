"""도메인 도구: retrieve_runbooks, query_metrics, MCP 도구(inspect_k8s, remediate) 로딩."""
from __future__ import annotations

import json
import re
import sys
import time
from contextlib import AsyncExitStack
from typing import Any, Literal

import httpx
from langchain_core.documents import Document
from langchain_core.tools import BaseTool, tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from src.config import ROOT_DIR, get_settings
from src.retriever import RetrieverError, get_retriever

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")
_MAX_SERIES = 20

MetricName = Literal[
    "cpu_usage_cores",
    "cpu_vs_limit",
    "cpu_throttling",
    "memory_working_set_mi",
    "memory_vs_limit",
    "restarts",
    "last_terminated_reason",
    "replicas",
]


@tool(response_format="content_and_artifact")
def retrieve_runbooks(query: str) -> tuple[str, list[Document]]:
    """Runbook과 Postmortem에서 증상과 유사한 대응 절차를 검색하고 출처(doc_id)를 반환한다.

    원인 가설이나 조치안을 세우기 전에 대응 절차의 근거를 찾을 때 사용한다.
    질의는 사용자 문장 그대로가 아니라 "서비스명 + 증상 키워드"로 구체적으로 쓴다.
    관련 문서가 없으면 "관련 문서 없음"을 반환하며, 그 경우 문서 내용을 지어내지 않는다.

    Args:
        query: 검색 질의. 예: "recommendationservice OOMKilled 반복 재시작 메모리 limit"

    Examples:
        retrieve_runbooks(query="cartservice ImagePullBackOff 이미지 태그 오류 롤백")
        retrieve_runbooks(query="frontend 응답 지연 CPU 포화 replica 확장")
    """
    try:
        result = get_retriever().search(query)
    except RetrieverError as e:
        return f"문서 검색에 실패했다: {e}", []
    if result.no_match:
        return f"관련 문서 없음 (최고 유사도 {result.best_score:.2f}). 문서 내용을 추정하지 않는다.", []
    text = "\n\n".join(f"[doc_id: {d.metadata['doc_id']}] (유사도 {d.metadata['score']})\n{d.page_content}" for d in result.docs)
    return text, result.docs


def _prom_get(client: httpx.Client, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Prometheus HTTP API를 호출해 결과 목록을 반환한다."""
    resp = client.get(path, params=params)
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus 오류: {body.get('error')}")
    return body["data"]["result"]


def _selector(namespace: str, service: str | None, extra: str = "") -> str:
    """PromQL 레이블 셀렉터를 만든다. 입력값은 검증된 값만 들어온다."""
    pod = f',pod=~"{service}-[a-z0-9]+-[a-z0-9]+"' if service else ""
    return f'namespace="{namespace}"{pod}{extra}'


def _build_queries(metric: str, namespace: str, service: str | None, minutes: int) -> tuple[str, str]:
    """지표 이름을 PromQL로 바꾼다. (쿼리, 종류) 를 반환하며 종류는 `range`(시계열) 또는 `instant`다."""
    ctr = ',container!="",container!="POD"'
    sel = _selector(namespace, service, ctr)
    lim_sel = _selector(namespace, service)
    if metric == "cpu_usage_cores":
        return f"sum by (pod) (rate(container_cpu_usage_seconds_total{{{sel}}}[1m]))", "range"
    if metric == "cpu_vs_limit":
        return (
            f"sum by (pod) (rate(container_cpu_usage_seconds_total{{{sel}}}[1m])) / "
            f'sum by (pod) (kube_pod_container_resource_limits{{{lim_sel},resource="cpu"}})',
            "range",
        )
    if metric == "cpu_throttling":
        return (
            f"sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[1m])) / "
            f"sum by (pod) (rate(container_cpu_cfs_periods_total{{{sel}}}[1m]))",
            "range",
        )
    if metric == "memory_working_set_mi":
        return f"sum by (pod, container) (container_memory_working_set_bytes{{{sel}}}) / 1048576", "range"
    if metric == "memory_vs_limit":
        return (
            f"max by (pod, container) (container_memory_working_set_bytes{{{sel}}}) / on (pod, container) "
            f'max by (pod, container) (kube_pod_container_resource_limits{{{lim_sel},resource="memory"}})',
            "range",
        )
    if metric == "restarts":
        return f"increase(kube_pod_container_status_restarts_total{{{lim_sel}}}[{minutes}m])", "instant"
    if metric == "last_terminated_reason":
        return f"kube_pod_container_status_last_terminated_reason{{{lim_sel}}} == 1", "instant"
    dep = f',deployment="{service}"' if service else ""
    return f'kube_deployment_status_replicas_unavailable{{namespace="{namespace}"{dep}}}', "instant"


def _summarize(series: dict[str, Any], kind: str) -> dict[str, Any]:
    """시계열 하나를 요약한다. 레이블과 최신·평균·최대·최소 값을 반환한다."""
    labels = {k: v for k, v in series["metric"].items() if k in ("pod", "container", "reason", "deployment")}
    if kind == "instant":
        return {**labels, "value": round(float(series["value"][1]), 4)}
    points = [(float(t), float(v)) for t, v in series["values"] if v != "NaN"]
    values = [v for _, v in points]
    if not values:
        return {**labels, "note": "값 없음"}
    return {
        **labels,
        # 삭제·교체된 Pod의 과거 시계열을 구분하기 위해 마지막 샘플이 얼마나 오래됐는지 표시한다
        "last_sample_age_sec": int(time.time() - points[-1][0]),
        "latest": round(values[-1], 4),
        "avg": round(sum(values) / len(values), 4),
        "max": round(max(values), 4),
        "min": round(min(values), 4),
    }


def _replicas_view(client: httpx.Client, namespace: str, service: str | None) -> list[dict[str, Any]]:
    """Deployment의 희망·가용·불가용 replica 수를 반환한다."""
    dep = f',deployment="{service}"' if service else ""
    names = {
        "desired": "kube_deployment_spec_replicas",
        "available": "kube_deployment_status_replicas_available",
        "unavailable": "kube_deployment_status_replicas_unavailable",
    }
    table: dict[str, dict[str, Any]] = {}
    for field, metric in names.items():
        for s in _prom_get(client, "/api/v1/query", {"query": f'{metric}{{namespace="{namespace}"{dep}}}'}):
            table.setdefault(s["metric"].get("deployment", "?"), {"deployment": s["metric"].get("deployment")})[field] = int(float(s["value"][1]))
    return list(table.values())


@tool
def query_metrics(
    metric: MetricName,
    service: str | None = None,
    namespace: str = "online-boutique",
    minutes: int | None = None,
) -> str:
    """Prometheus에서 서비스별 CPU·메모리·재시작·replica 지표를 조회한다.

    장애 원인을 수치로 뒷받침할 때 사용한다. 기본 조회 범위는 최근 30분이다.
    HTTP 에러율과 응답 지연은 이 도구가 아니라 inspect_k8s(action="http_stats")로 조회한다.
    보존 기간을 벗어난 시점의 데이터는 없으며, 그 경우 "데이터 없음"을 반환한다. 데이터를 추정하지 않는다.

    metric 종류:
    - cpu_usage_cores: Pod별 CPU 사용량(core)
    - cpu_vs_limit: CPU 사용량 / CPU limit (0.8 이상이면 포화 의심)
    - cpu_throttling: CPU 스로틀링 비율 (0.25 이상이면 지연 원인 가능)
    - memory_working_set_mi: 컨테이너별 메모리 사용량(Mi)
    - memory_vs_limit: 메모리 사용량 / 메모리 limit (0.9 이상이면 OOM 위험)
    - restarts: 조회 범위 동안 컨테이너 재시작 횟수 증가분
    - last_terminated_reason: 마지막 종료 사유(OOMKilled, Error 등)가 있는 컨테이너
    - replicas: Deployment별 희망·가용·불가용 replica 수

    Args:
        metric: 지표 종류
        service: 서비스(Deployment) 이름. 생략하면 네임스페이스 전체. 예: "frontend"
        namespace: 네임스페이스. 기본 online-boutique
        minutes: 조회 범위(분). 기본 30

    Examples:
        query_metrics(metric="memory_vs_limit", service="recommendationservice")
        query_metrics(metric="restarts", service="recommendationservice", minutes=15)
        query_metrics(metric="cpu_vs_limit", service="frontend")
    """
    cfg = get_settings().tools
    minutes = minutes or cfg.metrics_default_range_minutes
    if not 1 <= minutes <= cfg.metrics_max_range_minutes:
        return json.dumps({"error": f"minutes는 1~{cfg.metrics_max_range_minutes} 범위여야 한다"}, ensure_ascii=False)
    if not _NAME_RE.match(namespace) or (service and not _NAME_RE.match(service)):
        return json.dumps({"error": "서비스·네임스페이스 이름 형식이 올바르지 않다"}, ensure_ascii=False)

    base: dict[str, Any] = {"metric": metric, "service": service, "namespace": namespace, "range_minutes": minutes}
    try:
        with httpx.Client(base_url=cfg.prometheus_url, timeout=cfg.tool_timeout_sec) as client:
            if metric == "replicas":
                series: list[dict[str, Any]] = _replicas_view(client, namespace, service)
            else:
                query, kind = _build_queries(metric, namespace, service, minutes)
                if kind == "range":
                    end = time.time()
                    step = max(cfg.metrics_step_sec, minutes * 60 // 300)
                    raw = _prom_get(client, "/api/v1/query_range", {"query": query, "start": end - minutes * 60, "end": end, "step": step})
                else:
                    raw = _prom_get(client, "/api/v1/query", {"query": query})
                series = [_summarize(s, kind) for s in raw[:_MAX_SERIES]]
            if not series:
                retention = _prom_get_retention(client)
                note = f"데이터 없음. 해당 서비스·기간의 지표가 없거나 Prometheus 보존 기간(약 {retention}) 밖이다. 값을 추정하지 않는다."
                return json.dumps({**base, "series": [], "note": note}, ensure_ascii=False)
    except httpx.HTTPError as e:
        return json.dumps({**base, "error": f"Prometheus 조회 실패: {type(e).__name__}: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({**base, "error": f"지표 조회 실패: {type(e).__name__}: {e}"}, ensure_ascii=False)
    return json.dumps({**base, "series": series}, ensure_ascii=False)


def _prom_get_retention(client: httpx.Client) -> str:
    """Prometheus 데이터 보존 기간을 조회한다. 실패하면 `알 수 없음`을 반환한다."""
    try:
        resp = client.get("/api/v1/status/runtimeinfo")
        return str(resp.json()["data"].get("storageRetention", "알 수 없음"))
    except Exception:
        return "알 수 없음"


async def open_mcp_tools(stack: AsyncExitStack) -> dict[str, BaseTool]:
    """MCP 서버 2종(읽기 inspect_k8s, 쓰기 remediate)을 stdio로 띄우고 세션을 `stack`에 유지한다.

    호출마다 서버 프로세스를 새로 띄우면 호출당 수 초가 걸리므로 세션을 재사용한다.
    도구 이름 → 도구 매핑을 반환한다. 세션은 `stack`을 닫을 때 정리된다.
    """
    def server(module: str) -> dict[str, Any]:
        return {"transport": "stdio", "command": sys.executable, "args": ["-m", module], "cwd": str(ROOT_DIR)}

    mcp_client = MultiServerMCPClient(
        {"inspect_k8s": server("src.mcp_servers.k8s_read"), "remediate": server("src.mcp_servers.k8s_write")}
    )
    tools: dict[str, BaseTool] = {}
    for server_name in ("inspect_k8s", "remediate"):
        session = await stack.enter_async_context(mcp_client.session(server_name))
        for t in await load_mcp_tools(session):
            tools[t.name] = t
    return tools


def role_tools(mcp_tools: dict[str, BaseTool]) -> dict[str, list[BaseTool]]:
    """서브 Agent 역할별 도구 목록을 반환한다 (metrics_analyst, k8s_inspector, runbook_researcher, remediator)."""
    return {
        "metrics_analyst": [query_metrics],
        "k8s_inspector": [mcp_tools["inspect_k8s"]],
        "runbook_researcher": [retrieve_runbooks],
        "remediator": [mcp_tools["remediate"]],
    }
