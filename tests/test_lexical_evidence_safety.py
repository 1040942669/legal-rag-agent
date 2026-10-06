"""Synthetic-only evidence checks; no evaluation gold or live provider data."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from legal_rag.chat import (
    LEGAL_DISCLAIMER,
    LegalChatAssistant as _LegalChatAssistant,
    build_limited_structured_answer,
)
from legal_rag.evidence import (
    HISTORICAL_EVIDENCE_RULES_VERSION,
    check_evidence_sufficiency as _check_evidence_sufficiency,
)
from legal_rag.models import (
    AnswerClaim,
    Chunk,
    EvidenceCheck,
    SearchResult,
    StructuredAnswer,
    VerificationContext,
)
from legal_rag.retrieval import BM25Retriever, lexical_expansion_terms, tokenize
from legal_rag.retrieval_contracts import (
    RetrievalBoundary,
    RetrievedArticleProvenance,
    RetrievalProvenance,
)
from legal_rag.verifier import verify_answer


def check_evidence_sufficiency(query, results, **kwargs):
    """Pin the original candidate contract; modern regressions live separately."""
    return _check_evidence_sufficiency(query, results,
        rules_version=HISTORICAL_EVIDENCE_RULES_VERSION, **kwargs)


def LegalChatAssistant(*args, **kwargs):
    """Explicit historical fake replay, never the modern API execution policy."""
    return _LegalChatAssistant(*args, evidence_rules_version=HISTORICAL_EVIDENCE_RULES_VERSION, **kwargs)


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "false")
    monkeypatch.setenv("LEGAL_RAG_DISABLE_DOTENV", "true")


def evidence(
    law: str = "合成甲法",
    article: str = "第十条",
    *,
    rank: int = 1,
    score: object = 100.0,
    scope: str = "public",
) -> SearchResult:
    return SearchResult(
        chunk=Chunk(
            chunk_id=f"synthetic-{rank}",
            text="合成主体应当保存合成事项的记录。",
            law_names=[law],
            article_numbers=[article],
            source_files=["synthetic.txt"],
            line_nos=[rank],
            strategy="article",
            metadata={"snapshot_id": "synthetic-snapshot", "scope_id": scope},
        ),
        score=score,  # type: ignore[arg-type]
        rank=rank,
        retriever="bm25",
    )


class FixedRetriever:
    name = "bm25"

    def __init__(self, rows: list[SearchResult]):
        self.rows = rows
        self.calls = 0

    def retrieve(self, query: str, top_k: int = 5):
        self.calls += 1
        return self.rows[:top_k]


class FakeLimitedClient:
    def __init__(self):
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        limited = build_limited_structured_answer(EvidenceCheck(
            sufficient=False,
            missing_facts=[],
            missing_law_support=["synthetic_missing"],
            low_coverage=[],
            followup_queries=[],
            stop_reason="needs_followup",
            checked_result_count=1,
        ))
        return json.dumps(limited.to_dict(), ensure_ascii=False)


def test_explicit_law_article_cannot_be_assembled_from_different_laws() -> None:
    results = [
        evidence(article="第五条"),
        evidence(law="合成乙法", rank=2),
    ]
    check = check_evidence_sufficiency("《合成甲法》第十条规定了什么？", results)
    assert check.sufficient is False
    assert "missing_article:第十条" in check.missing_law_support
    assert check.stop_reason == "needs_followup"


def test_every_explicit_article_must_belong_to_the_same_explicit_law() -> None:
    check = check_evidence_sufficiency(
        "《合成甲法》第十条和第二十条分别规定了什么？",
        [evidence(), evidence(law="合成乙法", article="第二十条", rank=2)],
    )
    assert check.sufficient is False
    assert "missing_article:第二十条" in check.missing_law_support


def test_untyped_mixed_law_chunk_does_not_prove_article_ownership() -> None:
    row = evidence()
    mixed = replace(row, chunk=replace(
        row.chunk, law_names=["合成甲法", "合成乙法"],
        article_numbers=["第五条", "第十条"],
    ))
    check = check_evidence_sufficiency("《合成甲法》第十条是什么？", [mixed])
    assert check.sufficient is False
    assert "missing_article:第十条" in check.missing_law_support


def typed_mixed_evidence(target_law: str) -> SearchResult:
    row = evidence()
    boundary = RetrievalBoundary("public", "synthetic-snapshot", "a" * 64)
    entries = tuple(
        RetrievedArticleProvenance(
            article_id=f"article-{index}", law_id=f"law-{index}",
            version_id=f"version-{index}", article_number=article,
            title=title, valid_from=None, valid_to=None,
            source_ref="synthetic.txt", source_line=index,
            verification_status="unknown",
        )
        for index, (title, article) in enumerate([
            ("合成甲法", "第五条"), (target_law, "第十条"),
        ], start=1)
    )
    return replace(
        row,
        chunk=replace(row.chunk, law_names=["合成甲法", "合成乙法"],
                      article_numbers=["第五条", "第十条"]),
        provenance=RetrievalProvenance(
            boundary=boundary, scope_id="public", snapshot_id="synthetic-snapshot",
            profile_id="a" * 64, chunk_id=row.chunk.chunk_id,
            chunk_content_hash="b" * 64, chunk_payload_hash="c" * 64,
            snapshot_ordinal=0, embedding_hash=None, articles=entries,
        ),
    )


def test_typed_pairs_do_not_fall_back_to_mixed_law_union() -> None:
    check = check_evidence_sufficiency(
        "《合成甲法》第十条是什么？", [typed_mixed_evidence("合成乙法")],
    )
    assert check.sufficient is False
    assert "missing_article:第十条" in check.missing_law_support


def test_typed_pair_can_establish_article_ownership_in_mixed_law_chunk() -> None:
    assert check_evidence_sufficiency(
        "《合成甲法》第十条是什么？", [typed_mixed_evidence("合成甲法")],
    ).sufficient is True


def test_same_law_neighbor_chunk_can_cover_multiple_explicit_articles() -> None:
    row = evidence()
    neighbor = replace(row, chunk=replace(
        row.chunk, law_names=["合成甲法", "中华人民共和国合成甲法"],
        article_numbers=["第十条", "第二十条"],
    ))
    assert check_evidence_sufficiency(
        "《合成甲法》第十条和第二十条是什么？", [neighbor],
    ).sufficient is True


@pytest.mark.parametrize("other", ["合成甲法实施细则", "合成甲法实施条例", "劳动合同法"])
def test_related_law_title_is_not_an_exact_alias(other: str) -> None:
    requested = "合同法" if other == "劳动合同法" else "合成甲法"
    check = check_evidence_sufficiency(
        f"《{requested}》第十条规定了什么？", [evidence(law=other)]
    )
    assert check.sufficient is False
    assert f"missing_law:{requested}" in check.missing_law_support


@pytest.mark.parametrize("name", ["合成甲法", "中华人民共和国合成甲法", "《 合成 甲法 》"])
def test_only_bookmarks_whitespace_and_country_prefix_are_safe_aliases(name: str) -> None:
    check = check_evidence_sufficiency(
        "《中华人民共和国合成甲法》第十条规定了什么？",
        [evidence(law=name)],
    )
    assert check.sufficient is True


def test_full_common_title_and_short_alias_do_not_disable_pair_guard() -> None:
    check = check_evidence_sufficiency(
        "《中华人民共和国劳动法》第十条规定了什么？",
        [
            evidence(law="中华人民共和国劳动法", article="第五条"),
            evidence(law="合成乙法", rank=2),
        ],
    )
    assert check.sufficient is False
    assert "missing_article:第十条" in check.missing_law_support


def test_multiple_explicit_laws_do_not_create_a_guessed_cartesian_requirement() -> None:
    check = check_evidence_sufficiency(
        "《合成甲法》和《合成乙法》的第十条、第五条分别是什么？",
        [evidence(), evidence(law="合成乙法", article="第五条", rank=2)],
    )
    # Ambiguous pair relationships are not inferred by this narrow guard.
    assert check.sufficient is True


@pytest.mark.parametrize("score", [True, False, None, "100", complex(1, 1), float("nan"), float("inf"), -float("inf")])
def test_invalid_scores_fail_closed_instead_of_proving_sufficiency(score: object) -> None:
    check = check_evidence_sufficiency(
        "《合成甲法》第十条规定了什么？", [evidence(score=score)]
    )
    assert check.sufficient is False
    assert "invalid_scores" in check.low_coverage


def test_one_finite_high_score_does_not_hide_another_invalid_score() -> None:
    check = check_evidence_sufficiency(
        "《合成甲法》第十条规定了什么？",
        [evidence(score=1000000.0), evidence(rank=2, score=float("nan"))],
    )
    assert check.sufficient is False
    assert "invalid_scores" in check.low_coverage


@pytest.mark.parametrize("score", [-1, 0, 0.001, 0.01])
def test_existing_finite_low_score_reason_is_preserved(score: float) -> None:
    check = check_evidence_sufficiency(
        "《合成甲法》第十条规定了什么？", [evidence(score=score)]
    )
    assert check.sufficient is False
    assert "low_scores" in check.low_coverage
    assert "invalid_scores" not in check.low_coverage


@pytest.mark.parametrize("score", [1, 1.0])
def test_valid_finite_numeric_scores_remain_supported_inputs(score: float) -> None:
    assert check_evidence_sufficiency(
        "《合成甲法》第十条规定了什么？", [evidence(score=score)]
    ).sufficient is True


def test_high_scores_are_never_semantic_support_evidence() -> None:
    results = [evidence(score=1000000)]
    check = check_evidence_sufficiency("合成退货规则是什么？", results)
    answer = StructuredAnswer(
        answer_text="合成主体应当办理合成退货 [S1]。\n\n" + LEGAL_DISCLAIMER,
        answer_mode="evidence_answer",
        claims=[AnswerClaim(claim_id="C1", text="合成主体应当办理合成退货", source_ids=["S1"])],
        limitations=[],
        clarification_question=None,
    )
    verification = verify_answer(
        answer, results, evidence_check=check,
        expected_answer_mode="evidence_answer", disclaimer=LEGAL_DISCLAIMER,
    )
    assert verification.semantic_support_status in {"not_checked", "uncertain"}
    assert verification.semantic_support_status != "supported"


def test_private_target_article_is_not_counted_after_scope_filtering() -> None:
    retriever = FixedRetriever([
        evidence(scope="tenant-b"),
        evidence(article="第五条", rank=2),
    ])
    client = FakeLimitedClient()
    assistant = LegalChatAssistant(
        retriever, model="synthetic-fake", completion_client=client,
        verification_context=VerificationContext(
            snapshot_id="synthetic-snapshot", allowed_scope_ids=["public"]
        ),
    )
    retrieved = assistant.retrieve_turn(
        assistant.prepare_question("《合成甲法》第十条规定了什么？"),
        max_followup_rounds=0,
    )
    assistant.generate_turn(retrieved, generate=True)
    assert len(retrieved.results) == 1
    assert retrieved.evidence_check.sufficient is False
    assert retrieved.terminal_kind == "evidence_limited"
    assert client.calls == 0


def test_risk_refusal_still_precedes_retrieval_and_generation() -> None:
    retriever, client = FixedRetriever([evidence()]), FakeLimitedClient()
    assistant = LegalChatAssistant(retriever, model="synthetic-fake", completion_client=client)
    retrieved = assistant.retrieve_turn(assistant.prepare_question("如何伪造证据？"))
    assistant.generate_turn(retrieved, generate=True)
    assert retrieved.terminal_kind == "pre_retrieval_refusal"
    assert retriever.calls == 0
    assert client.calls == 0


def test_model_reported_insufficiency_does_not_override_expected_evidence_mode() -> None:
    client = FakeLimitedClient()
    assistant = LegalChatAssistant(
        FixedRetriever([evidence()]), model="synthetic-fake", completion_client=client,
    )
    retrieved = assistant.retrieve_turn(
        assistant.prepare_question("《合成甲法》第十条规定了什么？"),
        max_followup_rounds=0,
    )
    generated = assistant.generate_turn(retrieved, generate=True)
    verified = assistant.verify_turn(generated)
    assert retrieved.evidence_check.sufficient is True
    assert generated.expected_answer_mode == "evidence_answer"
    assert verified.pre_fallback_verification.passed is False
    assert verified.pre_fallback_verification.failure_reasons == ["response_mode_invalid"]
    assert verified.verification.passed is True
    assert client.calls == 1  # Synthetic complete, not a provider request.


@pytest.mark.parametrize("query", ["网上申请退税有哪些要求？", "在线办理退休业务。", "网上购买股票需要记录吗？", "买通他人伪造记录。", "商品期货投资。"])
def test_nonshopping_topics_do_not_acquire_consumer_goods_expansion(query: str) -> None:
    assert lexical_expansion_terms(query) == []


def test_negative_purchase_statement_is_not_changed_into_an_answer() -> None:
    query = "没有在网上买过商品，店家也不能退货。"
    terms = lexical_expansion_terms(query)
    assert set(terms) <= {"网络", "购买", "商品", "退货"}
    assert not any(term in terms for term in ("七日", "七天", "第二十五条", "消费者权益保护法"))
    tokens = tokenize(query, profile="local-lexical-v2")
    assert "没有" in tokens
    assert "不能" in tokens


def test_candidate_tokenizer_does_not_join_words_across_punctuation() -> None:
    assert "货合" not in tokenize("退货。合同", profile="local-lexical-v2")


def test_legacy_remains_default_and_candidate_trace_is_only_retrieval_metadata() -> None:
    chunk = evidence().chunk
    default = BM25Retriever([chunk])
    candidate = BM25Retriever([chunk], lexical_profile="local-lexical-v2")
    assert default.lexical_profile == "legacy-v1"
    rows = candidate.retrieve("合成记录", top_k=1)
    assert rows[0].trace["lexical_profile"] == "local-lexical-v2"
    assert "semantic_support_status" not in rows[0].trace


GOODS_RETURN_QUERY = "我在网上购买衣服后想退货，可以退吗？"


def profile_evidence(text: str, *, rank: int = 1, profile: str = "local-lexical-v2",
                     retriever: str = "bm25") -> SearchResult:
    row = evidence(rank=rank)
    return replace(row, chunk=replace(row.chunk, text=text), retriever=retriever,
                   trace={"lexical_profile": profile})


@pytest.mark.parametrize("texts", [
    ["合成主体登记房地产并依法保管登记资料。"],
    ["商品记录应当保管完整。"],
    ["依法约定退货的申请流程。"],
    ["商品记录应当保管完整。", "依法约定退货的申请流程。"],
])
def test_candidate_return_requires_goods_and_return_in_same_result(texts: list[str]) -> None:
    rows = [profile_evidence(text, rank=index) for index, text in enumerate(texts, start=1)]
    check = check_evidence_sufficiency(GOODS_RETURN_QUERY, rows)
    assert check.sufficient is False
    assert "missing_goods_return_anchor" in check.low_coverage


@pytest.mark.parametrize("goods", ["商品", "货物", "物品"])
def test_candidate_quality_return_anchor_does_not_require_online_channel(goods: str) -> None:
    rows = [profile_evidence(f"{goods}质量不符合要求时，可以申请退货。")]
    assert check_evidence_sufficiency(GOODS_RETURN_QUERY, rows).sufficient is True


@pytest.mark.parametrize("query", [
    "网上申请退税有哪些要求？", "网上购买股票后如何退税？",
    "没有在网上买过商品，店家也不能退货。", "网上购买衣服有哪些登记要求？",
])
def test_candidate_nonreturn_or_unrecognized_topics_do_not_require_return_anchor(query: str) -> None:
    assert "退货" not in lexical_expansion_terms(query)
    assert check_evidence_sufficiency(query, [profile_evidence("合成登记资料。")]).sufficient is True


@pytest.mark.parametrize("profile,retriever", [
    ("legacy-v1", "bm25"), ("local-lexical-v2", "dense"),
])
def test_candidate_return_necessary_condition_does_not_apply_to_other_retrievers(
    profile: str, retriever: str,
) -> None:
    rows = [profile_evidence("合成登记资料。", profile=profile, retriever=retriever)]
    assert check_evidence_sufficiency(GOODS_RETURN_QUERY, rows).sufficient is True


def test_candidate_missing_return_anchor_stops_before_model_generation() -> None:
    client = FakeLimitedClient()
    assistant = LegalChatAssistant(
        FixedRetriever([profile_evidence("合成房地产登记资料。")]),
        model="synthetic-fake", completion_client=client,
    )
    retrieved = assistant.retrieve_turn(assistant.prepare_question(GOODS_RETURN_QUERY),
                                        max_followup_rounds=0)
    assistant.generate_turn(retrieved, generate=True)
    assert retrieved.terminal_kind == "evidence_limited"
    assert "missing_goods_return_anchor" in retrieved.evidence_check.low_coverage
    assert client.calls == 0


def test_candidate_anchor_does_not_let_model_insufficiency_override_verifier() -> None:
    client = FakeLimitedClient()
    assistant = LegalChatAssistant(
        FixedRetriever([profile_evidence("商品质量不符合要求时，可以申请退货。")]),
        model="synthetic-fake", completion_client=client,
    )
    retrieved = assistant.retrieve_turn(assistant.prepare_question(GOODS_RETURN_QUERY),
                                        max_followup_rounds=0)
    generated = assistant.generate_turn(retrieved, generate=True)
    verified = assistant.verify_turn(generated)
    assert retrieved.evidence_check.sufficient is True
    assert generated.expected_answer_mode == "evidence_answer"
    assert verified.pre_fallback_verification.failure_reasons == ["response_mode_invalid"]
    assert verified.pre_fallback_verification.passed is False
    assert verified.verification.passed is True
    assert client.calls == 1
