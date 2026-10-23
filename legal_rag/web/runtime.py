"""Process-wide state for the web demo, built once at startup.

Building BM25 over the frozen corpus takes about a second, so every retriever
is built before the first request.  Dense and RRF retrievers are built only
when the embedding cache passes the same health check as ``legal-rag
cache-health``; otherwise they are listed as unavailable with the reason.
"""

from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from legal_rag.chat import DEFAULT_ANSWER_MAX_TOKENS, DEFAULT_ENABLE_THINKING
from legal_rag.chunking import load_chunks
from legal_rag.cli import (
    adaptive_options_from_args,
    create_retriever,
    resolve_index_dir,
)
from legal_rag.config import load_config, resolve_path
from legal_rag.embeddings import (
    embedding_cache_dir,
    inspect_embedding_cache,
    resolve_embedding_model,
)
from legal_rag.env import live_model_calls_allowed
from legal_rag.indexing import resolve_chunks_path
from legal_rag.llm import SILICONFLOW_MODEL_PREFIX, build_completion_client
from legal_rag.rerank import build_reranker, resolve_reranker_config
from legal_rag.retrieval import Retriever, RRFHybridRetriever

from .errors import describe_error

DEFAULT_RUNS_PATH = Path("artifacts/web/runs.jsonl")
MAX_WEB_TOP_K = 20
EXAMPLE_QUESTIONS = (
    "劳动合同法第三十九条规定了什么？",
    "民法典关于租赁合同的押金是怎么规定的？",
    "公司股东会的决议程序有哪些要求？",
)


@dataclass(frozen=True)
class ModelInfo:
    key: str
    name: str
    provider: str

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "name": self.name, "provider": self.provider}


@dataclass
class RetrieverSlot:
    key: str
    label: str
    retriever: Retriever | None
    unavailable_reason: str | None = None
    uses_embedding: bool = False

    @property
    def available(self) -> bool:
        return self.retriever is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "available": self.available,
            "reason": self.unavailable_reason,
            "uses_embedding": self.uses_embedding,
        }


@dataclass
class WebRuntime:
    retrievers: dict[str, RetrieverSlot]
    default_retriever: str
    default_top_k: int
    generator: ModelInfo
    completion_client_factory: Callable[[], Any]
    runs_path: Path = DEFAULT_RUNS_PATH
    embedding: ModelInfo | None = None
    reranker: Any | None = None
    reranker_info: ModelInfo | None = None
    reranker_error: str | None = None
    rerank_default: bool = False
    candidate_top_n: int = 20
    chat_options: dict[str, Any] = field(default_factory=dict)
    adaptive_options: dict[str, Any] = field(default_factory=dict)
    corpus: dict[str, Any] = field(default_factory=dict)
    embedding_cache: dict[str, Any] | None = None
    startup_ms: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    examples: tuple[str, ...] = EXAMPLE_QUESTIONS

    @property
    def adaptive_enabled(self) -> bool:
        return bool(self.adaptive_options.get("enabled", False))

    def options(self) -> dict[str, Any]:
        reranker = None
        if self.reranker_info is not None:
            reranker = {
                **self.reranker_info.to_dict(),
                "available": self.reranker is not None,
                "reason": self.reranker_error,
                "candidate_top_n": self.candidate_top_n,
            }
        return {
            "retrievers": [slot.to_dict() for slot in self.retrievers.values()],
            "defaults": {
                "retriever": self.default_retriever,
                "rerank": self.rerank_default and self.reranker is not None,
                "generate": True,
                "top_k": self.default_top_k,
            },
            "limits": {"top_k_min": 1, "top_k_max": MAX_WEB_TOP_K},
            "models": {
                "generator": self.generator.to_dict(),
                "embedding": self.embedding.to_dict() if self.embedding else None,
                "reranker": reranker,
            },
            "adaptive": {"enabled": self.adaptive_enabled},
            "corpus": self.corpus,
            "embedding_cache": self.embedding_cache,
            "startup_ms": self.startup_ms,
            "live_model_calls_allowed": live_model_calls_allowed(),
            "warnings": self.warnings,
            "examples": list(self.examples),
        }


def generator_info(model: str) -> ModelInfo:
    if model.startswith(SILICONFLOW_MODEL_PREFIX):
        return ModelInfo(
            key=model,
            name=model.removeprefix(SILICONFLOW_MODEL_PREFIX),
            provider="siliconflow",
        )
    return ModelInfo(key=model, name=model, provider="ollama")


def answer_client_factory(
    model: str, chat_options: dict[str, Any]
) -> Callable[[], Any]:
    """Build answer clients with the generation options the assistant uses.

    The page injects its own client so it can count tokens per request; the
    client must still answer with thinking off and a capped output, or every
    answer pays for a hidden reasoning chain.
    """

    def factory() -> Any:
        return build_completion_client(
            model,
            ollama_base_url=chat_options["ollama_base_url"],
            request_timeout=chat_options["request_timeout"],
            enable_thinking=DEFAULT_ENABLE_THINKING,
            max_tokens=DEFAULT_ANSWER_MAX_TOKENS,
        )

    return factory


def build_runtime(config_path: str | Path = "configs/default.yaml") -> WebRuntime:
    path = resolve_path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"Config not found: {path}")
    config = load_config(path)
    strategy = config["chunking"]["default_strategy"]
    top_k = int(config["retrieval"]["top_k"])
    startup_ms: dict[str, float] = {}
    warnings: list[str] = []

    started = time.perf_counter()
    chunks = load_chunks(resolve_chunks_path(resolve_index_dir(None, config, strategy)))
    startup_ms["load_chunks"] = _ms_since(started)

    def build(kind: str) -> Retriever:
        # "none" keeps the base retriever unwrapped; reranking is added per request.
        return create_retriever(
            kind,
            chunks,
            config,
            top_k=top_k,
            chunk_strategy=strategy,
            reranker_key="none",
        )

    started = time.perf_counter()
    bm25 = build("bm25")
    bm25.retrieve(EXAMPLE_QUESTIONS[0], top_k=top_k)
    startup_ms["bm25"] = _ms_since(started)

    model_config = resolve_embedding_model(config)
    embedding = ModelInfo(
        key=model_config.key,
        name=model_config.model_name,
        provider=model_config.provider,
    )
    cache_dir = embedding_cache_dir(
        config["embedding"]["cache_dir"], strategy, model_config.key
    ).resolve()
    started = time.perf_counter()
    report = inspect_embedding_cache(
        cache_dir,
        chunks=chunks,
        model_config=model_config,
        expected_chunk_strategy=strategy,
    )
    startup_ms["embedding_cache_health"] = _ms_since(started)
    dense: Retriever | None = None
    dense_reason = ""
    if report.valid:
        started = time.perf_counter()
        try:
            dense = build("dense")
        except Exception as exc:  # noqa: BLE001 - shown on the page instead.
            dense_reason = describe_error(exc)["message"]
        startup_ms["dense"] = _ms_since(started)
    else:
        codes = "、".join(
            issue.code for issue in report.issues if issue.severity == "error"
        )
        dense_reason = f"向量缓存不健康（{report.status}：{codes}），未构建向量检索"
    rrf: Retriever | None = None
    if dense is not None:
        retrieval = config["retrieval"]
        rrf = RRFHybridRetriever(
            bm25,
            dense,
            rrf_k=int(retrieval.get("rrf_k", 60)),
            bm25_weight=float(retrieval.get("rrf_bm25_weight", 1.0)),
            dense_weight=float(retrieval.get("rrf_dense_weight", 1.0)),
        )
    else:
        warnings.append(f"向量检索与 RRF 不可用：{dense_reason}")
    slots = {
        "bm25": RetrieverSlot("bm25", "BM25 关键词", bm25),
        "dense": RetrieverSlot(
            "dense",
            f"向量检索（{embedding.name}）",
            dense,
            None if dense is not None else dense_reason,
            uses_embedding=True,
        ),
        "rrf": RetrieverSlot(
            "rrf",
            "BM25 + 向量 RRF 融合",
            rrf,
            None if rrf is not None else f"依赖向量检索：{dense_reason}",
            uses_embedding=True,
        ),
    }

    reranking = config.get("reranking", {})
    configured_reranker = str(reranking.get("default") or "none")
    reranker_key = (
        configured_reranker
        if configured_reranker.lower() != "none"
        else next(iter(reranking.get("models") or {}), None)
    )
    reranker = reranker_info = reranker_error = None
    if reranker_key:
        reranker_config = resolve_reranker_config(config, reranker_key)
        reranker_info = ModelInfo(
            key=reranker_config.key,
            name=reranker_config.model_name,
            provider=reranker_config.provider,
        )
        try:
            reranker = build_reranker(reranker_config)
        except Exception as exc:  # noqa: BLE001 - shown on the page instead.
            reranker_error = describe_error(exc)["message"]
            warnings.append(f"重排不可用：{reranker_error}")

    model = config["models"]["default"]
    chat = config["chat"]
    chat_options = {
        "memory_token_limit": int(chat["memory_token_limit"]),
        "ollama_base_url": chat["ollama_base_url"],
        "request_timeout": chat["request_timeout"],
    }
    generator = generator_info(model)
    if generator.provider == "siliconflow" and not os.environ.get(
        "SILICONFLOW_API_KEY"
    ):
        warnings.append("未设置 SILICONFLOW_API_KEY：生成会失败，请在 .env 中配置后重启服务")
    if not live_model_calls_allowed():
        warnings.append("ALLOW_LIVE_MODEL_CALLS 未开启：向量检索、重排和生成都会被拦截")

    completion_client_factory = answer_client_factory(model, chat_options)

    configured_retriever = str(config["retrieval"].get("default", "bm25"))
    configured_retriever = {"hybrid": "rrf"}.get(
        configured_retriever, configured_retriever
    )
    default_retriever = (
        configured_retriever
        if configured_retriever in slots and slots[configured_retriever].available
        else "bm25"
    )
    return WebRuntime(
        retrievers=slots,
        default_retriever=default_retriever,
        default_top_k=top_k,
        generator=generator,
        completion_client_factory=completion_client_factory,
        runs_path=resolve_path(DEFAULT_RUNS_PATH),
        embedding=embedding,
        reranker=reranker,
        reranker_info=reranker_info,
        reranker_error=reranker_error,
        rerank_default=configured_reranker.lower() != "none",
        candidate_top_n=int(reranking.get("candidate_top_n", 20)),
        chat_options=chat_options,
        adaptive_options=adaptive_options_from_args(config, argparse.Namespace()),
        corpus={"chunk_strategy": strategy, "chunk_count": len(chunks)},
        embedding_cache={
            "status": report.status,
            "valid": report.valid,
            "cache_dir": str(cache_dir),
            "issues": [issue.to_dict() for issue in report.issues],
        },
        startup_ms=startup_ms,
        warnings=warnings,
    )


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
