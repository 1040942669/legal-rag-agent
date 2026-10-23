"""Regression tests for the 2026-10-22 demo verifier false rejection.

The demo trace (artifacts/demo_trace.jsonl, 18:15:49) recorded a correct
answer to "劳动合同法第三十九条规定了什么?" that cited [S1], [S2] and [S3],
all present in that run's evidence, yet was replaced by the conservative
answer with ``citation_ids_invalid``.  The trace kept only the visible
answer_text; the claims below are the cited spans copied verbatim from it,
which is the claim contract the QA prompt asks for.
"""

from __future__ import annotations

import json

from legal_rag.chat import LEGAL_DISCLAIMER, LegalChatAssistant, build_qa_prompt
from legal_rag.models import AnswerClaim, Chunk, SearchResult, StructuredAnswer
from legal_rag.verifier import verify_answer


QUESTION = "劳动合同法第三十九条规定了什么?"

# Real retrieved text (frozen corpus) of the three hits in the traced run.
EVIDENCE = (
    (
        "中华人民共和国劳动合同法",
        "《中华人民共和国劳动合同法》第三十九条规定，【用人单位单方解除劳动合同（过失性辞退）】"
        "劳动者有下列情形之一的，用人单位可以解除劳动合同：（一）在试用期间被证明不符合录用条件的；"
        "（二）严重违反用人单位的规章制度的；（三）严重失职，营私舞弊，给用人单位造成重大损害的；"
        "（四）劳动者同时与其他用人单位建立劳动关系，对完成本单位的工作任务造成严重影响，"
        "或者经用人单位提出，拒不改正的；（五）因本法第二十六条第一款第一项规定的情形致使劳动合同无效的；"
        "（六）被依法追究刑事责任的。",
    ),
    (
        "中华人民共和国劳动法",
        "《中华人民共和国劳动法》第三十九条规定，企业因生产特点不能实行本法第三十六条、"
        "第三十八条规定的，经劳动行政部门批准，可以实行其他工作和休息办法。",
    ),
    (
        "中华人民共和国职业病防治法",
        "《中华人民共和国职业病防治法》第三十九条规定，劳动者享有下列职业卫生保护权利："
        "（一）获得职业卫生教育、培训；（二）获得职业健康检查、职业病诊疗、康复等职业病防治服务。",
    ),
)

# Verbatim verifier_rejected.model_answer from the traced run.
TRACE_ANSWER_TEXT = (
    "结论：《中华人民共和国劳动合同法》第三十九条规定的是用人单位单方解除劳动合同（过失性辞退）的法定情形。"
    "依据：该条列明，劳动者有下列情形之一的，用人单位可以解除劳动合同：（一）在试用期间被证明不符合录用条件的；"
    "（二）严重违反用人单位的规章制度的；（三）严重失职，营私舞弊，给用人单位造成重大损害的；"
    "（四）劳动者同时与其他用人单位建立劳动关系，对完成本单位的工作任务造成严重影响，"
    "或者经用人单位提出，拒不改正的；（五）因本法第二十六条第一款第一项规定的情形致使劳动合同无效的；"
    "（六）被依法追究刑事责任的 [S1]。需注意，《中华人民共和国劳动法》第三十九条和"
    "《中华人民共和国职业病防治法》第三十九条另有不同规定，并非《劳动合同法》第三十九条 [S2][S3]。"
    "免责声明：仅供课程学习和法律文本检索参考，不构成法律意见。"
)
ENUMERATION_CLAIM = (
    "劳动者有下列情形之一的，用人单位可以解除劳动合同：（一）在试用期间被证明不符合录用条件的；"
    "（二）严重违反用人单位的规章制度的；（三）严重失职，营私舞弊，给用人单位造成重大损害的；"
    "（四）劳动者同时与其他用人单位建立劳动关系，对完成本单位的工作任务造成严重影响，"
    "或者经用人单位提出，拒不改正的；（五）因本法第二十六条第一款第一项规定的情形致使劳动合同无效的；"
    "（六）被依法追究刑事责任的"
)
DISTINCTION_CLAIM = (
    "需注意，《中华人民共和国劳动法》第三十九条和《中华人民共和国职业病防治法》第三十九条另有不同规定，"
    "并非《劳动合同法》第三十九条"
)


def _results() -> list[SearchResult]:
    return [
        SearchResult(
            chunk=Chunk(
                chunk_id=f"trace-{rank}",
                text=text,
                law_names=[law],
                article_numbers=["第三十九条"],
                source_files=[f"{law}.txt"],
                line_nos=[39],
                strategy="article",
                metadata={},
            ),
            score=1.0 / rank,
            rank=rank,
            retriever="fixture",
        )
        for rank, (law, text) in enumerate(EVIDENCE, start=1)
    ]


def _payload(answer_text: str, claims: list[tuple[str, list[str]]]) -> dict:
    return {
        "answer_text": answer_text,
        "answer_mode": "evidence_answer",
        "claims": [
            {"claim_id": f"C{index}", "text": text, "source_ids": source_ids}
            for index, (text, source_ids) in enumerate(claims, start=1)
        ],
        "limitations": [],
        "clarification_question": None,
    }


class _StaticRetriever:
    name = "fixture"

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return _results()


class _RecordedModel:
    """Replays one recorded provider response; never calls a real model."""

    def __init__(self, payload: dict) -> None:
        self.raw = json.dumps(payload, ensure_ascii=False)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.raw


def _answer(payload: dict) -> tuple[str, LegalChatAssistant]:
    assistant = LegalChatAssistant(
        _StaticRetriever(),
        model="fake",
        top_k=3,
        completion_client=_RecordedModel(payload),
    )
    answer, _ = assistant.answer(QUESTION, generate=True)
    return answer, assistant


def test_trace_answer_with_valid_citations_is_delivered_unchanged() -> None:
    payload = _payload(
        TRACE_ANSWER_TEXT,
        [(ENUMERATION_CLAIM, ["S1"]), (DISTINCTION_CLAIM, ["S2", "S3"])],
    )

    answer, assistant = _answer(payload)

    assert assistant.last_pre_fallback_verification is None
    assert assistant.last_verification.passed
    assert assistant.last_verification.citation_alignment_valid
    assert assistant.last_structured_answer.answer_mode == "evidence_answer"
    assert answer == TRACE_ANSWER_TEXT
    assert answer.endswith(LEGAL_DISCLAIMER)


# Complete raw response recorded on 2026-10-23 from DeepSeek-V4.1-Flash
# (thinking off, current QA prompt), with the BM25 top-5 evidence it saw.
PROBATION_QUESTION = "试用期最长可以约定多久?"
PROBATION_EVIDENCE = (
    ("中华人民共和国劳动法", "第二十一条", "《中华人民共和国劳动法》第二十一条规定，劳动合同可以约定试用期。试用期最长不得超过六个月。"),
    ("中华人民共和国民法典", "第六百三十七条", "《中华人民共和国民法典》第六百三十七条规定，试用买卖的当事人可以约定标的物的试用期限。"),
    (
        "中华人民共和国劳动合同法",
        "第十九条",
        "《中华人民共和国劳动合同法》第十九条规定，【试用期】劳动合同期限三个月以上不满一年的，"
        "试用期不得超过一个月；劳动合同期限一年以上不满三年的，试用期不得超过二个月；"
        "三年以上固定期限和无固定期限的劳动合同，试用期不得超过六个月。"
        "同一用人单位与同一劳动者只能约定一次试用期。以完成一定工作任务为期限的劳动合同"
        "或者劳动合同期限不满三个月的，不得约定试用期。",
    ),
    ("中华人民共和国劳动合同法", "第八十三条", "《中华人民共和国劳动合同法》第八十三条规定，用人单位违反本法规定与劳动者约定试用期的，由劳动行政部门责令改正。"),
    ("中华人民共和国公务员法", "第一百零三条", "《中华人民共和国公务员法》第一百零三条规定，聘任合同可以约定试用期，试用期为一个月至十二个月。"),
)
PROBATION_RAW_RESPONSE = (
    '{"answer_mode":"evidence_answer","claims":[{"claim_id":"C1","text":"劳动合同可以约定试用期，'
    '试用期最长不得超过六个月","source_ids":["S1"]},{"claim_id":"C2","text":"劳动合同期限三个月以上'
    '不满一年的，试用期不得超过一个月；劳动合同期限一年以上不满三年的，试用期不得超过二个月；三年以上'
    '固定期限和无固定期限的劳动合同，试用期不得超过六个月","source_ids":["S3"]},{"claim_id":"C3",'
    '"text":"以完成一定工作任务为期限的劳动合同或者劳动合同期限不满三个月的，不得约定试用期",'
    '"source_ids":["S3"]}],"answer_text":"一般劳动合同的试用期最长不得超过六个月，且具体上限随劳动合同'
    '期限不同而不同。劳动合同可以约定试用期，试用期最长不得超过六个月 [S1]。劳动合同期限三个月以上不满'
    '一年的，试用期不得超过一个月；劳动合同期限一年以上不满三年的，试用期不得超过二个月；三年以上固定期限'
    '和无固定期限的劳动合同，试用期不得超过六个月 [S3]。以完成一定工作任务为期限的劳动合同或者劳动合同'
    '期限不满三个月的，不得约定试用期 [S3]。","limitations":["聘任制公务员的试用期为一个月至十二个月，'
    '属特别规定，不适用一般劳动合同试用期上限"],"clarification_question":null}'
)


def test_recorded_enumeration_answer_from_live_model_is_delivered() -> None:
    results = [
        SearchResult(
            chunk=Chunk(
                chunk_id=f"probation-{rank}",
                text=text,
                law_names=[law],
                article_numbers=[article],
                source_files=[f"{law}.txt"],
                line_nos=[rank],
                strategy="article",
                metadata={},
            ),
            score=1.0 / rank,
            rank=rank,
            retriever="fixture",
        )
        for rank, (law, article, text) in enumerate(PROBATION_EVIDENCE, start=1)
    ]

    class StaticRetriever:
        name = "fixture"

        def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
            return results

    class RecordedModel:
        def complete(self, prompt: str) -> str:
            return PROBATION_RAW_RESPONSE

    assistant = LegalChatAssistant(
        StaticRetriever(), model="fake", top_k=5, completion_client=RecordedModel()
    )

    answer, _ = assistant.answer(PROBATION_QUESTION, generate=True)

    assert assistant.last_pre_fallback_verification is None
    assert assistant.last_verification.passed
    assert assistant.last_verification.cited_source_ids == ["S1", "S3"]
    assert "三年以上固定期限和无固定期限的劳动合同，试用期不得超过六个月 [S3]。" in answer
    assert answer.endswith(LEGAL_DISCLAIMER)


def test_prompt_example_satisfies_the_alignment_check_it_describes() -> None:
    example_claim = "劳动者有下列情形之一的，用人单位可以解除劳动合同"
    example_sentence = f"{example_claim} [S1]。"
    prompt = build_qa_prompt(
        question=QUESTION,
        original_question=QUESTION,
        memory="",
        results=_results(),
    )

    # Claims are written first, then copied verbatim into answer_text.
    assert prompt.index("- claims:") < prompt.index("- answer_text:")
    assert "一字不差" in prompt
    assert f"claim 为“{example_claim}”" in prompt
    assert f"answer_text 中写“{example_sentence}”" in prompt

    verification = verify_answer(
        StructuredAnswer(
            answer_text=f"第三十九条规定了过失性辞退的情形。{example_sentence}\n\n{LEGAL_DISCLAIMER}",
            answer_mode="evidence_answer",
            claims=[AnswerClaim(claim_id="C1", text=example_claim, source_ids=["S1"])],
            limitations=[],
            clarification_question=None,
        ),
        _results(),
        expected_answer_mode="evidence_answer",
        disclaimer=LEGAL_DISCLAIMER,
    )
    assert verification.passed


def test_fabricated_source_id_in_the_same_answer_is_still_rejected() -> None:
    fabricated_text = TRACE_ANSWER_TEXT.replace("[S2][S3]", "[S2][S7]")
    cases = (
        # Visible text and claim both cite S7, which this run never retrieved.
        _payload(
            fabricated_text,
            [(ENUMERATION_CLAIM, ["S1"]), (DISTINCTION_CLAIM, ["S2", "S7"])],
        ),
        # Only the hidden claim metadata cites S7.
        _payload(
            TRACE_ANSWER_TEXT,
            [(ENUMERATION_CLAIM, ["S1"]), (DISTINCTION_CLAIM, ["S2", "S3", "S7"])],
        ),
    )

    for payload in cases:
        answer, assistant = _answer(payload)

        rejected = assistant.last_pre_fallback_verification
        assert rejected is not None
        assert "citation_ids_invalid" in rejected.failure_reasons
        assert "S7" in rejected.missing_source_ids or not rejected.citation_alignment_valid
        assert assistant.last_structured_answer.answer_mode == "insufficient_evidence"
        assert "[S7]" not in answer
        assert "被依法追究刑事责任" not in answer


def test_enumeration_claim_may_carry_one_citation_at_its_end() -> None:
    answer = StructuredAnswer(
        answer_text=TRACE_ANSWER_TEXT,
        answer_mode="evidence_answer",
        claims=[
            AnswerClaim(claim_id="C1", text=ENUMERATION_CLAIM, source_ids=["S1"]),
        ],
        limitations=[],
        clarification_question=None,
    )

    verification = verify_answer(
        answer,
        _results(),
        expected_answer_mode="evidence_answer",
        disclaimer=LEGAL_DISCLAIMER,
    )

    assert verification.citation_alignment_valid
    assert verification.passed


def test_citation_outside_the_claimed_items_is_not_local_support() -> None:
    cases = (
        # The citation sits in a separate clause after the enumeration.
        (
            "用人单位可以解除劳动合同：（一）在试用期间被证明不符合录用条件的；"
            "（二）严重违反用人单位的规章制度的；参考来源 [S1]。",
            "用人单位可以解除劳动合同：（一）在试用期间被证明不符合录用条件的；"
            "（二）严重违反用人单位的规章制度的",
        ),
        # The claim covers only the first item; the citation is on the second.
        (
            "（一）在试用期间被证明不符合录用条件的；（二）严重违反用人单位的规章制度的 [S1]。",
            "（一）在试用期间被证明不符合录用条件的",
        ),
        # A sentence boundary still separates a claim from a later citation.
        (
            "（一）在试用期间被证明不符合录用条件的。（二）严重违反用人单位的规章制度的 [S1]。",
            "（一）在试用期间被证明不符合录用条件的；（二）严重违反用人单位的规章制度的",
        ),
    )
    for answer_text, claim_text in cases:
        answer = StructuredAnswer(
            answer_text=f"{answer_text}\n\n{LEGAL_DISCLAIMER}",
            answer_mode="evidence_answer",
            claims=[AnswerClaim(claim_id="C1", text=claim_text, source_ids=["S1"])],
            limitations=[],
            clarification_question=None,
        )

        verification = verify_answer(
            answer,
            _results(),
            expected_answer_mode="evidence_answer",
            disclaimer=LEGAL_DISCLAIMER,
        )

        assert not verification.citation_alignment_valid, answer_text
        assert not verification.passed
