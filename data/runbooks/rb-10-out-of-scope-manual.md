# RB-10 · 지원 범위 밖 조치와 수동 대응 안내

- 문서 ID: RB-10
- 대상: Agent가 직접 실행할 수 없는 모든 조치
- 최종 수정: 2026-09-20

## 원칙

- Agent가 실행할 수 있는 조치는 **restart, scale, rollback, limit 변경 4종**이며 대상은 `online-boutique` 네임스페이스로 한정된다.
- 그 밖의 조치가 필요하면 Agent는 실행하지 않고, 원인 분석 결과와 함께 **수동 조치 가이드**를 제시한다.
- 수동 조치는 담당 팀이 직접 수행하며, Agent는 실행을 대신하거나 승인 요청을 만들지 않는다.

## 범위 밖 유형별 안내

### PVC 용량 부족

- 증상: 파드 로그에 `no space left on device`, PVC 사용률 100% 근처
- 확인: `kubectl get pvc -n online-boutique`, `kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes`
- 수동 조치 가이드: StorageClass가 볼륨 확장(`allowVolumeExpansion: true`)을 지원하면 PVC의 `spec.resources.requests.storage`를 늘린다. 지원하지 않으면 새 볼륨으로 데이터를 옮겨야 한다. 불필요한 데이터 정리 여부를 서비스 담당자와 먼저 확인한다.
- 담당: 플랫폼 팀 (`#platform-oncall`)

### 설정(ConfigMap·Secret·환경변수) 직접 수정

- Agent는 설정 값을 직접 바꾸지 않는다. 직전 revision이 정상이었다면 롤백(RB-07)으로 먼저 복구하고, 올바른 설정은 배포 파이프라인으로 재배포한다.
- 담당: 서비스 개발 팀

### 허용 네임스페이스 밖 리소스 (kube-system 등)

- 예: coredns, kube-proxy, 모니터링 스택 구성 요소
- 클러스터 전체에 영향을 주므로 Agent가 조치하지 않는다.
- 수동 조치 가이드: 이상 증상과 확인한 근거(이벤트, 로그)를 정리해 플랫폼 팀에 전달한다. coredns 이상이 의심되면 서비스 로그의 `no such host` 오류 빈도를 근거로 첨부한다.
- 담당: 플랫폼 팀 (`#platform-oncall`)

### 네트워크 정책·인그레스·레지스트리 문제

- 예: NetworkPolicy로 인한 통신 차단, 레지스트리 인증 실패(`unauthorized`), 레지스트리 접속 지연(`i/o timeout`)
- 수동 조치 가이드: 차단된 출발지·목적지와 포트, 실패 메시지를 정리해 담당 팀에 전달한다.
- 담당: 플랫폼 팀 (`#platform-oncall`)

### 정책 허용 범위를 넘는 4종 조치

- 예: replica를 10개 넘게 늘리기, limit을 2배 넘게 또는 CPU 2 core·메모리 4Gi 넘게 올리기, 최근 5개 revision보다 오래된 버전으로 롤백
- Agent는 허용 범위 안에서 가능한 대안(예: 2배까지 확장 후 재평가)을 제시한다.
- 범위를 넘는 조치가 꼭 필요하면 SRE 리드(`sre-approvers`)가 직접 판단·수행한다.

### 리소스 삭제

- 네임스페이스, Deployment, PVC 등 리소스 삭제는 어떤 경우에도 Agent가 실행하거나 제안하지 않는다.

## 에스컬레이션 시 전달할 내용

- 증상과 시작 시각
- Agent가 확인한 근거 (메트릭, 로그, 이벤트)
- 필요한 조치와 Agent가 실행하지 않은 이유
- 영향 범위 (RB-09 장애 영향 범위 참고)
