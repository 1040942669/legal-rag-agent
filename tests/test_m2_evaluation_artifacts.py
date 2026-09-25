from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

import pytest

import legal_rag.evaluation as evaluation_module
from legal_rag.chat import LegalChatAssistant
from legal_rag.evaluation import evaluate, evaluate_case
from legal_rag.evaluation_artifacts import (
    eval_case_from_artifact,
    eval_case_to_artifact,
    eval_record_from_artifact,
    eval_record_to_artifact,
    search_result_from_artifact,
    search_result_to_artifact,
)
from legal_rag.models import Chunk, EvalCase, SearchResult
from legal_rag.retrieval_contracts import (
    RetrievedArticleProvenance,
    RetrievalBoundary,
    RetrievalProvenance,
    chunk_payload_fingerprint,
)


class _EmptyRetriever:
    name = "empty-fixture"

    def retrieve(self, query: str, top_k: int = 5):
        return []


class _ListTraceWriter:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)


class _SuccessfulJudge:
    def complete(self, prompt: str) -> str:
        return (
            '{"faithfulness":0.9,"relevance":0.8,"completeness":0.7,'
            '"passed":true,"comment":"fixture"}'
        )


class _FailingAssistant:
    llm = None

    def answer(self, question: str, *, generate: bool = True):
        raise RuntimeError("synthetic service failure")


def _case(
    case_id: str = "case-a",
    *,
    session_group: str | None = None,
    turn_index: int = 0,
) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        question=f"问题-{case_id}",
        case_type="synthetic",
        expected_law="合成规则",
        expected_articles=["第一条"],
        keywords=["合成"],
        expected_behavior="evidence_answer",
        session_group=session_group,
        turn_index=turn_index,
        schema_version=1,
    )


def _evaluated_record():
    return evaluate_case(
        case=_case(),
        retriever=_EmptyRetriever(),
        chunk_strategy="synthetic",
    ).record


def _bound_search_result() -> SearchResult:
    boundary = RetrievalBoundary(
        scope_id="scope-a",
        snapshot_id="snapshot-a",
        profile_id="a" * 64,
        law_ids=("law-a",),
        version_ids=("version-a",),
        article_ids=("article-a",),
        article_numbers=("第一条",),
        effective_on="2025-01-15",
    )
    article = RetrievedArticleProvenance(
        article_id="article-a",
        law_id="law-a",
        version_id="version-a",
        article_number="第一条",
        title="虚构测试法",
        valid_from="2025-01-01",
        valid_to="2026-01-01",
        source_ref="fixtures/bound.txt",
        source_line=7,
        verification_status="verified",
    )
    chunk = Chunk(
        chunk_id="bound-chunk-a",
        text="受不可变检索边界约束的完整证据",
        law_names=[article.title],
        article_numbers=[article.article_number],
        source_files=[article.source_ref],
        line_nos=[article.source_line],
        strategy="article",
        metadata={
            "scope_id": boundary.scope_id,
            "snapshot_id": boundary.snapshot_id,
            "profile_id": boundary.profile_id,
            "access_scope_ids": [boundary.scope_id],
            "law_ids": [article.law_id],
            "version_ids": [article.version_id],
            "article_ids": [article.article_id],
            "article_refs": [article.to_metadata()],
            "boundary_fingerprint": boundary.fingerprint,
        },
    )
    provenance = RetrievalProvenance(
        boundary=boundary,
        scope_id=boundary.scope_id,
        snapshot_id=boundary.snapshot_id,
        profile_id=boundary.profile_id,
        chunk_id=chunk.chunk_id,
        chunk_content_hash="c" * 64,
        chunk_payload_hash=chunk_payload_fingerprint(chunk),
        snapshot_ordinal=3,
        embedding_hash="e" * 64,
        articles=(article,),
    )
    return SearchResult(
        chunk=chunk,
        score=0.99,
        rank=1,
        retriever="postgres-exact",
        trace={"boundary_fingerprint": boundary.fingerprint},
        provenance=provenance,
    )


def test_evaluate_case_matches_legacy_batch_record_and_trace(monkeypatch) -> None:
    monkeypatch.setattr(evaluation_module.time, "perf_counter", lambda: 1.0)
    case = _case()
    writer = _ListTraceWriter()

    legacy = evaluate(
        cases=[case],
        retriever=_EmptyRetriever(),
        chunk_strategy="synthetic",
        trace_writer=writer,
    )
    single = evaluate_case(
        case=case,
        retriever=_EmptyRetriever(),
        chunk_strategy="synthetic",
    )

    assert len(legacy) == 1
    assert asdict(single.record) == asdict(legacy[0])
    assert single.trace_record == writer.records[0]


def test_evaluate_case_preserves_caller_owned_session_memory() -> None:
    retriever = _EmptyRetriever()
    assistant = LegalChatAssistant(
        retriever,
        model="deterministic-offline-fixture",
    )

    evaluate_case(
        case=_case("turn-0", session_group="session-a", turn_index=0),
        retriever=retriever,
        chunk_strategy="synthetic",
        model="deterministic-offline-fixture",
        generate=True,
        assistant=assistant,
    )
    evaluate_case(
        case=_case("turn-1", session_group="session-a", turn_index=1),
        retriever=retriever,
        chunk_strategy="synthetic",
        model="deterministic-offline-fixture",
        generate=True,
        assistant=assistant,
    )

    assert [role for role, _ in assistant.memory.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert assistant.memory.messages[0][1] == "问题-turn-0"
    assert assistant.memory.messages[2][1] == "问题-turn-1"


def test_eval_case_artifact_round_trip_is_strict_and_defensive() -> None:
    case = _case("turn-1", session_group="session-a", turn_index=1)

    artifact = eval_case_to_artifact(case)
    restored = eval_case_from_artifact(artifact)
    artifact["case"]["question"] = "caller mutation"

    assert restored == case
    assert case.question == "问题-turn-1"


def test_search_result_artifact_preserves_complete_evidence() -> None:
    result = SearchResult(
        chunk=Chunk(
            chunk_id="chunk-1",
            text="完整的合成证据正文",
            law_names=["合成规则"],
            article_numbers=["第一条"],
            source_files=["synthetic.txt"],
            line_nos=[7],
            strategy="article",
            metadata={"snapshot_id": "fixture-v1", "scope_ids": ["public"]},
        ),
        score=0.875,
        rank=1,
        retriever="synthetic",
        trace={"bm25_rank": 1, "features": {"article_boost": 0.2}},
    )

    artifact = search_result_to_artifact(result)
    restored = search_result_from_artifact(artifact)
    artifact["search_result"]["chunk"]["text"] = "caller mutation"
    artifact["search_result"]["trace"]["bm25_rank"] = 99

    assert restored == result
    assert result.chunk.text == "完整的合成证据正文"
    assert result.trace["bm25_rank"] == 1


def test_bound_search_result_artifact_round_trip_preserves_typed_provenance() -> None:
    result = _bound_search_result()

    artifact = search_result_to_artifact(result)
    restored = search_result_from_artifact(artifact)

    assert artifact["artifact_schema_version"] == 1
    assert artifact["search_result"]["provenance"]["boundary"]["effective_on"] == (
        "2025-01-15"
    )
    assert (
        artifact["search_result"]["provenance"]["articles"][0]["valid_from"]
        == "2025-01-01"
    )
    assert restored == result
    assert isinstance(restored.provenance, RetrievalProvenance)
    assert isinstance(restored.provenance.boundary, RetrievalBoundary)
    assert isinstance(restored.provenance.articles[0], RetrievedArticleProvenance)


def test_legacy_unbound_search_result_artifact_without_provenance_is_accepted() -> None:
    artifact = search_result_to_artifact(
        SearchResult(
            chunk=Chunk(
                chunk_id="legacy-unbound",
                text="legacy evidence",
                law_names=[],
                article_numbers=[],
                source_files=["legacy.txt"],
                line_nos=[1],
                strategy="article",
                metadata={},
            ),
            score=1.0,
            rank=1,
            retriever="legacy",
            trace={},
        )
    )
    artifact["search_result"].pop("provenance")

    restored = search_result_from_artifact(artifact)

    assert restored.provenance is None
    assert restored.chunk.chunk_id == "legacy-unbound"


@pytest.mark.parametrize("marker_location", ["metadata", "trace"])
def test_legacy_bound_marker_without_provenance_is_rejected(
    marker_location: str,
) -> None:
    artifact = search_result_to_artifact(_bound_search_result())
    artifact["search_result"].pop("provenance")
    if marker_location == "metadata":
        artifact["search_result"]["trace"].pop("boundary_fingerprint")
    else:
        artifact["search_result"]["chunk"]["metadata"].pop("boundary_fingerprint")

    with pytest.raises(ValueError, match="requires complete typed provenance"):
        search_result_from_artifact(artifact)


def test_scope_bound_artifact_cannot_hide_by_removing_only_fingerprints() -> None:
    artifact = search_result_to_artifact(_bound_search_result())
    artifact["search_result"].pop("provenance")
    artifact["search_result"]["chunk"]["metadata"].pop("boundary_fingerprint")
    artifact["search_result"]["trace"].pop("boundary_fingerprint")

    with pytest.raises(ValueError, match="requires complete typed provenance"):
        search_result_from_artifact(artifact)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda artifact: artifact["search_result"]["chunk"].update(
            {"text": "tampered evidence"}
        ),
        lambda artifact: artifact["search_result"]["trace"].update(
            {"boundary_fingerprint": "f" * 64}
        ),
        lambda artifact: artifact["search_result"]["provenance"]["boundary"].update(
            {"scope_id": "scope-b"}
        ),
        lambda artifact: artifact["search_result"]["provenance"]["articles"][0].update(
            {"article_id": "article-b"}
        ),
        lambda artifact: artifact["search_result"]["provenance"]["articles"][0].update(
            {"unexpected": True}
        ),
    ],
)
def test_bound_search_result_artifact_tampering_is_rejected(mutate) -> None:
    artifact = search_result_to_artifact(_bound_search_result())
    mutate(artifact)

    with pytest.raises(ValueError):
        search_result_from_artifact(artifact)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda artifact: artifact.update({"unexpected": True}),
        lambda artifact: artifact.update({"artifact_schema_version": True}),
        lambda artifact: artifact["search_result"].update({"rank": True}),
        lambda artifact: artifact["search_result"].update({"score": float("nan")}),
        lambda artifact: artifact["search_result"]["chunk"].update({"text": "\ud800"}),
        lambda artifact: artifact["search_result"]["chunk"].pop("metadata"),
    ],
)
def test_invalid_search_result_artifact_is_rejected(mutate) -> None:
    artifact = search_result_to_artifact(
        SearchResult(
            chunk=Chunk(
                chunk_id="chunk-1",
                text="evidence",
                law_names=[],
                article_numbers=[],
                source_files=["fixture.txt"],
                line_nos=[1],
                strategy="article",
                metadata={},
            ),
            score=1.0,
            rank=1,
            retriever="fixture",
            trace={},
        )
    )
    mutate(artifact)

    with pytest.raises(ValueError):
        search_result_from_artifact(artifact)


def test_eval_record_artifact_round_trip_preserves_canonical_na() -> None:
    record = _evaluated_record()

    artifact = eval_record_to_artifact(record)
    restored = eval_record_from_artifact(artifact)
    artifact["evaluation_record"]["canonical_metrics"]["answer_text"][
        "unavailable_reason"
    ] = "caller mutation"

    assert asdict(restored) == asdict(record)
    assert (
        record.canonical_metrics["answer_text"]["unavailable_reason"]
        == "retrieval_only"
    )


def test_eval_record_artifact_accepts_programmatic_terminal_and_judge_success() -> None:
    retriever = _EmptyRetriever()
    assistant = LegalChatAssistant(
        retriever,
        model="deterministic-offline-fixture",
    )
    record = evaluate_case(
        case=_case(),
        retriever=retriever,
        chunk_strategy="synthetic",
        model="deterministic-offline-fixture",
        generate=True,
        assistant=assistant,
        judge_client=_SuccessfulJudge(),
    ).record

    restored = eval_record_from_artifact(eval_record_to_artifact(record))

    assert asdict(restored) == asdict(record)
    assert restored.execution["generation"]["status"] == "not_run"
    assert restored.execution["judge"]["status"] == "succeeded"


def test_eval_record_artifact_accepts_explicit_service_failure() -> None:
    record = evaluate_case(
        case=_case(),
        retriever=_EmptyRetriever(),
        chunk_strategy="synthetic",
        model="deterministic-offline-fixture",
        generate=True,
        assistant=_FailingAssistant(),
    ).record

    restored = eval_record_from_artifact(eval_record_to_artifact(record))

    assert asdict(restored) == asdict(record)
    assert restored.execution["service"]["status"] == "error"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda artifact: artifact["evaluation_record"].update({"unexpected": True}),
        lambda artifact: artifact["evaluation_record"].pop("case_id"),
        lambda artifact: artifact["evaluation_record"].update({"latency_ms": True}),
        lambda artifact: artifact["evaluation_record"].update(
            {"metrics_schema_version": 1}
        ),
        lambda artifact: artifact["evaluation_record"].update({"hit_at_5": 2}),
        lambda artifact: artifact["evaluation_record"]["canonical_metrics"][
            "hit_at_5"
        ].update({"value": 1}),
        lambda artifact: artifact["evaluation_record"]["canonical_metrics"][
            "hit_at_5"
        ].update({"value": "one"}),
        lambda artifact: artifact["evaluation_record"]["canonical_metrics"][
            "mrr"
        ].update({"value": 9.0}),
        lambda artifact: artifact["evaluation_record"]["execution"]["service"].update(
            {"status": "degraded", "reason": None}
        ),
        lambda artifact: artifact["evaluation_record"]["execution"]["service"].update(
            {"status": "error", "reason": "   "}
        ),
        lambda artifact: artifact["evaluation_record"]["execution"]["judge"].update(
            {"status": "degraded", "reason": "provider_error"}
        ),
        lambda artifact: artifact["evaluation_record"]["execution"]["judge"].update(
            {"status": "succeeded", "reason": None}
        ),
        lambda artifact: artifact["evaluation_record"]["canonical_metrics"].pop("mrr"),
        lambda artifact: artifact["evaluation_record"]["canonical_metrics"][
            "semantic_support_status"
        ].update({"value": "definitely_supported", "unavailable_reason": None}),
        lambda artifact: artifact["evaluation_record"]["canonical_metrics"][
            "hit_at_5"
        ].update({"value": None, "unavailable_reason": None}),
        lambda artifact: artifact["evaluation_record"].update(
            {"generation_attempt": {"attempted": True}}
        ),
        lambda artifact: artifact["evaluation_record"].update({"llm_failed_calls": 10}),
        lambda artifact: artifact["evaluation_record"].update(
            {
                "assistant_llm_calls": 1,
                "token_usage_calls": 1,
                "input_tokens": 3,
                "output_tokens": 4,
                "total_tokens": 1,
            }
        ),
        lambda artifact: artifact["evaluation_record"].update({"answer": "\ud800"}),
        lambda artifact: artifact["evaluation_record"].update(
            {"llm_latency_ms": float("inf")}
        ),
    ],
)
def test_invalid_eval_record_artifact_is_rejected(mutate) -> None:
    artifact = deepcopy(eval_record_to_artifact(_evaluated_record()))
    mutate(artifact)

    with pytest.raises(ValueError):
        eval_record_from_artifact(artifact)
