"""설정 로딩. `.env`, `config/settings.yaml`, `config/policy.yaml`을 읽어 타입이 있는 객체로 제공한다."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

ROOT_DIR: Path = Path(__file__).resolve().parent.parent
CONFIG_DIR: Path = ROOT_DIR / "config"


class LLMConfig(BaseModel):
    """LLM 호출 설정."""

    model_id: str
    region: str
    temperature: float
    max_output_tokens: int
    retries: int
    timeout_sec: int


class EmbeddingConfig(BaseModel):
    """임베딩 호출 설정."""

    model_id: str
    region: str
    timeout_sec: int


class LimitsConfig(BaseModel):
    """요청당 실행 한도."""

    request_timeout_sec: int
    max_tool_calls: int
    subagent_max_iterations: int
    max_tokens: int
    max_cost_usd: float


class ToolsConfig(BaseModel):
    """도구 공통 설정."""

    tool_timeout_sec: int
    log_max_lines: int
    metrics_default_range_minutes: int
    metrics_max_range_minutes: int
    metrics_step_sec: int
    http_stats_window_minutes: int
    http_stats_max_lines: int
    prometheus_url: str
    read_kubeconfig: str
    write_kubeconfig: str


class ApprovalConfig(BaseModel):
    """승인 대기와 체크포인트 설정."""

    wait_minutes: int
    checkpoint_db: str
    checkpoint_retention_days: int


class VerificationConfig(BaseModel):
    """조치 후 검증 설정."""

    initial_delay_sec: int
    interval_sec: int
    max_wait_sec: int


class RagConfig(BaseModel):
    """RAG 파이프라인 설정."""

    data_dirs: list[str]
    persist_dir: str
    collection: str
    chunk_size: int
    chunk_overlap: int
    top_k: int
    similarity_threshold: float
    bm25_weight: float
    vector_weight: float


class Settings(BaseModel):
    """`config/settings.yaml` 전체."""

    llm: LLMConfig
    embedding: EmbeddingConfig
    limits: LimitsConfig
    tools: ToolsConfig
    approval: ApprovalConfig
    verification: VerificationConfig
    rag: RagConfig


class ScalePolicy(BaseModel):
    """scale 조치 허용 범위."""

    min_replicas: int
    max_replicas: int
    max_factor: float


class LimitsPolicy(BaseModel):
    """set_limits 조치 허용 범위."""

    max_factor: float
    max_cpu: str
    max_memory: str


class RollbackPolicy(BaseModel):
    """rollback 조치 허용 범위."""

    max_revisions_back: int


class ApprovalPolicy(BaseModel):
    """승인자 정책. 승인 권한은 Slack User ID 목록으로 판정한다."""

    group: str
    user_ids: list[str]


class Policy(BaseModel):
    """`config/policy.yaml` 전체."""

    namespace: str
    actions: list[str]
    scale: ScalePolicy
    limits: LimitsPolicy
    rollback: RollbackPolicy
    approval: ApprovalPolicy
    cooldown_sec: int


def _load_yaml(path: Path) -> dict[str, Any]:
    """YAML 파일을 읽어 dict로 반환한다."""
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def load_env() -> None:
    """`.env`를 환경변수로 로드한다. 값은 코드·로그에 남기지 않는다."""
    load_dotenv(ROOT_DIR / ".env")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """운영 파라미터를 반환한다."""
    load_env()
    return Settings(**_load_yaml(CONFIG_DIR / "settings.yaml"))


@lru_cache(maxsize=1)
def get_policy() -> Policy:
    """실행 정책을 반환한다."""
    return Policy(**_load_yaml(CONFIG_DIR / "policy.yaml"))


def resolve_path(relative: str) -> Path:
    """프로젝트 루트 기준 상대 경로를 절대 경로로 바꾼다."""
    path = Path(relative)
    return path if path.is_absolute() else ROOT_DIR / path


def langfuse_enabled() -> bool:
    """Langfuse 키가 모두 설정되어 있는지 반환한다. 없으면 관측을 건너뛴다."""
    load_env()
    return all(os.getenv(k) for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"))
