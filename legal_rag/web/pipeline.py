"""Run one question through the staged chat pipeline and report every node.

Each node event carries ``node``, ``status`` (running, done, skipped or
failed), ``elapsed_ms`` and a ``detail`` object built only from the staged
turn objects, ``SearchResult.trace`` and the completion usage counters.  A
value the pipeline does not expose is left out rather than estimated.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from legal_rag.chat import (
    GeneratedTurn,
    LegalChatAssistant,
    RetrievedTurn,
    VerifiedTurn,
)
from legal_rag.llm import usage_delta, usage_snapshot
from legal_rag.models import SearchResult
from legal_rag.query import should_use_adaptive
from legal_rag.rerank import RerankingRetriever, retriever_runtime_metrics

from .errors import describe_error
from .observers import CompletionTap, RerankCall, RerankTap, RetrievalTap, RetrieveCall
from .runtime import WebRuntime

NODES = (
    ("analysis", "问题分析"),
    ("retrieval", "检索"),
    ("rerank", "重排"),
    ("evidence", "证据检查"),
    ("followup", "补充检索"),
    ("generation", "生成"),
    ("verification", "校验"),
)
NODE_LABELS = dict(NODES)
TERMINAL_GENERATION_REASONS = {
    "pre_retrieval_refusal": "风险标记触发检索前拒答，不调用生成模型",
    "evidence_limited": "证据不足，返回程序化回答，不调用生成模型",
}
FOLLOWUP_SKIP_REASONS = {
    "sufficient": "首次检索证据充分，无需补充检索",
    "needs_clarification": "缺少事实信息，需要用户澄清，不做补充检索",
    "no_followup_query": "证据不足，但没有可用的补充检索词",
    "max_rounds_reached": "补充检索轮数上限为 0",
}
SOURCE_TRACE_FIELDS = (
    "base_rank",
    "base_score",
    "rerank_score",
    "bm25_rank",
    "bm25_score",
    "dense_rank",
    "dense_score",
)
LOGGER = logging.getLogger(__name__)
_RUNS_LOCK = threading.Lock()

Emit = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class AskOptions:
    question: str
    retriever: str
    rerank: bool
    generate: bool
    top_k: int


class PipelineRun:
    """One request: staged assistant calls plus the events that describe them.

    A fresh assistant, completion client and reranking wrapper are built per
    request so usage counters and conversation memory never mix between runs;
    the prebuilt base retrievers and the reranker client are shared.
    """

    def __init__(
        self,
        runtime: WebRuntime,
        options: AskOptions,
        emit: Emit,
        *,
        run_id: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.options = options
        self._emit = emit
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.status: dict[str, str] = {}
        self.elapsed: dict[str, float] = {}
        self.errors: list[dict[str, Any]] = []
        self._started = time.perf_counter()
        self._node_started: dict[str, float] = {}
        self._at_ms: dict[str, float] = {}
        self._adaptive = False
        self._base_tap: RetrievalTap | None = None
        self._outer_tap: RetrievalTap | None = None
        self._rerank_tap: RerankTap | None = None
        self._reranking: RerankingRetriever | None = None
        self._client: CompletionTap | None = None

    def run(self) -> dict[str, Any]:
        """Execute the pipeline, emit every event and return the run record."""

        self._emit(
            "start",
            {
                "run_id": self.run_id,
                "question": self.options.question,
                "options": asdict(self.options),
            },
        )
        final: dict[str, Any] | None = None
        fatal: dict[str, Any] | None = None
        try:
            final = self._run_stages()
        except Exception as exc:  # noqa: BLE001 - reported to the page and run log.
            fatal = describe_error(exc)
            failed_nodes = [
                node for node, status in self.status.items() if status == "running"
            ]
            for node in failed_nodes:
                self._failed(node, fatal)
            for node, _label in NODES:
                if node not in self.status:
                    self._skipped(node, "前序节点失败，未执行")
            self.errors.append({**fatal, "nodes": failed_nodes, "fatal": True})
            self._emit(
                "error",
                {"run_id": self.run_id, **self.errors[-1], "timings": self._timings()},
            )
        else:
            self._emit("final", final)
        record = self._record(final, fatal)
        self._write_record(record)
        return record

    # -- pipeline stages -------------------------------------------------

    def _run_stages(self) -> dict[str, Any]:
        runtime, options = self.runtime, self.options
        slot = runtime.retrievers[options.retriever]
        self._base_tap = RetrievalTap(slot.retriever, self._on_base_call)
        retriever: Any = self._base_tap
        if options.rerank:
            self._rerank_tap = RerankTap(runtime.reranker, self._on_rerank_call)
            self._reranking = RerankingRetriever(
                self._base_tap,
                self._rerank_tap,
                candidate_top_n=runtime.candidate_top_n,
            )
            retriever = self._reranking
        self._outer_tap = RetrievalTap(retriever, self._on_outer_call)
        self._client = CompletionTap(runtime.completion_client_factory())
        adaptive = runtime.adaptive_options
        assistant = LegalChatAssistant(
            self._outer_tap,
            model=runtime.generator.key,
            top_k=options.top_k,
            completion_client=self._client,
            adaptive_enabled=runtime.adaptive_enabled,
            adaptive_use_llm=bool(adaptive.get("use_llm", False)),
            adaptive_max_queries=int(adaptive.get("max_queries", 3)),
            adaptive_per_plan_top_k=adaptive.get("per_plan_top_k"),
            normalizer_retries=int(adaptive.get("normalizer_retries", 0)),
            **runtime.chat_options,
        )

        self._running("analysis")
        prepared = assistant.prepare_question(options.question)
        analysis = prepared.analysis
        would_trigger = should_use_adaptive(analysis)
        self._adaptive = runtime.adaptive_enabled and would_trigger
        self._done(
            "analysis",
            {
                "standalone_question": prepared.standalone_question,
                "risk_flags": list(analysis.risk_flags),
                "law_names": list(analysis.law_names),
                "article_numbers": list(analysis.article_numbers),
                "case_type_hints": list(analysis.case_type_hints),
                "complexity_flags": list(analysis.complexity_flags),
                "confidence": analysis.confidence,
                "adaptive": {
                    "enabled": runtime.adaptive_enabled,
                    "triggered": self._adaptive,
                    "would_trigger": would_trigger,
                    "reasons": list(analysis.adaptive_reasons),
                },
            },
        )

        retrieved = assistant.retrieve_turn(prepared)
        self._finish_retrieval(retrieved)
        generated = self._generate(assistant, retrieved)
        verified = self._verify(assistant, generated)
        assistant.commit_turn(verified)
        return self._final(retrieved, generated, verified)

    # -- live retrieval events -------------------------------------------
    #
    # Direct retrieval makes exactly one top-level retriever call before the
    # evidence check, so call 0 is the retrieval and any later call is a
    # follow-up the check asked for.  Adaptive retrieval first runs one call
    # per plan; those calls are told apart only after the turn returns.

    def _on_outer_call(self, event: str, call: RetrieveCall) -> None:
        if event == "start" and call.index == 0:
            self._running("retrieval", {"query": call.query, "top_k": call.top_k})
            return
        if self._adaptive:
            return
        if event == "end" and call.index == 0:
            if self.options.rerank:
                rerank_calls = self._rerank_calls_within([call])
                if rerank_calls:
                    self._done(
                        "rerank",
                        self._rerank_detail(call.results, rerank_calls),
                        elapsed_ms=sum(item.elapsed_ms for item in rerank_calls),
                    )
                else:
                    self._skipped("rerank", "检索没有候选，未调用重排")
            self._running("evidence")
        elif event == "start" and call.index == 1:
            self._done(
                "evidence",
                {
                    "sufficient": False,
                    "followup_triggered": True,
                    "first_followup_query": call.query,
                },
            )
            self._running("followup", {"query": call.query})

    def _on_base_call(self, event: str, call: RetrieveCall) -> None:
        if self._adaptive or event != "end" or call.index != 0:
            return
        self._done(
            "retrieval", self._retrieval_detail([call]), elapsed_ms=call.elapsed_ms
        )
        if not self.options.rerank:
            self._skipped("rerank", "重排开关关闭")

    def _on_rerank_call(self, event: str, call: RerankCall) -> None:
        outer_calls = self._outer_tap.calls if self._outer_tap is not None else []
        if self._adaptive or event != "start" or len(outer_calls) != 1:
            return
        self._running(
            "rerank", {"model": self._reranker_name(), "documents": call.documents}
        )

    def _finish_retrieval(self, retrieved: RetrievedTurn) -> None:
        outer_calls = self._outer_tap.calls if self._outer_tap is not None else []
        if not outer_calls:
            reason = (
                "风险标记触发检索前拒答"
                if retrieved.terminal_kind == "pre_retrieval_refusal"
                else "本轮没有调用检索器"
            )
            for node in ("retrieval", "rerank", "evidence", "followup"):
                self._skipped(node, reason)
            return
        if self._adaptive:
            self._finish_adaptive_retrieval(retrieved, outer_calls)
        elif self.status.get("followup") == "running":
            self._done("followup", self._followup_detail(retrieved, outer_calls[1:]))
        else:
            self._done("evidence", evidence_detail(retrieved))
            self._skip_followup(retrieved)

    def _finish_adaptive_retrieval(
        self, retrieved: RetrievedTurn, outer_calls: list[RetrieveCall]
    ) -> None:
        adaptive = retrieved.adaptive_result
        plan_count = (
            max(1, len(adaptive.plans))
            if adaptive is not None and adaptive.adaptive_used
            else 1
        )
        primary, followups = outer_calls[:plan_count], outer_calls[plan_count:]
        base_calls = [
            call
            for call in (self._base_tap.calls if self._base_tap is not None else [])
            if any(call.ran_within(outer) for outer in primary)
        ]
        detail = self._retrieval_detail(base_calls)
        if adaptive is not None:
            detail["adaptive"] = {
                "used": adaptive.adaptive_used,
                "plans": [plan.to_dict() for plan in adaptive.plans],
                "merge": adaptive.merge_trace,
            }
        self._done(
            "retrieval",
            detail,
            elapsed_ms=sum(call.elapsed_ms for call in base_calls),
        )
        if not self.options.rerank:
            self._skipped("rerank", "重排开关关闭")
        else:
            rerank_calls = self._rerank_calls_within(primary)
            if rerank_calls:
                self._done(
                    "rerank",
                    self._rerank_detail(
                        [result for call in primary for result in call.results],
                        rerank_calls,
                    ),
                    elapsed_ms=sum(call.elapsed_ms for call in rerank_calls),
                )
            else:
                self._skipped("rerank", "检索没有候选，未调用重排")
        evidence_started = primary[-1].finished or time.perf_counter()
        if followups:
            self._done(
                "evidence",
                {
                    "sufficient": False,
                    "followup_triggered": True,
                    "first_followup_query": followups[0].query,
                },
                elapsed_ms=(followups[0].started - evidence_started) * 1000,
            )
            self._done(
                "followup",
                self._followup_detail(retrieved, followups),
                elapsed_ms=(time.perf_counter() - followups[0].started) * 1000,
            )
        else:
            self._done(
                "evidence",
                evidence_detail(retrieved),
                elapsed_ms=(time.perf_counter() - evidence_started) * 1000,
            )
            self._skip_followup(retrieved)

    def _skip_followup(self, retrieved: RetrievedTurn) -> None:
        adaptive = retrieved.adaptive_result
        stop_reason = adaptive.followup_trace.get("stop_reason") if adaptive else None
        self._skipped(
            "followup",
            FOLLOWUP_SKIP_REASONS.get(stop_reason, "未触发补充检索"),
            {"stop_reason": stop_reason},
        )

    def _rerank_calls_within(self, outer_calls: list[RetrieveCall]) -> list[RerankCall]:
        if self._rerank_tap is None:
            return []
        return [
            call
            for call in self._rerank_tap.calls
            if any(call.ran_within(outer) for outer in outer_calls)
        ]

    # -- node details ----------------------------------------------------

    def _retrieval_detail(self, calls: list[RetrieveCall]) -> dict[str, Any]:
        results = [result for call in calls for result in call.results]
        slot = self.runtime.retrievers[self.options.retriever]
        detail: dict[str, Any] = {
            "retriever": slot.key,
            "retriever_name": getattr(slot.retriever, "name", slot.key),
            "label": slot.label,
            "queries": [call.query for call in calls],
            "requested_top_k": calls[0].top_k if calls else None,
            "candidate_count": len(results),
            "exact_pin": exact_pin_summary(slot.key, results),
        }
        hints = parsed_citation_hints(results)
        if hints is not None:
            detail["parsed_citations"] = hints
        if slot.uses_embedding and self.runtime.embedding is not None:
            detail["embedding_model"] = self.runtime.embedding.name
        detail["top_candidates"] = [candidate_row(result) for result in results[:10]]
        return detail

    def _rerank_detail(
        self, results: list[SearchResult], rerank_calls: list[RerankCall]
    ) -> dict[str, Any]:
        info = self.runtime.reranker_info
        return {
            "model": self._reranker_name(),
            "provider": info.provider if info is not None else None,
            "calls": len(rerank_calls),
            "candidate_count": sum(call.documents for call in rerank_calls),
            "returned": len(results),
            "promoted_from_outside_top_k": sum(
                1
                for result in results
                if isinstance(result.trace.get("base_rank"), int)
                and result.trace["base_rank"] > self.options.top_k
            ),
            "ranking": [
                {
                    "rank": result.rank,
                    "base_rank": result.trace.get("base_rank"),
                    "label": source_label(result),
                    "base_score": result.trace.get("base_score"),
                    "rerank_score": result.trace.get("rerank_score", result.score),
                }
                for result in results
            ],
        }

    def _followup_detail(
        self, retrieved: RetrievedTurn, calls: list[RetrieveCall]
    ) -> dict[str, Any]:
        adaptive = retrieved.adaptive_result
        trace = adaptive.followup_trace if adaptive is not None else {}
        return {
            "rounds_used": trace.get("rounds_used"),
            "stop_reason": trace.get("stop_reason"),
            "queries": trace.get("queries", [call.query for call in calls]),
            "retriever_calls": len(calls),
            "evidence_after_followup": evidence_detail(retrieved),
        }

    def _reranker_name(self) -> str | None:
        info = self.runtime.reranker_info
        if info is not None:
            return info.name
        return self._rerank_tap.name if self._rerank_tap is not None else None

    # -- generation and verification -------------------------------------

    def _generate(
        self, assistant: LegalChatAssistant, retrieved: RetrievedTurn
    ) -> GeneratedTurn:
        generator = self.runtime.generator
        if retrieved.terminal_answer is not None:
            self._skipped(
                "generation",
                TERMINAL_GENERATION_REASONS.get(
                    retrieved.terminal_kind or "",
                    f"{retrieved.terminal_kind}：不调用生成模型",
                ),
                {"terminal_kind": retrieved.terminal_kind},
            )
        elif not self.options.generate:
            self._skipped("generation", "生成开关关闭，只返回检索结果")
        else:
            self._running(
                "generation", {"model": generator.name, "provider": generator.provider}
            )
        before = usage_snapshot(self._client)
        generated = assistant.generate_turn(retrieved, generate=self.options.generate)
        if self.status.get("generation") != "running":
            return generated
        detail = self._generation_detail(
            generated, usage_delta(before, usage_snapshot(self._client))
        )
        if generated.kind == "generation_error":
            last_error = self._client.last_error if self._client is not None else None
            error = (
                describe_error(last_error)
                if last_error is not None
                else {"kind": "provider", "message": "生成模型调用失败，未拿到具体错误"}
            )
            self._failed("generation", error, detail)
            self.errors.append({**error, "nodes": ["generation"], "fatal": False})
            self._emit("error", {"run_id": self.run_id, **self.errors[-1]})
        else:
            self._done("generation", detail)
        return generated

    def _generation_detail(
        self, generated: GeneratedTurn, usage: dict[str, Any]
    ) -> dict[str, Any]:
        generator = self.runtime.generator
        calls = self._client.calls if self._client is not None else []
        detail: dict[str, Any] = {
            "model": generator.name,
            "provider": generator.provider,
            "kind": generated.kind,
            "llm_calls": usage.get("calls"),
            "failed_calls": usage.get("failed_calls"),
        }
        if calls:
            detail["prompt_chars"] = calls[-1].prompt_chars
            detail["model_call_ms"] = round(calls[-1].elapsed_ms, 1)
        if usage.get("token_usage_calls"):
            detail["prompt_tokens"] = usage.get("input_tokens")
            detail["completion_tokens"] = usage.get("output_tokens")
            detail["total_tokens"] = usage.get("total_tokens")
        else:
            detail["tokens_reported"] = False
        answer = generated.answer
        if answer is not None:
            detail["answer_mode"] = answer.answer_mode
            detail["schema_valid"] = answer.schema_valid
            detail["adapter_source"] = answer.adapter_source
            detail["claim_count"] = len(answer.claims)
            if answer.parse_errors:
                detail["parse_errors"] = list(answer.parse_errors)
        return detail

    def _verify(
        self, assistant: LegalChatAssistant, generated: GeneratedTurn
    ) -> VerifiedTurn:
        if generated.kind == "retrieval_only":
            verified = assistant.verify_turn(generated)
            self._skipped("verification", "未生成回答，没有需要校验的引用")
            return verified
        self._running("verification")
        verified = assistant.verify_turn(generated)
        draft_check = verified.pre_fallback_verification
        final_check = verified.verification
        checked = draft_check if draft_check is not None else final_check
        detail: dict[str, Any] = {
            "passed": checked.passed if checked is not None else None,
            "replaced_with_fallback": draft_check is not None,
            "failure_reasons": list(checked.failure_reasons) if checked else [],
        }
        if generated.kind != "model":
            detail["note"] = "程序化回答，没有调用生成模型"
        if draft_check is not None:
            detail["draft_checks"] = draft_check.to_dict()
        if final_check is not None:
            detail["final_answer_mode"] = final_check.actual_answer_mode
            detail["final_checks"] = final_check.to_dict()
        self._done("verification", detail)
        return verified

    # -- final payload and run record ------------------------------------

    def _final(
        self,
        retrieved: RetrievedTurn,
        generated: GeneratedTurn,
        verified: VerifiedTurn,
    ) -> dict[str, Any]:
        draft_answer = verified.pre_fallback_answer
        draft_check = verified.pre_fallback_verification
        final_answer = verified.final_answer
        draft = None
        if draft_check is not None and draft_answer is not None:
            draft = {
                "text": draft_answer.answer_text,
                "verified": False,
                "label": "未通过校验的原始草稿",
                "failure_reasons": list(draft_check.failure_reasons),
            }
        return {
            "run_id": self.run_id,
            "question": self.options.question,
            "answer": {
                "text": verified.answer_text,
                "kind": generated.kind,
                "mode": final_answer.answer_mode if final_answer else "retrieval_only",
            },
            "replaced_by_verifier": draft_check is not None,
            "draft": draft,
            "sources": [source_card(result) for result in retrieved.results],
            "models": self._models(),
            "usage": self._usage(),
            "timings": self._timings(),
            "nodes": dict(self.status),
            "errors": list(self.errors),
        }

    def _models(self) -> dict[str, Any]:
        runtime = self.runtime
        slot = runtime.retrievers[self.options.retriever]
        retrieved_any = bool(self._outer_tap is not None and self._outer_tap.calls)
        embedding = None
        if runtime.embedding is not None:
            embedding = {
                **runtime.embedding.to_dict(),
                "used": slot.uses_embedding and retrieved_any,
            }
        reranker = None
        if runtime.reranker_info is not None:
            reranker = {
                **runtime.reranker_info.to_dict(),
                "used": bool(self._rerank_tap is not None and self._rerank_tap.calls),
            }
        return {
            "retriever": {
                "key": slot.key,
                "name": getattr(slot.retriever, "name", slot.key),
                "label": slot.label,
                "used": retrieved_any,
            },
            "embedding": embedding,
            "reranker": reranker,
            "generator": {
                **runtime.generator.to_dict(),
                "used": bool(self._client is not None and self._client.calls),
            },
        }

    def _usage(self) -> dict[str, Any]:
        usage: dict[str, Any] = {
            "llm": usage_snapshot(self._client) if self._client is not None else None
        }
        if self._reranking is not None:
            usage["rerank"] = retriever_runtime_metrics(self._reranking)
        return usage

    def _timings(self) -> dict[str, Any]:
        return {
            "nodes": dict(self.elapsed),
            "total_ms": self._now_ms(),
            "first_results_ms": (
                self._at_ms.get("retrieval")
                if self.status.get("retrieval") == "done"
                else None
            ),
        }

    def _record(
        self, final: dict[str, Any] | None, fatal: dict[str, Any] | None
    ) -> dict[str, Any]:
        return {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "question": self.options.question,
            "options": asdict(self.options),
            "status": "failed" if fatal is not None else "succeeded",
            "nodes": {
                node: {
                    "status": self.status.get(node),
                    "elapsed_ms": self.elapsed.get(node),
                }
                for node, _label in NODES
            },
            "timings": self._timings(),
            "errors": self.errors,
            "models": final["models"] if final else self._models(),
            "usage": final["usage"] if final else self._usage(),
            "answer": final["answer"] if final else None,
            "replaced_by_verifier": bool(final and final["replaced_by_verifier"]),
            "draft": final["draft"] if final else None,
            "sources": [
                {
                    "id": card["id"],
                    "chunk_id": card["chunk_id"],
                    "label": card["label"],
                    "score": card["score"],
                    "base_rank": card.get("base_rank"),
                }
                for card in (final["sources"] if final else [])
            ],
        }

    def _write_record(self, record: dict[str, Any]) -> None:
        path = self.runtime.runs_path
        line = json.dumps(record, ensure_ascii=False, default=json_default)
        try:
            with _RUNS_LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError as exc:
            LOGGER.warning("could not append the web run record to %s: %s", path, exc)

    # -- event helpers ---------------------------------------------------

    def _now_ms(self) -> float:
        return round((time.perf_counter() - self._started) * 1000, 1)

    def _since(self, node: str) -> float:
        started = self._node_started.get(node)
        return (time.perf_counter() - started) * 1000 if started is not None else 0.0

    def _node(
        self, node: str, status: str, elapsed_ms: float, detail: dict[str, Any]
    ) -> None:
        self.status[node] = status
        if status in {"done", "failed"}:
            self.elapsed[node] = round(elapsed_ms, 1)
        at_ms = self._now_ms()
        self._at_ms[node] = at_ms
        self._emit(
            "node",
            {
                "run_id": self.run_id,
                "node": node,
                "label": NODE_LABELS[node],
                "status": status,
                "elapsed_ms": round(elapsed_ms, 1),
                "at_ms": at_ms,
                "detail": detail,
            },
        )

    def _running(self, node: str, detail: dict[str, Any] | None = None) -> None:
        self._node_started[node] = time.perf_counter()
        self._node(node, "running", 0.0, detail or {})

    def _done(
        self,
        node: str,
        detail: dict[str, Any],
        *,
        elapsed_ms: float | None = None,
    ) -> None:
        if elapsed_ms is None:
            elapsed_ms = self._since(node)
        self._node(node, "done", elapsed_ms, detail)

    def _skipped(
        self, node: str, reason: str, detail: dict[str, Any] | None = None
    ) -> None:
        self._node(node, "skipped", 0.0, {"reason": reason, **(detail or {})})

    def _failed(
        self,
        node: str,
        error: dict[str, Any],
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._node(
            node, "failed", self._since(node), {**(detail or {}), "error": error}
        )


def evidence_detail(retrieved: RetrievedTurn) -> dict[str, Any]:
    check = retrieved.evidence_check
    if check is None:
        return {}
    return {
        **check.to_dict(),
        "missing": [
            *check.missing_law_support,
            *check.missing_facts,
            *check.low_coverage,
        ],
    }


def source_label(result: SearchResult) -> str:
    chunk = result.chunk
    law = "、".join(chunk.law_names) or "未知法律"
    article = "、".join(chunk.article_numbers)
    return f"{law} {article}".strip()


def has_exact_pin(trace: dict[str, Any]) -> bool:
    """True when BM25 pinned this chunk as an exact (law, article) match."""

    if trace.get("exact_citation_boost"):
        return True
    bm25_trace = trace.get("bm25_trace")
    return isinstance(bm25_trace, dict) and bool(
        bm25_trace.get("exact_citation_boost")
    )


def parsed_citation_hints(results: list[SearchResult]) -> dict[str, Any] | None:
    """Law and article hints BM25 resolved against the corpus, if any ran."""

    for result in results:
        trace = result.trace.get("bm25_trace", result.trace)
        if not isinstance(trace, dict) or "law_hints" not in trace:
            continue
        hints = {
            "law_hints": list(trace.get("law_hints", [])),
            "article_hints": list(trace.get("article_hints", [])),
        }
        if trace.get("unresolved_law_titles"):
            hints["unresolved_law_titles"] = list(trace["unresolved_law_titles"])
        return hints
    return None


def exact_pin_summary(kind: str, results: list[SearchResult]) -> dict[str, Any]:
    if kind == "dense":
        return {"supported": False, "applied": False, "note": "向量检索不做精确条号置顶"}
    pinned = []
    for result in results:
        if not has_exact_pin(result.trace):
            continue
        item: dict[str, Any] = {"rank": result.rank, "label": source_label(result)}
        if "bm25_rank" in result.trace:
            item["bm25_rank"] = result.trace["bm25_rank"]
        pinned.append(item)
    summary: dict[str, Any] = {"supported": True, "applied": bool(pinned)}
    if pinned:
        summary["pinned"] = pinned
    else:
        hints = parsed_citation_hints(results) or {}
        summary["note"] = (
            "解析出条号，但没有法律名加条号的精确匹配"
            if hints.get("article_hints")
            else "问题没有同时给出法律名和条号"
        )
    return summary


def candidate_row(result: SearchResult) -> dict[str, Any]:
    row: dict[str, Any] = {
        "rank": result.rank,
        "label": source_label(result),
        "score": result.score,
    }
    for key in ("bm25_rank", "dense_rank"):
        if key in result.trace:
            row[key] = result.trace[key]
    if has_exact_pin(result.trace):
        row["exact_pin"] = True
    return row


def source_card(result: SearchResult) -> dict[str, Any]:
    chunk = result.chunk
    card: dict[str, Any] = {
        "id": f"S{result.rank}",
        "rank": result.rank,
        "chunk_id": chunk.chunk_id,
        "label": source_label(result),
        "law_names": list(chunk.law_names),
        "article_numbers": list(chunk.article_numbers),
        "score": result.score,
        "retriever": result.retriever,
        "exact_pin": has_exact_pin(result.trace),
        "source_files": list(chunk.source_files),
        "line_nos": list(chunk.line_nos),
        "text": chunk.text,
    }
    for key in SOURCE_TRACE_FIELDS:
        if key in result.trace:
            card[key] = result.trace[key]
    return card


def json_default(value: Any) -> Any:
    """Serialize NumPy scalars and other stray values without failing a stream."""

    item = getattr(value, "item", None)
    if callable(item):
        return item()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)
