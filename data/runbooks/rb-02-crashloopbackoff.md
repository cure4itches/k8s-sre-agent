# RB-02 · CrashLoopBackOff (설정 오류로 인한 기동 실패)

- 문서 ID: RB-02
- 대상 서비스: online-boutique 전체 (특히 checkoutservice, paymentservice, frontend)
- 관련 조치 유형: rollback
- 승인 필요: 예 (`sre-approvers`)
- 최종 수정: 2026-09-12

## 증상

- 파드 상태가 `CrashLoopBackOff`이며, 컨테이너가 기동 직후 종료되기를 반복한다.
- 재시작 간격이 점점 길어진다 (지수 백오프, 최대 5분).
- 사용자 영향: 해당 서비스에 의존하는 기능이 실패한다. checkoutservice가 실패하면 주문(Place Order) 단계에서 frontend가 5xx를 반환한다.

## 탐지 신호

- `kube_pod_container_status_waiting_reason{namespace="online-boutique", reason="CrashLoopBackOff"} == 1`
- `increase(kube_pod_container_status_restarts_total{namespace="online-boutique"}[15m]) > 3`
- 종료 코드가 0 또는 1이며 `Reason`이 `Error` 또는 `Completed` (OOMKilled가 아님)

## 진단 절차

1. 이전 컨테이너의 로그를 확인한다. `kubectl logs <pod> -n online-boutique --previous`
2. 로그에서 다음과 같은 기동 실패 메시지를 찾는다.
   - 필수 환경변수 누락: `environment variable "PAYMENT_SERVICE_ADDR" not set`
   - 잘못된 주소·포트: `failed to connect`, `connection refused`, `no such host`
   - 설정 값 형식 오류: `invalid value`, `parse error`
3. 현재 Deployment의 환경변수와 직전 revision을 비교한다.
   - `kubectl rollout history deployment/<name> -n online-boutique`
   - `kubectl rollout history deployment/<name> -n online-boutique --revision=<N>`
4. 최근 배포 직후부터 발생했다면 배포된 설정 변경이 원인일 가능성이 높다.
5. OOMKilled(`Exit Code: 137`)라면 RB-01을, 이미지 문제(`ImagePullBackOff`)라면 RB-03을 따른다.

### checkoutservice 필수 환경변수

checkoutservice는 아래 환경변수가 모두 올바른 서비스 주소를 가리켜야 기동된다. 하나라도 비어 있거나 잘못되면 기동에 실패하거나 주문 처리 중 오류가 난다.

- `PRODUCT_CATALOG_SERVICE_ADDR`, `SHIPPING_SERVICE_ADDR`, `PAYMENT_SERVICE_ADDR`, `EMAIL_SERVICE_ADDR`, `CURRENCY_SERVICE_ADDR`, `CART_SERVICE_ADDR`
- 각 값의 형식은 `<서비스명>:<포트>`이다. 서비스별 포트는 RB-09를 참고한다.

## 조치

- **1순위: 직전 정상 revision으로 롤백 (rollback)**
  - RB-07 롤백 표준 절차를 따른다.
  - 롤백 대상 revision 번호와 그 revision의 설정이 정상이었다는 근거(배포 시각, 장애 시작 시각)를 승인 요청에 포함한다.
- 설정 값을 직접 수정하는 조치는 지원하는 4종에 포함되지 않는다. 설정 수정이 필요하면 롤백으로 서비스를 먼저 복구한 뒤, 배포 파이프라인에서 올바른 설정으로 재배포하도록 안내한다.
- 재시작(restart)은 설정 오류에는 효과가 없으므로 제안하지 않는다.

## 검증

- 롤백 후 60초가 지나면 파드가 `Running`이고 재시작 횟수가 더 늘지 않는지 확인한다.
- 이전 로그에 있던 오류 메시지가 새 파드 로그에 나타나지 않아야 한다.
- 의존 서비스(예: frontend)의 오류 로그가 줄어드는지 확인한다.

## 롤백

- 롤백 자체가 문제를 일으키면 다시 `kubectl rollout undo`로 원래 revision으로 돌아갈 수 있다. 단, 원래 revision은 장애 상태였으므로 즉시 수동 대응으로 전환한다.

## 관련 문서

- RB-07 롤백 표준 절차
- RB-09 Online Boutique 서비스 구성과 의존 관계
- PM-2026-002 checkoutservice 환경변수 오류 배포 장애 회고
