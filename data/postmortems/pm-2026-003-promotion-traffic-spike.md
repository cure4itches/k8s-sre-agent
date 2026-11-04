# PM-2026-003 · 프로모션 트래픽 급증으로 frontend 응답 지연

- 문서 ID: PM-2026-003
- 발생일: 2026-09-02
- 심각도: SEV-3
- 영향 서비스: frontend, productcatalogservice (사용자 영향: 전체 페이지 응답 속도)
- 관련 Runbook: RB-04, RB-06
- 작성: SRE 팀 on-call 담당자

## 요약

할인 프로모션 시작과 함께 유입 트래픽이 평소의 약 3배로 늘어 frontend와 productcatalogservice의 CPU가 포화되었고 페이지 응답이 크게 느려졌다. 두 서비스의 replica를 단계적으로 확장해 복구했다.

## 영향

- 약 35분 동안 페이지 응답 시간이 평소의 4~6배로 늘었다.
- 일부 요청은 타임아웃으로 실패했다. 데이터 손실은 없었다.

## 타임라인

- 19:00 프로모션 시작, 트래픽 급증
- 19:04 frontend 응답 시간 증가 알람 (`http.resp.took_ms` 평소 대비 4배)
- 19:08 on-call 담당자 확인. frontend 파드 CPU 사용률이 limit 대비 0.95, 스로틀링 비율 0.4 확인
- 19:12 productcatalogservice도 CPU 포화 상태임을 확인. frontend 요청 대부분이 상품 조회를 호출하므로 함께 확장하기로 결정
- 19:14 frontend replica 1 → 2, productcatalogservice replica 1 → 2 확장 (승인 후 실행)
- 19:20 CPU 사용률 0.75로 감소했으나 응답 시간은 여전히 평소의 2배
- 19:22 frontend replica 2 → 4, productcatalogservice replica 2 → 3 추가 확장 (승인 후 실행)
- 19:30 CPU 사용률 0.55, 응답 시간 평소 수준으로 회복
- 19:35 5분간 안정 유지 확인, 복구 선언
- 21:30 프로모션 트래픽 감소 후 replica 원복 (승인 후 실행)

## 근본 원인

- 프로모션 일정이 운영 팀에 사전 공유되지 않아 용량을 미리 늘리지 못했다.
- 트래픽에 따라 자동으로 확장하는 설정(HPA)이 없었다.

## 대응에서 잘된 점

- CPU 사용률과 스로틀링 지표로 용량 부족을 빠르게 판단했다.
- 1회 확장 폭을 2배 이내로 제한하고 검증 후 추가 확장해 과도한 확장을 피했다.

## 아쉬운 점

- 첫 확장에서 하위 서비스(productcatalogservice) 포화 여부를 늦게 확인했다.
- 트래픽 감소 후 replica 원복을 잊을 뻔했다.

## 재발 방지 조치

- 마케팅 프로모션 일정을 운영 팀 캘린더에 공유하는 절차 마련
- frontend와 productcatalogservice에 HPA 도입 검토
- RB-04에 하위 서비스 포화 여부를 함께 확인하는 단계와 원복 조치 안내 추가
