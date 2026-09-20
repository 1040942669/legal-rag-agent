from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypeVar

from .adaptive import retrieve_adaptive
from .chat import LegalChatAssistant
from .chat_artifacts import (
    evidence_check_from_artifact,
    evidence_check_to_artifact,
    generated_turn_from_artifact,
    generated_turn_to_artifact,
    prepared_question_from_artifact,
    prepared_question_to_artifact,
    query_analysis_from_artifact,
    query_analysis_to_artifact,
    structured_answer_from_artifact,
    structured_answer_to_artifact,
    verification_result_from_artifact,
    verification_result_to_artifact,
    verified_turn_from_artifact,
    verified_turn_to_artifact,
    retrieved_turn_from_artifact,
    retrieved_turn_to_artifact,
)
from .evaluation_artifacts import (
    eval_case_from_artifact,
    eval_case_to_artifact,
    eval_record_from_artifact,
    eval_record_to_artifact,
    search_result_from_artifact,
    search_result_to_artifact,
)
from .evaluation_contracts import validate_eval_case
from .evaluation_scoring import (
    CompletedCaseOutcome,
    EvaluatedCase,
    ModelUsageDelta,
    score_completed_case,
)
from .experiment_runner import (
    OBSERVATION_STAGES,
    AttemptControls,
    CaseExecution,
    CaseExecutionError,
    RunnerContractError,
    RunnerControls,
    RunnerStopped,
    StageObservation,
    WorkUnit,
)
from .experiment_runtime import (
    EXTERNAL_CALL_KINDS,
    ExactStageCache,
    ExperimentContractError,
    StageExecution,
    StageValue,
    build_stage_cache_key,
    canonical_hash,
    canonical_json_bytes,
    validate_experiment_manifest,
)
from .judge import JudgeResult, judge_answer
from .llm import CompletionUsage, usage_delta, usage_snapshot
from .models import EvalCase, VerificationContext
from .provider_errors import (
    MAX_PROVIDER_TIMEOUT_SECONDS,
    ProviderCallError,
    provider_call_error,
)
from .query import analyze_query
from .retrieval import BM25Retriever, Retriever


ADAPTER_ARTIFACT_SCHEMA_VERSION = 1
EVALUATION_OUTPUT_SCHEMA_VERSION = 2
_T = TypeVar("_T")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CompletionClient(Protocol):
    def complete(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class EvaluationRuntimeSpec:
    """Immutable wiring for one production evaluation runtime.

    Factories, rather than mutable provider clients, are accepted so every
    work-unit/retry receives isolated usage counters and conversation state.
    """

    manifest: Mapping[str, Any]
    cache: ExactStageCache
    retriever: Retriever
    assistant_factory: Callable[[], LegalChatAssistant]
    model: str
    chunk_strategy: str
    generate: bool
    retriever_provider_free: bool = False
    judge_client_factory: Callable[[], CompletionClient] | None = None
    adaptive_llm_client_factory: Callable[[], CompletionClient] | None = None
    trace_metadata: Mapping[str, Any] | None = None


def build_manifest_case(case: EvalCase, *, ordinal: int) -> dict[str, Any]:
    """Build the only manifest case shape accepted by this adapter."""

    validate_eval_case(case)
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ExperimentContractError("manifest case ordinal must be non-negative")
    artifact = eval_case_to_artifact(case)
    return {
        "ordinal": ordinal,
        "case_id": case.case_id,
        "case_hash": canonical_hash(artifact),
        "session_group": case.session_group,
        "turn_index": case.turn_index,
        "evaluation_case": artifact,
    }


def build_manifest_cases(
    cases: list[EvalCase] | tuple[EvalCase, ...],
) -> list[dict[str, Any]]:
    return [
        build_manifest_case(case, ordinal=index) for index, case in enumerate(cases)
    ]


def validate_manifest_case(value: Mapping[str, Any]) -> tuple[dict[str, Any], EvalCase]:
    """Reject manifest/gold drift before any runtime or provider is built."""

    if not isinstance(value, Mapping):
        raise ExperimentContractError("manifest evaluation case must be an object")
    payload = _json_object(value)
    required = {
        "ordinal",
        "case_id",
        "case_hash",
        "session_group",
        "turn_index",
        "evaluation_case",
    }
    _require_exact_fields("manifest evaluation case", payload, required)
    ordinal = payload["ordinal"]
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ExperimentContractError("manifest case ordinal must be non-negative")
    case = eval_case_from_artifact(payload["evaluation_case"])
    expected = build_manifest_case(case, ordinal=ordinal)
    if canonical_json_bytes(payload) != canonical_json_bytes(expected):
        raise ExperimentContractError(
            "manifest evaluation case identity is inconsistent"
        )
    return expected, case


class LegalEvaluationRuntimeFactory:
    """Build one isolated case runtime per runner work unit and retry."""

    def __init__(self, spec: EvaluationRuntimeSpec) -> None:
        self.spec = _validate_spec(spec)
        self._instance_lock = threading.Lock()
        self._runtime_owned_objects: list[object] = []
        dataset_cases = self.spec.manifest["dataset"].get("cases")
        if not isinstance(dataset_cases, list):
            raise ExperimentContractError("manifest dataset cases must be a list")
        for expected_ordinal, raw_case in enumerate(dataset_cases):
            case_payload, _ = validate_manifest_case(raw_case)
            if case_payload["ordinal"] != expected_ordinal:
                raise ExperimentContractError(
                    "manifest evaluation case ordinals must be contiguous"
                )
            if not self.spec.generate and case_payload["session_group"] is not None:
                raise ExperimentContractError(
                    "retrieval-only evaluation does not support session cases"
                )

    def __call__(
        self,
        work_unit: WorkUnit,
        resume_state: Mapping[str, Any] | None,
        controls: RunnerControls,
    ) -> CaseRuntime:
        if controls.cache_mode not in {"fresh", "cache", "replay"}:
            raise ExperimentContractError("runtime received an invalid cache mode")
        if work_unit.session_group is None and resume_state is not None:
            raise ExperimentContractError(
                "independent work units cannot receive resume state"
            )
        for raw_case in work_unit.cases:
            validate_manifest_case(raw_case)
        assistant = self.spec.assistant_factory()
        if not isinstance(assistant, LegalChatAssistant):
            raise ExperimentContractError(
                "assistant_factory must return a LegalChatAssistant"
            )
        if assistant.retriever is not self.spec.retriever:
            raise ExperimentContractError(
                "assistant and evaluation runtime must share one retriever"
            )
        if assistant.model != self.spec.model:
            raise ExperimentContractError(
                "assistant model does not match the evaluation runtime spec"
            )
        expected_top_k = self.spec.manifest["contracts"]["retrieval"]["parameters"][
            "top_k"
        ]
        if assistant.top_k != expected_top_k:
            raise ExperimentContractError(
                "assistant top_k does not match the retrieval contract"
            )
        expected_runtime = _manifest_assistant_runtime(self.spec.manifest)
        actual_runtime = {
            "top_k": assistant.top_k,
            "memory_token_limit": assistant.memory.token_limit,
            "adaptive_enabled": assistant.adaptive_enabled,
            "adaptive_use_llm": assistant.adaptive_use_llm,
            "adaptive_max_queries": assistant.adaptive_max_queries,
            "adaptive_per_plan_top_k": assistant.adaptive_per_plan_top_k,
            "adaptive_normalizer_retries": assistant.normalizer_retries,
            "condense_with_llm": assistant.condense_with_llm,
        }
        if canonical_json_bytes(actual_runtime) != canonical_json_bytes(
            expected_runtime
        ):
            raise ExperimentContractError(
                "assistant runtime does not match manifest config.summary"
            )
        expected_scope = self.spec.manifest["contracts"]["retrieval"]["scope"]
        actual_scope = _verification_context_payload(assistant.verification_context)
        if canonical_json_bytes(actual_scope) != canonical_json_bytes(expected_scope):
            raise ExperimentContractError(
                "assistant verification scope does not match the retrieval contract"
            )
        _require_completion_usage("assistant completion client", assistant.llm)
        judge_client = (
            self.spec.judge_client_factory()
            if self.spec.judge_client_factory is not None
            else None
        )
        adaptive_client = (
            self.spec.adaptive_llm_client_factory()
            if self.spec.adaptive_llm_client_factory is not None
            else None
        )
        expected_timeouts = _manifest_provider_timeouts(self.spec.manifest)
        actual_timeouts = {
            "assistant": _provider_timeout_value(
                "assistant", getattr(assistant.llm, "request_timeout", None)
            ),
            "judge": _provider_timeout_value(
                "judge", getattr(judge_client, "request_timeout", None)
            ),
            "adaptive": _provider_timeout_value(
                "adaptive", getattr(adaptive_client, "request_timeout", None)
            ),
        }
        if canonical_json_bytes(actual_timeouts) != canonical_json_bytes(
            expected_timeouts
        ):
            raise ExperimentContractError(
                "provider request timeouts do not match manifest config.summary"
            )
        if judge_client is not None:
            _require_completion_usage("judge completion client", judge_client)
        if adaptive_client is not None:
            _require_completion_usage("adaptive completion client", adaptive_client)
        if self.spec.generate and adaptive_client is not None:
            raise ExperimentContractError(
                "generated evaluation cannot use a separate adaptive client"
            )
        if not self.spec.generate:
            if assistant.adaptive_use_llm and adaptive_client is None:
                raise ExperimentContractError(
                    "retrieval-only adaptive LLM requires an adaptive client"
                )
            if not assistant.adaptive_use_llm and adaptive_client is not None:
                raise ExperimentContractError(
                    "adaptive client configured while adaptive LLM is disabled"
                )
        owned = [
            assistant,
            assistant.memory,
            assistant.llm,
            assistant.llm.usage,
        ]
        if judge_client is not None:
            owned.extend((judge_client, judge_client.usage))
        if adaptive_client is not None:
            owned.extend((adaptive_client, adaptive_client.usage))
        if len({id(item) for item in owned}) != len(owned):
            raise ExperimentContractError(
                "assistant, generation, normalizer, and judge clients must be isolated"
            )
        with self._instance_lock:
            if any(
                item is existing
                for item in owned
                for existing in self._runtime_owned_objects
            ):
                raise ExperimentContractError(
                    "runtime factories must not reuse assistants or provider clients"
                )
            self._runtime_owned_objects.extend(owned)
        initial_state = _run_without_assistant_provider(
            assistant,
            assistant.export_session_state,
            phase="initial session export",
        )
        if initial_state.get("memory", {}).get("messages") != []:
            raise ExperimentContractError(
                "assistant_factory must return an assistant without history"
            )
        initial_memory = assistant.memory
        if resume_state is not None:
            _run_without_assistant_provider(
                assistant,
                lambda: assistant.restore_session_state(resume_state),
                phase="session restore",
            )
            if assistant.memory is not initial_memory:
                with self._instance_lock:
                    if any(
                        assistant.memory is existing
                        for existing in self._runtime_owned_objects
                    ):
                        raise ExperimentContractError(
                            "restored conversation memory must be isolated"
                        )
                    self._runtime_owned_objects.append(assistant.memory)
            restored_state = _run_without_assistant_provider(
                assistant,
                assistant.export_session_state,
                phase="restored session export",
            )
            if canonical_json_bytes(restored_state) != canonical_json_bytes(
                resume_state
            ):
                raise ExperimentContractError(
                    "assistant restored session state does not match its checkpoint"
                )
        return CaseRuntime(
            spec=self.spec,
            assistant=assistant,
            judge_client=judge_client,
            adaptive_client=adaptive_client,
            session_group=work_unit.session_group,
            cache_mode=controls.cache_mode,
        )


class CaseRuntime:
    def __init__(
        self,
        *,
        spec: EvaluationRuntimeSpec,
        assistant: LegalChatAssistant,
        judge_client: CompletionClient | None,
        adaptive_client: CompletionClient | None,
        session_group: str | None,
        cache_mode: str,
    ) -> None:
        self.spec = spec
        self.assistant = assistant
        self.judge_client = judge_client
        self.adaptive_client = adaptive_client
        self.session_group = session_group
        self.cache_mode = cache_mode
        self._assistant_client = assistant.llm

    def execute(
        self,
        case: Mapping[str, Any],
        controls: AttemptControls,
    ) -> CaseExecution:
        return self._execute(case, controls)

    def _execute(
        self,
        case: Mapping[str, Any],
        controls: AttemptControls,
    ) -> CaseExecution:
        if controls.cache_mode != self.cache_mode:
            raise RunnerContractError("attempt and runtime cache modes disagree")
        manifest_case, evaluation_case = validate_manifest_case(case)
        if manifest_case["session_group"] != self.session_group:
            raise RunnerContractError("case session group does not match its runtime")
        if self.spec.generate:
            return self._execute_generated(evaluation_case, controls)
        return self._execute_retrieval_only(evaluation_case, controls)

    def _execute_generated(
        self,
        case: EvalCase,
        controls: AttemptControls,
    ) -> CaseExecution:
        observations = _base_observations()
        assistant_case_usage_before = usage_snapshot(self._assistant_client)
        session_state = self._without_assistant_provider(
            self.assistant.export_session_state,
            phase="session export",
        )
        query_identity = {
            "question": case.question,
            "session_state": session_state,
            "condense_with_llm": self.assistant.condense_with_llm,
            "completion_model": self.spec.model,
        }
        normalizer_before = usage_snapshot(self._assistant_client)

        def produce_prepared() -> Mapping[str, Any]:
            return self._with_assistant_client(
                controls,
                stage="query_analysis",
                kind="normalizer",
                operation=lambda: prepared_question_to_artifact(
                    self.assistant.prepare_question(case.question)
                ),
            )

        prepared, observations["query_analysis"], prepared_execution = (
            self._cached_stage(
                cache_stage="query_analysis",
                observation_stage="query_analysis",
                input_payload={
                    "query_hash": canonical_hash(query_identity),
                    **query_identity,
                },
                artifact_kind="prepared_question",
                producer=produce_prepared,
                decoder=lambda artifact: self._without_assistant_provider(
                    lambda: prepared_question_from_artifact(
                        artifact, assistant=self.assistant
                    ),
                    phase="prepared artifact binding",
                ),
                observations=observations,
                controls=controls,
            )
        )
        prepared_artifact = prepared_question_to_artifact(prepared)
        scope_payload = _verification_context_payload(
            self.assistant.verification_context
        )
        retrieval_runtime = _retrieval_runtime_identity(self.assistant)

        def produce_retrieved() -> Mapping[str, Any]:
            return self._with_assistant_client(
                controls,
                stage="retrieval",
                kind="normalizer",
                operation=lambda: retrieved_turn_to_artifact(
                    self.assistant.retrieve_turn(prepared)
                ),
            )

        retrieved, observations["retrieval"], retrieved_execution = self._cached_stage(
            cache_stage="retrieval",
            observation_stage="retrieval",
            input_payload={
                "normalized_query_hash": canonical_hash(prepared.standalone_question),
                "query_representation_hash": canonical_hash(prepared_artifact),
                "prepared_artifact_sha256": canonical_hash(prepared_artifact),
                "session_state_sha256": canonical_hash(session_state),
                "scope": scope_payload,
                "runtime": retrieval_runtime,
            },
            artifact_kind="retrieved_turn",
            producer=produce_retrieved,
            decoder=lambda artifact: retrieved_turn_from_artifact(
                artifact, prepared=prepared
            ),
            observations=observations,
            controls=controls,
        )
        normalizer_usage = ModelUsageDelta.from_mapping(
            usage_delta(normalizer_before, usage_snapshot(self._assistant_client))
        )
        _validate_usage_ledger(
            "assistant normalizer",
            normalizer_usage,
            controls,
            (("query_analysis", "normalizer"), ("retrieval", "normalizer")),
            model_usage_role="normalizer",
        )
        retrieved_artifact = retrieved_turn_to_artifact(retrieved)
        evidence_hash = canonical_hash(retrieved_artifact)
        assistant_usage_before = usage_snapshot(self._assistant_client)

        def produce_generated() -> Mapping[str, Any]:
            return self._with_assistant_client(
                controls,
                stage="generation",
                kind="generation",
                operation=lambda: generated_turn_to_artifact(
                    self.assistant.generate_turn(retrieved, generate=True)
                ),
            )

        generated, observations["generation"], generated_execution = self._cached_stage(
            cache_stage="generation",
            observation_stage="generation",
            input_payload={
                "context_history_hash": canonical_hash(session_state),
                "evidence_hash": evidence_hash,
                "prepared_artifact_sha256": canonical_hash(prepared_artifact),
                "retrieved_artifact_sha256": evidence_hash,
            },
            artifact_kind="generated_turn",
            producer=produce_generated,
            decoder=lambda artifact: generated_turn_from_artifact(
                artifact, retrieved=retrieved
            ),
            observations=observations,
            controls=controls,
        )
        assistant_usage = ModelUsageDelta.from_mapping(
            usage_delta(
                assistant_usage_before,
                usage_snapshot(self._assistant_client),
            )
        )
        _validate_usage_ledger(
            "assistant generation",
            assistant_usage,
            controls,
            (("generation", "generation"),),
            model_usage_role="assistant",
        )
        if generated.generation_error is not None:
            observations["generation"] = _observation_from_execution(
                generated_execution,
                status="error",
                error_code=generated.generation_error,
            )
        generated_artifact = generated_turn_to_artifact(generated)

        verified, observations["verification"], verified_execution = self._cached_stage(
            cache_stage="verification",
            observation_stage="verification",
            input_payload={
                "draft_hash": canonical_hash(generated_artifact),
                "evidence_hash": evidence_hash,
                "scope_hash": canonical_hash(scope_payload),
                "generated_artifact_sha256": canonical_hash(generated_artifact),
            },
            artifact_kind="verified_turn",
            producer=lambda: verified_turn_to_artifact(
                self._without_assistant_provider(
                    lambda: self.assistant.verify_turn(generated),
                    phase="verification",
                )
            ),
            decoder=lambda artifact: verified_turn_from_artifact(
                artifact, generated=generated
            ),
            observations=observations,
            controls=controls,
        )
        # A cache checksum proves bytes, not that current verifier code agrees.
        reverify_started = time.perf_counter()
        currently_verified = self._without_assistant_provider(
            lambda: self.assistant.verify_turn(generated),
            phase="current verification",
        )
        reverify_duration_ms = (time.perf_counter() - reverify_started) * 1000
        observations["verification"] = _observation_from_execution(
            verified_execution,
            extra_duration_ms=reverify_duration_ms,
        )
        if canonical_json_bytes(verified_turn_to_artifact(verified)) != (
            canonical_json_bytes(verified_turn_to_artifact(currently_verified))
        ):
            raise ExperimentContractError(
                "cached verification disagrees with the current verifier"
            )

        judge_result: JudgeResult | None = None
        judge_usage = ModelUsageDelta()
        judge_execution: StageExecution | None = None
        if self.judge_client is None:
            observations["judge"] = StageObservation.not_run("judge_disabled")
        elif generated.generation_error is not None:
            observations["judge"] = StageObservation.not_run("generation_error")
        else:
            judge_usage_before = usage_snapshot(self.judge_client)
            judge_result, observations["judge"], judge_execution = self._cached_stage(
                cache_stage="judge",
                observation_stage="judge",
                input_payload={
                    "answer_hash": canonical_hash(
                        {
                            "answer_text": verified.answer_text,
                            "final_answer": (
                                structured_answer_to_artifact(verified.final_answer)
                                if verified.final_answer is not None
                                else None
                            ),
                        }
                    ),
                    "evidence_hash": evidence_hash,
                    "question": case.question,
                },
                artifact_kind="judge_result",
                producer=lambda: _judge_result_to_artifact(
                    judge_answer(
                        _ControlledCompletionClient(
                            self.judge_client,
                            controls,
                            stage="judge",
                            kind="judge",
                        ),
                        question=case.question,
                        answer=verified.answer_text,
                        results=list(retrieved.results),
                    )
                ),
                decoder=_judge_result_from_artifact,
                observations=observations,
                controls=controls,
            )
            judge_usage = ModelUsageDelta.from_mapping(
                usage_delta(judge_usage_before, usage_snapshot(self.judge_client))
            )
            _validate_usage_ledger(
                "judge",
                judge_usage,
                controls,
                (("judge", "judge"),),
                model_usage_role="judge",
            )
            if judge_result.status == "error":
                observations["judge"] = _observation_from_execution(
                    judge_execution,
                    status="error",
                    error_code=judge_result.error_code or "judge_error",
                )

        elapsed = (
            sum(
                execution.duration_ms
                for execution in (
                    prepared_execution,
                    retrieved_execution,
                    generated_execution,
                    verified_execution,
                    judge_execution,
                )
                if execution is not None
            )
            + reverify_duration_ms
        )
        adaptive = retrieved.adaptive_result
        if adaptive is None:
            adaptive_trace: Mapping[str, Any] = {
                "enabled": self.assistant.adaptive_enabled,
                "used": False,
            }
            scoring_analysis = prepared.analysis
        else:
            adaptive_trace = adaptive.to_trace()
            scoring_analysis = adaptive.analysis
        evidence_check = retrieved.evidence_check
        if evidence_check is None:
            # Pre-retrieval refusal intentionally has no evidence.  The pure
            # scorer still requires an explicit value object.
            from .evidence import check_evidence_sufficiency

            evidence_check = check_evidence_sufficiency(
                prepared.standalone_question,
                [],
                analysis=prepared.analysis,
            )
        scoring_results = tuple(
            _safe_scoring_result(item) for item in retrieved.results
        )
        outcome = CompletedCaseOutcome(
            case=case,
            model=self.spec.model,
            retriever=getattr(self.assistant.retriever, "name", "unknown"),
            chunk_strategy=self.spec.chunk_strategy,
            top_k=self.assistant.top_k,
            generate=True,
            results=scoring_results,
            answer=verified.answer_text,
            analysis=scoring_analysis,
            adaptive_trace=_without_raw_response(adaptive_trace),
            evidence_check=evidence_check,
            verification=verified.verification,
            structured_answer=verified.final_answer,
            pre_fallback_answer=verified.pre_fallback_answer,
            pre_fallback_verification=verified.pre_fallback_verification,
            generation_kind=generated.kind,
            generation_error=generated.generation_error,
            judge_configured=self.judge_client is not None,
            judge_result=judge_result,
            error="",
            latency_ms=int(elapsed),
            assistant_usage=assistant_usage,
            normalizer_usage=normalizer_usage,
            judge_usage=judge_usage,
            trace_metadata=(
                _without_raw_response(self.spec.trace_metadata)
                if self.spec.trace_metadata is not None
                else None
            ),
        )
        evaluated = score_completed_case(outcome)
        stage_executions = {
            "query_analysis": prepared_execution,
            "query_embedding": None,
            "retrieval": retrieved_execution,
            "rerank": None,
            "generation": generated_execution,
            "verification": verified_execution,
            "judge": judge_execution,
        }
        stage_identity = _build_stage_identity(observations, stage_executions)
        output = _evaluation_output(
            outcome,
            evaluated,
            manifest=self.spec.manifest,
            stage_identity=stage_identity,
        )
        decode_evaluation_output(
            output,
            expected_manifest=self.spec.manifest,
            expected_case=case,
            expected_stage_identity=stage_identity,
        )
        self._without_assistant_provider(
            lambda: self.assistant.commit_turn(verified),
            phase="turn commit",
        )
        state_after = (
            self._without_assistant_provider(
                self.assistant.export_session_state,
                phase="session export",
            )
            if self.session_group is not None
            else None
        )
        assistant_case_usage = ModelUsageDelta.from_mapping(
            usage_delta(
                assistant_case_usage_before,
                usage_snapshot(self._assistant_client),
            )
        )
        _validate_usage_ledger(
            "assistant case total",
            assistant_case_usage,
            controls,
            (
                ("query_analysis", "normalizer"),
                ("retrieval", "normalizer"),
                ("generation", "generation"),
            ),
        )
        return CaseExecution(
            output=output,
            stage_observations=observations,
            session_state_after=state_after,
        )

    def _execute_retrieval_only(
        self,
        case: EvalCase,
        controls: AttemptControls,
    ) -> CaseExecution:
        observations = _base_observations()
        assistant_case_usage_before = usage_snapshot(self._assistant_client)
        session_state = self._without_assistant_provider(
            self.assistant.export_session_state,
            phase="session export",
        )
        analysis, observations["query_analysis"], analysis_execution = (
            self._cached_stage(
                cache_stage="query_analysis",
                observation_stage="query_analysis",
                input_payload={
                    "query_hash": canonical_hash(
                        {"question": case.question, "session_state": session_state}
                    ),
                    "question": case.question,
                    "session_state": session_state,
                },
                artifact_kind="query_analysis",
                producer=lambda: query_analysis_to_artifact(
                    analyze_query(case.question)
                ),
                decoder=query_analysis_from_artifact,
                observations=observations,
                controls=controls,
            )
        )
        analysis_artifact = query_analysis_to_artifact(analysis)
        adaptive_usage_before = usage_snapshot(self.adaptive_client)
        retrieval_runtime = _retrieval_runtime_identity(self.assistant)

        def produce_retrieval() -> Mapping[str, Any]:
            controlled_client = (
                _ControlledCompletionClient(
                    self.adaptive_client,
                    controls,
                    stage="retrieval",
                    kind="normalizer",
                )
                if self.adaptive_client is not None
                else None
            )
            adaptive = retrieve_adaptive(
                case.question,
                self.spec.retriever,
                top_k=self.assistant.top_k,
                enabled=self.assistant.adaptive_enabled,
                use_llm=self.assistant.adaptive_use_llm,
                llm_client=controlled_client,
                max_queries=self.assistant.adaptive_max_queries,
                per_plan_top_k=self.assistant.adaptive_per_plan_top_k,
                normalizer_retries=self.assistant.normalizer_retries,
            )
            if adaptive.analysis != analysis:
                raise ExperimentContractError(
                    "retrieval analysis disagrees with query-analysis stage"
                )
            return _retrieval_only_to_artifact(adaptive, analysis_artifact)

        retrieval_facts, observations["retrieval"], retrieval_execution = (
            self._cached_stage(
                cache_stage="retrieval",
                observation_stage="retrieval",
                input_payload={
                    "normalized_query_hash": canonical_hash(case.question),
                    "query_representation_hash": canonical_hash(analysis_artifact),
                    "analysis_artifact_sha256": canonical_hash(analysis_artifact),
                    "session_state_sha256": canonical_hash(session_state),
                    "scope": _verification_context_payload(
                        self.assistant.verification_context
                    ),
                    "runtime": retrieval_runtime,
                },
                artifact_kind="retrieval_only_result",
                producer=produce_retrieval,
                decoder=lambda artifact: _retrieval_only_from_artifact(
                    artifact, analysis_artifact
                ),
                observations=observations,
                controls=controls,
            )
        )
        normalizer_usage = ModelUsageDelta.from_mapping(
            usage_delta(adaptive_usage_before, usage_snapshot(self.adaptive_client))
        )
        _validate_usage_ledger(
            "adaptive normalizer",
            normalizer_usage,
            controls,
            (("retrieval", "normalizer"),),
            model_usage_role="normalizer",
        )
        observations["generation"] = StageObservation.not_run("retrieval_only")
        observations["verification"] = StageObservation.not_run("retrieval_only")
        observations["judge"] = StageObservation.not_run("retrieval_only")
        scoring_results = tuple(
            _safe_scoring_result(item) for item in retrieval_facts["results"]
        )
        outcome = CompletedCaseOutcome(
            case=case,
            model=self.spec.model,
            retriever=getattr(self.assistant.retriever, "name", "unknown"),
            chunk_strategy=self.spec.chunk_strategy,
            top_k=self.assistant.top_k,
            generate=False,
            results=scoring_results,
            answer="",
            analysis=analysis,
            adaptive_trace=_without_raw_response(retrieval_facts["adaptive_trace"]),
            evidence_check=retrieval_facts["evidence_check"],
            verification=None,
            structured_answer=None,
            pre_fallback_answer=None,
            pre_fallback_verification=None,
            generation_kind="retrieval_only",
            generation_error=None,
            judge_configured=self.judge_client is not None,
            judge_result=None,
            error="",
            latency_ms=int(
                analysis_execution.duration_ms + retrieval_execution.duration_ms
            ),
            assistant_usage=ModelUsageDelta(),
            normalizer_usage=normalizer_usage,
            judge_usage=ModelUsageDelta(),
            trace_metadata=(
                _without_raw_response(self.spec.trace_metadata)
                if self.spec.trace_metadata is not None
                else None
            ),
        )
        evaluated = score_completed_case(outcome)
        stage_executions = {
            "query_analysis": analysis_execution,
            "query_embedding": None,
            "retrieval": retrieval_execution,
            "rerank": None,
            "generation": None,
            "verification": None,
            "judge": None,
        }
        stage_identity = _build_stage_identity(observations, stage_executions)
        output = _evaluation_output(
            outcome,
            evaluated,
            manifest=self.spec.manifest,
            stage_identity=stage_identity,
        )
        decode_evaluation_output(
            output,
            expected_manifest=self.spec.manifest,
            expected_case=case,
            expected_stage_identity=stage_identity,
        )
        state_after = (
            self._without_assistant_provider(
                self.assistant.export_session_state,
                phase="session export",
            )
            if self.session_group is not None
            else None
        )
        assistant_case_usage = ModelUsageDelta.from_mapping(
            usage_delta(
                assistant_case_usage_before,
                usage_snapshot(self._assistant_client),
            )
        )
        _validate_usage_ledger(
            "retrieval-only assistant total",
            assistant_case_usage,
            controls,
            (),
        )
        return CaseExecution(
            output=output,
            stage_observations=observations,
            session_state_after=state_after,
        )

    def _cached_stage(
        self,
        *,
        cache_stage: str,
        observation_stage: str,
        input_payload: Mapping[str, Any],
        artifact_kind: str,
        producer: Callable[[], Mapping[str, Any]],
        decoder: Callable[[Mapping[str, Any]], _T],
        observations: Mapping[str, StageObservation],
        controls: AttemptControls,
    ) -> tuple[_T, StageObservation, StageExecution]:
        before, _ = controls.stage_counts(observation_stage)
        cache_key = build_stage_cache_key(
            self.spec.manifest,
            cache_stage,
            input_payload,
        )
        started = time.perf_counter()

        def produce_value() -> StageValue:
            artifact = _json_object(producer())
            after, _ = controls.stage_counts(observation_stage)
            delta = {kind: after[kind] - before[kind] for kind in EXTERNAL_CALL_KINDS}
            return StageValue(
                payload=_artifact_envelope(artifact_kind, artifact),
                external_calls=delta,
            )

        def failed_stage_observations(
            error_code: str,
            *,
            downstream_reason: str,
        ) -> dict[str, StageObservation]:
            failed = dict(observations)
            failed[observation_stage] = StageObservation(
                status="error",
                origin="fresh",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                cache_key=cache_key,
                source_external_calls={},
                error_code=error_code,
            )
            for stage, observation in tuple(failed.items()):
                if (
                    observation.status == "not_run"
                    and observation.unavailable_reason == "stage_not_executed"
                ):
                    failed[stage] = StageObservation.not_run(downstream_reason)
            return failed

        try:
            execution = self.spec.cache.execute(
                manifest=self.spec.manifest,
                stage=cache_stage,
                input_payload=input_payload,
                mode=self.cache_mode,
                producer=produce_value,
            )
        except RunnerStopped as error:
            raise CaseExecutionError(
                "controlled_interrupt",
                retryable=True,
                interrupted=True,
                stage_observations=failed_stage_observations(
                    "controlled_interrupt",
                    downstream_reason="upstream_controlled_interrupt",
                ),
            ) from error
        except ProviderCallError as error:
            persisted_code = f"provider_{error.error_code}"
            raise CaseExecutionError(
                persisted_code,
                retryable=error.retryable,
                stage_observations=failed_stage_observations(
                    persisted_code,
                    downstream_reason="upstream_provider_failure",
                ),
            ) from error
        artifact = _artifact_from_envelope(execution.payload, artifact_kind)
        restored = decoder(artifact)
        observation = _observation_from_execution(execution)
        return restored, observation, execution

    def _with_assistant_client(
        self,
        controls: AttemptControls,
        *,
        stage: str,
        kind: str,
        operation: Callable[[], _T],
    ) -> _T:
        if self.assistant.llm is not self._assistant_client:
            raise ExperimentContractError(
                "assistant completion client changed outside the adapter"
            )
        self.assistant.llm = _ControlledCompletionClient(
            self._assistant_client,
            controls,
            stage=stage,
            kind=kind,
        )
        try:
            return operation()
        finally:
            self.assistant.llm = self._assistant_client

    def _without_assistant_provider(
        self,
        operation: Callable[[], _T],
        *,
        phase: str,
    ) -> _T:
        if self.assistant.llm is not self._assistant_client:
            raise ExperimentContractError(
                "assistant completion client changed outside the adapter"
            )
        return _run_without_assistant_provider(
            self.assistant,
            operation,
            phase=phase,
        )


def decode_evaluation_output(
    value: Mapping[str, Any],
    *,
    expected_manifest: Mapping[str, Any] | None = None,
    expected_case: EvalCase | Mapping[str, Any] | None = None,
    expected_stage_identity: Mapping[str, Any] | None = None,
) -> EvaluatedCase:
    """Strictly decode output and recompute both record and trace."""

    payload = _json_object(value)
    _reject_raw_response_keys(payload)
    _require_exact_fields(
        "evaluation output",
        payload,
        {
            "evaluation_output_schema_version",
            "identity",
            "scoring_facts",
            "scoring_facts_sha256",
            "record",
            "trace_record",
            "result_sha256",
        },
    )
    _require_schema_version(
        "evaluation output",
        payload["evaluation_output_schema_version"],
        EVALUATION_OUTPUT_SCHEMA_VERSION,
    )
    identity = _validate_output_identity(payload["identity"])
    facts = _json_object(payload["scoring_facts"])
    if payload["scoring_facts_sha256"] != canonical_hash(facts):
        raise ExperimentContractError("evaluation scoring facts checksum mismatch")
    outcome = _outcome_from_facts(facts)
    _validate_stage_outcome_consistency(identity["stages"], outcome)
    case_artifact = eval_case_to_artifact(outcome.case)
    if identity["case_artifact_sha256"] != canonical_hash(case_artifact):
        raise ExperimentContractError(
            "evaluation output case identity disagrees with scoring facts"
        )
    if expected_manifest is not None:
        manifest = validate_experiment_manifest(expected_manifest)
        if identity["manifest_hash"] != manifest["identity"]["manifest_hash"]:
            raise ExperimentContractError(
                "evaluation output manifest identity mismatch"
            )
        _validate_outcome_manifest_contract(outcome, manifest)
    if expected_case is not None:
        expected_case_value = _expected_eval_case(expected_case)
        if identity["case_artifact_sha256"] != canonical_hash(
            eval_case_to_artifact(expected_case_value)
        ):
            raise ExperimentContractError("evaluation output case identity mismatch")
    if expected_stage_identity is not None:
        expected_stages = _validate_stage_identity(expected_stage_identity)
        if canonical_json_bytes(identity["stages"]) != canonical_json_bytes(
            expected_stages
        ):
            raise ExperimentContractError("evaluation output stage identity mismatch")
    recomputed = score_completed_case(outcome)
    record = eval_record_from_artifact(payload["record"])
    canonical_record = eval_record_to_artifact(record)
    expected_record = eval_record_to_artifact(recomputed.record)
    if canonical_json_bytes(canonical_record) != canonical_json_bytes(expected_record):
        raise ExperimentContractError(
            "evaluation record disagrees with its scoring facts"
        )
    trace_record = _json_object(payload["trace_record"])
    if canonical_json_bytes(trace_record) != canonical_json_bytes(
        recomputed.trace_record
    ):
        raise ExperimentContractError(
            "evaluation trace disagrees with its scoring facts"
        )
    scoring_facts_sha256 = canonical_hash(facts)
    result_core = {
        "identity": identity,
        "scoring_facts_sha256": scoring_facts_sha256,
        "record": canonical_record,
        "trace_record": trace_record,
    }
    if payload["result_sha256"] != canonical_hash(result_core):
        raise ExperimentContractError("evaluation output checksum mismatch")
    return EvaluatedCase(record=record, trace_record=deepcopy(trace_record))


class _ControlledCompletionClient:
    def __init__(
        self,
        client: CompletionClient,
        controls: AttemptControls,
        *,
        stage: str,
        kind: str,
    ) -> None:
        self._client = client
        self._controls = controls
        self._stage = stage
        self._kind = kind

    @property
    def usage(self) -> Any:
        return getattr(self._client, "usage", None)

    @property
    def propagate_provider_errors(self) -> bool:
        """Request strict provider failures only inside the experiment runner."""

        return True

    @property
    def propagate_control_errors(self) -> bool:
        """Prevent runner stop/contract failures from entering fallback artifacts."""

        return True

    def complete(self, prompt: str) -> Any:
        usage_before = usage_snapshot(self._client)
        attempted_before, failed_before = self._controls.stage_counts(self._stage)

        def dispatch() -> Any:
            try:
                return self._client.complete(prompt)
            except (
                ProviderCallError,
                RunnerStopped,
                RunnerContractError,
                ExperimentContractError,
            ):
                raise
            except Exception as error:
                safe_error = provider_call_error(
                    error,
                    provider="completion_client",
                    operation=self._kind,
                )
            raise safe_error from None

        try:
            return self._controls.call(
                self._stage,
                self._kind,
                dispatch,
            )
        finally:
            attempted_after, failed_after = self._controls.stage_counts(self._stage)
            usage = usage_delta(usage_before, usage_snapshot(self._client))
            attempted = attempted_after[self._kind] - attempted_before[self._kind]
            failed = failed_after[self._kind] - failed_before[self._kind]
            role = "assistant" if self._kind == "generation" else self._kind
            persisted_usage = {**usage, "calls": attempted, "failed_calls": failed}
            try:
                self._controls.record_model_usage(role, persisted_usage)
            except ExperimentContractError as error:
                # The attempt ledger is authoritative for dispatch counts.  If a
                # malformed client usage object cannot be retained safely, keep
                # those counts and fail closed without persisting invented tokens.
                self._controls.record_model_usage(
                    role,
                    {
                        "calls": attempted,
                        "failed_calls": failed,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "token_usage_calls": 0,
                        "latency_ms": 0.0,
                    },
                )
                usage_record_error: ExperimentContractError | None = error
            else:
                usage_record_error = None
            if usage["calls"] != attempted or usage["failed_calls"] != failed:
                raise ExperimentContractError(
                    "completion usage disagrees with its controlled provider call"
                )
            if usage_record_error is not None:
                raise usage_record_error


class _ProviderForbiddenCompletionClient:
    def __init__(self, client: CompletionClient, *, phase: str) -> None:
        self._client = client
        self._phase = phase
        self.attempted = False

    @property
    def usage(self) -> Any:
        return getattr(self._client, "usage", None)

    def complete(self, prompt: str) -> Any:
        self.attempted = True
        raise ExperimentContractError(
            f"assistant {self._phase} must not call a completion provider"
        )


def _run_without_assistant_provider(
    assistant: LegalChatAssistant,
    operation: Callable[[], _T],
    *,
    phase: str,
) -> _T:
    client = assistant.llm
    proxy = _ProviderForbiddenCompletionClient(client, phase=phase)
    assistant.llm = proxy
    try:
        result = operation()
    finally:
        assistant.llm = client
    if proxy.attempted:
        raise ExperimentContractError(
            f"assistant {phase} attempted a forbidden completion call"
        )
    return result


def _validate_usage_ledger(
    name: str,
    usage: ModelUsageDelta,
    controls: AttemptControls,
    bindings: tuple[tuple[str, str], ...],
    *,
    model_usage_role: str | None = None,
) -> None:
    attempted = 0
    failed = 0
    for stage, kind in bindings:
        stage_attempted, stage_failed = controls.stage_counts(stage)
        attempted += stage_attempted[kind]
        failed += stage_failed[kind]
    if usage.calls != attempted or usage.failed_calls != failed:
        raise ExperimentContractError(
            f"{name} CompletionUsage disagrees with the attempt call ledger"
        )
    if model_usage_role is not None:
        recorded = controls.model_usage().get(model_usage_role)
        if recorded is None or canonical_json_bytes(recorded) != canonical_json_bytes(
            asdict(usage)
        ):
            raise ExperimentContractError(
                f"{name} CompletionUsage disagrees with persisted attempt usage"
            )


def _base_observations() -> dict[str, StageObservation]:
    return {
        "query_analysis": StageObservation.not_run("stage_not_executed"),
        "query_embedding": StageObservation.not_run(
            "query_embedding_disabled_by_retriever_contract"
        ),
        "retrieval": StageObservation.not_run("stage_not_executed"),
        "rerank": StageObservation.not_run("rerank_disabled"),
        "generation": StageObservation.not_run("stage_not_executed"),
        "verification": StageObservation.not_run("stage_not_executed"),
        "judge": StageObservation.not_run("stage_not_executed"),
    }


def _observation_from_execution(
    execution: StageExecution,
    *,
    status: str = "succeeded",
    error_code: str | None = None,
    extra_duration_ms: float = 0.0,
) -> StageObservation:
    if not isinstance(execution, StageExecution):
        raise ExperimentContractError("stage execution has an invalid type")
    if (
        isinstance(extra_duration_ms, bool)
        or not isinstance(extra_duration_ms, (int, float))
        or not math.isfinite(float(extra_duration_ms))
        or extra_duration_ms < 0
    ):
        raise ExperimentContractError("extra stage duration must be non-negative")
    return StageObservation(
        status=status,
        origin=execution.origin,
        duration_ms=round(execution.duration_ms + float(extra_duration_ms), 3),
        cache_key=execution.cache_key,
        source_external_calls=execution.source_external_calls,
        error_code=error_code,
    )


def _build_stage_identity(
    observations: Mapping[str, StageObservation],
    executions: Mapping[str, StageExecution | None],
) -> dict[str, dict[str, Any]]:
    if set(observations) != set(OBSERVATION_STAGES) or set(executions) != set(
        OBSERVATION_STAGES
    ):
        raise ExperimentContractError(
            "stage identity inputs must contain all observation stages"
        )
    identity: dict[str, dict[str, Any]] = {}
    for stage in OBSERVATION_STAGES:
        observation = observations[stage]
        execution = executions[stage]
        if not isinstance(observation, StageObservation):
            raise ExperimentContractError("stage observation has an invalid type")
        if execution is None:
            if observation.status != "not_run":
                raise ExperimentContractError(
                    "executed stage identity is missing its cache execution"
                )
            cache_key = None
            payload_sha256 = None
        else:
            if observation.status == "not_run":
                raise ExperimentContractError(
                    "not-run stage cannot contain a cache execution"
                )
            if observation.cache_key != execution.cache_key:
                raise ExperimentContractError(
                    "stage observation cache key disagrees with execution"
                )
            cache_key = execution.cache_key
            payload_sha256 = execution.payload_sha256
        identity[stage] = {
            "status": observation.status,
            "cache_key": cache_key,
            "payload_sha256": payload_sha256,
        }
    return _validate_stage_identity(identity)


def _validate_stage_identity(value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    payload = _json_object(value)
    if set(payload) != set(OBSERVATION_STAGES):
        raise ExperimentContractError(
            "stage identity must contain every observation stage exactly once"
        )
    result: dict[str, dict[str, Any]] = {}
    for stage in OBSERVATION_STAGES:
        item = _json_object(payload[stage])
        _require_exact_fields(
            f"stage identity {stage}",
            item,
            {"status", "cache_key", "payload_sha256"},
        )
        status = item["status"]
        if status not in {"succeeded", "error", "not_run"}:
            raise ExperimentContractError("stage identity status is invalid")
        cache_key = item["cache_key"]
        payload_sha256 = item["payload_sha256"]
        if status == "not_run":
            if cache_key is not None or payload_sha256 is not None:
                raise ExperimentContractError(
                    "not-run stage identity cannot contain cache hashes"
                )
        else:
            for name, digest in (
                ("cache_key", cache_key),
                ("payload_sha256", payload_sha256),
            ):
                if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                    raise ExperimentContractError(
                        f"executed stage identity {name} must be a SHA-256"
                    )
        result[stage] = {
            "status": status,
            "cache_key": cache_key,
            "payload_sha256": payload_sha256,
        }
    return result


def _validate_output_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentContractError("evaluation output identity must be an object")
    payload = _json_object(value)
    _require_exact_fields(
        "evaluation output identity",
        payload,
        {"manifest_hash", "case_artifact_sha256", "stages"},
    )
    for name in ("manifest_hash", "case_artifact_sha256"):
        digest = payload[name]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ExperimentContractError(
                f"evaluation output identity {name} must be a SHA-256"
            )
    return {
        "manifest_hash": payload["manifest_hash"],
        "case_artifact_sha256": payload["case_artifact_sha256"],
        "stages": _validate_stage_identity(payload["stages"]),
    }


def _validate_stage_outcome_consistency(
    stages: Mapping[str, Mapping[str, Any]],
    outcome: CompletedCaseOutcome,
) -> None:
    expected = {
        "query_analysis": "succeeded",
        "query_embedding": "not_run",
        "retrieval": "succeeded",
        "rerank": "not_run",
        "generation": "not_run",
        "verification": "not_run",
        "judge": "not_run",
    }
    if outcome.generate:
        expected["generation"] = (
            "error" if outcome.generation_error is not None else "succeeded"
        )
        expected["verification"] = "succeeded"
        if outcome.judge_result is not None:
            expected["judge"] = (
                "error" if outcome.judge_result.status == "error" else "succeeded"
            )
    actual = {stage: stages[stage]["status"] for stage in OBSERVATION_STAGES}
    if actual != expected:
        raise ExperimentContractError(
            "evaluation output stage identity disagrees with execution facts"
        )


def _validate_outcome_manifest_contract(
    outcome: CompletedCaseOutcome,
    manifest: Mapping[str, Any],
) -> None:
    expected = {
        "model": manifest["contracts"]["generation"]["model"],
        "retriever": manifest["contracts"]["retrieval"]["kind"],
        "chunk_strategy": manifest["contracts"]["chunking"]["strategy"],
        "top_k": manifest["contracts"]["retrieval"]["parameters"]["top_k"],
        "generate": manifest["config"]["summary"]["generate"],
        "judge_configured": manifest["contracts"]["judge"]["enabled"],
    }
    actual = {
        "model": outcome.model,
        "retriever": outcome.retriever,
        "chunk_strategy": outcome.chunk_strategy,
        "top_k": outcome.top_k,
        "generate": outcome.generate,
        "judge_configured": outcome.judge_configured,
    }
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise ExperimentContractError(
            "evaluation execution facts disagree with the manifest contract"
        )
    adaptive_enabled = outcome.adaptive_trace.get("enabled")
    if adaptive_enabled is not manifest["config"]["summary"]["adaptive_enabled"]:
        raise ExperimentContractError(
            "evaluation adaptive facts disagree with the manifest contract"
        )


def _expected_eval_case(value: EvalCase | Mapping[str, Any]) -> EvalCase:
    if isinstance(value, EvalCase):
        validate_eval_case(value)
        return deepcopy(value)
    if not isinstance(value, Mapping):
        raise ExperimentContractError("expected_case has an invalid type")
    if "evaluation_case" in value:
        _, case = validate_manifest_case(value)
        return case
    return eval_case_from_artifact(value)


def _reject_raw_response_keys(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "raw_response":
                raise ExperimentContractError(
                    f"evaluation output contains raw_response at {path}"
                )
            _reject_raw_response_keys(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_raw_response_keys(item, path=f"{path}[{index}]")


def _require_schema_version(name: str, value: Any, expected: int) -> int:
    if type(value) is not int or value != expected:
        raise ExperimentContractError(f"{name} schema is unsupported")
    return value


def _artifact_envelope(kind: str, artifact: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(kind, str) or not kind.strip():
        raise ExperimentContractError("adapter artifact kind must be non-empty")
    canonical_artifact = _json_object(artifact)
    return {
        "adapter_artifact_schema_version": ADAPTER_ARTIFACT_SCHEMA_VERSION,
        "artifact_kind": kind,
        "artifact": canonical_artifact,
        "artifact_sha256": canonical_hash(canonical_artifact),
    }


def _artifact_from_envelope(value: Any, expected_kind: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentContractError("cached adapter artifact must be an object")
    payload = _json_object(value)
    _require_exact_fields(
        "cached adapter artifact",
        payload,
        {
            "adapter_artifact_schema_version",
            "artifact_kind",
            "artifact",
            "artifact_sha256",
        },
    )
    _require_schema_version(
        "cached adapter artifact",
        payload["adapter_artifact_schema_version"],
        ADAPTER_ARTIFACT_SCHEMA_VERSION,
    )
    if payload["artifact_kind"] != expected_kind:
        raise ExperimentContractError("cached adapter artifact kind mismatch")
    artifact = _json_object(payload["artifact"])
    if payload["artifact_sha256"] != canonical_hash(artifact):
        raise ExperimentContractError("cached adapter artifact checksum mismatch")
    return artifact


def _verification_context_payload(
    context: VerificationContext | None,
) -> dict[str, Any]:
    if context is None:
        return {"configured": False, "snapshot_id": None, "allowed_scope_ids": None}
    if not isinstance(context, VerificationContext):
        raise ExperimentContractError("verification context has an invalid type")
    payload = {
        "configured": True,
        "snapshot_id": context.snapshot_id,
        "allowed_scope_ids": (
            list(context.allowed_scope_ids)
            if context.allowed_scope_ids is not None
            else None
        ),
    }
    canonical_json_bytes(payload)
    return payload


def _retrieval_runtime_identity(
    assistant: LegalChatAssistant,
) -> dict[str, Any]:
    retriever_name = getattr(assistant.retriever, "name", None)
    if not isinstance(retriever_name, str) or not retriever_name.strip():
        raise ExperimentContractError("retriever name must be a non-empty string")
    for name in ("adaptive_enabled", "adaptive_use_llm"):
        if not isinstance(getattr(assistant, name), bool):
            raise ExperimentContractError(f"assistant {name} must be a boolean")
    for name, minimum in (
        ("adaptive_max_queries", 1),
        ("normalizer_retries", 0),
    ):
        value = getattr(assistant, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ExperimentContractError(
                f"assistant {name} must be an integer >= {minimum}"
            )
    per_plan_top_k = assistant.adaptive_per_plan_top_k
    if per_plan_top_k is not None and (
        isinstance(per_plan_top_k, bool)
        or not isinstance(per_plan_top_k, int)
        or per_plan_top_k < 1
    ):
        raise ExperimentContractError(
            "assistant adaptive_per_plan_top_k must be null or positive"
        )
    payload = {
        "retriever_name": retriever_name,
        "provider_free": True,
        "uses_query_embedding": False,
        "top_k": assistant.top_k,
        "adaptive_enabled": assistant.adaptive_enabled,
        "adaptive_use_llm": assistant.adaptive_use_llm,
        "adaptive_max_queries": assistant.adaptive_max_queries,
        "adaptive_per_plan_top_k": assistant.adaptive_per_plan_top_k,
        "normalizer_retries": assistant.normalizer_retries,
    }
    return _json_object(payload)


def _retrieval_only_to_artifact(
    adaptive: Any,
    analysis_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    if adaptive.analysis != query_analysis_from_artifact(analysis_artifact):
        raise ExperimentContractError("retrieval-only analysis mismatch")
    artifact = {
        "artifact_schema_version": ADAPTER_ARTIFACT_SCHEMA_VERSION,
        "retrieval_only_result": {
            "analysis_sha256": canonical_hash(analysis_artifact),
            "results": [
                search_result_to_artifact(result) for result in adaptive.results
            ],
            "evidence_check": evidence_check_to_artifact(adaptive.evidence_check),
            "adaptive_trace": adaptive.to_trace(),
        },
    }
    _retrieval_only_from_artifact(artifact, analysis_artifact)
    return _json_object(artifact)


def _retrieval_only_from_artifact(
    artifact: Mapping[str, Any],
    analysis_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _json_object(artifact)
    _require_exact_fields(
        "retrieval-only artifact",
        payload,
        {"artifact_schema_version", "retrieval_only_result"},
    )
    _require_schema_version(
        "retrieval-only artifact",
        payload["artifact_schema_version"],
        ADAPTER_ARTIFACT_SCHEMA_VERSION,
    )
    body = _json_object(payload["retrieval_only_result"])
    _require_exact_fields(
        "retrieval-only result",
        body,
        {"analysis_sha256", "results", "evidence_check", "adaptive_trace"},
    )
    if body["analysis_sha256"] != canonical_hash(analysis_artifact):
        raise ExperimentContractError("retrieval-only analysis checksum mismatch")
    raw_results = body["results"]
    if not isinstance(raw_results, list):
        raise ExperimentContractError("retrieval-only results must be a list")
    results = [search_result_from_artifact(item) for item in raw_results]
    for expected_rank, result in enumerate(results, start=1):
        if result.rank != expected_rank:
            raise ExperimentContractError(
                "retrieval-only result ranks must be contiguous"
            )
    evidence = evidence_check_from_artifact(body["evidence_check"])
    if evidence.checked_result_count != len(results):
        raise ExperimentContractError(
            "retrieval-only evidence count does not match results"
        )
    adaptive_trace = _json_object(body["adaptive_trace"])
    return {
        "results": results,
        "evidence_check": evidence,
        "adaptive_trace": adaptive_trace,
    }


def _judge_result_to_artifact(result: JudgeResult) -> dict[str, Any]:
    if not isinstance(result, JudgeResult):
        raise ExperimentContractError("judge result has an invalid type")
    artifact = {
        "artifact_schema_version": ADAPTER_ARTIFACT_SCHEMA_VERSION,
        "judge_result": {
            "faithfulness": result.faithfulness,
            "relevance": result.relevance,
            "completeness": result.completeness,
            "passed": result.passed,
            "comment": result.comment,
            "source": result.source,
            "error": result.error,
            "status": result.status,
            "error_code": result.error_code,
        },
    }
    _judge_result_from_artifact(artifact)
    return _json_object(artifact)


def _judge_result_from_artifact(artifact: Mapping[str, Any]) -> JudgeResult:
    payload = _json_object(artifact)
    _require_exact_fields(
        "judge result artifact",
        payload,
        {"artifact_schema_version", "judge_result"},
    )
    _require_schema_version(
        "judge result artifact",
        payload["artifact_schema_version"],
        ADAPTER_ARTIFACT_SCHEMA_VERSION,
    )
    body = _json_object(payload["judge_result"])
    fields = {
        "faithfulness",
        "relevance",
        "completeness",
        "passed",
        "comment",
        "source",
        "error",
        "status",
        "error_code",
    }
    _require_exact_fields("judge result", body, fields)
    for name in ("comment", "source", "error", "status"):
        if not isinstance(body[name], str):
            raise ExperimentContractError(f"judge result {name} must be a string")
    if body["error_code"] is not None and not isinstance(body["error_code"], str):
        raise ExperimentContractError(
            "judge result error_code must be a string or null"
        )
    for name in ("faithfulness", "relevance", "completeness"):
        value = body[name]
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ExperimentContractError(f"judge result {name} is invalid")
    if body["passed"] is not None and not isinstance(body["passed"], bool):
        raise ExperimentContractError("judge result passed must be boolean or null")
    return JudgeResult(**body, raw_response="")


def _evaluation_output(
    outcome: CompletedCaseOutcome,
    evaluated: EvaluatedCase,
    *,
    manifest: Mapping[str, Any],
    stage_identity: Mapping[str, Any],
) -> dict[str, Any]:
    validated_manifest = validate_experiment_manifest(manifest)
    facts = _outcome_to_facts(outcome)
    record = eval_record_to_artifact(evaluated.record)
    trace_record = _json_object(evaluated.trace_record)
    identity = {
        "manifest_hash": validated_manifest["identity"]["manifest_hash"],
        "case_artifact_sha256": canonical_hash(eval_case_to_artifact(outcome.case)),
        "stages": _validate_stage_identity(stage_identity),
    }
    result_core = {
        "identity": identity,
        "scoring_facts_sha256": canonical_hash(facts),
        "record": record,
        "trace_record": trace_record,
    }
    return {
        "evaluation_output_schema_version": EVALUATION_OUTPUT_SCHEMA_VERSION,
        "identity": identity,
        "scoring_facts": facts,
        "scoring_facts_sha256": canonical_hash(facts),
        "record": record,
        "trace_record": trace_record,
        "result_sha256": canonical_hash(result_core),
    }


def _without_raw_response(value: Any) -> Any:
    """Return canonical JSON with provider-response fields removed recursively."""

    if isinstance(value, Mapping):
        return {
            key: _without_raw_response(item)
            for key, item in value.items()
            if key != "raw_response"
        }
    if isinstance(value, (list, tuple)):
        return [_without_raw_response(item) for item in value]
    return json.loads(canonical_json_bytes(value).decode("utf-8"))


def _safe_scoring_result(value: Any) -> Any:
    artifact = search_result_to_artifact(value)
    scrubbed = _without_raw_response(artifact)
    return search_result_from_artifact(scrubbed)


_OUTCOME_FACT_FIELDS = {
    "facts_schema_version",
    "case",
    "model",
    "retriever",
    "chunk_strategy",
    "top_k",
    "generate",
    "results",
    "answer",
    "analysis",
    "adaptive_trace",
    "evidence_check",
    "verification",
    "structured_answer",
    "pre_fallback_answer",
    "pre_fallback_verification",
    "generation_kind",
    "generation_error",
    "judge_configured",
    "judge_result",
    "error",
    "latency_ms",
    "assistant_usage",
    "normalizer_usage",
    "judge_usage",
    "trace_metadata",
}


def _outcome_to_facts(outcome: CompletedCaseOutcome) -> dict[str, Any]:
    # The raw generation/judge response is deliberately absent.  Every value
    # retained here is sufficient to recompute the public record and trace.
    facts = {
        "facts_schema_version": EVALUATION_OUTPUT_SCHEMA_VERSION,
        "case": eval_case_to_artifact(outcome.case),
        "model": outcome.model,
        "retriever": outcome.retriever,
        "chunk_strategy": outcome.chunk_strategy,
        "top_k": outcome.top_k,
        "generate": outcome.generate,
        "results": [search_result_to_artifact(item) for item in outcome.results],
        "answer": outcome.answer,
        "analysis": query_analysis_to_artifact(outcome.analysis),
        "adaptive_trace": dict(outcome.adaptive_trace),
        "evidence_check": evidence_check_to_artifact(outcome.evidence_check),
        "verification": (
            verification_result_to_artifact(outcome.verification)
            if outcome.verification is not None
            else None
        ),
        "structured_answer": (
            structured_answer_to_artifact(outcome.structured_answer)
            if outcome.structured_answer is not None
            else None
        ),
        "pre_fallback_answer": (
            structured_answer_to_artifact(outcome.pre_fallback_answer)
            if outcome.pre_fallback_answer is not None
            else None
        ),
        "pre_fallback_verification": (
            verification_result_to_artifact(outcome.pre_fallback_verification)
            if outcome.pre_fallback_verification is not None
            else None
        ),
        "generation_kind": outcome.generation_kind,
        "generation_error": outcome.generation_error,
        "judge_configured": outcome.judge_configured,
        "judge_result": (
            _judge_result_to_artifact(outcome.judge_result)
            if outcome.judge_result is not None
            else None
        ),
        "error": outcome.error,
        "latency_ms": outcome.latency_ms,
        "assistant_usage": asdict(outcome.assistant_usage),
        "normalizer_usage": asdict(outcome.normalizer_usage),
        "judge_usage": asdict(outcome.judge_usage),
        "trace_metadata": (
            dict(outcome.trace_metadata) if outcome.trace_metadata is not None else None
        ),
    }
    canonical = _json_object(facts)
    # Reuse the decoder as the final strictness boundary.
    _outcome_from_facts(canonical)
    return canonical


def _outcome_from_facts(value: Mapping[str, Any]) -> CompletedCaseOutcome:
    facts = _json_object(value)
    _require_exact_fields("completed case facts", facts, _OUTCOME_FACT_FIELDS)
    _require_schema_version(
        "completed case facts",
        facts["facts_schema_version"],
        EVALUATION_OUTPUT_SCHEMA_VERSION,
    )
    raw_results = facts["results"]
    if not isinstance(raw_results, list):
        raise ExperimentContractError("completed case facts results must be a list")
    results = tuple(search_result_from_artifact(item) for item in raw_results)
    verification = (
        None
        if facts["verification"] is None
        else verification_result_from_artifact(facts["verification"])
    )
    structured_answer = (
        None
        if facts["structured_answer"] is None
        else structured_answer_from_artifact(facts["structured_answer"])
    )
    pre_fallback_answer = (
        None
        if facts["pre_fallback_answer"] is None
        else structured_answer_from_artifact(facts["pre_fallback_answer"])
    )
    pre_fallback_verification = (
        None
        if facts["pre_fallback_verification"] is None
        else verification_result_from_artifact(facts["pre_fallback_verification"])
    )
    judge_result = (
        None
        if facts["judge_result"] is None
        else _judge_result_from_artifact(facts["judge_result"])
    )
    for name in (
        "model",
        "retriever",
        "chunk_strategy",
        "answer",
        "generation_kind",
        "error",
    ):
        if not isinstance(facts[name], str):
            raise ExperimentContractError(
                f"completed case facts {name} must be a string"
            )
    if facts["generation_error"] is not None and not isinstance(
        facts["generation_error"], str
    ):
        raise ExperimentContractError(
            "completed case facts generation_error must be a string or null"
        )
    if not isinstance(facts["generate"], bool) or not isinstance(
        facts["judge_configured"], bool
    ):
        raise ExperimentContractError(
            "completed case generate/judge_configured must be booleans"
        )
    for name in ("top_k", "latency_ms"):
        item = facts[name]
        if isinstance(item, bool) or not isinstance(item, int):
            raise ExperimentContractError(
                f"completed case facts {name} must be an integer"
            )
    adaptive_trace = _json_object(facts["adaptive_trace"])
    trace_metadata = (
        None
        if facts["trace_metadata"] is None
        else _json_object(facts["trace_metadata"])
    )
    outcome = CompletedCaseOutcome(
        case=eval_case_from_artifact(facts["case"]),
        model=facts["model"],
        retriever=facts["retriever"],
        chunk_strategy=facts["chunk_strategy"],
        top_k=facts["top_k"],
        generate=facts["generate"],
        results=results,
        answer=facts["answer"],
        analysis=query_analysis_from_artifact(facts["analysis"]),
        adaptive_trace=adaptive_trace,
        evidence_check=evidence_check_from_artifact(facts["evidence_check"]),
        verification=verification,
        structured_answer=structured_answer,
        pre_fallback_answer=pre_fallback_answer,
        pre_fallback_verification=pre_fallback_verification,
        generation_kind=facts["generation_kind"],
        generation_error=facts["generation_error"],
        judge_configured=facts["judge_configured"],
        judge_result=judge_result,
        error=facts["error"],
        latency_ms=facts["latency_ms"],
        assistant_usage=ModelUsageDelta.from_mapping(facts["assistant_usage"]),
        normalizer_usage=ModelUsageDelta.from_mapping(facts["normalizer_usage"]),
        judge_usage=ModelUsageDelta.from_mapping(facts["judge_usage"]),
        trace_metadata=trace_metadata,
    )
    # Pure scoring owns the complete cross-field validator.
    score_completed_case(outcome)
    return outcome


def _validate_spec(spec: EvaluationRuntimeSpec) -> EvaluationRuntimeSpec:
    if not isinstance(spec, EvaluationRuntimeSpec):
        raise TypeError("spec must be an EvaluationRuntimeSpec")
    manifest = validate_experiment_manifest(spec.manifest)
    if not isinstance(spec.cache, ExactStageCache):
        raise ExperimentContractError("spec.cache must be an ExactStageCache")
    if not isinstance(spec.model, str) or not spec.model.strip():
        raise ExperimentContractError("spec.model must be non-empty")
    if not isinstance(spec.chunk_strategy, str) or not spec.chunk_strategy.strip():
        raise ExperimentContractError("spec.chunk_strategy must be non-empty")
    if not isinstance(spec.generate, bool):
        raise ExperimentContractError("spec.generate must be a boolean")
    generation_model = manifest["contracts"]["generation"]["model"]
    if spec.model != generation_model:
        raise ExperimentContractError(
            "spec.model must match contracts.generation.model"
        )
    chunk_strategy = manifest["contracts"]["chunking"]["strategy"]
    if spec.chunk_strategy != chunk_strategy:
        raise ExperimentContractError(
            "spec.chunk_strategy must match contracts.chunking.strategy"
        )
    configured_generate = manifest["config"]["summary"].get("generate")
    if not isinstance(configured_generate, bool):
        raise ExperimentContractError(
            "manifest config.summary.generate must be a boolean"
        )
    if spec.generate is not configured_generate:
        raise ExperimentContractError(
            "spec.generate must match manifest config.summary.generate"
        )
    _manifest_assistant_runtime(manifest)
    _manifest_provider_timeouts(manifest)
    execution_mode = manifest["execution_mode"]
    allowed_execution_modes = (
        {"smoke-generation", "full-regression"}
        if spec.generate
        else {"offline", "retrieval"}
    )
    if execution_mode not in allowed_execution_modes:
        raise ExperimentContractError(
            "manifest execution_mode is incompatible with spec.generate"
        )
    retrieval_contract = manifest["contracts"]["retrieval"]
    parameters = retrieval_contract["parameters"]
    if set(parameters) != {"top_k"}:
        raise ExperimentContractError(
            "D4c retrieval contract parameters must contain only top_k"
        )
    if retrieval_contract["filters"] != {}:
        raise ExperimentContractError("D4c does not support implicit retrieval filters")
    retriever_name = getattr(spec.retriever, "name", None)
    if not isinstance(retriever_name, str) or not retriever_name.strip():
        raise ExperimentContractError("retriever name must be a non-empty string")
    if retrieval_contract["kind"] != retriever_name:
        raise ExperimentContractError(
            "retriever name must match contracts.retrieval.kind"
        )
    if not isinstance(spec.retriever_provider_free, bool):
        raise ExperimentContractError("retriever_provider_free must be a boolean")
    if type(spec.retriever) is not BM25Retriever:
        if not spec.retriever_provider_free or (
            getattr(spec.retriever, "provider_free", None) is not True
        ):
            raise ExperimentContractError(
                "custom retrievers require both spec authorization and an explicit "
                "provider_free=True declaration"
            )
        if getattr(spec.retriever, "uses_query_embedding", None) is not False:
            raise ExperimentContractError(
                "D4c custom retrievers must declare uses_query_embedding=False; "
                "query embedding observation is implemented in D5"
            )
        if manifest["runtime"]["concurrency"] > 1 and (
            getattr(spec.retriever, "thread_safe", None) is not True
        ):
            raise ExperimentContractError(
                "concurrent custom retrievers require thread_safe=True"
            )
    if manifest["contracts"]["rerank"]["enabled"]:
        raise ExperimentContractError(
            "D4c cannot prove separate reranker provider control"
        )
    judge_enabled = manifest["contracts"]["judge"]["enabled"]
    if judge_enabled != (spec.judge_client_factory is not None):
        raise ExperimentContractError(
            "judge contract and judge_client_factory must agree"
        )
    if not spec.generate and judge_enabled:
        raise ExperimentContractError(
            "retrieval-only evaluation cannot enable the answer judge"
        )
    if spec.generate and spec.adaptive_llm_client_factory is not None:
        raise ExperimentContractError(
            "generated evaluation cannot configure a separate adaptive client"
        )
    copied_metadata = (
        _json_object(spec.trace_metadata) if spec.trace_metadata is not None else None
    )
    return EvaluationRuntimeSpec(
        manifest=manifest,
        cache=spec.cache,
        retriever=spec.retriever,
        assistant_factory=spec.assistant_factory,
        model=spec.model,
        chunk_strategy=spec.chunk_strategy,
        generate=spec.generate,
        retriever_provider_free=spec.retriever_provider_free,
        judge_client_factory=spec.judge_client_factory,
        adaptive_llm_client_factory=spec.adaptive_llm_client_factory,
        trace_metadata=copied_metadata,
    )


def _json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentContractError("value must be a JSON object")
    copied = json.loads(canonical_json_bytes(dict(value)).decode("utf-8"))
    if not isinstance(copied, dict):  # pragma: no cover
        raise ExperimentContractError("value must be a JSON object")
    return copied


def _provider_timeout_value(name: str, value: Any) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
        or value > MAX_PROVIDER_TIMEOUT_SECONDS
    ):
        raise ExperimentContractError(
            f"provider timeout {name!r} must be null or a bounded positive number"
        )
    return float(value)


def _manifest_provider_timeouts(manifest: Mapping[str, Any]) -> dict[str, float | None]:
    summary = manifest["config"]["summary"]
    value = summary.get("provider_timeouts")
    required = {"assistant", "judge", "adaptive"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise ExperimentContractError(
            "manifest config.summary.provider_timeouts fields are invalid"
        )
    return {
        name: _provider_timeout_value(name, value[name]) for name in sorted(required)
    }


def _manifest_assistant_runtime(manifest: Mapping[str, Any]) -> dict[str, Any]:
    summary = manifest["config"]["summary"]
    required = {
        "top_k",
        "memory_token_limit",
        "adaptive_enabled",
        "adaptive_use_llm",
        "adaptive_max_queries",
        "adaptive_per_plan_top_k",
        "adaptive_normalizer_retries",
        "condense_with_llm",
    }
    missing = sorted(required - set(summary))
    if missing:
        raise ExperimentContractError(
            "manifest config.summary is missing assistant runtime fields: "
            + ",".join(missing)
        )
    for name in ("adaptive_enabled", "adaptive_use_llm", "condense_with_llm"):
        if not isinstance(summary[name], bool):
            raise ExperimentContractError(
                f"manifest config.summary.{name} must be a boolean"
            )
    for name, minimum in (
        ("top_k", 1),
        ("memory_token_limit", 1),
        ("adaptive_max_queries", 1),
        ("adaptive_normalizer_retries", 0),
    ):
        value = summary[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ExperimentContractError(
                f"manifest config.summary.{name} must be an integer >= {minimum}"
            )
    per_plan_top_k = summary["adaptive_per_plan_top_k"]
    if per_plan_top_k is not None and (
        isinstance(per_plan_top_k, bool)
        or not isinstance(per_plan_top_k, int)
        or per_plan_top_k < 1
    ):
        raise ExperimentContractError(
            "manifest config.summary.adaptive_per_plan_top_k must be null or positive"
        )
    contract_top_k = manifest["contracts"]["retrieval"]["parameters"]["top_k"]
    if summary["top_k"] != contract_top_k:
        raise ExperimentContractError(
            "manifest config.summary.top_k must match the retrieval contract"
        )
    return {
        name: deepcopy(summary[name])
        for name in (
            "top_k",
            "memory_token_limit",
            "adaptive_enabled",
            "adaptive_use_llm",
            "adaptive_max_queries",
            "adaptive_per_plan_top_k",
            "adaptive_normalizer_retries",
            "condense_with_llm",
        )
    }


def _require_completion_usage(name: str, client: Any) -> CompletionUsage:
    usage = getattr(client, "usage", None)
    if not isinstance(usage, CompletionUsage):
        raise ExperimentContractError(
            f"{name} must expose an isolated CompletionUsage ledger"
        )
    if getattr(client, "hidden_retries_disabled", None) is not True:
        raise ExperimentContractError(
            f"{name} must declare hidden_retries_disabled=True"
        )
    return usage


def _require_exact_fields(
    name: str, value: Mapping[str, Any], fields: set[str]
) -> None:
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing or unknown:
        raise ExperimentContractError(
            f"{name} fields are invalid; missing={missing}, unknown={unknown}"
        )
