# RB-03 · ImagePullBackOff (이미지 풀 실패)

- 문서 ID: RB-03
- 대상 서비스: online-boutique 전체
- 관련 조치 유형: rollback
- 승인 필요: 예 (`sre-approvers`)
- 최종 수정: 2026-09-12

## 증상

- 새 버전 배포 후 파드가 `ImagePullBackOff` 또는 `ErrImagePull` 상태에 머문다.
- 새 ReplicaSet의 파드는 뜨지 않고, 롤링 업데이트가 진행되지 않는다.
- 사용자 영향: 롤링 업데이트 전략상 기존 파드가 남아 있으면 영향이 작다. 기존 파드가 모두 교체된 상태거나 replica가 1이면 서비스 중단이 발생한다. cartservice가 중단되면 장바구니 조회·추가가 실패한다.

## 탐지 신호

- `kube_pod_container_status_waiting_reason{namespace="online-boutique", reason=~"ImagePullBackOff|ErrImagePull"} == 1`
- `kube_deployment_status_replicas_unavailable{namespace="online-boutique"} > 0`
- 이벤트에 `Failed to pull image`, `manifest unknown`, `not found` 메시지가 나타난다.

## 진단 절차

1. 파드 이벤트를 확인한다. `kubectl describe pod <pod> -n online-boutique`의 `Events`에서 실패 메시지를 본다.
2. 실패 원인을 구분한다.
   - `manifest unknown` 또는 `not found`: 존재하지 않는 이미지 태그 (가장 흔함)
   - `unauthorized` 또는 `denied`: 레지스트리 인증 문제 (이 문서의 조치 범위 밖, RB-10 참고)
   - `i/o timeout`: 레지스트리 네트워크 문제 (이 문서의 조치 범위 밖, RB-10 참고)
3. 현재 이미지와 직전 revision의 이미지를 비교한다. `kubectl rollout history deployment/<name> -n online-boutique --revision=<N>`
4. 롤아웃 상태를 확인한다. `kubectl rollout status deployment/<name> -n online-boutique`

## 조치

- **존재하지 않는 이미지 태그인 경우: 직전 revision으로 롤백 (rollback)**
  - RB-07 롤백 표준 절차를 따른다.
  - 승인 요청에 현재 이미지 태그, 롤백 후 이미지 태그, 이벤트 메시지를 근거로 포함한다.
- 올바른 이미지 태그를 추측해서 지정하지 않는다. 정확한 태그는 배포 담당자가 확인 후 재배포한다.
- 레지스트리 인증·네트워크 문제는 지원하는 4종 조치로 해결할 수 없으므로 RB-10에 따라 수동 조치로 안내한다.

## 검증

- 롤백 후 `kubectl rollout status`가 완료되고 모든 파드가 `Running`인지 확인한다.
- `kube_deployment_status_replicas_unavailable`이 0이 되어야 한다.
- 실행 중인 이미지 태그가 롤백 대상 revision의 태그와 일치하는지 확인한다.

## 관련 문서

- RB-07 롤백 표준 절차
- RB-10 지원 범위 밖 조치와 수동 대응 안내
