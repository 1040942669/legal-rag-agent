from __future__ import annotations

import json
from copy import deepcopy

import pytest

from legal_rag.chat import GeneratedTurn, LegalChatAssistant, VerifiedTurn
from legal_rag.models import Chunk, SearchResult
from legal_rag.retrieval_contracts import (
    RetrievedArticleProvenance,
    RetrievalBoundary,
    RetrievalProvenance,
    chunk_payload_fingerprint,
)
from legal_rag.services.run_executor import (
    CompletedHistoryMessage,
    DeterministicRunExecutor,
    ExecutionFailure,
    ExecutionTimeout,
    LegalChatRunExecutor,
    RunExecutionInput,
)


PROFILE_ID = "a" * 64
BOUNDARY = RetrievalBoundary(
    scope_id="scope-a",
    snapshot_id="snapshot-a",
    profile_id=PROFILE_ID,
)
OTHER_BOUNDARY = RetrievalBoundary(
    scope_id="scope-b",
    snapshot_id="snapshot-b",
    profile_id="b" * 64,
)
DRAFT_MARKER = "REJECTED_DRAFT_MARKER_7F3C"


def _execution_input(
    *,
    history: tuple[CompletedHistoryMessage, ...] = (),
    boundary: RetrievalBoundary = BOUNDARY,
) -> RunExecutionInput:
    return RunExecutionInput(
        run_id="run-1",
        question="《合成测试法》第一条规定什么？",
        history=history,
        scope_id=boundary.scope_id,
        snapshot_id=boundary.snapshot_id,
        snapshot_revision=7,
        activation_id="activation-7",
        profile_id=boundary.profile_id,
        boundary_fingerprint=boundary.fingerprint,
        request_options={"generation": "verified-only"},
    )


def _bound_result(boundary: RetrievalBoundary = BOUNDARY) -> SearchResult:
    article = RetrievedArticleProvenance(
        article_id="article-1",
        law_id="law-1",
        version_id="law-1-v1",
        article_number="第一条",
        title="合成测试法",
        valid_from="2024-01-01",
        valid_to=None,
        source_ref="fixtures/m4-run-executor.txt",
        source_line=1,
        verification_status="verified",
    )
    chunk = Chunk(
        chunk_id="synthetic-a",
        text="合成测试法第一条规定，合成主体应当遵守合成义务。",
        law_names=[article.title],
        article_numbers=[article.article_number],
        source_files=[article.source_ref],
        line_nos=[article.source_line],
        strategy="article",
        metadata={
            "scope_id": boundary.scope_id,
            "snapshot_id": boundary.snapshot_id,
            "access_scope_ids": [boundary.scope_id],
            "profile_id": boundary.profile_id,
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
        snapshot_ordinal=0,
        embedding_hash="e" * 64,
        articles=(article,),
    )
    return SearchResult(
        chunk=chunk,
        score=1.0,
        rank=1,
        retriever="m4-fixture",
        trace={"boundary_fingerprint": boundary.fingerprint},
        provenance=provenance,
    )


class _BoundRetriever:
    name = "m4-fixture"

    def __init__(
        self,
        *,
        boundary: RetrievalBoundary = BOUNDARY,
        results: tuple[SearchResult, ...] = (),
        timeout: bool = False,
    ) -> None:
        self._boundary = boundary
        self.results = results
        self.timeout = timeout
        self.calls = 0

    @property
    def retrieval_boundary(self) -> RetrievalBoundary:
        return self._boundary

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        del query
        self.calls += 1
        if self.timeout:
            raise TimeoutError("PRIVATE_PROVIDER_TIMEOUT_DETAIL")
        return deepcopy(list(self.results[:top_k]))


class _NoCompletionClient:
    def complete(self, prompt: str) -> str:
        del prompt
        raise AssertionError("the completion client must not be called")


class _InvalidCitationClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> str:
        del prompt
        self.calls += 1
        return json.dumps(
            {
                "answer_text": f"{DRAFT_MARKER} [S999]。",
                "answer_mode": "evidence_answer",
                "claims": [
                    {
                        "claim_id": "C1",
                        "text": DRAFT_MARKER,
                        "source_ids": ["S999"],
                    }
                ],
                "limitations": [],
                "clarification_question": None,
            },
            ensure_ascii=False,
        )


class _RecordingAssistant(LegalChatAssistant):
    observed_verified: VerifiedTurn | None = None

    def verify_turn(self, generated: GeneratedTurn) -> VerifiedTurn:
        verified = super().verify_turn(generated)
        self.observed_verified = verified
        return verified


def _assistant(
    retriever: _BoundRetriever,
    *,
    client: object | None = None,
    recording: bool = False,
) -> LegalChatAssistant:
    assistant_type = _RecordingAssistant if recording else LegalChatAssistant
    return assistant_type(
        retriever,
        model="deterministic-offline-fixture",
        completion_client=client or _NoCompletionClient(),
    )


def test_linear_executor_has_explicit_offline_semantic_stage_without_repeated_checker():
    from legal_rag.semantic import SemanticPolicy, SemanticAssessment, SegmentAssessment
    policy = SemanticPolicy("offline-fixture", "r1", "p1", "a" * 64, True)
    class Client:
        def complete(self, prompt):
            return json.dumps({"answer_text": "合成主体应当遵守合成义务[S1]。", "answer_mode": "evidence_answer",
                "claims": [{"claim_id": "c1", "text": "合成主体应当遵守合成义务", "source_ids": ["S1"]}],
                "limitations": [], "clarification_question": None}, ensure_ascii=False)
    class Checker:
        def __init__(self):
            self.policy, self.calls = policy, 0
        def assess(self, request):
            self.calls += 1
            return SemanticAssessment(request.fingerprint, policy.fingerprint, policy.checker_id,
                policy.checker_revision, policy.prompt_version, tuple(SegmentAssessment(segment.segment_id,
                "supported", segment.source_ids, ()) for segment in request.segments))
    checker = Checker()
    assistant = LegalChatAssistant(_BoundRetriever(results=(_bound_result(),)), model="offline-fixture",
        completion_client=Client(), semantic_policy=policy, semantic_checker=checker)
    result = LegalChatRunExecutor(lambda _: assistant).execute(_execution_input())
    assert result.verification_payload["semantic_support_status"] == "supported"
    assert checker.calls == 1


def test_linear_executor_rejects_ungoverned_completion_checker_before_any_request():
    from legal_rag.semantic import SemanticPolicy, CompletionSemanticChecker
    policy = SemanticPolicy("completion-checker", "r1", "p1", "a" * 64, True)
    client = _NoCompletionClient()
    assistant = LegalChatAssistant(_BoundRetriever(results=(_bound_result(),)), model="offline-fixture",
        completion_client=client, semantic_policy=policy, semantic_checker=CompletionSemanticChecker(policy, client))
    with pytest.raises(ExecutionFailure, match="ungoverned_checker_disabled"):
        LegalChatRunExecutor(lambda _: assistant).execute(_execution_input())


def test_linear_executor_rejects_paid_policy_instead_of_ignoring_missing_lease_authority():
    from dataclasses import replace
    from legal_rag.services.execution_policy import ServiceExecutionPolicy, GenerationPolicy
    policy = ServiceExecutionPolicy(generation=GenerationPolicy(enabled=True, model="Qwen/test",
        allowed_scope_ids=("scope-a",), pricing_acknowledged=True, egress_acknowledged=True,
        price_revision="fixture-r1", input_rate="0.4", output_rate="3.2", budget="1"))
    assistant = _assistant(_BoundRetriever(results=(_bound_result(),)))
    with pytest.raises(ExecutionFailure, match="paid_policy_requires_graph"):
        LegalChatRunExecutor(lambda _: assistant).execute(replace(_execution_input(), execution_policy=policy.to_dict()))


@pytest.mark.parametrize("status", ["unsupported", "uncertain", "not_checked", "error"])
def test_graph_required_semantic_fallback_records_limited_completion_and_safe_rejection(monkeypatch, status):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from legal_rag.harness.nodes import BoundedHarnessNodes, NodeArtifactRef
    from legal_rag.harness.state import HARNESS_GRAPH_VERSION, new_harness_state
    from legal_rag.semantic import SemanticPolicy, SemanticAssessment, SegmentAssessment
    policy = SemanticPolicy("offline-fixture", "r1", "p1", "a" * 64, True)
    class Client:
        governed = True  # Fake only: this unit checks status, not monetary authority.
        def complete(self, prompt):
            return json.dumps({"answer_text": "合成主体应当遵守合成义务[S1]。", "answer_mode": "evidence_answer",
                "claims": [{"claim_id": "c1", "text": "合成主体应当遵守合成义务", "source_ids": ["S1"]}],
                "limitations": [], "clarification_question": None}, ensure_ascii=False)
    class Checker:
        def __init__(self):
            self.policy, self.calls = policy, 0
        def assess(self, request):
            self.calls += 1
            return SemanticAssessment(request.fingerprint, policy.fingerprint, policy.checker_id,
                policy.checker_revision, policy.prompt_version, tuple(SegmentAssessment(segment.segment_id,
                status, segment.source_ids, ("model_private_reason",)) for segment in request.segments))
    class Persistence:
        payload = None
        def has_outcome_unknown(self, *args, **kwargs):
            return False
        def save_node_artifact(self, *args, payload, **kwargs):
            self.payload = payload
            return NodeArtifactRef("safe-result", "f" * 64)
        def get_budget(self, run_id):
            return SimpleNamespace(retrieval_rounds_used=1, model_attempts_used=2,
                                   tool_attempts_used=1, embedding_attempts_used=0)
    checker, persistence = Checker(), Persistence()
    assistant = LegalChatAssistant(_BoundRetriever(results=(_bound_result(),)), model="offline-fixture",
        completion_client=Client(), semantic_policy=policy, semantic_checker=checker)
    execution_input = _execution_input()
    retrieved = assistant.retrieve_turn(assistant.prepare_question(execution_input.question))
    monkeypatch.setattr(BoundedHarnessNodes, "_retrieved", lambda self, state: retrieved)
    nodes = BoundedHarnessNodes(assistant, execution_input, persistence, "worker-fixture", 1,
                               lambda *_: None, generate_enabled=True)
    state = new_harness_state(run_id=execution_input.run_id, session_id="session-fixture", user_id="owner-fixture",
        graph_version=HARNESS_GRAPH_VERSION, retrieval_config_hash="d" * 64,
        question=execution_input.question, bounded_history_refs=[], snapshot_id=BOUNDARY.snapshot_id,
        embedding_profile_id=PROFILE_ID, execution_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1))
    state["completion_status"], state["stop_reason"] = "succeeded", "sufficient"
    changed = nodes.generate(state)
    assert checker.calls == 1
    assert changed["completion_status"] == "completed_with_limits"
    assert changed["stop_reason"] == "semantic_not_supported_or_unknown"
    rejected = persistence.payload["verification_payload"]["rejected_draft_check"]
    assert rejected["schema_version"] == 1 and rejected["passed"] is False
    assert rejected["structural_passed"] is True and rejected["semantic_check_required"] is True
    assert rejected["semantic_support_status"] == status
    assert "model_private_reason" not in json.dumps(persistence.payload)
    assert persistence.payload["answer_payload"]["answer_mode"] == "insufficient_evidence"


def _public_wire_payload(result, events) -> str:
    return json.dumps(
        {
            "answer_text": result.answer_text,
            "answer_payload": result.answer_payload,
            "evidence_payload": result.evidence_payload,
            "verification_payload": result.verification_payload,
            "events": events,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def test_safe_result_legacy_payload_does_not_acquire_modern_gate_and_rejection_summary_is_strict():
    from legal_rag.services.run_executor import SafeRunResult
    result = LegalChatRunExecutor(lambda _: _assistant(_BoundRetriever(results=(_bound_result(),))),
                                  generate=False).execute(_execution_input())
    payload = {"answer_text": result.answer_text, "answer_payload": dict(result.answer_payload),
               "evidence_payload": dict(result.evidence_payload), "verification_payload": dict(result.verification_payload)}
    restored = SafeRunResult(**payload)
    assert restored.verification_payload == result.verification_payload
    assert "rejected_draft_check" not in restored.verification_payload
    assert "semantic_support_status" not in restored.verification_payload
    valid = {"schema_version": 1, "passed": False, "structural_passed": True,
             "semantic_check_required": True, "semantic_support_status": "unsupported",
             "failure_reasons": ["semantic_unsupported"]}
    modern = deepcopy(payload)
    modern["verification_payload"].update(fallback_used=True, rejected_draft_check=valid)
    assert SafeRunResult(**modern).verification_payload["rejected_draft_check"] == valid
    from legal_rag.services.run_service import _coerce_safe_result, UnsafePayloadError
    assert _coerce_safe_result(modern).verification_payload["rejected_draft_check"] == valid
    for change in ({"schema_version": 2}, {"passed": True}, {"failure_reasons": [DRAFT_MARKER]},
                   {"failure_reasons": [[]]}, {"semantic_support_status": {}}, {"claim_text": DRAFT_MARKER}):
        invalid = deepcopy(modern)
        invalid["verification_payload"]["rejected_draft_check"].update(change)
        with pytest.raises(ValueError, match="rejected draft check"):
            SafeRunResult(**invalid)
        with pytest.raises(UnsafePayloadError, match="rejected draft check"):
            _coerce_safe_result(invalid)


def test_deterministic_executor_emits_safe_output_in_fixed_order() -> None:
    history_secret = "PRIVATE_COMPLETED_HISTORY"
    execution_input = _execution_input(
        history=(
            CompletedHistoryMessage(role="user", content=history_secret),
            CompletedHistoryMessage(role="assistant", content="private answer"),
        )
    )
    events: list[tuple[str, dict[str, object]]] = []

    result = DeterministicRunExecutor("Verified offline answer.").execute(
        execution_input,
        lambda event_type, payload: events.append((event_type, dict(payload))),
    )

    assert events == [
        (
            "retrieval.completed",
            {
                "result_count": 0,
                "checked_result_count": 0,
                "rejected_count": 0,
                "stop_reason": "offline_deterministic",
            },
        ),
        ("generation.started", {}),
        (
            "verification.completed",
            {"passed": True, "fallback_used": False},
        ),
    ]
    assert result.answer_payload == {
        "answer_text": "Verified offline answer.",
        "answer_mode": "insufficient_evidence",
        "claims": [],
        "limitations": ["Offline deterministic executor was used."],
        "clarification_question": None,
    }
    assert result.evidence_payload["boundary_fingerprint"] == BOUNDARY.fingerprint
    assert result.evidence_payload["sources"] == []
    assert result.verification_payload == {
        "passed": True,
        "performed": False,
        "fallback_used": False,
        "mode": "offline_deterministic",
    }
    public_wire = _public_wire_payload(result, events)
    assert history_secret not in public_wire
    assert execution_input.question not in public_wire
    assert "verified-only" not in public_wire


@pytest.mark.parametrize(
    "history",
    [
        (CompletedHistoryMessage(role="user", content="orphan"),),
        (
            CompletedHistoryMessage(role="assistant", content="wrong first role"),
            CompletedHistoryMessage(role="user", content="wrong second role"),
        ),
    ],
)
def test_run_input_rejects_history_that_is_not_complete_and_alternating(
    history: tuple[CompletedHistoryMessage, ...],
) -> None:
    with pytest.raises(ValueError, match="complete|alternate"):
        _execution_input(history=history)


def test_legal_chat_executor_restores_only_completed_history() -> None:
    history = (
        CompletedHistoryMessage(role="user", content="第一轮问题"),
        CompletedHistoryMessage(role="assistant", content="第一轮回答"),
        CompletedHistoryMessage(role="user", content="第二轮问题"),
        CompletedHistoryMessage(role="assistant", content="第二轮回答"),
    )
    created: list[LegalChatAssistant] = []

    def factory(_input: RunExecutionInput) -> LegalChatAssistant:
        assistant = _assistant(_BoundRetriever())
        created.append(assistant)
        return assistant

    result = LegalChatRunExecutor(factory).execute(_execution_input(history=history))

    assert result.verification_payload["passed"] is True
    assert len(created) == 1
    assert created[0].memory.messages == [
        (message.role, message.content) for message in history
    ]
    assert all(
        content != _execution_input().question
        for _role, content in created[0].memory.messages
    )


def test_legal_chat_executor_trims_long_history_to_assistant_memory_contract() -> None:
    history = tuple(
        CompletedHistoryMessage(
            role="user" if index % 2 == 0 else "assistant",
            content=f"第{index // 2 + 1}轮{'问题' if index % 2 == 0 else '回答'}",
        )
        for index in range(12)
    )
    created: list[LegalChatAssistant] = []

    def factory(_input: RunExecutionInput) -> LegalChatAssistant:
        assistant = _assistant(_BoundRetriever())
        created.append(assistant)
        return assistant

    LegalChatRunExecutor(factory).execute(_execution_input(history=history))

    assert created[0].memory.messages == [
        (message.role, message.content) for message in history[-8:]
    ]


def test_legal_chat_executor_trims_complete_turns_to_token_budget() -> None:
    history = (
        CompletedHistoryMessage(role="user", content="旧" * 40),
        CompletedHistoryMessage(role="assistant", content="答" * 40),
        CompletedHistoryMessage(role="user", content="新问题"),
        CompletedHistoryMessage(role="assistant", content="新回答"),
    )
    created: list[LegalChatAssistant] = []

    def factory(_input: RunExecutionInput) -> LegalChatAssistant:
        assistant = LegalChatAssistant(
            _BoundRetriever(),
            model="deterministic-offline-fixture",
            memory_token_limit=30,
            completion_client=_NoCompletionClient(),
        )
        created.append(assistant)
        return assistant

    LegalChatRunExecutor(factory).execute(_execution_input(history=history))

    assert created[0].memory.messages == [("user", "新问题"), ("assistant", "新回答")]


def test_legal_chat_executor_rejects_an_assistant_with_local_history() -> None:
    assistant = _assistant(_BoundRetriever())
    assistant.memory.add_turn("local user", "local assistant")

    with pytest.raises(ExecutionFailure) as raised:
        LegalChatRunExecutor(lambda _input: assistant).execute(_execution_input())

    assert raised.value.to_safe_dict() == {
        "error_code": "assistant_not_fresh",
        "stage": "history_restore",
        "retryable": False,
    }


def test_rejected_pre_fallback_draft_never_reaches_result_or_events() -> None:
    client = _InvalidCitationClient()
    assistant = _assistant(
        _BoundRetriever(results=(_bound_result(),)),
        client=client,
        recording=True,
    )
    events: list[tuple[str, dict[str, object]]] = []

    result = LegalChatRunExecutor(lambda _input: assistant).execute(
        _execution_input(),
        lambda event_type, payload: events.append((event_type, dict(payload))),
    )

    assert client.calls == 1
    assert isinstance(assistant, _RecordingAssistant)
    assert assistant.observed_verified is not None
    assert assistant.observed_verified.pre_fallback_answer is not None
    assert DRAFT_MARKER in assistant.observed_verified.pre_fallback_answer.answer_text
    assert result.answer_payload["answer_mode"] == "insufficient_evidence"
    assert result.verification_payload["fallback_used"] is True
    assert events[-1] == (
        "verification.completed",
        {"passed": True, "fallback_used": True},
    )
    public_wire = _public_wire_payload(result, events)
    assert DRAFT_MARKER not in public_wire
    assert "S999" not in public_wire
    assert "pre_fallback_answer" not in public_wire
    assert "raw_response" not in public_wire


def test_boundary_mismatch_fails_closed_before_any_public_event() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    assistant = _assistant(_BoundRetriever(boundary=OTHER_BOUNDARY))

    with pytest.raises(ExecutionFailure) as raised:
        LegalChatRunExecutor(lambda _input: assistant).execute(
            _execution_input(),
            lambda event_type, payload: events.append((event_type, dict(payload))),
        )

    assert raised.value.to_safe_dict() == {
        "error_code": "retrieval_boundary_mismatch",
        "stage": "retrieval",
        "retryable": False,
    }
    assert events == []


def test_timeout_is_mapped_to_retryable_execution_timeout() -> None:
    assistant = _assistant(_BoundRetriever(timeout=True))

    with pytest.raises(ExecutionTimeout) as raised:
        LegalChatRunExecutor(lambda _input: assistant).execute(_execution_input())

    assert raised.value.to_safe_dict() == {
        "error_code": "execution_timeout",
        "stage": "retrieval",
        "retryable": True,
    }
    assert "PRIVATE_PROVIDER_TIMEOUT_DETAIL" not in str(raised.value)
