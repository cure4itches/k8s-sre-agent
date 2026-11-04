# RB-08 · 리소스 limit 변경 표준 절차

- 문서 ID: RB-08
- 대상 서비스: online-boutique 전체 Deployment
- 관련 조치 유형: limit 변경
- 승인 필요: 예 (`sre-approvers`)
- 최종 수정: 2026-09-10

## 언제 쓰는가

- 메모리 limit 부족으로 OOMKilled가 반복될 때 (RB-01)
- CPU limit이 낮아 스로틀링으로 지연이 생기고, replica 확장만으로 해결되지 않을 때 (RB-04)

## 정책 제한

- 허용 네임스페이스: `online-boutique`
- 1회 변경 폭: 현재 limit의 **최대 2배**
- 상한: CPU **2 core**, 메모리 **4Gi**
- 하향 조정도 같은 절차와 승인을 거친다.
- 범위를 넘는 변경이 필요하면 2배까지만 먼저 적용하고 검증 후 다시 제안하거나, RB-10에 따라 담당자에게 확인한다.

## 사전 확인

1. 현재 requests와 limits를 확인한다. `kubectl get deploy <name> -n online-boutique -o jsonpath='{.spec.template.spec.containers[*].resources}'`
2. 최근 30분 사용량의 최댓값을 확인한다 (메모리: `container_memory_working_set_bytes`, CPU: `container_cpu_usage_seconds_total`).
3. 노드에 여유 자원이 있는지 확인한다. requests를 크게 올리면 새 파드가 `Pending`에 머물 수 있다.

## 값 정하기

- 메모리 limit: 관측된 최대 사용량의 1.3배와 현재 limit의 1.5배 중 작은 값을 시작값으로 한다.
- CPU limit: 관측된 최대 사용량의 1.5배를 시작값으로 한다.
- requests는 limit의 50% 이상으로 맞춰 스케줄링과 실제 사용량의 차이를 줄인다.
- 값은 메모리 `Mi`/`Gi`, CPU `m`(millicore) 단위로 적는다.

## 승인 요청에 포함할 내용

- 대상 Deployment와 컨테이너 이름
- 변경 전·후 requests/limits 값
- 근거: 최대 사용량과 limit 비교, OOMKilled 또는 스로틀링 기록
- 예상 영향: Deployment 템플릿이 바뀌어 파드가 순차 교체된다. 교체 중 일시적으로 처리 용량이 줄어든다.
- 되돌리는 방법: `kubectl rollout undo deployment/<name> -n online-boutique`

## 실행

- `kubectl set resources deployment/<name> -n online-boutique -c <container> --limits=cpu=<값>,memory=<값> --requests=cpu=<값>,memory=<값>`
- `kubectl rollout status deployment/<name> -n online-boutique --timeout=5m`

## 검증

- 새 파드가 모두 `Ready`이고 `Pending` 파드가 없는지 확인한다.
- 변경된 값이 실제 파드 스펙에 반영되었는지 확인한다.
- 5분간 OOMKilled 재발 여부(메모리) 또는 스로틀링 비율 감소(CPU)를 확인한다.
