from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from legal_rag.llm import CompletionUsage
from legal_rag.models import Chunk, SearchResult
from legal_rag.provider_errors import ProviderCallError
from legal_rag.web.app import create_app
from legal_rag.web.runtime import ModelInfo, RetrieverSlot, WebRuntime

ARTICLE_TEXT = {
    "第一条": "合成测试法第一条规定，合成主体应当遵守合成义务。",
    "第二条": "合成测试法第二条规定，合成主体违反合成义务的，应当承担合成责任。",
}
NODE_ORDER = (
    "analysis",
    "retrieval",
    "rerank",
    "evidence",
    "followup",
    "generation",
    "verification",
)


def _result(article: str, rank: int, *, exact: bool = False) -> SearchResult:
    trace: dict[str, Any] = {
        "bm25_score": 3.0 / rank,
        "law_hints": ["合成测试法"],
        "article_hints": ["第一条"],
    }
    if exact:
        trace["exact_citation_boost"] = 1000.0
    return SearchResult(
        chunk=Chunk(
            chunk_id=f"synthetic-{article}",
            text=ARTICLE_TEXT[article],
            law_names=["合成测试法"],
            article_numbers=[article],
            source_files=["synthetic.txt"],
            line_nos=[list(ARTICLE_TEXT).index(article) + 1],
            strategy="article",
        ),
        score=10.0 / rank,
        rank=rank,
        retriever="bm25",
        trace=trace,
    )


class _Retriever:
    """First call returns 第一条 only; later calls also return 第二条."""

    name = "bm25"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        self.queries.append(query)
        if len(self.queries) == 1:
            return [_result("第一条", 1, exact=True)]
        return [_result("第二条", 1), _result("第一条", 2, exact=True)][:top_k]


class _FailingRetriever:
    name = "dense"

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        raise ProviderCallError(
            "network", provider="siliconflow", operation="embedding"
        )


class _ReverseReranker:
    name = "fake_rerank"

    def score(self, query: str, results: list[SearchResult]) -> list[float]:
        return [float(index) for index in range(len(results))]


class _AnswerClient:
    def __init__(self, source_id: str = "S1") -> None:
        self.source_id = source_id
        self.usage = CompletionUsage()
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.usage.calls += 1
        self.usage.record_tokens(input_tokens=120, output_tokens=30, total_tokens=150)
        return json.dumps(
            {
                "answer_text": f"合成主体应当遵守合成义务 [{self.source_id}]。",
                "answer_mode": "evidence_answer",
                "claims": [
                    {
                        "claim_id": "C1",
                        "text": "合成主体应当遵守合成义务",
                        "source_ids": [self.source_id],
                    }
                ],
                "limitations": [],
                "clarification_question": None,
            },
            ensure_ascii=False,
        )


class _MissingKeyClient:
    def __init__(self) -> None:
        self.usage = CompletionUsage()

    def complete(self, prompt: str) -> str:
        self.usage.calls += 1
        self.usage.failed_calls += 1
        raise ProviderCallError(
            "configuration",
            provider="siliconflow",
            operation="completion",
            cause_type="MissingApiKey",
        )


def _runtime(
    tmp_path: Path,
    *,
    retriever: Any = None,
    client: Any = None,
    reranker: Any = None,
    adaptive: bool = False,
) -> WebRuntime:
    return WebRuntime(
        retrievers={
            "bm25": RetrieverSlot("bm25", "BM25 关键词", retriever or _Retriever()),
            "dense": RetrieverSlot(
                "dense",
                "向量检索（fixture）",
                None,
                "向量缓存不健康",
                uses_embedding=True,
            ),
            "failing": RetrieverSlot(
                "failing", "会失败的向量检索", _FailingRetriever(), uses_embedding=True
            ),
        },
        default_retriever="bm25",
        default_top_k=5,
        generator=ModelInfo(
            "siliconflow:fixture/model", "fixture/model", "siliconflow"
        ),
        completion_client_factory=lambda: client or _AnswerClient(),
        runs_path=tmp_path / "web" / "runs.jsonl",
        embedding=ModelInfo("fixture_embedding", "fixture/embedding", "siliconflow"),
        reranker=reranker,
        reranker_info=(
            ModelInfo("fake_rerank", "fixture/reranker", "siliconflow")
            if reranker is not None
            else None
        ),
        candidate_top_n=4,
        adaptive_options={"enabled": adaptive, "use_llm": False, "max_queries": 3},
    )


def _events(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def _ask(runtime: WebRuntime, **payload: Any) -> list[tuple[str, dict[str, Any]]]:
    with TestClient(create_app(runtime)) as client:
        response = client.post("/api/ask", json=payload)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return _events(response.text)


def _node_events(events) -> list[tuple[str, str]]:
    return [(data["node"], data["status"]) for kind, data in events if kind == "node"]


def _last_node(events, node: str) -> dict[str, Any]:
    nodes = [data for kind, data in events if kind == "node" and data["node"] == node]
    return nodes[-1]


def _final(events) -> dict[str, Any]:
    finals = [data for kind, data in events if kind == "final"]
    assert len(finals) == 1
    return finals[0]


class _TwoResultRetriever:
    name = "bm25"

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return [_result("第一条", 1, exact=True), _result("第二条", 2)][:top_k]


def test_generated_answer_streams_every_node_in_pipeline_order(tmp_path: Path) -> None:
    events = _ask(_runtime(tmp_path), question="《合成测试法》第一条规定了什么？")

    assert events[0][0] == "start"
    assert events[-1][0] == "final"
    assert _node_events(events) == [
        ("analysis", "running"),
        ("analysis", "done"),
        ("retrieval", "running"),
        ("retrieval", "done"),
        ("rerank", "skipped"),
        ("evidence", "running"),
        ("evidence", "done"),
        ("followup", "skipped"),
        ("generation", "running"),
        ("generation", "done"),
        ("verification", "running"),
        ("verification", "done"),
    ]
    for kind, data in events:
        if kind == "node":
            assert set(data) >= {"node", "status", "elapsed_ms", "detail", "at_ms"}

    analysis = _last_node(events, "analysis")["detail"]
    assert analysis["law_names"] == ["合成测试法"]
    assert analysis["article_numbers"] == ["第一条"]
    assert analysis["adaptive"]["triggered"] is False
    retrieval = _last_node(events, "retrieval")["detail"]
    assert retrieval["candidate_count"] == 1
    assert retrieval["exact_pin"]["applied"] is True
    assert retrieval["exact_pin"]["pinned"][0]["rank"] == 1
    evidence = _last_node(events, "evidence")["detail"]
    assert evidence["sufficient"] is True and evidence["missing"] == []
    assert _last_node(events, "followup")["detail"]["stop_reason"] == "sufficient"
    generation = _last_node(events, "generation")["detail"]
    assert generation["model"] == "fixture/model"
    assert generation["prompt_tokens"] == 120
    assert generation["completion_tokens"] == 30
    verification = _last_node(events, "verification")["detail"]
    assert verification["passed"] is True
    assert verification["replaced_with_fallback"] is False

    final = _final(events)
    assert final["answer"]["kind"] == "model"
    assert "[S1]" in final["answer"]["text"]
    assert final["replaced_by_verifier"] is False and final["draft"] is None
    assert final["sources"] == [
        {
            "id": "S1",
            "rank": 1,
            "chunk_id": "synthetic-第一条",
            "label": "合成测试法 第一条",
            "law_names": ["合成测试法"],
            "article_numbers": ["第一条"],
            "score": 10.0,
            "retriever": "bm25",
            "exact_pin": True,
            "source_files": ["synthetic.txt"],
            "line_nos": [1],
            "text": ARTICLE_TEXT["第一条"],
            "bm25_score": 3.0,
        }
    ]
    models = final["models"]
    assert models["retriever"]["key"] == "bm25"
    assert models["generator"] == {
        "key": "siliconflow:fixture/model",
        "name": "fixture/model",
        "provider": "siliconflow",
        "used": True,
    }
    assert models["embedding"]["used"] is False
    assert models["reranker"] is None
    assert final["usage"]["llm"]["total_tokens"] == 150
    assert set(final["timings"]["nodes"]) == {
        "analysis",
        "retrieval",
        "evidence",
        "generation",
        "verification",
    }
    assert final["timings"]["first_results_ms"] is not None
    assert final["errors"] == []


def test_retrieval_only_skips_generation_and_verification(tmp_path: Path) -> None:
    events = _ask(
        _runtime(tmp_path), question="《合成测试法》第一条规定了什么？", generate=False
    )

    statuses = dict(_node_events(events))
    assert statuses["generation"] == "skipped"
    assert statuses["verification"] == "skipped"
    assert "生成开关关闭" in _last_node(events, "generation")["detail"]["reason"]
    final = _final(events)
    assert final["answer"]["kind"] == "retrieval_only"
    assert final["answer"]["mode"] == "retrieval_only"
    assert final["models"]["generator"]["used"] is False


def test_rejected_draft_is_attached_and_marked_unverified(tmp_path: Path) -> None:
    events = _ask(
        _runtime(tmp_path, client=_AnswerClient(source_id="S9")),
        question="《合成测试法》第一条规定了什么？",
    )

    verification = _last_node(events, "verification")
    assert verification["status"] == "done"
    assert verification["detail"]["passed"] is False
    assert verification["detail"]["replaced_with_fallback"] is True
    assert "citation_ids_invalid" in verification["detail"]["failure_reasons"]
    final = _final(events)
    assert final["replaced_by_verifier"] is True
    assert final["draft"]["verified"] is False
    assert final["draft"]["label"] == "未通过校验的原始草稿"
    assert "[S9]" in final["draft"]["text"]
    assert "[S9]" not in final["answer"]["text"]


def test_rerank_reports_rankings_before_and_after(tmp_path: Path) -> None:
    events = _ask(
        _runtime(
            tmp_path, retriever=_TwoResultRetriever(), reranker=_ReverseReranker()
        ),
        question="《合成测试法》第一条规定了什么？",
        rerank=True,
        top_k=2,
    )

    nodes = _node_events(events)
    assert nodes.index(("retrieval", "done")) < nodes.index(("rerank", "running"))
    assert nodes.index(("rerank", "done")) < nodes.index(("evidence", "running"))
    retrieval = _last_node(events, "retrieval")["detail"]
    assert retrieval["requested_top_k"] == 4
    rerank = _last_node(events, "rerank")["detail"]
    assert rerank["model"] == "fixture/reranker"
    assert rerank["candidate_count"] == 2
    assert [(row["rank"], row["base_rank"]) for row in rerank["ranking"]] == [
        (1, 2),
        (2, 1),
    ]
    final = _final(events)
    assert [(card["id"], card["base_rank"]) for card in final["sources"]] == [
        ("S1", 2),
        ("S2", 1),
    ]
    assert final["models"]["reranker"]["used"] is True
    assert final["usage"]["rerank"]["rerank_calls"] == 1


def test_followup_retrieval_is_reported_when_evidence_is_missing(
    tmp_path: Path,
) -> None:
    retriever = _Retriever()
    events = _ask(
        _runtime(tmp_path, retriever=retriever),
        question="《合成测试法》第二条规定了什么？",
    )

    assert len(retriever.queries) == 2
    nodes = _node_events(events)
    assert nodes.index(("evidence", "done")) < nodes.index(("followup", "running"))
    assert nodes.index(("followup", "running")) < nodes.index(("followup", "done"))
    evidence = _last_node(events, "evidence")["detail"]
    assert evidence["sufficient"] is False
    assert evidence["first_followup_query"] == retriever.queries[1]
    followup = _last_node(events, "followup")["detail"]
    assert followup["rounds_used"] == 1
    assert followup["stop_reason"] == "sufficient_after_followup"
    assert followup["evidence_after_followup"]["sufficient"] is True
    assert {card["label"] for card in _final(events)["sources"]} == {
        "合成测试法 第一条",
        "合成测试法 第二条",
    }


def test_pre_retrieval_refusal_skips_retrieval_nodes(tmp_path: Path) -> None:
    retriever = _Retriever()
    events = _ask(
        _runtime(tmp_path, retriever=retriever), question="怎么伪造公章才不被发现？"
    )

    assert retriever.queries == []
    statuses = dict(_node_events(events))
    for node in ("retrieval", "rerank", "evidence", "followup", "generation"):
        assert statuses[node] == "skipped"
    assert statuses["verification"] == "done"
    assert "illegal_help" in _last_node(events, "analysis")["detail"]["risk_flags"]
    final = _final(events)
    assert final["answer"]["kind"] == "pre_retrieval_refusal"
    assert final["sources"] == []


def test_missing_key_marks_generation_failed_and_still_answers(tmp_path: Path) -> None:
    events = _ask(
        _runtime(tmp_path, client=_MissingKeyClient()),
        question="《合成测试法》第一条规定了什么？",
    )

    generation = _last_node(events, "generation")
    assert generation["status"] == "failed"
    assert generation["detail"]["error"]["kind"] == "missing_key"
    assert "SILICONFLOW_API_KEY" in generation["detail"]["error"]["message"]
    errors = [data for kind, data in events if kind == "error"]
    assert len(errors) == 1
    assert errors[0]["fatal"] is False and errors[0]["nodes"] == ["generation"]
    assert _last_node(events, "verification")["status"] == "done"
    final = _final(events)
    assert final["answer"]["kind"] == "generation_error"
    assert final["errors"][0]["kind"] == "missing_key"


def test_provider_failure_in_retrieval_ends_with_a_fatal_error_event(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    events = _ask(runtime, question="合成义务是什么？", retriever="failing")

    assert [kind for kind, _data in events if kind in {"final", "error"}] == ["error"]
    assert events[-1][0] == "error"
    error = events[-1][1]
    assert error["fatal"] is True
    assert error["kind"] == "network"
    assert error["nodes"] == ["retrieval"]
    assert "网络连接失败" in error["message"]
    statuses = dict(_node_events(events))
    assert statuses["retrieval"] == "failed"
    for node in ("rerank", "evidence", "followup", "generation", "verification"):
        assert statuses[node] == "skipped"
        assert "前序节点失败" in _last_node(events, node)["detail"]["reason"]

    record = json.loads(runtime.runs_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["status"] == "failed"
    assert record["errors"][0]["kind"] == "network"


def test_adaptive_retrieval_reports_every_node_after_the_plans_run(
    tmp_path: Path,
) -> None:
    retriever = _TwoResultRetriever()
    events = _ask(
        _runtime(tmp_path, retriever=retriever, adaptive=True),
        question="这个有没有依据？另外合成义务还有哪些规定？",
    )

    assert _last_node(events, "analysis")["detail"]["adaptive"]["triggered"] is True
    nodes = _node_events(events)
    seen = [node for node, status in nodes if status != "running"]
    assert sorted(seen) == sorted(NODE_ORDER)
    assert nodes.index(("retrieval", "running")) < nodes.index(("retrieval", "done"))
    retrieval = _last_node(events, "retrieval")["detail"]
    assert retrieval["adaptive"]["used"] is True
    assert len(retrieval["queries"]) == len(retrieval["adaptive"]["plans"])
    _final(events)


def test_each_run_appends_one_json_line(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    _ask(runtime, question="《合成测试法》第一条规定了什么？")
    _ask(runtime, question="《合成测试法》第一条规定了什么？", generate=False)

    lines = runtime.runs_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert [record["options"]["generate"] for record in records] == [True, False]
    assert records[0]["status"] == "succeeded"
    assert records[0]["nodes"]["generation"]["status"] == "done"
    assert records[0]["sources"][0]["id"] == "S1"
    assert records[0]["usage"]["llm"]["total_tokens"] == 150


@pytest.mark.parametrize(
    ("payload", "status_code", "code"),
    [
        ({"question": "   "}, 400, "empty_question"),
        ({"question": "合成义务", "retriever": "nope"}, 400, "unknown_retriever"),
        ({"question": "合成义务", "retriever": "dense"}, 409, "retriever_unavailable"),
        ({"question": "合成义务", "rerank": True}, 409, "reranker_unavailable"),
    ],
)
def test_requests_that_cannot_run_are_rejected_before_streaming(
    tmp_path: Path, payload: dict[str, Any], status_code: int, code: str
) -> None:
    with TestClient(create_app(_runtime(tmp_path))) as client:
        response = client.post("/api/ask", json=payload)

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code


def test_top_k_outside_the_page_limit_is_a_validation_error(tmp_path: Path) -> None:
    with TestClient(create_app(_runtime(tmp_path))) as client:
        response = client.post("/api/ask", json={"question": "合成义务", "top_k": 99})

    assert response.status_code == 422


def test_options_report_availability_defaults_and_models(tmp_path: Path) -> None:
    with TestClient(create_app(_runtime(tmp_path))) as client:
        options = client.get("/api/options").json()

    retrievers = {item["key"]: item for item in options["retrievers"]}
    assert retrievers["bm25"]["available"] is True
    assert retrievers["dense"] == {
        "key": "dense",
        "label": "向量检索（fixture）",
        "available": False,
        "reason": "向量缓存不健康",
        "uses_embedding": True,
    }
    assert options["defaults"] == {
        "retriever": "bm25",
        "rerank": False,
        "generate": True,
        "top_k": 5,
    }
    assert options["models"]["generator"]["name"] == "fixture/model"
    assert options["models"]["reranker"] is None
    assert len(options["examples"]) == 3


def test_index_page_is_self_contained(tmp_path: Path) -> None:
    with TestClient(create_app(_runtime(tmp_path))) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert 'lang="zh-CN"' in page
    assert "/api/ask" in page and "/api/options" in page
    for marker in ("<script src=", "<link rel=\"stylesheet\"", "@import", "https://"):
        assert marker not in page
