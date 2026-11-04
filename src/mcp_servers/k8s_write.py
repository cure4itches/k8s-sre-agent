"""remediate: 클러스터 변경 조치 4종(restart, scale, rollback, set_limits)을 실행하는 쓰기 MCP 서버.

쓰기 전용 ServiceAccount(`.kube/write.kubeconfig`)를 쓰며, 권한은 `online-boutique`의 Deployment patch뿐이다.
에이전트가 승인 절차(interrupt)를 거친 뒤에만 호출하는 것이 원칙이지만, 서버도 방어선으로
정책(`config/policy.yaml`)과 승인자 ID를 다시 검증한다 (SERVICE.md R1, R2).
`dry_run=True`는 변경 없이 정책 검증과 변경 전·후 값만 반환한다 (승인 요청을 만들기 전 점검용).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any

from kubernetes import client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException
from kubernetes.utils import parse_quantity
from mcp.server.fastmcp import FastMCP

from src.config import get_policy, get_settings, resolve_path
from src.schemas import ActionType

mcp = FastMCP("remediate", log_level="WARNING")

_REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
_AUDIT_ANNOTATION = "sre-copilot.io/last-remediation"
_MI = Decimal(1024 * 1024)


class PolicyViolation(Exception):
    """정책 위반. `alternative`에 허용 범위 안의 대안을 담는다."""

    def __init__(self, message: str, alternative: str | None = None) -> None:
        super().__init__(message)
        self.alternative = alternative


@lru_cache(maxsize=1)
def _apps() -> client.AppsV1Api:
    """쓰기 전용 kubeconfig로 AppsV1 클라이언트를 만든다."""
    path = resolve_path(get_settings().tools.write_kubeconfig)
    return client.AppsV1Api(k8s_config.new_client_from_config(config_file=str(path)))


def _timeout() -> int:
    """API 호출 타임아웃(초)."""
    return get_settings().tools.tool_timeout_sec


def _fmt_mem(value: Decimal) -> str:
    """바이트 값을 `Mi` 단위 문자열로 바꾼다."""
    return f"{int(value / _MI)}Mi"


def _fmt_cpu(value: Decimal) -> str:
    """core 값을 `m`(millicore) 단위 문자열로 바꾼다."""
    return f"{int(value * 1000)}m"


def _fmt(resource: str, value: Decimal) -> str:
    """리소스 종류에 맞게 값을 문자열로 바꾼다."""
    return _fmt_cpu(value) if resource == "cpu" else _fmt_mem(value)


def _check_cooldown(dep: client.V1Deployment) -> None:
    """같은 Deployment에 연속 조치하지 않도록 최근 조치 시각을 확인한다."""
    raw = (dep.metadata.annotations or {}).get(_AUDIT_ANNOTATION)
    if not raw:
        return
    try:
        last = datetime.fromisoformat(json.loads(raw)["at"])
    except (ValueError, KeyError, TypeError):
        return
    wait = get_policy().cooldown_sec - (datetime.now(timezone.utc) - last).total_seconds()
    if wait > 0:
        raise PolicyViolation(f"같은 Deployment에 {get_policy().cooldown_sec}초 안에 연속 조치할 수 없다", f"{int(wait)}초 뒤에 다시 시도한다")


def _plan_restart(dep: client.V1Deployment, params: dict[str, Any]) -> tuple[dict[str, Any], Any, dict[str, Any], dict[str, Any]]:
    """restart 조치를 계획한다. 파드 템플릿 어노테이션을 바꿔 순차 교체를 일으킨다."""
    now = datetime.now(timezone.utc).isoformat()
    body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
    return {}, body, {"replicas": dep.spec.replicas}, {"rolling_restart": True}


def _plan_scale(dep: client.V1Deployment, params: dict[str, Any]) -> tuple[dict[str, Any], Any, dict[str, Any], dict[str, Any]]:
    """scale 조치를 계획한다. 1~최대 replica, 1회 최대 배수를 검증한다."""
    pol = get_policy().scale
    current = int(dep.spec.replicas or 0)
    allowed_max = min(pol.max_replicas, int(current * pol.max_factor))
    try:
        target = int(params["replicas"])
    except (KeyError, TypeError, ValueError):
        raise PolicyViolation('params에 정수 replicas가 필요하다. 예: {"replicas": 2}') from None
    if target == current:
        raise PolicyViolation(f"이미 replica가 {current}개다")
    if not pol.min_replicas <= target <= pol.max_replicas:
        raise PolicyViolation(
            f"replica {target}은 허용 범위({pol.min_replicas}~{pol.max_replicas})를 벗어난다",
            f"현재 {current}개에서 1회 최대 {allowed_max}개까지 변경할 수 있다",
        )
    if target > current * pol.max_factor:
        raise PolicyViolation(
            f"1회 변경은 현재 값의 최대 {pol.max_factor:g}배까지만 허용한다 (현재 {current}개 → {target}개 요청)",
            f"먼저 {allowed_max}개로 확장하고 검증 후 다시 제안한다",
        )
    return {}, {"spec": {"replicas": target}}, {"replicas": current}, {"replicas": target}


def _revisions(dep: client.V1Deployment) -> list[tuple[int, client.V1ReplicaSet]]:
    """Deployment의 (revision, ReplicaSet) 목록을 최신순으로 반환한다."""
    selector = ",".join(f"{k}={v}" for k, v in dep.spec.selector.match_labels.items())
    sets = _apps().list_namespaced_replica_set(
        dep.metadata.namespace, label_selector=selector, _request_timeout=_timeout()
    ).items
    out = [(int((rs.metadata.annotations or {}).get(_REVISION_ANNOTATION, 0)), rs) for rs in sets]
    return sorted([r for r in out if r[0]], key=lambda r: r[0], reverse=True)


def _container_view(containers: list[client.V1Container]) -> dict[str, Any]:
    """revision 비교용으로 컨테이너의 이미지, 환경변수, 리소스를 요약한다. 참조형 환경변수는 값을 숨긴다."""
    return {
        c.name: {
            "image": c.image,
            "env": {
                e.name: ("<valueFrom 참조>" if e.value_from is not None else (e.value or "<빈 값>"))
                for e in (c.env or [])
            },
            "limits": dict((c.resources.limits or {}) if c.resources else {}),
            "requests": dict((c.resources.requests or {}) if c.resources else {}),
        }
        for c in containers
    }


def _plan_rollback(dep: client.V1Deployment, params: dict[str, Any]) -> tuple[dict[str, Any], Any, dict[str, Any], dict[str, Any]]:
    """rollback 조치를 계획한다. 최근 N개 revision 안의 현재가 아닌 revision만 허용한다."""
    window = get_policy().rollback.max_revisions_back
    current = int(dep.metadata.annotations.get(_REVISION_ANNOTATION, 0))
    revs = _revisions(dep)
    allowed = [(n, rs) for i, (n, rs) in enumerate(revs) if i < window and n != current]
    if not allowed:
        raise PolicyViolation("되돌아갈 이전 revision이 없다")
    if params.get("to_revision") is None:
        target_rev, rs = allowed[0]
    else:
        try:
            target_rev = int(params["to_revision"])
        except (TypeError, ValueError):
            raise PolicyViolation("to_revision은 정수여야 한다") from None
        match = [(n, r) for n, r in allowed if n == target_rev]
        if not match:
            raise PolicyViolation(
                f"revision {target_rev}은 롤백 허용 범위(최근 {window}개 revision, 현재 {current} 제외)에 없다",
                f"선택 가능한 revision: {[n for n, _ in allowed]}",
            )
        rs = match[0][1]
    template = _apps().api_client.sanitize_for_serialization(rs.spec.template)
    (template.get("metadata", {}).get("labels") or {}).pop("pod-template-hash", None)
    before = {"revision": current, "containers": _container_view(dep.spec.template.spec.containers)}
    after = {"revision": target_rev, "containers": _container_view(rs.spec.template.spec.containers)}
    return {"json_patch": True}, [{"op": "replace", "path": "/spec/template", "value": template}], before, after


def _resource_cap(resource: str) -> Decimal:
    """정책의 리소스 상한을 Decimal로 반환한다."""
    pol = get_policy().limits
    return parse_quantity(pol.max_cpu if resource == "cpu" else pol.max_memory)


def _plan_set_limits(dep: client.V1Deployment, params: dict[str, Any]) -> tuple[dict[str, Any], Any, dict[str, Any], dict[str, Any]]:
    """set_limits 조치를 계획한다. 1회 최대 배수와 CPU·메모리 상한, requests ≤ limits를 검증한다."""
    pol = get_policy().limits
    containers = dep.spec.template.spec.containers
    name = params.get("container") or (containers[0].name if len(containers) == 1 else None)
    spec = next((c for c in containers if c.name == name), None)
    if spec is None:
        raise PolicyViolation(f"container를 지정해야 한다. 가능한 값: {[c.name for c in containers]}")
    cur_res = spec.resources or client.V1ResourceRequirements()
    cur = {"limits": dict(cur_res.limits or {}), "requests": dict(cur_res.requests or {})}
    new = {"limits": dict(params.get("limits") or {}), "requests": dict(params.get("requests") or {})}
    if not new["limits"] and not new["requests"]:
        raise PolicyViolation('params에 limits 또는 requests가 필요하다. 예: {"container":"server","limits":{"memory":"512Mi"}}')

    for resource, raw in new["limits"].items():
        if resource not in ("cpu", "memory"):
            raise PolicyViolation(f"지원하지 않는 리소스: {resource} (cpu, memory만 가능)")
        value = parse_quantity(raw)
        cap = _resource_cap(resource)
        before = parse_quantity(cur["limits"][resource]) if resource in cur["limits"] else None
        ceilings = [cap] + ([before * Decimal(str(pol.max_factor))] if before else [])
        allowed_max = min(ceilings)
        if value > cap:
            raise PolicyViolation(
                f"{resource} limit {raw}은 상한({_fmt(resource, cap)})을 넘는다",
                f"현재 {cur['limits'].get(resource, '미설정')}에서 1회 최대 {_fmt(resource, allowed_max)}까지 변경할 수 있다",
            )
        if before and value > before * Decimal(str(pol.max_factor)):
            raise PolicyViolation(
                f"1회 변경은 현재 limit의 최대 {pol.max_factor:g}배까지만 허용한다 ({resource}: {cur['limits'][resource]} → {raw} 요청)",
                f"먼저 {_fmt(resource, allowed_max)}까지 올리고 검증 후 다시 제안한다",
            )
    for resource in ("cpu", "memory"):
        req = new["requests"].get(resource) or cur["requests"].get(resource)
        lim = new["limits"].get(resource) or cur["limits"].get(resource)
        if req and lim and parse_quantity(req) > parse_quantity(lim):
            raise PolicyViolation(f"{resource} requests({req})가 limits({lim})보다 클 수 없다")

    resources = {k: v for k, v in new.items() if v}
    body = {"spec": {"template": {"spec": {"containers": [{"name": spec.name, "resources": resources}]}}}}
    return {}, body, {"container": spec.name, **cur}, {"container": spec.name, "limits": {**cur["limits"], **new["limits"]}, "requests": {**cur["requests"], **new["requests"]}}


_PLANNERS = {
    ActionType.RESTART: _plan_restart,
    ActionType.SCALE: _plan_scale,
    ActionType.ROLLBACK: _plan_rollback,
    ActionType.SET_LIMITS: _plan_set_limits,
}


def _result(status: str, **fields: Any) -> str:
    """결과를 JSON 문자열로 만든다."""
    return json.dumps({"status": status, **fields}, ensure_ascii=False, default=str)


@mcp.tool()
def remediate(
    action_type: ActionType,
    target: str,
    params: dict[str, Any] | None = None,
    namespace: str = "online-boutique",
    approver: str = "",
    dry_run: bool = False,
) -> str:
    """Deployment에 변경 조치 1건을 실행한다. 호출하면 승인 요청이 만들어지고, 승인자의 승인 후에만 실행된다.

    조치 유형은 4종뿐이며 대상은 `online-boutique` 네임스페이스의 Deployment로 한정된다.
    정책(replica 1~10·1회 2배, limit 1회 2배·CPU 2·메모리 4Gi, rollback 최근 5개 revision)을 어기면
    실행하지 않고 허용 범위 안의 대안을 반환한다. dry_run=true이면 변경 없이 정책 검증과
    변경 전·후 값만 반환한다 (승인 요청을 만들기 전 점검용).

    action_type별 params:
    - restart: 없음. 파드를 순차 교체한다.
    - scale: {"replicas": 정수}. 예: {"replicas": 2}
    - rollback: {"to_revision": 정수}. 생략하면 직전 revision. 예: {"to_revision": 1}
    - set_limits: {"container": "server", "limits": {"memory": "512Mi", "cpu": "500m"}, "requests": {...}}. limits와 requests 중 바꿀 것만 준다.

    Args:
        action_type: restart | scale | rollback | set_limits
        target: 대상 Deployment 이름. 예: "recommendationservice"
        params: 조치 파라미터 (위 설명 참고)
        namespace: 네임스페이스. online-boutique만 허용
        approver: 호출하는 쪽이 채우지 않는다. 승인 절차에서 시스템이 승인자의 Slack User ID로 채우며, 정책의 승인자 목록에 없으면 서버가 거부한다
        dry_run: true이면 실행하지 않고 검증만 한다

    Examples:
        remediate(action_type="scale", target="frontend", params={"replicas": 2}, dry_run=True)
        remediate(action_type="set_limits", target="recommendationservice", params={"container": "server", "limits": {"memory": "512Mi"}})
        remediate(action_type="rollback", target="checkoutservice", params={"to_revision": 1})
    """
    pol = get_policy()
    params = params or {}
    base = {"action": action_type.value, "target": target, "namespace": namespace, "dry_run": dry_run}
    if namespace != pol.namespace:
        return _result("rejected", **base, message=f"허용 네임스페이스({pol.namespace}) 밖은 변경할 수 없다", alternative="수동 조치 가이드를 안내한다 (RB-10)")
    if action_type.value not in pol.actions:
        return _result("rejected", **base, message=f"허용되지 않는 조치 유형: {action_type.value}")
    if not dry_run and approver not in pol.approval.user_ids:
        return _result("rejected", **base, message="승인자 정보가 없거나 등록된 승인자가 아니다 (R1)")

    try:
        dep = _apps().read_namespaced_deployment(target, namespace, _request_timeout=_timeout())
        _check_cooldown(dep)
        extra, body, before, after = _PLANNERS[action_type](dep, params)
        if dry_run:
            return _result("ok", **base, before=before, after=after, message="정책 검증을 통과했다. 실행하지 않았다")

        audit = json.dumps({"action": action_type.value, "approver": approver, "at": datetime.now(timezone.utc).isoformat()})
        if extra.get("json_patch"):
            body = body + [{"op": "add", "path": "/metadata/annotations/" + _AUDIT_ANNOTATION.replace("/", "~1"), "value": audit}]
        else:
            body["metadata"] = {"annotations": {_AUDIT_ANNOTATION: audit}}
        _apps().patch_namespaced_deployment(target, namespace, body, _request_timeout=_timeout())
        return _result("ok", **base, before=before, after=after, message="변경을 적용했다. 롤아웃은 비동기로 진행되므로 검증이 필요하다")
    except PolicyViolation as e:
        return _result("rejected", **base, message=str(e), alternative=e.alternative)
    except ApiException as e:
        if e.status == 404:
            return _result("error", **base, message=f"Deployment {target}을 찾을 수 없다")
        return _result("error", **base, message=f"Kubernetes API 오류: HTTP {e.status} {e.reason}")
    except Exception as e:  # 타임아웃, 연결 실패 등
        return _result("error", **base, message=f"실행 실패: {type(e).__name__}: {e}")


if __name__ == "__main__":
    mcp.run(transport="stdio")
