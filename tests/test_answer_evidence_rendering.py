"""Every retrieved article reaches the model and the reader in full.

The 2026-10-22 demo answer said "[S1] 在第五项处截断".  That chunk came from a
stale index whose article text itself stopped at "（五）因本法"; the QA prompt
never sliced evidence.  These tests pin that guarantee for the prompt and
cover the one renderer that did cut articles mid-item: the retrieval-only
answer, which kept only the first 400 characters of each article.
"""

from __future__ import annotations

import json

from legal_rag.chat import (
    LEGAL_DISCLAIMER,
    LegalChatAssistant,
    build_qa_prompt,
    render_retrieval_only_answer,
)
from legal_rag.models import Chunk, SearchResult

LAST_ITEM = "（十二）合成主体应当在最后一项规定的期限内完成全部合成义务。"


def _long_article() -> str:
    items = "".join(
        f"（{number}）合成主体应当按照合成测试法规定的第{index}项程序办理登记、备案、公示和报告手续，"
        f"并在办理完毕后十五日内向合成主管部门提交书面材料；"
        for index, number in enumerate(
            ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一"], start=1
        )
    )
    return "合成测试法第一条规定，合成主体有下列义务：" + items + LAST_ITEM


def _result(rank: int, text: str) -> SearchResult:
    return SearchResult(
        chunk=Chunk(
            chunk_id=f"synthetic-{rank}",
            text=text,
            law_names=["合成测试法"],
            article_numbers=[f"第{rank}条"],
            source_files=["synthetic.txt"],
            line_nos=[rank],
            strategy="article",
            metadata={},
        ),
        score=1.0 / rank,
        rank=rank,
        retriever="fixture",
    )


def _results() -> list[SearchResult]:
    return [
        _result(1, _long_article()),
        _result(2, "合成测试法第二条规定，合成主体应当保存合成记录。"),
        _result(3, "合成测试法第三条规定，合成主体不得伪造合成记录。"),
    ]


def test_long_article_fixture_exceeds_every_display_cut() -> None:
    assert len(_long_article()) > 600


def test_qa_prompt_contains_every_article_in_full_and_in_rank_order() -> None:
    results = _results()

    prompt = build_qa_prompt(
        question="合成测试法第一条规定了什么?",
        original_question="合成测试法第一条规定了什么?",
        memory="",
        results=results,
    )

    positions = []
    for result in results:
        assert result.chunk.text in prompt
        positions.append(prompt.index(f"[S{result.rank}]"))
    assert positions == sorted(positions)
    assert LAST_ITEM in prompt


def test_generation_sends_full_articles_to_the_model() -> None:
    results = _results()

    class StaticRetriever:
        name = "fixture"

        def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
            return results

    class RecordingModel:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return json.dumps(
                {
                    "answer_text": "合成主体应当保存合成记录 [S2]。",
                    "answer_mode": "evidence_answer",
                    "claims": [
                        {
                            "claim_id": "C1",
                            "text": "合成主体应当保存合成记录",
                            "source_ids": ["S2"],
                        }
                    ],
                    "limitations": [],
                    "clarification_question": None,
                },
                ensure_ascii=False,
            )

    model = RecordingModel()
    assistant = LegalChatAssistant(
        StaticRetriever(), model="fake", top_k=3, completion_client=model
    )

    assistant.answer("合成测试法第一条规定了什么?", generate=True)

    assert len(model.prompts) == 1
    for result in results:
        assert result.chunk.text in model.prompts[0]


def test_retrieval_only_answer_never_cuts_an_article_in_half() -> None:
    results = _results()

    rendered = render_retrieval_only_answer(results)

    for result in results:
        assert f"[S{result.rank}]" in rendered
        assert result.chunk.text in rendered
    assert LAST_ITEM in rendered


def test_retrieval_only_turn_shows_full_articles_and_still_verifies() -> None:
    results = _results()

    class StaticRetriever:
        name = "fixture"

        def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
            return results

    assistant = LegalChatAssistant(StaticRetriever(), model="fake", top_k=3)

    answer, _ = assistant.answer("合成测试法第一条规定了什么?", generate=False)

    assert LAST_ITEM in answer
    assert answer.endswith(LEGAL_DISCLAIMER)
    assert assistant.last_generation_kind == "retrieval_only"
