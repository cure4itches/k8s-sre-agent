# RB-01 · 컨테이너 OOMKilled 반복

- 문서 ID: RB-01
- 대상 서비스: online-boutique 전체 (특히 recommendationservice, cartservice, adservice)
- 관련 조치 유형: limit 변경 (보조: restart)
- 승인 필요: 예 (`sre-approvers`)
- 최종 수정: 2026-09-12

## 증상

- 파드가 반복적으로 재시작되고 `RESTARTS` 값이 계속 증가한다.
- `kubectl describe pod`의 `Last State`에 `Terminated`, `Reason: OOMKilled`, `Exit Code: 137`이 표시된다.
- 재시작 직전 메모리 사용량이 메모리 limit 근처까지 올라간다.
- 사용자 영향: 해당 서비스를 호출하는 화면에서 간헐적 오류 또는 지연이 발생한다. recommendationservice의 경우 상품 상세 페이지의 추천 영역이 비거나 frontend가 5xx를 반환할 수 있다.

## 탐지 신호

- 재시작 횟수 증가
  - `increase(kube_pod_container_status_restarts_total{namespace="online-boutique"}[15m]) > 2`
- 마지막 종료 사유가 OOMKilled
  - `kube_pod_container_status_last_terminated_reason{namespace="online-boutique", reason="OOMKilled"} == 1`
- 메모리 사용률 (limit 대비)
  - `max by (pod) (container_memory_working_set_bytes{namespace="online-boutique", container="server"}) / max by (pod) (kube_pod_container_resource_limits{namespace="online-boutique", resource="memory"})`
  - 0.9 이상이 지속되면 OOM 위험이 높다.

## 진단 절차

1. 재시작이 발생한 파드와 컨테이너를 확인한다. `kubectl get pods -n online-boutique`
2. 종료 사유를 확인한다. `kubectl describe pod <pod> -n online-boutique`에서 `Last State`의 `Reason`이 `OOMKilled`인지 본다.
3. 현재 메모리 requests/limits를 확인한다. `kubectl get deploy <name> -n online-boutique -o jsonpath='{.spec.template.spec.containers[0].resources}'`
4. 최근 30분 메모리 사용량 추이를 limit과 비교한다. 사용량이 limit에 닿은 직후 재시작되었다면 limit 부족이 원인일 가능성이 높다.
5. 최근 배포 이력을 확인한다. 배포 직후부터 메모리가 늘었다면 코드 변경(메모리 누수, 캐시 증가)일 수 있으며 이 경우 RB-07 롤백 절차를 우선 검토한다.
6. 노드 메모리 압박(`MemoryPressure`)으로 인한 축출(Evicted)과 구분한다. 축출은 `Reason: Evicted`로 표시되며 이 문서의 대상이 아니다.

## 조치

- **1순위: 메모리 limit 상향 (limit 변경)**
  - RB-08 리소스 limit 변경 표준 절차를 따른다.
  - 1회 변경 폭은 현재 limit의 최대 2배, 상한은 메모리 4Gi이다.
  - 권장 시작값: 관측된 최대 사용량의 1.3배 또는 현재 limit의 1.5배 중 작은 값
  - requests도 함께 조정해 limit과의 차이가 너무 크지 않게 한다 (requests는 limit의 50% 이상 권장).
- **보조: 재시작 (restart)**
  - 메모리 누수가 의심되고 limit 상향 전 임시 완화가 필요할 때만 사용한다. 근본 해결이 아니므로 반드시 원인 조치를 함께 제안한다.
- 배포 직후 발생한 경우: limit 상향보다 롤백(RB-07)을 먼저 검토한다.

## 검증

- 조치 후 60초가 지나면 새 파드가 `Running`이고 `READY`가 정상인지 확인한다.
- 이후 5분 동안 재시작 횟수가 증가하지 않아야 한다.
- 메모리 사용률(limit 대비)이 0.8 미만으로 유지되는지 확인한다.
- 5분 안에 다시 OOMKilled가 발생하면 "실패"로 판정하고 limit 재조정 또는 롤백을 새 조치안으로 제안한다.

## 롤백

- limit 변경은 Deployment 템플릿을 바꾸므로 새 revision이 생긴다. 문제가 생기면 `kubectl rollout undo deployment/<name> -n online-boutique`로 직전 revision으로 되돌린다.

## 관련 문서

- RB-07 롤백 표준 절차
- RB-08 리소스 limit 변경 표준 절차
- PM-2026-001 recommendationservice OOMKilled 장애 회고
