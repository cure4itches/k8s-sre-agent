# RB-05 · 파드 응답 불능 (Running이지만 요청을 처리하지 못함)

- 문서 ID: RB-05
- 대상 서비스: online-boutique 전체 (특히 productcatalogservice, currencyservice)
- 관련 조치 유형: restart
- 승인 필요: 예 (`sre-approvers`)
- 최종 수정: 2026-09-15

## 증상

- 파드 상태는 `Running`인데 특정 파드로 가는 요청만 타임아웃되거나 실패한다.
- 같은 Deployment의 다른 파드는 정상이므로 오류가 간헐적으로 나타난다.
- 해당 파드의 로그가 일정 시점 이후 더 이상 남지 않거나, CPU 사용량이 0 또는 100%에 고정되어 있다.

## 탐지 신호

- 특정 파드의 readiness 실패
  - `kube_pod_status_ready{namespace="online-boutique", condition="false"} == 1`
- 특정 파드만 CPU 사용량이 다른 파드와 크게 다름 (0 근처 고정 또는 limit에 고정)
- 이벤트에 `Readiness probe failed` 또는 `Liveness probe failed`가 반복된다.
- 호출하는 쪽 로그에 해당 서비스로의 `context deadline exceeded`가 늘어난다.

## 진단 절차

1. Deployment의 파드 목록과 상태를 확인한다. `kubectl get pods -n online-boutique -l app=<name> -o wide`
2. 이상 파드를 특정한다. readiness 상태, CPU 사용량, 마지막 로그 시각을 다른 파드와 비교한다.
3. 이상 파드의 로그를 확인한다. `kubectl logs <pod> -n online-boutique --tail=200`
   - 마지막 로그 이후 아무 출력이 없다면 프로세스 행(hang)을 의심한다.
4. 파드 이벤트에서 probe 실패 기록을 확인한다.
5. 모든 파드가 같은 증상이라면 이 문서가 아니라 용량(RB-04) 또는 의존 서비스(RB-06) 문제일 가능성이 높다.

## 조치

- **1순위: 재시작 (restart)**
  - `kubectl rollout restart deployment/<name> -n online-boutique`로 파드를 순차 교체한다.
  - rollout restart는 Deployment 단위로 동작하므로 정상 파드도 함께 교체된다는 점을 승인 요청의 예상 영향에 적는다.
  - replica가 1이면 교체 중 짧은 중단이 생길 수 있음을 함께 적는다.
- 재시작 후 같은 증상이 반복되면 코드 결함일 가능성이 높으므로 최근 배포 여부를 확인하고 RB-07 롤백을 검토한다.

## 검증

- 재시작 후 60초가 지나면 모든 파드가 `Ready`인지 확인한다.
- 이후 5분 동안 probe 실패 이벤트가 발생하지 않아야 한다.
- 호출하는 쪽 로그의 타임아웃 오류가 사라지는지 확인한다.

## 관련 문서

- RB-04 트래픽 급증으로 인한 응답 지연
- RB-06 5xx 에러율 증가와 의존 서비스 추적
