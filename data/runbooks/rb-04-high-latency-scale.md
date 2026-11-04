# RB-04 · 트래픽 급증으로 인한 응답 지연

- 문서 ID: RB-04
- 대상 서비스: frontend, productcatalogservice, currencyservice, recommendationservice 등 요청량에 비례해 부하가 늘어나는 서비스
- 관련 조치 유형: scale
- 승인 필요: 예 (`sre-approvers`)
- 최종 수정: 2026-09-15

## 증상

- 페이지 응답이 느려지고, 일부 요청이 타임아웃된다.
- 파드 CPU 사용량이 CPU limit에 붙어 있고 CPU 스로틀링이 증가한다.
- 오류보다 지연이 먼저 나타나며, 지연이 심해지면 상위 서비스에서 타임아웃으로 인한 5xx가 함께 늘어난다.

## 탐지 신호

- CPU 사용률 (limit 대비)
  - `sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="online-boutique", container="server"}[5m])) / sum by (pod) (kube_pod_container_resource_limits{namespace="online-boutique", resource="cpu"})`
  - 0.8 이상이 5분 넘게 지속되면 용량 부족을 의심한다.
- CPU 스로틀링 비율
  - `rate(container_cpu_cfs_throttled_periods_total{namespace="online-boutique"}[5m]) / rate(container_cpu_cfs_periods_total{namespace="online-boutique"}[5m])`
  - 0.25 이상이면 스로틀링이 지연의 원인일 가능성이 높다.
- frontend 응답 시간
  - frontend 로그의 `http.resp.took_ms` 값이 평소보다 크게 늘어난다.
- 요청량 증가
  - loadgenerator 또는 유입 트래픽 증가 시점과 지연 증가 시점이 일치하는지 본다.

## 진단 절차

1. 지연이 시작된 시각과 서비스를 특정한다. frontend 지연이 먼저인지, 하위 서비스 지연이 전파된 것인지 구분한다 (RB-06 의존 관계 참고).
2. 해당 서비스의 CPU 사용률과 스로틀링을 확인한다.
3. 파드 상태가 정상(`Running`, 재시작 없음)인지 확인한다. 재시작이 있다면 RB-01 또는 RB-02를 먼저 본다.
4. 최근 배포가 없는지 확인한다. 배포 직후 지연이라면 코드 성능 저하일 수 있으므로 RB-07 롤백을 검토한다.
5. 현재 replica 수를 확인한다. `kubectl get deploy <name> -n online-boutique`

## 조치

- **1순위: replica 확장 (scale)**
  - 목표 replica = 현재 replica × (현재 CPU 사용률 / 0.6), 소수점 올림
  - 정책 제한: replica는 1~10, 1회 변경 시 현재 값의 최대 2배까지만 허용한다.
  - 계산 결과가 2배를 넘으면 2배로 먼저 확장한 뒤 검증 결과를 보고 다시 제안한다.
  - 10을 넘는 확장이 필요하면 정책 범위 밖이므로 RB-10에 따라 담당자에게 확인한다.
- CPU limit이 지나치게 낮게 잡혀 있고 replica 확장 효과가 작다면 CPU limit 상향(RB-08)을 함께 검토한다.
- 트래픽이 줄어든 뒤에는 원래 replica로 축소하는 조치를 별도로 제안한다.

## 검증

- 확장 후 60초가 지나면 새 파드가 모두 `Ready`인지 확인한다.
- 이후 5분 동안 파드당 CPU 사용률이 0.7 미만으로 내려가는지 확인한다.
- frontend 응답 시간(`http.resp.took_ms`)이 평소 수준으로 돌아오는지 확인한다.

## 관련 문서

- RB-06 5xx 에러율 증가와 의존 서비스 추적
- RB-08 리소스 limit 변경 표준 절차
- PM-2026-003 프로모션 트래픽 급증 장애 회고
