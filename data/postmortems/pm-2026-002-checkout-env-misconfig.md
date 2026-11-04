# PM-2026-002 · checkoutservice 환경변수 오류 배포로 주문 실패

- 문서 ID: PM-2026-002
- 발생일: 2026-08-19
- 심각도: SEV-2
- 영향 서비스: checkoutservice (사용자 영향: 주문 전체)
- 관련 Runbook: RB-02, RB-06, RB-07
- 작성: SRE 팀 on-call 담당자

## 요약

checkoutservice 배포 시 `PAYMENT_SERVICE_ADDR` 환경변수 값이 잘못 들어가 결제 서비스 호출이 실패했고, 이후 설정 정리 과정에서 같은 환경변수가 빠진 버전이 배포되어 CrashLoopBackOff로 이어졌다. 직전 정상 revision으로 롤백해 복구했다.

## 영향

- 약 27분 동안 주문(Place Order)이 모두 실패했다.
- 상품 조회와 장바구니 기능은 정상이었다.

## 타임라인

- 10:02 checkoutservice 신규 버전 배포. `PAYMENT_SERVICE_ADDR`에 잘못된 포트가 들어감
- 10:06 frontend 5xx 증가 알람. 오류가 `/cart/checkout` 경로에 집중
- 10:09 on-call 담당자가 처음에는 frontend 문제로 보고 frontend 로그를 확인
- 10:14 checkoutservice 로그에서 `failed to charge card: rpc error: code = Unavailable` 확인, 결제 경로 문제로 범위를 좁힘
- 10:16 담당 개발자가 환경변수를 고친다며 설정을 정리한 버전을 재배포. 이 과정에서 `PAYMENT_SERVICE_ADDR`가 누락됨
- 10:18 checkoutservice가 CrashLoopBackOff 상태로 전환. 이전 컨테이너 로그에 `environment variable "PAYMENT_SERVICE_ADDR" not set` 확인
- 10:22 배포 이력을 비교해 두 단계 전 revision이 정상이었음을 확인
- 10:25 해당 revision으로 롤백 (승인 후 실행)
- 10:29 checkoutservice 정상 기동, 주문 성공 확인
- 10:33 5분간 5xx 재발 없음 확인, 복구 선언

## 근본 원인

- 환경변수 값 검증 없이 배포가 진행되었다.
- 장애 중 원인 수정을 위한 재배포가 검토 없이 이루어져 장애가 더 커졌다.

## 대응에서 잘된 점

- 상위 서비스(frontend)가 아니라 오류 로그를 따라 하위 서비스를 추적해 원인을 찾았다.
- 배포 이력 비교로 정상 revision을 빠르게 특정했다.

## 아쉬운 점

- 초기에 5xx를 반환하는 frontend를 원인으로 오인해 5분을 소비했다.
- 장애 중 검토 없는 재배포가 두 번째 장애를 만들었다. 장애 중에는 수정 배포보다 롤백을 우선했어야 했다.

## 재발 방지 조치

- checkoutservice 기동 시 필수 환경변수를 검증하고, 누락 시 명확한 오류를 남기도록 개선
- 배포 파이프라인에 서비스 주소 형식(`<서비스명>:<포트>`) 검증 추가
- 장애 대응 중에는 수정 배포 대신 롤백을 우선한다는 원칙을 RB-02와 RB-07에 반영
- 5xx 장애 시 호출 경로를 따라 원인 서비스를 찾는 절차를 RB-06으로 문서화
