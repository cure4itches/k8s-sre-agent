# RB-09 · Online Boutique 서비스 구성과 의존 관계

- 문서 ID: RB-09
- 대상 서비스: online-boutique 전체
- 문서 유형: 서비스 아키텍처 참고 문서
- 최종 수정: 2026-09-05

## 개요

Online Boutique는 `online-boutique` 네임스페이스에 배포된 마이크로서비스 기반 쇼핑몰이다. 서비스 간 통신은 gRPC를 사용하며, 사용자는 frontend(HTTP)로만 접근한다. 아래 목록에 없는 서비스는 이 시스템에 존재하지 않는다.

## 서비스 목록

| 서비스 | 언어 | 서비스 포트 | 역할 |
|--------|------|:-----------:|------|
| frontend | Go | 80 | 웹 UI 제공, 모든 사용자 요청의 진입점 |
| cartservice | C# | 7070 | 장바구니 저장·조회 (redis-cart 사용) |
| productcatalogservice | Go | 3550 | 상품 목록·검색·상세 정보 제공 |
| currencyservice | Node.js | 7000 | 환율 변환 |
| paymentservice | Node.js | 50051 | 결제(카드 승인) 처리. 실제 결제는 하지 않는 모의 서비스 |
| shippingservice | Go | 50051 | 배송비 견적과 배송 처리 (모의) |
| emailservice | Python | 5000 | 주문 확인 메일 발송 (모의) |
| checkoutservice | Go | 5050 | 주문 처리 오케스트레이션 |
| recommendationservice | Python | 8080 | 장바구니 기반 상품 추천 |
| adservice | Java | 9555 | 문맥 기반 광고 제공 |
| loadgenerator | Python (Locust) | - | 사용자 트래픽을 흉내 내는 부하 생성기 |
| redis-cart | Redis | 6379 | 장바구니 데이터 저장소 |

## 의존 관계

- frontend → productcatalogservice, currencyservice, cartservice, recommendationservice, shippingservice, checkoutservice, adservice
- checkoutservice → productcatalogservice, cartservice, currencyservice, shippingservice, paymentservice, emailservice
- recommendationservice → productcatalogservice
- cartservice → redis-cart
- loadgenerator → frontend

## 장애 영향 범위

| 장애 서비스 | 사용자 영향 |
|-------------|-------------|
| frontend | 전체 서비스 접속 불가 |
| productcatalogservice | 상품 목록·상세·추천·주문 모두 실패 (영향 범위가 가장 넓은 하위 서비스) |
| cartservice / redis-cart | 장바구니와 주문 실패 |
| checkoutservice | 주문만 실패, 조회 기능은 정상 |
| paymentservice | 주문 시 결제 단계 실패, checkoutservice 로그에 결제 오류 기록 |
| currencyservice | 가격 표시와 주문 실패 |
| recommendationservice | 추천 영역만 비거나 상세 페이지 일부 오류 |
| adservice | 광고 영역만 비어 있음, 핵심 기능 영향 작음 |
| shippingservice | 배송비 표시와 주문 실패 |
| emailservice | 주문은 완료되나 확인 메일 미발송 |

## 공통 정보

- 각 서비스는 같은 이름의 Deployment와 Service로 배포된다 (예: `deployment/cartservice`).
- 애플리케이션 컨테이너 이름은 `server`다 (redis-cart는 `redis`, loadgenerator는 `main`).
- 레이블 셀렉터는 `app=<서비스명>`이다.
