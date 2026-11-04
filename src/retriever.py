"""RAG 파이프라인: Markdown 문서 인덱싱과 하이브리드(BM25 + 벡터) 검색."""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from botocore.config import Config as BotoConfig
from chromadb.config import Settings as ChromaSettings
from langchain_aws import BedrockEmbeddings
from langchain_chroma import Chroma
from langchain_classic.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from src.config import ROOT_DIR, RagConfig, Settings, get_settings, resolve_path

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]+|[가-힣]+")
_HANGUL_RE = re.compile(r"[가-힣]")
_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3")]
_NO_SECTION = "문서 정보"


class RetrieverError(RuntimeError):
    """검색·인덱싱 중 외부 호출(임베딩, 벡터 저장소)이 실패했을 때 발생한다."""


@dataclass
class RetrievalResult:
    """검색 결과. `no_match`이면 관련 문서가 없다는 뜻이며 `docs`는 비어 있다."""

    docs: list[Document] = field(default_factory=list)
    best_score: float = 0.0
    no_match: bool = True


def tokenize(text: str) -> list[str]:
    """BM25용 토크나이저. 영문·숫자 토큰과 한글 어절, 한글 2-gram을 함께 반환한다."""
    tokens: list[str] = []
    for tok in _TOKEN_RE.findall(text.lower()):
        tokens.append(tok)
        if _HANGUL_RE.match(tok) and len(tok) > 1:
            tokens.extend(tok[i : i + 2] for i in range(len(tok) - 1))
    return tokens


def _section_name(meta: dict[str, str]) -> str:
    """헤더 메타데이터에서 `H2` 또는 `H2 > H3` 형식의 섹션 이름을 만든다."""
    h2, h3 = meta.get("h2"), meta.get("h3")
    if h2 and h3:
        return f"{h2} > {h3}"
    return h2 or h3 or _NO_SECTION


def split_markdown(path: Path, cfg: RagConfig) -> list[Document]:
    """Markdown 파일 하나를 헤더 기준으로 나눈 뒤, 최대 길이를 넘는 섹션은 다시 분할한다.

    각 청크의 `doc_id`는 `파일명#섹션` 형식이다. 한 섹션이 여러 청크로 나뉘면 `:순번`을 붙인다.
    """
    text = path.read_text(encoding="utf-8")
    title_match = re.search(r"^#\s+(.+)$", text, flags=re.M)
    title = title_match.group(1).strip() if title_match else path.stem

    header_splitter = MarkdownHeaderTextSplitter(_HEADERS, strip_headers=True)

    chunks: list[Document] = []
    for section in header_splitter.split_text(text):
        body = section.page_content.strip()
        if not body:
            continue
        name = _section_name(section.metadata)
        prefix = f"[{title}] {name}\n"
        # 접두어 길이만큼 본문 한도를 줄여 최종 청크가 최대 길이를 넘지 않게 한다
        limit = cfg.chunk_size - len(prefix)
        parts = [body] if len(body) <= limit else RecursiveCharacterTextSplitter(
            chunk_size=limit, chunk_overlap=cfg.chunk_overlap
        ).split_text(body)
        for i, part in enumerate(parts, start=1):
            doc_id = f"{path.name}#{name}" + (f":{i}" if len(parts) > 1 else "")
            chunks.append(
                Document(
                    page_content=prefix + part,
                    metadata={"doc_id": doc_id, "source": path.name, "section": name, "title": title},
                )
            )
    return chunks


def load_chunks(cfg: RagConfig) -> list[Document]:
    """설정된 데이터 폴더의 모든 Markdown 문서를 청크로 만든다."""
    chunks: list[Document] = []
    for d in cfg.data_dirs:
        for path in sorted(resolve_path(d).glob("*.md")):
            chunks.extend(split_markdown(path, cfg))
    if not chunks:
        raise RetrieverError(f"인덱싱할 문서가 없습니다: {cfg.data_dirs}")
    return chunks


def _embeddings(settings: Settings) -> BedrockEmbeddings:
    """Bedrock 임베딩 클라이언트를 만든다. 타임아웃과 재시도를 설정한다."""
    boto_cfg = BotoConfig(
        read_timeout=settings.embedding.timeout_sec,
        connect_timeout=settings.embedding.timeout_sec,
        retries={"max_attempts": settings.llm.retries, "mode": "standard"},
    )
    return BedrockEmbeddings(
        model_id=settings.embedding.model_id, region_name=settings.embedding.region, config=boto_cfg
    )


def _vectorstore(settings: Settings) -> Chroma:
    """코사인 거리 기반 Chroma 컬렉션을 연다."""
    rag = settings.rag
    return Chroma(
        collection_name=rag.collection,
        embedding_function=_embeddings(settings),
        persist_directory=str(resolve_path(rag.persist_dir)),
        collection_metadata={"hnsw:space": "cosine"},
        client_settings=ChromaSettings(anonymized_telemetry=False),
    )


def build_index(reindex: bool = False) -> int:
    """문서를 청크로 나눠 벡터 저장소에 저장한다. `reindex`이면 기존 컬렉션을 지우고 다시 만든다.

    Returns:
        인덱싱된 청크 수
    """
    settings = get_settings()
    store = _vectorstore(settings)
    try:
        if reindex:
            store.delete_collection()
            store = _vectorstore(settings)
        chunks = load_chunks(settings.rag)
        store.add_documents(chunks, ids=[c.metadata["doc_id"] for c in chunks])
    except Exception as e:  # 임베딩·저장소 오류를 호출자가 구분할 수 있게 감싼다
        raise RetrieverError(f"인덱싱 실패: {type(e).__name__}: {e}") from e
    return len(chunks)


class HybridRetriever:
    """BM25 + 벡터 하이브리드 검색기. 유사도가 임계값 미만이면 "관련 문서 없음"을 반환한다."""

    def __init__(self, settings: Settings | None = None) -> None:
        """청크를 읽어 BM25를 만들고, 벡터 저장소가 비어 있으면 인덱싱한다."""
        self._settings = settings or get_settings()
        rag = self._settings.rag
        self._chunks = load_chunks(rag)
        self._store = _vectorstore(self._settings)
        if self._store._collection.count() == 0:
            build_index()
            self._store = _vectorstore(self._settings)
        bm25 = BM25Retriever.from_documents(
            self._chunks, k=rag.top_k * 2, preprocess_func=tokenize
        )
        vector = self._store.as_retriever(search_kwargs={"k": rag.top_k * 2})
        self._ensemble = EnsembleRetriever(
            retrievers=[bm25, vector], weights=[rag.bm25_weight, rag.vector_weight], id_key="doc_id"
        )

    def _similarities(self, query: str) -> dict[str, float]:
        """질의와 모든 청크의 코사인 유사도를 `doc_id`별로 계산한다 (코사인 거리 = 1 - 유사도)."""
        pairs = self._store.similarity_search_with_score(query, k=len(self._chunks))
        return {doc.metadata["doc_id"]: 1.0 - dist for doc, dist in pairs}

    def search(self, query: str) -> RetrievalResult:
        """질의와 관련된 문서를 최대 top_k개 반환한다.

        Args:
            query: 증상 또는 질문 (예: "recommendationservice OOMKilled 반복 재시작")
        """
        rag = self._settings.rag
        try:
            sims = self._similarities(query)
            candidates = self._ensemble.invoke(query)
        except Exception as e:
            raise RetrieverError(f"검색 실패: {type(e).__name__}: {e}") from e

        best = max(sims.values(), default=0.0)
        kept: list[Document] = []
        for doc in candidates:
            score = sims.get(doc.metadata["doc_id"], 0.0)
            if score >= rag.similarity_threshold:
                kept.append(Document(page_content=doc.page_content, metadata={**doc.metadata, "score": round(score, 4)}))
            if len(kept) == rag.top_k:
                break
        return RetrievalResult(docs=kept, best_score=best, no_match=not kept)


@lru_cache(maxsize=1)
def get_retriever() -> HybridRetriever:
    """프로세스 안에서 공유하는 검색기를 반환한다."""
    return HybridRetriever()


def main() -> None:
    """CLI: `--reindex`로 재인덱싱, `--query`로 검색을 시험한다."""
    parser = argparse.ArgumentParser(description="RAG 인덱스 관리")
    parser.add_argument("--reindex", action="store_true", help="벡터 인덱스를 다시 만든다")
    parser.add_argument("--query", type=str, help="검색을 시험한다")
    args = parser.parse_args()

    if args.reindex:
        print(f"인덱싱 완료: 청크 {build_index(reindex=True)}개 (경로: {ROOT_DIR / get_settings().rag.persist_dir})")
    if args.query:
        result = get_retriever().search(args.query)
        print(f"best_score={result.best_score:.3f} no_match={result.no_match}")
        for d in result.docs:
            print(f"- {d.metadata['doc_id']} (score={d.metadata['score']})")


if __name__ == "__main__":
    main()
