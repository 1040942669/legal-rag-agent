from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from legal_rag.chat import LEGAL_DISCLAIMER
from legal_rag.models import AnswerClaim, Chunk, SearchResult, StructuredAnswer
from legal_rag.verifier import verify_answer


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "m1_verification_examples.json"


def load_examples() -> dict[str, Any]:
    raw = FIXTURE_PATH.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "fixture must be UTF-8 without BOM"
    return json.loads(raw.decode("utf-8"))


def find_example(fixture: dict[str, Any], example_id: str) -> dict[str, Any]:
    return next(item for item in fixture["examples"] if item["id"] == example_id)


def build_answer(payload: dict[str, Any]) -> StructuredAnswer:
    return StructuredAnswer(
        answer_text=payload["answer_text"],
        answer_mode=payload["answer_mode"],
        claims=[
            AnswerClaim(
                claim_id=claim["claim_id"],
                text=claim["text"],
                source_ids=claim["source_ids"],
            )
            for claim in payload["claims"]
        ],
        limitations=payload["limitations"],
        clarification_question=payload["clarification_question"],
    )


def build_results(payloads: list[dict[str, Any]]) -> list[SearchResult]:
    results = []
    for payload in payloads:
        assert payload["source_id"] == f"S{payload['rank']}"
        results.append(
            SearchResult(
                chunk=Chunk(
                    chunk_id=payload["chunk_id"],
                    text=payload["text"],
                    law_names=[payload["document_name"]],
                    article_numbers=[payload["section"]],
                    source_files=[payload["source_file"]],
                    line_nos=[payload["line_no"]],
                    strategy="synthetic_fixture",
                    metadata={"synthetic": True, "non_legal": True},
                ),
                score=payload["score"],
                rank=payload["rank"],
                retriever="static_synthetic_fixture",
            )
        )
    return results


def test_fixture_is_explicitly_synthetic_non_legal_and_not_a_quality_claim() -> None:
    fixture = load_examples()

    assert fixture["schema_version"] == 1
    assert fixture["synthetic"] is True
    assert fixture["non_legal"] is True
    assert fixture["contains_real_legal_material"] is False
    assert fixture["quality_scope"]["measures_real_legal_answer_quality"] is False
    assert len(fixture["examples"]) >= 2
    assert {item["requirement_id"] for item in fixture["examples"]} >= {
        "M1-T01",
        "M1-T04",
    }

    for example in fixture["examples"]:
        assert example["synthetic"] is True
        assert example["non_legal"] is True
        assert example["before_behavior"]
        assert example["after_expected"]
        assert example["disclaimer"] == LEGAL_DISCLAIMER
        assert all(
            evidence["source_file"] == "synthetic_non_legal.txt"
            for evidence in example["evidence"]
        )


def test_disclaimer_regression_example_is_not_counted_as_refusal() -> None:
    fixture = load_examples()
    example = find_example(fixture, "m1-t01-disclaimer-is-not-refusal")
    before = example["before_behavior"]
    expected = example["after_expected"]

    assert before["refusal_present"] is True
    assert before["matched_token"] in example["answer"]["answer_text"]

    verification = verify_answer(
        build_answer(example["answer"]),
        build_results(example["evidence"]),
        expected_answer_mode=example["expected_answer_mode"],
        disclaimer=example["disclaimer"],
    )

    assert verification.refusal_present is expected["refusal_present"]
    assert verification.disclaimer_present is expected["disclaimer_present"]
    assert verification.response_mode_valid is expected["response_mode_valid"]


def test_real_but_irrelevant_source_id_never_implies_semantic_support() -> None:
    fixture = load_examples()
    example = find_example(fixture, "m1-t04-real-id-does-not-prove-support")
    before = example["before_behavior"]
    expected = example["after_expected"]

    assert before["citation_ids_valid"] is True
    assert before["semantic_check_skipped"] is True
    assert before["unsupported_claims"] == []

    verification = verify_answer(
        build_answer(example["answer"]),
        build_results(example["evidence"]),
        expected_answer_mode=example["expected_answer_mode"],
        disclaimer=example["disclaimer"],
    )

    assert verification.citation_ids_valid is expected["citation_ids_valid"]
    assert verification.semantic_support_status in expected[
        "semantic_support_status_allowed"
    ]
    assert verification.semantic_support_status not in expected[
        "semantic_support_status_forbidden"
    ]
    assert verification.unsupported_claims
