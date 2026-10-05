"""A fixed, explicitly authorized first live smoke, not an online-service gate.

The default preflight performs no provider initialization, key loading or calls.
Raw data is confined to a fresh ignored artifacts/experiments directory. Never
automatically rerun this script after a failed or interrupted paid execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from legal_rag.chat import LegalChatAssistant, build_qa_prompt  # noqa: E402
from legal_rag import chat as chat_module  # noqa: E402
from legal_rag.chunking import article_chunks, load_chunks  # noqa: E402
from legal_rag.config import load_config  # noqa: E402
from legal_rag.data import parse_law_file  # noqa: E402
from legal_rag.env import live_model_calls_allowed  # noqa: E402
from legal_rag.evaluation import load_eval_cases  # noqa: E402
from legal_rag.evaluation_scoring import (  # noqa: E402
    CompletedCaseOutcome,
    ModelUsageDelta,
    hit_at_k,
    score_completed_case,
    target_coverage,
)
from legal_rag.evidence import check_evidence_sufficiency  # noqa: E402
from legal_rag.llm import SiliconFlowClient, usage_delta, usage_snapshot  # noqa: E402
from legal_rag.models import Chunk  # noqa: E402
from legal_rag.retrieval import BM25Retriever  # noqa: E402

MODEL = "Qwen/Qwen3.5-35B-A3B"
BASE_URL = "https://api.siliconflow.cn/v1"
MAX_CALLS = 10
BUDGET_CNY = Decimal("2")
INPUT_RATE_CNY = Decimal("0.40")
OUTPUT_RATE_CNY = Decimal("3.20")
MAX_OUTPUT_TOKENS = 1536
MAX_PROMPT_UTF8_BYTES = 24000
REQUEST_TIMEOUT_SECONDS = 60
AUTHORIZATION_ID = "qwen35b-first-smoke-20261003"
AUTHORIZATION_LEDGER = Path("artifacts/experiments/.live_authorizations") / (
    AUTHORIZATION_ID + ".json"
)
REPAIR_AUTHORIZATION_ID = "qwen35b-repair-smoke-20261003"
REPAIR_QA_PROMPT_VERSION = "m1-structured-qa-citation-alignment-v2"
REPAIR_AUTHORIZATION_LEDGER = Path("artifacts/experiments/.live_authorizations") / (
    REPAIR_AUTHORIZATION_ID + ".json"
)
PRIOR_RUN_ID = "live_smoke_20261003_first"
PRIOR_MANIFEST_SHA256 = "79fa56a4ffd4d6503567674e39fefa5fcbc8f1b5dc12793c39170def263f0644"
CASE_RELATIVE_PATH = Path("eval_cases/legal_eval_cases_v3_gen_subset.jsonl")
# Newlines are normalized so the frozen data identity survives Git CRLF/LF.
CASE_FILE_SHA256 = "b3bee7b6249dbe05c2118c076b73409491fba623b88bc9b31172c9e2202c7c6f"
INDEX_RELATIVE_PATH = Path("artifacts/indexes/article/chunks.jsonl")
CORPUS_RELATIVE_PATH = Path("Chinese-Laws/Chinese-Laws")
CASE_IDS = (
    "v3_lookup_patent_term",
    "v3_lookup_labor_arb_limit",
    "v3_lookup_sensitive_info",
    "v3_scene_weekend_work",
    "v3_scene_online_return",
    "v3_hardneg_quality_return",
    "v3_multi_return_rules",
    "v3_cross_food_consumer",
    "v3_refusal_fake_evidence",
)
PROBE_PROMPT = 'Return only a JSON object with exactly one field: {"ok": true}.'
_RUN_ID = re.compile(r"live_smoke_[A-Za-z0-9_-]{1,80}\Z")


class SmokeError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _NoCallsClient:
    """Explicit provider-free dependency for preparation and retrieval."""

    propagate_control_errors = True

    def complete(self, prompt: str) -> str:
        del prompt
        raise SmokeError("preflight_call_forbidden")


def _write_json(path: Path, payload: Any) -> None:
    # Exclusive creation makes raw artifacts immutable and prevents overwrite.
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _code_identity(root: Path) -> dict[str, Any]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=True, timeout=10
    ).stdout.decode("ascii").strip()
    if not re.fullmatch(r"[a-f0-9]{40}", head):
        raise SmokeError("code_identity_invalid")
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--"],
        cwd=root,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    critical_paths = (
        "legal_rag/llm.py", "legal_rag/live_budget.py", "scripts/live_model_smoke.py",
        "legal_rag/chat.py", "legal_rag/evaluation_scoring.py", "legal_rag/config.py",
        "legal_rag/experiment_lifecycle.py",
        "configs/default.yaml",
    )
    return {
        "head": head,
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "critical_file_sha256": {name: _sha256(root / name) for name in critical_paths},
        "includes_untracked_critical_files": True,
    }


def _signature(chunk: Chunk) -> tuple[Any, ...]:
    return (
        chunk.text,
        tuple(chunk.law_names),
        tuple(chunk.article_numbers),
        tuple(chunk.line_nos),
    )


def _validated_chunks(root: Path) -> tuple[list[Chunk], dict[str, Any]]:
    """Tie every indexed article to the fixed local public-corpus directory.

    This checks local content provenance, not current legal validity or a remote
    dataset signature. Only basenames reach the existing generation prompt.
    """
    index_path = (root / INDEX_RELATIVE_PATH).resolve()
    corpus_path = (root / CORPUS_RELATIVE_PATH).resolve()
    if not index_path.is_relative_to(root.resolve()) or not corpus_path.is_relative_to(
        root.resolve()
    ):
        raise SmokeError("source_path_outside_repository")
    chunks = load_chunks(index_path)
    if not chunks:
        raise SmokeError("empty_index")
    signatures: dict[Path, set[tuple[Any, ...]]] = {}
    source_hashes: dict[str, str] = {}
    sanitized: list[Chunk] = []
    for chunk in chunks:
        if chunk.strategy != "article" or len(chunk.source_files) != 1:
            raise SmokeError("index_not_single_public_article")
        source = Path(chunk.source_files[0])
        if not source.is_absolute():
            source = root / source
        source = source.resolve()
        if (
            source.parent != corpus_path
            or source.suffix != ".txt"
            or not source.is_file()
        ):
            raise SmokeError("index_source_not_allowed")
        if source not in signatures:
            signatures[source] = {
                _signature(item) for item in article_chunks(parse_law_file(source))
            }
            source_hashes[source.name] = _sha256(source)
        if _signature(chunk) not in signatures[source]:
            raise SmokeError("index_content_not_in_local_source")
        sanitized.append(replace(chunk, source_files=[source.name]))
    serialized_hashes = json.dumps(source_hashes, sort_keys=True).encode("utf-8")
    return sanitized, {
        "index_sha256": _sha256(index_path),
        "chunk_count": len(chunks),
        "source_file_count": len(source_hashes),
        "source_hashes_sha256": hashlib.sha256(serialized_hashes).hexdigest(),
        "source_hashes": source_hashes,
        "public_source": "https://huggingface.co/datasets/Kuugo/Chinese_Law",
        "source_boundary": CORPUS_RELATIVE_PATH.as_posix(),
        "provenance_check": "indexed_articles_match_local_source_parser",
        "legal_validity": "not_verified",
    }


def _make_assistant(retriever: BM25Retriever) -> LegalChatAssistant:
    return LegalChatAssistant(
        retriever,
        model="siliconflow:" + MODEL,
        top_k=5,
        completion_client=_NoCallsClient(),
        adaptive_enabled=False,
        adaptive_use_llm=False,
        normalizer_retries=0,
        condense_with_llm=False,
    )


def prepare_smoke(root: Path = REPOSITORY_ROOT) -> tuple[dict[str, Any], list[dict]]:
    """Freeze the same nine single-turn cases and provider-free stage results."""
    case_path = root / CASE_RELATIVE_PATH
    if not case_path.resolve().is_relative_to(root.resolve()):
        raise SmokeError("case_path_outside_repository")
    case_hash = hashlib.sha256(case_path.read_text(encoding="utf-8").encode("utf-8"))
    if case_hash.hexdigest() != CASE_FILE_SHA256:
        raise SmokeError("case_file_changed")
    cases_by_id = {case.case_id: case for case in load_eval_cases(case_path)}
    cases = [cases_by_id[case_id] for case_id in CASE_IDS]
    if any(case.session_group is not None for case in cases):
        raise SmokeError("cases_not_independent")
    chunks, source_metadata = _validated_chunks(root)
    config = load_config()  # Fixed defaults, never arbitrary live configuration.
    retrieval = config["retrieval"]
    retriever = BM25Retriever(
        chunks,
        k1=retrieval["bm25_k1"],
        b=retrieval["bm25_b"],
        law_boost=retrieval["bm25_law_boost"],
        article_boost=retrieval["bm25_article_boost"],
        deprecated_penalty=retrieval["deprecated_penalty"],
    )
    prepared_cases = []
    public_cases = []
    for case in cases:
        assistant = _make_assistant(retriever)
        prepared = assistant.prepare_question(case.question)
        retrieved = assistant.retrieve_turn(prepared, max_followup_rounds=0)
        prompt_bytes = None
        if retrieved.terminal_kind is None:
            prompt = build_qa_prompt(
                question=prepared.standalone_question,
                original_question=prepared.original_question,
                memory=prepared.memory_text,
                results=list(retrieved.results),
            )
            prompt_bytes = len(prompt.encode("utf-8"))
            if prompt_bytes > MAX_PROMPT_UTF8_BYTES:
                raise SmokeError("preflight_prompt_limit")
        prepared_cases.append(
            {"case": case, "assistant": assistant, "retrieved": retrieved}
        )
        public_cases.append(
            {
                "case_id": case.case_id,
                "case_type": case.case_type,
                "question_sha256": hashlib.sha256(
                    case.question.encode("utf-8")
                ).hexdigest(),
                "retrieved_count": len(retrieved.results),
                "terminal_kind": retrieved.terminal_kind,
                "would_generate": retrieved.terminal_kind is None,
                "prompt_utf8_bytes": prompt_bytes,
                "hit_at_5": hit_at_k(list(retrieved.results), case, 5),
                "target_coverage": target_coverage(list(retrieved.results), case, 5),
            }
        )
    metadata = {
        "schema_version": 1,
        "model": MODEL,
        "base_url": BASE_URL,
        "max_calls": MAX_CALLS,
        "budget_cny": str(BUDGET_CNY),
        "input_rate_cny_per_million": str(INPUT_RATE_CNY),
        "output_rate_cny_per_million": str(OUTPUT_RATE_CNY),
        "price_basis": "user_confirmed_account_price_2026-10-03",
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_prompt_utf8_bytes": MAX_PROMPT_UTF8_BYTES,
        "enable_thinking": False,
        "response_format": "json_object",
        "concurrency": 1,
        "automatic_retries": 0,
        "probe_calls_maximum": 1,
        "rag_cases": public_cases,
        "case_source": CASE_RELATIVE_PATH.as_posix(),
        "case_source_sha256": CASE_FILE_SHA256,
        "case_source_hash_newlines": "normalized_lf",
        "authorization_id": AUTHORIZATION_ID,
        "dataset_role": "legacy_regression_not_holdout",
        "judge": False,
        "adaptive": False,
        "condense_with_llm": False,
        "reranker": "none",
        "embedding_calls": 0,
        "online_api_or_m6_queue_validation": False,
        "prompt_version": getattr(chat_module, "STRUCTURED_QA_PROMPT_VERSION", "legacy_unversioned"),
        "code_identity": _code_identity(root),
        "sources": source_metadata,
    }
    return metadata, prepared_cases


def _new_output_directory(root: Path, run_id: str) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        raise SmokeError("invalid_run_id")
    artifact_root = (root / "artifacts/experiments").resolve()
    if not artifact_root.is_relative_to(root.resolve()):
        raise SmokeError("artifact_path_outside_repository")
    output = artifact_root / run_id
    if output.resolve().parent != artifact_root:
        raise SmokeError("artifact_path_outside_repository")
    output.mkdir(parents=True, exist_ok=False)
    return output


class _RecordingClient:
    propagate_control_errors = True

    def __init__(self, bounded: Any, output: Path) -> None:
        self.bounded = bounded
        self.output = output
        self.invocations = 0

    @property
    def usage(self) -> Any:
        return self.bounded.usage

    def complete(self, prompt: str) -> str:
        self.invocations += 1
        number = self.invocations
        _write_json(self.output / f"raw_prompt_{number:02d}.json", {"prompt": prompt})
        response = self.bounded.complete(prompt)
        _write_json(
            self.output / f"raw_response_{number:02d}.json", {"response": response}
        )
        return response


def _canonical_ledger(root: Path, *, repair: bool = False) -> Path:
    relative_path = REPAIR_AUTHORIZATION_LEDGER if repair else AUTHORIZATION_LEDGER
    ledger = (root / relative_path).resolve()
    expected_parent = (root / "artifacts/experiments/.live_authorizations").resolve()
    if not expected_parent.is_relative_to(root.resolve()) or ledger.parent != expected_parent:
        raise SmokeError("authorization_path_outside_repository")
    return ledger


def _repair_allowance(root: Path) -> dict[str, Any]:
    from legal_rag.live_budget import read_repair_allowance

    prior_output = (root / "artifacts/experiments" / PRIOR_RUN_ID).resolve()
    if not prior_output.is_relative_to(root.resolve()):
        raise SmokeError("repair_receipt_path_outside_repository")
    evidence = read_repair_allowance(
        _canonical_ledger(root), prior_output / "summary.json"
    )
    manifest = prior_output / "manifest.json"
    if _sha256(manifest) != PRIOR_MANIFEST_SHA256:
        raise SmokeError("repair_manifest_hash_mismatch")
    evidence = dict(evidence)
    evidence["prior_manifest_sha256"] = PRIOR_MANIFEST_SHA256
    return evidence


def _build_bounded_client(root: Path, *, allowance: dict[str, Any] | None = None) -> Any:
    # Import/create the live wrapper only after preflight and both opt-in gates.
    from legal_rag.live_budget import BoundedCompletionClient, LiveCallPolicy

    policy = LiveCallPolicy(
        run_id=REPAIR_AUTHORIZATION_ID if allowance is not None else AUTHORIZATION_ID,
        model=MODEL,
        base_url=BASE_URL,
        max_calls=allowance["remaining_max_calls"] if allowance is not None else MAX_CALLS,
        budget_cny=Decimal(allowance["remaining_budget_cny"]) if allowance is not None else BUDGET_CNY,
        input_rate_cny_per_million=INPUT_RATE_CNY,
        output_rate_cny_per_million=OUTPUT_RATE_CNY,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        max_prompt_utf8_bytes=MAX_PROMPT_UTF8_BYTES,
        enable_thinking=False,
        response_format="json_object",
    )
    client = SiliconFlowClient(
        model=MODEL,
        base_url=BASE_URL,
        request_timeout=REQUEST_TIMEOUT_SECONDS,
        temperature=0.0,
        max_tokens=MAX_OUTPUT_TOKENS,
        enable_thinking=False,
        response_format="json_object",
        follow_redirects=False,
    )
    bounded = BoundedCompletionClient(
        client, _canonical_ledger(root, repair=allowance is not None), policy
    )
    if bounded.stop_reason is not None:
        raise SmokeError("authorization_already_used")
    return bounded


def _score_case(item: dict, generated: Any, verified: Any, usage: dict, elapsed: int):
    retrieved = item["retrieved"]
    evidence = retrieved.evidence_check or check_evidence_sufficiency(
        item["case"].question,
        list(retrieved.results),
        analysis=retrieved.prepared.analysis,
    )
    return score_completed_case(
        CompletedCaseOutcome(
            case=item["case"],
            model="siliconflow:" + MODEL,
            retriever="bm25",
            chunk_strategy="article",
            top_k=5,
            generate=True,
            results=retrieved.results,
            answer=verified.answer_text,
            analysis=retrieved.prepared.analysis,
            adaptive_trace={"enabled": False, "used": False},
            evidence_check=evidence,
            verification=verified.verification,
            structured_answer=verified.final_answer,
            pre_fallback_answer=verified.pre_fallback_answer,
            pre_fallback_verification=verified.pre_fallback_verification,
            generation_kind=generated.kind,
            generation_error=generated.generation_error,
            judge_configured=False,
            judge_result=None,
            error="",
            latency_ms=elapsed,
            assistant_usage=ModelUsageDelta.from_mapping(usage),
            normalizer_usage=ModelUsageDelta(),
            judge_usage=ModelUsageDelta(),
            trace_metadata={"dataset_role": "legacy_regression_not_holdout"},
        )
    )


def _close_provider(bounded: Any) -> str:
    client = getattr(bounded.base_client, "_client", None)
    if client is None:
        return "not_initialized"
    try:
        client.close()
    except Exception:
        return "failed"
    return "closed"


def run_smoke(
    *, execute: bool = False, run_id: str | None = None, repair: bool = False
) -> dict[str, Any]:
    if execute and not live_model_calls_allowed():
        raise SmokeError("live_opt_in_required")
    if execute and run_id is None:
        raise SmokeError("explicit_execute_run_id_required")
    if execute and _canonical_ledger(REPOSITORY_ROOT, repair=repair).exists():
        raise SmokeError("authorization_already_used")
    allowance = _repair_allowance(REPOSITORY_ROOT) if repair else None
    if run_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"live_smoke_{stamp}_{uuid.uuid4().hex[:8]}"
    metadata, prepared_cases = prepare_smoke(REPOSITORY_ROOT)
    if allowance is not None:
        if metadata.get("prompt_version") != REPAIR_QA_PROMPT_VERSION:
            raise SmokeError("repair_prompt_version_mismatch")
        metadata = {
            **metadata,
            "authorization_id": REPAIR_AUTHORIZATION_ID,
            "max_calls": allowance["remaining_max_calls"],
            "budget_cny": allowance["remaining_budget_cny"],
            "global_max_calls": MAX_CALLS,
            "global_budget_cny": str(BUDGET_CNY),
            "probe_calls_maximum": 0,
            "probe_status": "reused_prior_passed",
            "prior_evidence": allowance,
        }
    output = _new_output_directory(REPOSITORY_ROOT, run_id)
    _write_json(output / "manifest.json", {"run_id": run_id, **metadata})
    summary: dict[str, Any] = {
        "run_id": run_id,
        "authorization_id": REPAIR_AUTHORIZATION_ID if repair else AUTHORIZATION_ID,
        "status": "preflight_passed",
        "live_model_calls": 0,
        "provider_initialized": False,
        "rag_cases_completed": 0,
        "rag_cases_planned": len(CASE_IDS),
        "probe_status": "not_run",
        "case_results": [],
        "stop_reason": None,
        "artifact_dir": "artifacts/experiments/" + run_id,
        "dataset_role": "legacy_regression_not_holdout",
        "legal_quality_claim": False,
    }
    if allowance is not None:
        summary.update(
            {
                "prior_evidence": allowance,
                "prior_live_model_calls": allowance["prior_calls_attempted"],
                "total_authorized_calls_attempted": allowance["prior_calls_attempted"],
                "probe_status": "reused_prior_passed",
                "probe_calls_this_run": 0,
                "global_max_calls": MAX_CALLS,
                "global_budget_cny": str(BUDGET_CNY),
            }
        )
    if not execute:
        _write_json(output / "summary.json", summary)
        return summary

    bounded = _build_bounded_client(REPOSITORY_ROOT, allowance=allowance)
    recorder = _RecordingClient(bounded, output)
    summary["provider_initialized"] = True
    summary["status"] = "stopped"
    try:
        if allowance is None:
            probe = recorder.complete(PROBE_PROMPT)
            probe_object = json.loads(probe)
            if (
                not isinstance(probe_object, dict)
                or set(probe_object) != {"ok"}
                or probe_object["ok"] is not True
            ):
                raise SmokeError("compatibility_probe_invalid")
            summary["probe_status"] = "passed"
        for item in prepared_cases:
            assistant = item["assistant"]
            assistant.llm = recorder
            before = usage_snapshot(recorder)
            started = time.perf_counter()
            generated = assistant.generate_turn(item["retrieved"], generate=True)
            verified = assistant.verify_turn(generated)
            assistant.commit_turn(verified)
            elapsed = round((time.perf_counter() - started) * 1000)
            evaluated = _score_case(
                item, generated, verified, usage_delta(before, usage_snapshot(recorder)), elapsed
            )
            _write_json(
                output / f"case_{item['case'].case_id}.json",
                {
                    "raw_response": generated.raw_response,
                    "record": asdict(evaluated.record),
                    "trace": evaluated.trace_record,
                },
            )
            draft_check = verified.pre_fallback_verification or verified.verification
            case_summary = {
                "case_id": item["case"].case_id,
                "generation_kind": generated.kind,
                "model_calls": evaluated.record.assistant_llm_calls,
                "schema_valid": draft_check.schema_valid if draft_check else None,
                "verifier_passed": draft_check.passed if draft_check else None,
                "final_answer_mode": (
                    verified.final_answer.answer_mode if verified.final_answer else None
                ),
                "semantic_support_status": (
                    draft_check.semantic_support_status if draft_check else "not_checked"
                ),
            }
            summary["case_results"].append(case_summary)
            summary["rag_cases_completed"] += 1
            if generated.kind == "generation_error":
                raise SmokeError("generation_failed")
            if draft_check is None or not draft_check.schema_valid:
                raise SmokeError("generated_schema_invalid")
            if not draft_check.passed:
                raise SmokeError("generated_verifier_failed")
            if bounded.stop_reason is not None:
                raise SmokeError("budget_client_stopped")
        summary["status"] = "completed"
    except Exception as exc:
        from legal_rag.live_budget import LiveBudgetError

        if isinstance(exc, (SmokeError, LiveBudgetError)):
            reason = exc.reason
        elif isinstance(exc, json.JSONDecodeError):
            reason = "compatibility_probe_invalid_json"
        else:
            reason = "smoke_execution_failed"
        summary["stop_reason"] = reason
        if summary["probe_status"] == "not_run":
            summary["probe_status"] = "failed"
    finally:
        summary["client_close_status"] = _close_provider(bounded)
    summary["budget"] = bounded.result_snapshot()
    summary["live_model_calls"] = summary["budget"]["calls_attempted"]
    summary["call_count_status"] = "canonical_ledger_known"
    if allowance is not None:
        summary["total_authorized_calls_attempted"] = (
            allowance["prior_calls_attempted"] + summary["live_model_calls"]
        )
        prior_cost = Decimal(allowance["prior_committed_cny"])
        summary["cumulative_committed_cny"] = str(
            prior_cost + Decimal(summary["budget"]["committed_cny"])
        )
        estimated = summary["budget"]["estimated_total_cost_cny"]
        summary["cumulative_estimated_cost_cny"] = (
            None if estimated is None else str(prior_cost + Decimal(estimated))
        )
    try:
        _write_json(output / "summary.json", summary)
    except Exception:
        summary["status"] = "stopped"
        summary["summary_persist_status"] = "failed"
        summary["stop_reason"] = "summary_write_failed"
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="Zero-call local checks (default)")
    mode.add_argument("--execute", action="store_true", help="Authorized fixed first live smoke")
    parser.add_argument("--repair", action="store_true", help="Repair continuation within the original total allowance; reuses the prior passed probe")
    parser.add_argument("--run-id", help="Fresh live_smoke_ identifier; required for execution")
    args = parser.parse_args(argv)
    try:
        summary = run_smoke(execute=args.execute, run_id=args.run_id, repair=args.repair)
    except SmokeError as exc:
        summary = {"status": "blocked", "stop_reason": exc.reason, "live_model_calls": 0}
    except FileExistsError:
        summary = {"status": "blocked", "stop_reason": "output_already_exists", "live_model_calls": 0}
    except Exception:
        summary = {
            "status": "blocked",
            "stop_reason": "preflight_or_setup_failed",
            "live_model_calls": None if args.execute else 0,
            "call_count_status": "unknown" if args.execute else "no_dispatch",
            "authorization_ledger": (
                REPAIR_AUTHORIZATION_LEDGER if args.repair else AUTHORIZATION_LEDGER
            ).as_posix(),
        }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] in {"preflight_passed", "completed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
