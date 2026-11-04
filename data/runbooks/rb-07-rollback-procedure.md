# RB-07 · 롤백 표준 절차

- 문서 ID: RB-07
- 대상 서비스: online-boutique 전체 Deployment
- 관련 조치 유형: rollback
- 승인 필요: 예 (`sre-approvers`)
- 최종 수정: 2026-09-10

## 언제 쓰는가

- 배포 직후 장애가 시작되었고, 직전 revision에서는 정상이었다는 근거가 있을 때
- 대표 사례: 잘못된 설정 배포(RB-02), 존재하지 않는 이미지 태그(RB-03), 배포 후 성능 저하나 메모리 증가

## 사전 확인

1. 배포 이력을 확인한다. `kubectl rollout history deployment/<name> -n online-boutique`
2. 장애 시작 시각과 마지막 배포 시각을 비교한다. 배포 이후에 장애가 시작되었는지가 롤백의 핵심 근거다.
3. 롤백 대상 revision의 내용(이미지 태그, 환경변수, 리소스)을 확인한다. `kubectl rollout history deployment/<name> -n online-boutique --revision=<N>`
4. 롤백 대상은 **최근 5개 revision 이내**로 제한한다. 그보다 오래된 revision으로 돌아가야 한다면 정책 범위 밖이므로 담당자에게 확인한다.

## 승인 요청에 포함할 내용

- 대상 Deployment와 네임스페이스
- 현재 revision과 롤백 대상 revision 번호
- 두 revision의 차이 (이미지 태그, 설정, 리소스)
- 롤백 근거 (장애 시작 시각과 배포 시각, 오류 로그 또는 이벤트)
- 예상 영향: 롤링 업데이트로 파드가 순차 교체되며, 롤백 대상 revision 이후의 기능 변경이 함께 되돌려진다.
- 되돌리는 방법: 다시 `kubectl rollout undo`로 현재 revision으로 복귀할 수 있다.

## 실행

- 직전 revision으로: `kubectl rollout undo deployment/<name> -n online-boutique`
- 특정 revision으로: `kubectl rollout undo deployment/<name> -n online-boutique --to-revision=<N>`
- 롤아웃 완료 대기: `kubectl rollout status deployment/<name> -n online-boutique --timeout=5m`

## 검증

- 롤아웃이 완료되고 모든 파드가 `Ready`인지 확인한다.
- 실행 중인 이미지 태그와 설정이 롤백 대상 revision과 같은지 확인한다.
- 장애 증상(재시작, 오류 로그, 5xx)이 사라지는지 5분간 확인한다.

## 주의

- 롤백은 장애를 멈추는 조치이지 원인 해결이 아니다. 롤백 후에는 배포 담당자가 원인을 고쳐 재배포하도록 안내한다.
- 같은 Deployment에 2분 안에 연속으로 조치하지 않는다.
