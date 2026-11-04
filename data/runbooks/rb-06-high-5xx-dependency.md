# RB-06 · 5xx 에러율 증가와 의존 서비스 추적

- 문서 ID: RB-06
- 대상 서비스: frontend, checkoutservice 및 그 하위 서비스
- 관련 조치 유형: 원인 서비스에 따라 restart / scale / rollback / limit 변경
- 승인 필요: 예 (`sre-approvers`)
- 최종 수정: 2026-09-18

## 증상

- 사용자 화면에서 오류 페이지가 늘어나고, frontend 로그의 `http.resp.status`에 500대 응답이 증가한다.
- 오류가 특정 기능(예: 주문, 장바구니, 환율 표시)에 몰려 있다.
- 5xx를 반환하는 서비스가 원인 서비스가 아닐 수 있다. Online Boutique는 gRPC 호출이 연쇄되므로 하위 서비스 장애가 상위 서비스의 5xx로 나타난다.

## 탐지 신호

- frontend 로그에서 5xx 비율이 평소보다 증가한다.
- 오류 로그에 특정 하위 서비스 이름과 함께 `rpc error`, `Unavailable`, `DeadlineExceeded`가 나타난다.
- 하위 서비스 중 하나가 재시작 증가, CrashLoopBackOff, CPU 포화 등 이상 신호를 보인다.

## 기능별 호출 경로

| 사용자 기능 | 호출 경로 |
|-------------|-----------|
| 상품 목록·상세 | frontend → productcatalogservice, currencyservice, recommendationservice, adservice |
| 장바구니 | frontend → cartservice → redis-cart |
| 주문 (Place Order) | frontend → checkoutservice → cartservice, productcatalogservice, currencyservice, shippingservice, paymentservice, emailservice |
| 배송비 표시 | frontend → shippingservice |

## 진단 절차

1. 오류가 몰린 기능을 특정한다. frontend 로그의 `http.req.path`별로 5xx를 집계한다.
   - `/cart/checkout` 경로에 몰리면 주문 경로(checkoutservice 이하)를 의심한다.
2. 위 호출 경로를 따라 하위 서비스로 내려가며 상태를 확인한다.
   - 각 서비스의 파드 상태, 재시작 횟수, CPU·메모리 사용률을 본다.
   - 상위 서비스 로그의 오류 메시지에 나오는 하위 서비스 이름을 따라간다.
3. 가장 아래에서 이상 신호를 보이는 서비스를 원인 서비스로 판단한다.
   - 예: checkoutservice 로그에 `failed to charge card: rpc error: code = Unavailable`이 있고 paymentservice가 CrashLoopBackOff라면 원인 서비스는 paymentservice다.
4. 원인 서비스의 증상에 맞는 Runbook으로 이동한다.
   - 재시작 반복·OOMKilled → RB-01
   - 기동 실패·CrashLoopBackOff → RB-02
   - 이미지 풀 실패 → RB-03
   - CPU 포화·지연 → RB-04
   - 특정 파드만 응답 불능 → RB-05

## 조치

- 5xx를 반환하는 상위 서비스가 아니라 **원인 서비스**를 대상으로 조치안을 만든다.
- 상위 서비스(예: checkoutservice)를 재시작해도 하위 서비스 장애는 해결되지 않으므로 제안하지 않는다.
- 원인 서비스를 특정할 근거가 부족하면 단정하지 말고 "확신 불가"로 보고하고, 추가로 확인할 서비스와 지표를 제시한다.

## 검증

- 원인 서비스 조치 후 frontend 로그의 5xx 비율이 평소 수준으로 돌아오는지 확인한다.
- 상위 서비스 로그의 하위 서비스 호출 오류가 사라지는지 확인한다.

## 관련 문서

- RB-09 Online Boutique 서비스 구성과 의존 관계
- PM-2026-002 checkoutservice 환경변수 오류 배포 장애 회고
