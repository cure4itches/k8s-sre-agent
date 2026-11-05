# CLAUDE.md · K8s 장애 대응 Copilot

무엇을, 왜 만드는지는 @SERVICE.md 가 기준이다. 이 문서는 **어떻게 만드는지**만 다룬다.
두 문서가 충돌하면 SERVICE.md를 따르고, 충돌 사실을 사용자에게 알린다.

## 기술 스택

- Python 3.14 (`python3 -m venv .venv`), 의존성은 `requirements.txt`
- LangChain, LangGraph. 서브 Agent는 `langchain.agents.create_agent`로 만든다 (ReAct)
- LLM: Amazon Bedrock `ChatBedrockConverse` · 모델 `us.anthropic.claude-sonnet-4-6` · 리전 `us-west-2` · temperature 0. 쿼터 초과(Throttling) 시 `config/settings.yaml`의 `llm.fallbacks` 순서로 대체 모델(모델 ID·리전)로 로테이션한다 (`src/agent.py`의 `RotatingBedrock`)
- 임베딩: `BedrockEmbeddings` · `amazon.titan-embed-text-v2:0` (리전 `us-east-1`)
- 벡터 저장소: ChromaDB (로컬 영속 경로 `.chroma/`)
- MCP: 서버는 `mcp`(FastMCP)로 구현, Agent 쪽은 `langchain-mcp-adapters`로 연결
- 클러스터 접근: `kubernetes` Python client. **MCP 서버 안에서만** 사용한다
- 메트릭: Prometheus HTTP API (`httpx`)
- API 서버: FastAPI + uvicorn
- Slack: `slack_bolt` Socket Mode
- 체크포인트: LangGraph `AsyncSqliteSaver` (`.checkpoints.db`), HITL 승인 대기 상태 보존용
- 관측: Langfuse Cloud (셀프호스팅은 실행 환경 리소스 부족으로 사용하지 않는다), `langfuse` CallbackHandler. 키는 `.env`의 `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`
- 평가: `ragas` + LLM-as-Judge (Bedrock 동일 모델)
- 실행 환경: kind 클러스터, Online Boutique, kube-prometheus-stack

## 폴더 구조 (제출 규약. 바꾸지 않는다)

```
src/agent.py            메인 그래프: Supervisor + 서브 Agent 4종
src/tools.py            도메인 도구 (retrieve_runbooks, query_metrics, MCP 도구 로딩)
src/retriever.py        RAG 파이프라인 (인덱싱, 하이브리드 검색)
src/schemas.py          Pydantic 모델 (진단, 조치안, API 요청·응답)
src/prompts.py          시스템 프롬프트
src/guardrails.py       가드레일 미들웨어
src/mcp_servers/k8s_read.py    inspect_k8s (읽기 전용 MCP 서버)
src/mcp_servers/k8s_write.py   remediate (쓰기 MCP 서버, 조치 4종)
src/api.py              POST /query, POST /resume
src/slack_app.py        Slack 봇 (Socket Mode), API(/query, /resume)를 호출
src/config.py           설정 로딩
config/settings.yaml    운영 파라미터
config/policy.yaml      실행 정책 (네임스페이스, 조치 유형, 허용 범위)
data/runbooks/          Runbook 10건
data/postmortems/       Postmortem 3건
infra/                  kind 설정, Online Boutique·모니터링 설치, 장애 주입 스크립트
evaluation/test_queries.csv     인-아웃 세트 (자체 평가·최종 채점 기준)
evaluation/run_eval.py          자체 평가 실행기
evaluation/round1_report.md     1차 자체 평가 결과
evaluation/round2_report.md     2차 자체 평가 결과 (개선 루프 5·6회차와 3차 평가 결과 포함)
evaluation/results/             라운드별 원 데이터 (round{N}.json, 다음 리포트의 개선폭 계산에 사용)
docs/images/                    README용 Slack·Langfuse 사용 예시 캡처
Dockerfile, requirements.txt, run.sh, README.md
```

## 아키텍처

- Supervisor가 서브 Agent에 위임한다: `metrics_analyst`(query_metrics), `k8s_inspector`(inspect_k8s), `runbook_researcher`(retrieve_runbooks), `remediator`(remediate)
- 흐름: 조사 → 진단 → 조치안 → 승인 → 실행 → 검증
- 진단과 조치안은 `with_structured_output`으로 Pydantic 구조화 출력을 받는다
  - `Diagnosis`: hypotheses[{cause, confidence(high|medium|low), evidence[]}]
  - `RemediationPlan`: target, action_type, change, impact, rollback, evidence[]
- 승인: `remediator`는 `remediate` 호출 직전에 `interrupt()`로 멈춘다. 재개는 `Command(resume={"decision": "approve|edit|reject", "params": {...}, "approver": "..."})`
- 검증: 실행 60초 뒤 재조회, 30초 간격 최대 5분. 판정은 `success | failure | uncertain`. 실패해도 추가 조치를 자동 실행하지 않는다
- 대화 상태 키: `/query`는 `thread_id`를 받으면 그 thread를 이어가고, 없으면 새로 만든다 (응답에 `thread_id` 포함). Slack은 스레드 ts를 thread_id로 쓴다

## RAG

- `data/runbooks`, `data/postmortems`를 Markdown 헤더 기준으로 청크 분할 (최대 1,000자, overlap 150자)
- 하이브리드 검색: BM25 + 벡터 (`EnsembleRetriever`), top-k 4
- 코사인 유사도 0.5 미만이면 "관련 문서 없음"을 반환한다. 문서 내용을 지어내지 않는다
- 반환 문서마다 `doc_id`를 `파일명#섹션` 형식으로 붙인다
- 재인덱싱: `python -m src.retriever --reindex`

## 도구 규칙

- `remediate`의 `action_type`은 enum 4개로 고정한다: `restart | scale | rollback | set_limits`
- 읽기 MCP 서버와 쓰기 MCP 서버는 다른 ServiceAccount를 쓴다. 쓰기 SA는 `online-boutique` 네임스페이스의 Deployment patch 권한만 가진다
- 읽기 SA는 모든 네임스페이스의 pods, pods/log, events, deployments, replicasets 조회(get/list/watch)만 가진다. Secret 조회 권한은 주지 않는다. 읽기는 전체 네임스페이스, 변경은 `online-boutique`만 허용한다
- 로그 조회는 최대 200줄, 메트릭 기본 조회 범위는 최근 30분
- 파드의 모든 컨테이너(sidecar 포함) 로그를 조회한다. 롤링 업데이트 중에는 이전 파드와 새 파드가 함께 보이므로 파드의 ReplicaSet·revision을 함께 반환한다
- HTTP 에러율·지연은 `inspect_k8s`가 frontend 로그의 `http.resp.status`, `http.resp.took_ms`를 집계해 반환한다 (로그 200줄 제한과 별개로 서버 내부에서 집계). `query_metrics`는 Prometheus 지표만 다룬다
- 도구 설명(docstring)에 언제 쓰는지와 입력 예시를 적는다. Agent는 이것을 보고 도구를 고른다

## 가드레일 (src/guardrails.py)

LangChain agent middleware로 구현하고 Agent 코드에 섞지 않는다. SERVICE.md 4장의 규칙 번호(R1~R5)를 주석에 단다.

- 입력 (`before_model`): 프롬프트 인젝션 탐지(규칙 + LLM 분류), 시크릿·PII 마스킹 (R4, R5)
- Tool 결과 (`wrap_tool_call`): 마스킹, 외부 텍스트를 `<tool_data>` 태그로 감싸 데이터로만 취급 (R4, R5)
- 실행 정책 (`wrap_tool_call`): `config/policy.yaml`로 검증을 통과해야 승인 요청을 만든다 (R1, R2)
- 출력 (`after_model`): 응답의 시크릿 redaction, 근거 없는 단정 문구 점검 (R3, R4)
- 차단하면 `trace`에 `guardrail` step과 사유를 남긴다

## API 형식 (제출 규약)

```
POST /query
요청: {"question": "사용자 질의"}
응답: {"answer": "근거 기반 응답",
       "contexts": [{"doc_id": "...", "text": "..."}],
       "trace": [{"step": "...", "input": "...", "output": "..."}]}
```

- `trace.step` 값: `supervisor`, `retrieve`, `tool:<도구명>`, `guardrail`, `approval`, `verify`
- 승인이 필요한 요청은 `answer`에 승인 대기 상태를 안내하고 `trace`에 `approval` step을 남긴 뒤 멈춘다. 평가 중에는 승인을 자동으로 하지 않는다
- 승인 재개: `POST /resume` · 요청 `{"thread_id": "...", "decision": "approve|edit|reject", "params": {...}, "approver": "..."}` · 응답은 `/query`와 같은 형식. `approver`(Slack User ID)가 `config/policy.yaml`의 `approval.user_ids`에 없으면 거부한다 (Slack 무료 워크스페이스는 사용자 그룹을 만들 수 없어 User ID 목록으로 판정한다). Slack 버튼은 이 엔드포인트를 호출한다

## 설정

- 시크릿(AWS, Slack, Langfuse 키)은 `.env`에서 읽고 코드·프롬프트·트레이스에 남기지 않는다
- 운영 파라미터는 `config/settings.yaml`에 두고 코드에 숫자를 하드코딩하지 않는다. 기본값:
  - 요청당 타임아웃 5분 · Tool 호출 25회 · 서브 Agent ReAct 반복 8회 · 토큰 200k · 비용 USD 1.00
  - LLM 재시도 3회 (지수 백오프) · Tool 타임아웃 30초 · 최대 출력 4,096 토큰
  - 승인 대기 15분 (만료 시 거절) · 체크포인트 보존 7일
  - 모델 로테이션: 대체 모델 목록 `llm.fallbacks` · 일일 쿼터 초과 모델은 1시간, 일시적 Throttling 모델은 60초 건너뜀
- `config/policy.yaml` 기본값: 네임스페이스 `online-boutique` · scale 1~10 (1회 최대 2배) · limit 1회 최대 2배, 상한 CPU 2 / 메모리 4Gi · rollback 최근 5개 revision · 승인자 역할 `sre-approvers` (실제 승인 권한은 Slack User ID 목록 `approval.user_ids`로 판정)

## 자체 평가

- `evaluation/test_queries.csv`를 UTF-8로 읽는다. 필드 안의 여러 항목은 세미콜론(;)으로 구분한다
- `note`에 적힌 장애 주입(INC-x)을 먼저 적용한 뒤 해당 문항을 실행한다 (`./run.sh chaos INC-x`)
- `run_eval.py`는 `POST /query`로 문항을 실행하고 다음 세 조건을 모두 만족하면 통과로 판정한다
  1. `forbidden` 항목이 답변에 없다 (규칙 기반, 판단이 필요한 항목은 Judge)
  2. `expected_tools`의 도구가 `trace`에 있다 (규칙 기반). `remediate`는 `approval` step이 있으면 호출 의도로 인정한다 (승인 전에는 실제 호출되지 않으므로)
  3. `expected_traits`를 모두 충족한다 (LLM-as-Judge, 항목별 pass/fail과 사유 기록)
- 문항마다 3회 실행해 다수결로 판정한다
- RAGAS(`context_recall`, `context_precision`, `faithfulness`, `answer_relevancy`)는 `expected_tools`에 `retrieve_runbooks`가 있는 문항에 대해 산출한다. reference는 `expected_traits`를 쓴다. `faithfulness`는 응답이 클러스터 조회 내용에 근거하므로 Runbook 문서와 도구 조회 결과를 근거(context)로 산출하고, 나머지 지표는 검색된 문서만 쓴다. 조치 유형 일치는 응답이 제안한 조치 목록에 기대 조치가 포함되면 일치로 본다
- 결과는 Langfuse score로도 기록한다
- Bedrock 쿼터가 모든 대체 모델에서 소진되면 평가를 중단하고 리포트를 만들지 않는다. 리포트에는 실제로 쓰인 모델과 호출 수를 적는다
- 리포트 `round{N}_report.md`에 카테고리별 통과율, 실패 문항과 사유, RAGAS 지표, 이전 라운드 대비 개선폭을 적는다. `.md` 규칙상 리포트는 round1/round2만 두므로 3차 이후 결과는 `round2_report.md`에 섹션으로 합치고 원 데이터는 `evaluation/results/`에 둔다
- 개선 루프: 실패 문항 하나 선택 → 원인이 답변인지 판정 기준인지 진단 → 한 가지만 수정 → 재평가. 전부 통과하거나 6회 반복하면 멈추고 수정 내역을 정리한다

## 코드 규칙

- 파일 하나에 한 가지 역할만 둔다
- 함수와 도구에는 한국어 docstring을 쓴다
- 모든 함수에 타입 힌트를 단다
- 프롬프트는 `src/prompts.py`에만 둔다
- 외부 호출(LLM, Prometheus, K8s)은 타임아웃과 예외 처리를 갖춘다

## 하지 말 것

- 요청하지 않은 파일을 새로 만들지 않는다. `.md` 파일은 SERVICE.md, CLAUDE.md, README.md, round1/round2 리포트만 둔다 (`data/runbooks`, `data/postmortems`의 데이터 파일은 제외)
- 기존 파일을 통째로 다시 쓰지 않는다. 바뀐 부분만 고친다
- `remediate`를 `interrupt()` 없이 호출하는 코드 경로를 만들지 않는다
- cluster-admin 권한을 쓰지 않는다. 허용 네임스페이스 밖 리소스를 변경하지 않는다 (조회만 가능)
- 사용자 확인 없이 `test_queries.csv`의 문항이나 기대값을 바꾸거나 지우지 않는다
- 특정 평가 문항의 input 문자열로 분기하는 등 평가 세트에만 맞춘 하드코딩을 하지 않는다

## 자주 쓰는 명령

```
./run.sh setup              # kind + Online Boutique + 모니터링 설치
./run.sh chaos INC-1        # 장애 주입 (reset으로 원복)
python -m src.retriever --reindex
uvicorn src.api:app --port 8000
python -m src.slack_app
python evaluation/run_eval.py --round 1
```