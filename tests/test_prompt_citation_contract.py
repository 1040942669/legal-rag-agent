"""Synthetic-only checks for the prompt/verifier citation layout contract."""

from __future__ import annotations

import json

import pytest

import legal_rag.chat as chat
from legal_rag.experiment_lifecycle import _manifest_contracts
from legal_rag.models import AnswerClaim, Chunk, SearchResult, StructuredAnswer
from legal_rag.verifier import verify_answer


def result(rank: int = 1) -> SearchResult:
    return SearchResult(
        chunk=Chunk(
            chunk_id=f"synthetic-{rank}",
            text="合成测试主体应当履行合成义务。",
            law_names=["合成测试法"], article_numbers=["第一条"],
            source_files=["synthetic.txt"], line_nos=[1], strategy="article",
        ), score=1.0, rank=rank, retriever="deterministic",
    )


def prompt(results: list[SearchResult]) -> str:
    return chat.build_qa_prompt(
        question="合成义务是什么？", original_question="合成义务是什么？",
        memory="", results=results,
    )


def test_prompt_requires_visible_same_sentence_citations_and_verbatim_claims() -> None:
    text = prompt([result()])
    assert "只在 claims.source_ids 中填写编号不算正文引用" in text
    assert "句末标点之前" in text
    assert "逐字复制" in text
    assert "不得跨句或跨行" in text
    assert "只在回答末尾集中列出引用不能替代同句引用" in text


@pytest.mark.parametrize("rank", [1, 4])
def test_layout_example_is_valid_and_uses_a_visible_catalog_id(rank: int) -> None:
    text = prompt([result(rank)])
    example_text = text.split("引用布局示例（仅说明格式，不是检索证据，禁止复制示例结论）:\n", 1)[1]
    example = json.loads(example_text.split("\n\n最近对话:", 1)[0])
    assert example["claims"][0]["source_ids"] == [f"S{rank}"]
    assert example["answer_text"].rstrip().endswith(chat.LEGAL_DISCLAIMER)
    verification = verify_answer(
        json.dumps(example, ensure_ascii=False), [result(rank)],
        disclaimer=chat.LEGAL_DISCLAIMER,
    )
    assert verification.schema_valid is True
    assert verification.citation_alignment_valid is True
    assert verification.passed is True


def test_empty_evidence_example_does_not_invent_a_source() -> None:
    text = prompt([])
    example = json.loads(text.split(
        "引用布局示例（仅说明格式，不是检索证据，禁止复制示例结论）:\n", 1,
    )[1].split("\n\n最近对话:", 1)[0])
    assert example["answer_mode"] == "insufficient_evidence"
    assert example["claims"] == []
    assert "[S1]" not in example["answer_text"]
    assert verify_answer(
        json.dumps(example, ensure_ascii=False), [], disclaimer=chat.LEGAL_DISCLAIMER,
    ).passed is True


def test_prompt_identity_changes_without_changing_the_parser_schema() -> None:
    assert chat.STRUCTURED_QA_PROMPT_VERSION == "m1-structured-qa-citation-alignment-v2"
    assert chat.STRUCTURED_ANSWER_PARSER_VERSION == "m1-structured-answer-v1"
    assert _manifest_contracts(top_k=5, judge_enabled=False)["generation"][
        "prompt_version"
    ] == chat.STRUCTURED_QA_PROMPT_VERSION


@pytest.mark.parametrize("body,claim_text,source_ids", [
    ("合成主体应当履行合成义务。", "合成主体应当履行合成义务", ["S1"]),
    ("合成主体应当履行合成义务。参考资料 [S1]。", "合成主体应当履行合成义务", ["S1"]),
    ("合成主体应当履行合成义务。 [S1]", "合成主体应当履行合成义务", ["S1"]),
    ("合成主体应当履行合成义务 [S1]。另一合成结论 [S1]。", "合成主体应当履行合成义务。另一合成结论", ["S1"]),
    ("合成主体应当履行合成义务 [S1]。", "合成主体应当履行合成义务", ["S2"]),
])
def test_bad_layouts_remain_rejected(
    body: str, claim_text: str, source_ids: list[str],
) -> None:
    answer = StructuredAnswer(
        answer_text=body + "\n\n" + chat.LEGAL_DISCLAIMER,
        answer_mode="evidence_answer",
        claims=[AnswerClaim(claim_id="C1", text=claim_text, source_ids=source_ids)],
        limitations=[], clarification_question=None,
    )
    verification = verify_answer(answer, [result(), result(2)], disclaimer=chat.LEGAL_DISCLAIMER)
    assert verification.schema_valid is True
    assert verification.passed is False
    assert "citation_ids_invalid" in verification.failure_reasons
