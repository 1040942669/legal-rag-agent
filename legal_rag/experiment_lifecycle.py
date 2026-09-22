from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .chat import LegalChatAssistant
from .chunking import build_chunks
from .data import discover_law_files, parse_law_file, stable_id
from .experiment_adapter import EvaluationRuntimeSpec, LegalEvaluationRuntimeFactory
from .experiment_aggregation import (
    aggregate_experiment,
    aggregation_json_bytes,
    render_aggregation_csv,
    render_aggregation_jsonl,
    render_aggregation_markdown,
    validate_aggregation_bundle,
)
from .experiment_datasets import (
    MODE_ORDER,
    default_dataset_registry_path,
    load_dataset_registry,
)
from .experiment_runner import ExperimentRunner, RunnerSummary
from .experiment_runtime import (
    EXTERNAL_CALL_KINDS,
    IMPLEMENTATION_COMPONENTS,
    ExactStageCache,
    ExperimentContractError,
    build_experiment_manifest,
    canonical_hash,
    canonical_json_bytes,
)
from .experiment_store import ArtifactConflictError, ExperimentStore
from .llm import CompletionUsage
from .models import Chunk, LawArticle
from .retrieval import BM25Retriever


LIFECYCLE_PLAN_SCHEMA_VERSION = 1
AGGREGATION_PUBLICATION_SCHEMA_VERSION = 1
DEFAULT_EXPERIMENT_ROOT = Path("artifacts/experiments")
DEFAULT_CACHE_DIRECTORY_NAME = "_stage_cache"
DEFAULT_OFFLINE_CORPUS = Path("tests/fixtures/synthetic/synthetic_non_law.txt")
OFFLINE_CORPUS_SHA256 = (
    "e06414011dd7ca8a7a78729942aac6829bd402af29c3965076848828e577c468"
)
PROVIDER_FREE_MODEL = "provider-forbidden"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExperimentLifecycleError(RuntimeError):
    """Raised when a lifecycle action cannot prove a safe execution contract."""


@dataclass(frozen=True)
class CorpusSnapshot:
    source_path: Path
    locator: str
    snapshot_hash: str
    index_hash: str
    file_count: int
    chunks: tuple[Chunk, ...]


@dataclass(frozen=True)
class LifecycleExecution:
    experiment_id: str
    manifest_hash: str
    resume_compatibility_hash: str
    experiment_directory: Path
    cache_directory: Path
    summary: RunnerSummary

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "manifest_hash": self.manifest_hash,
            "resume_compatibility_hash": self.resume_compatibility_hash,
            "experiment_directory": self.experiment_directory.as_posix(),
            "cache_directory": self.cache_directory.as_posix(),
            "summary": self.summary.to_dict(),
        }


@dataclass(frozen=True)
class AggregationPublication:
    experiment_id: str
    aggregation_key: str
    bundle_hash: str
    directory: Path
    files: Mapping[str, Mapping[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "publication_schema_version": AGGREGATION_PUBLICATION_SCHEMA_VERSION,
            "experiment_id": self.experiment_id,
            "aggregation_key": self.aggregation_key,
            "bundle_hash": self.bundle_hash,
            "directory": self.directory.as_posix(),
            "files": {name: dict(value) for name, value in self.files.items()},
        }


class _ProviderForbiddenCompletionClient:
    """Usage-compatible client whose only behavior is to fail before I/O."""

    hidden_retries_disabled = True
    propagate_provider_errors = True
    request_timeout = None

    def __init__(self) -> None:
        self.usage = CompletionUsage()

    def complete(self, prompt: str) -> str:
        del prompt
        raise ExperimentContractError(
            "provider-free lifecycle attempted a forbidden completion call"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_repository_root(repository_root: str | Path | None = None) -> Path:
    if repository_root is not None:
        root = Path(repository_root).resolve()
    else:
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ExperimentLifecycleError(
                "experiment lifecycle requires an explicit Git repository root"
            ) from exc
        root = Path(completed.stdout.strip()).resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise ExperimentLifecycleError(f"not a Git repository root: {root}")
    return root


def _git_bytes(root: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ExperimentLifecycleError(
            "unable to establish the current Git identity"
        ) from exc
    return completed.stdout


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except OSError:
        return False
    file_attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(file_attributes & reparse_flag)


def repository_code_identity(repository_root: str | Path) -> dict[str, Any]:
    """Return commit plus a content-only dirty proof, including untracked files."""

    root = resolve_repository_root(repository_root)
    commit = _git_bytes(root, "rev-parse", "HEAD").decode("ascii").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ExperimentLifecycleError("Git HEAD is not a supported object ID")
    tracked_diff = _git_bytes(root, "diff", "--binary", "--no-ext-diff", "HEAD", "--")
    raw_untracked = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    untracked_names = sorted(name for name in raw_untracked.split(b"\0") if name)
    untracked: list[dict[str, Any]] = []
    for raw_name in untracked_names:
        relative_name = os.fsdecode(raw_name)
        path = (root / relative_name).absolute()
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ExperimentLifecycleError(
                f"untracked path cannot be read safely: {relative_name}"
            ) from exc
        if not resolved.is_relative_to(root) or _is_reparse_point(path):
            raise ExperimentLifecycleError(f"untracked path is unsafe: {relative_name}")
        if not path.is_file():
            continue
        untracked.append(
            {
                "path": PurePosixPath(relative_name).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    dirty = bool(tracked_diff or untracked)
    diff_hash = (
        canonical_hash(
            {
                "tracked_diff_sha256": hashlib.sha256(tracked_diff).hexdigest(),
                "untracked": untracked,
            }
        )
        if dirty
        else None
    )
    return {"commit": commit, "dirty": dirty, "diff_hash": diff_hash}


_IMPLEMENTATION_FILES: Mapping[str, tuple[str, ...]] = {
    "chunking": ("legal_rag/chunking.py", "legal_rag/data.py", "legal_rag/models.py"),
    "query_analysis": (
        "legal_rag/query.py",
        "legal_rag/query_understanding.py",
        "legal_rag/adaptive.py",
        "legal_rag/planning.py",
        "legal_rag/chat_artifacts.py",
        "legal_rag/experiment_adapter.py",
    ),
    "embedding": ("legal_rag/embeddings.py",),
    "retrieval": (
        "legal_rag/retrieval.py",
        "legal_rag/evidence.py",
        "legal_rag/experiment_adapter.py",
    ),
    "rerank": ("legal_rag/rerank.py",),
    "generation": (
        "legal_rag/chat.py",
        "legal_rag/chat_artifacts.py",
        "legal_rag/llm.py",
        "legal_rag/experiment_adapter.py",
    ),
    "verification": (
        "legal_rag/verifier.py",
        "legal_rag/evidence.py",
        "legal_rag/chat_artifacts.py",
        "legal_rag/experiment_adapter.py",
    ),
    "judge": ("legal_rag/judge.py", "legal_rag/experiment_adapter.py"),
    "aggregation": (
        "legal_rag/experiment_aggregation.py",
        "legal_rag/evaluation_scoring.py",
        "legal_rag/evaluation_artifacts.py",
    ),
}


def stage_implementation_fingerprints(
    repository_root: str | Path,
) -> dict[str, str]:
    root = resolve_repository_root(repository_root)
    if set(_IMPLEMENTATION_FILES) != set(IMPLEMENTATION_COMPONENTS):
        raise ExperimentLifecycleError(
            "lifecycle implementation fingerprint catalog is incomplete"
        )
    fingerprints: dict[str, str] = {}
    for component in sorted(_IMPLEMENTATION_FILES):
        evidence: list[dict[str, str]] = []
        for relative_name in _IMPLEMENTATION_FILES[component]:
            path = root / relative_name
            if (
                not path.is_file()
                or _is_reparse_point(path)
                or not path.resolve().is_relative_to(root)
            ):
                raise ExperimentLifecycleError(
                    f"implementation source is missing or unsafe: {relative_name}"
                )
            evidence.append({"path": relative_name, "sha256": _sha256_file(path)})
        fingerprints[component] = canonical_hash(
            {"component": component, "files": evidence}
        )
    return fingerprints


def _normalized_utf8_bytes(path: Path) -> bytes:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ExperimentLifecycleError(f"corpus file is not UTF-8: {path}") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _corpus_files(path: Path) -> tuple[Path, tuple[Path, ...]]:
    requested = path.absolute()
    if _is_reparse_point(requested):
        raise ExperimentLifecycleError("corpus path must not be a symbolic link")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise ExperimentLifecycleError(f"corpus path does not exist: {path}") from exc
    if resolved.is_file():
        return resolved.parent, (resolved,)
    if not resolved.is_dir():
        raise ExperimentLifecycleError(
            "corpus path must be a UTF-8 text file or directory"
        )
    files = tuple(discover_law_files(resolved))
    if not files:
        raise ExperimentLifecycleError("corpus directory contains no .txt files")
    for item in files:
        if _is_reparse_point(item):
            raise ExperimentLifecycleError("corpus files must not be symbolic links")
    return resolved, files


def _corpus_locator(path: Path, repository_root: Path) -> str:
    resolved = path.resolve()
    if resolved.is_relative_to(repository_root):
        return resolved.relative_to(repository_root).as_posix()
    return f"external:{resolved.name}"


def _chunk_payload(chunk: Chunk) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "law_names": list(chunk.law_names),
        "article_numbers": list(chunk.article_numbers),
        "source_files": list(chunk.source_files),
        "line_nos": list(chunk.line_nos),
        "strategy": chunk.strategy,
        "metadata": dict(chunk.metadata),
    }


def build_corpus_snapshot(
    corpus_path: str | Path,
    *,
    repository_root: str | Path,
    require_offline_fixture: bool = False,
) -> CorpusSnapshot:
    root = resolve_repository_root(repository_root)
    requested = Path(corpus_path)
    base, files = _corpus_files(requested)
    file_evidence: list[dict[str, Any]] = []
    articles: list[LawArticle] = []
    for file_path in files:
        normalized = _normalized_utf8_bytes(file_path)
        relative_name = file_path.relative_to(base).as_posix()
        file_evidence.append(
            {
                "path": relative_name,
                "size": len(normalized),
                "sha256": hashlib.sha256(normalized).hexdigest(),
            }
        )
        logical_source = f"corpus/{relative_name}"
        for article in parse_law_file(file_path):
            articles.append(
                replace(
                    article,
                    article_id=stable_id(
                        logical_source,
                        str(article.line_no),
                        article.raw_text,
                    ),
                    source_file=logical_source,
                )
            )
    snapshot_hash = canonical_hash(
        {"normalization": "utf8_lf_v1", "files": file_evidence}
    )
    if require_offline_fixture:
        if len(files) != 1 or file_evidence[0]["sha256"] != OFFLINE_CORPUS_SHA256:
            raise ExperimentLifecycleError(
                "offline mode requires the tracked synthetic corpus fixture"
            )
    chunks = tuple(build_chunks(articles, "article"))
    if not chunks:
        raise ExperimentLifecycleError("corpus produced no article chunks")
    index_hash = canonical_hash([_chunk_payload(chunk) for chunk in chunks])
    source_path = files[0] if len(files) == 1 else base
    return CorpusSnapshot(
        source_path=source_path,
        locator=_corpus_locator(source_path, root),
        snapshot_hash=snapshot_hash,
        index_hash=index_hash,
        file_count=len(files),
        chunks=chunks,
    )


def execution_environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.system(),
        "platform_release": platform.release(),
        "machine": platform.machine() or "unknown",
        "cpu_count": os.cpu_count(),
    }


def _resolve_input_path(value: str | Path, repository_root: Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (repository_root / path).resolve()


def _manifest_contracts(*, top_k: int, judge_enabled: bool) -> dict[str, Any]:
    return {
        "chunking": {"strategy": "article", "version": "article-v1"},
        "embedding": {
            "model": "not-applicable-bm25",
            "revision": "provider-free-v1",
            "dimension": 1,
            "normalized": False,
            "query_text_version": "bm25-tokenize-v1",
            "document_text_version": "bm25-tokenize-v1",
        },
        "retrieval": {
            "kind": "bm25",
            "parameters": {"top_k": top_k},
            "filters": {},
            "scope": {
                "configured": False,
                "snapshot_id": None,
                "allowed_scope_ids": None,
            },
            "query_analysis": {"version": "rules-v1"},
        },
        "rerank": {"enabled": False, "config": {}},
        "generation": {
            "model": PROVIDER_FREE_MODEL,
            "revision": "unconfigured-provider-v1",
            "prompt_version": "m1-structured-answer-v1",
            "parameters": {"temperature": 0.0},
        },
        "verification": {"schema_version": 2, "rules_version": "m1"},
        "judge": (
            {
                "enabled": True,
                "model": "unconfigured-judge",
                "revision": "unconfigured-provider-v1",
                "prompt_version": "judge-template-v1",
                "rules_version": "m1",
            }
            if judge_enabled
            else {"enabled": False}
        ),
    }


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExperimentLifecycleError(f"{name} must be a positive integer")
    return value


def _non_negative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExperimentLifecycleError(f"{name} must be a non-negative integer")
    return value


def build_lifecycle_plan(
    *,
    experiment_id: str,
    mode: str = "offline",
    dataset_id: str | None = None,
    cache_mode: str = "fresh",
    repository_root: str | Path | None = None,
    registry_path: str | Path | None = None,
    corpus_path: str | Path | None = None,
    top_k: int = 3,
    concurrency: int = 1,
    max_retries: int = 0,
    random_seed: int = 42,
    judge_enabled: bool = False,
    allow_external_calls: bool = False,
    created_at: str | None = None,
    manifest_environment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build but do not persist a registry-backed experiment manifest."""

    root = resolve_repository_root(repository_root)
    if mode not in MODE_ORDER:
        raise ExperimentLifecycleError(f"unsupported experiment mode: {mode!r}")
    top_k = _positive_int("top_k", top_k)
    concurrency = _positive_int("concurrency", concurrency)
    max_retries = _non_negative_int("max_retries", max_retries)
    if isinstance(random_seed, bool) or not isinstance(random_seed, int):
        raise ExperimentLifecycleError("random_seed must be an integer")
    registry_file = _resolve_input_path(
        registry_path or default_dataset_registry_path(root), root
    )
    registry = load_dataset_registry(registry_file, repository_root=root)
    selection = registry.resolve_mode(
        mode,
        dataset_id=dataset_id,
        judge_enabled=judge_enabled,
        allow_external_calls=allow_external_calls,
        cache_mode=cache_mode,
    )
    selected_corpus = _resolve_input_path(
        corpus_path or DEFAULT_OFFLINE_CORPUS,
        root,
    )
    if mode != "offline" and corpus_path is None:
        raise ExperimentLifecycleError(
            f"{mode} mode requires an explicit --corpus path"
        )
    corpus = build_corpus_snapshot(
        selected_corpus,
        repository_root=root,
        require_offline_fixture=mode == "offline",
    )
    code = repository_code_identity(root)
    code["stage_implementation_fingerprints"] = stage_implementation_fingerprints(root)
    config_summary = {
        "profile": "m2-provider-free-bm25",
        "dataset_id": selection.dataset.entry.dataset_id,
        "corpus_locator": corpus.locator,
        "top_k": top_k,
        "memory_token_limit": 2000,
        "generate": selection.generate,
        "judge_enabled": selection.judge_enabled,
        "allow_external_calls": selection.external_calls_allowed,
        "adaptive_enabled": False,
        "adaptive_use_llm": False,
        "adaptive_max_queries": 3,
        "adaptive_per_plan_top_k": None,
        "adaptive_normalizer_retries": 0,
        "condense_with_llm": False,
        "provider_timeouts": {"assistant": None, "judge": None, "adaptive": None},
    }
    runtime = {
        "random_seed": random_seed,
        "concurrency": concurrency,
        "max_retries": max_retries,
        "timing_scope": "runner_stage_wall_clock",
        "provider_limits": {kind: 1 for kind in EXTERNAL_CALL_KINDS},
        "retry_backoff_ms": 0,
    }
    manifest = build_experiment_manifest(
        experiment_id=experiment_id,
        execution_mode=mode,
        default_cache_mode=cache_mode,
        code=code,
        config_summary=config_summary,
        corpus={
            "snapshot_hash": corpus.snapshot_hash,
            "index_hash": corpus.index_hash,
            "source_kind": "synthetic_fixture" if mode == "offline" else "local_text",
            "file_count": corpus.file_count,
        },
        dataset=selection.dataset.manifest_payload(),
        contracts=_manifest_contracts(top_k=top_k, judge_enabled=judge_enabled),
        runtime=runtime,
        environment=dict(manifest_environment or execution_environment()),
        created_at=created_at or utc_now(),
    )
    runnable = not manifest["config"]["summary"]["generate"]
    return {
        "plan_schema_version": LIFECYCLE_PLAN_SCHEMA_VERSION,
        "runnable": runnable,
        "blocked_reason": (
            None
            if runnable
            else "generation modes require an explicit budgeted provider implementation"
        ),
        "network_policy": "forbidden"
        if not allow_external_calls
        else "explicit_opt_in",
        "manifest": manifest,
    }


def _corpus_path_from_manifest(
    manifest: Mapping[str, Any],
    *,
    repository_root: Path,
    override: str | Path | None,
) -> Path:
    if override is not None:
        return _resolve_input_path(override, repository_root)
    locator = manifest["config"]["summary"].get("corpus_locator")
    if not isinstance(locator, str) or not locator:
        raise ExperimentLifecycleError("stored manifest has no corpus locator")
    if locator.startswith("external:"):
        raise ExperimentLifecycleError(
            "resume/replay of an external corpus requires an explicit --corpus path"
        )
    return _resolve_input_path(locator, repository_root)


def _rebuild_from_manifest(
    stored_manifest: Mapping[str, Any],
    *,
    experiment_id: str,
    cache_mode: str,
    repository_root: Path,
    registry_path: str | Path | None,
    corpus_path: str | Path | None,
    preserve_manifest_envelope: bool,
) -> tuple[dict[str, Any], CorpusSnapshot]:
    summary = stored_manifest["config"]["summary"]
    runtime = stored_manifest["runtime"]
    selected_corpus = _corpus_path_from_manifest(
        stored_manifest,
        repository_root=repository_root,
        override=corpus_path,
    )
    plan = build_lifecycle_plan(
        experiment_id=experiment_id,
        mode=stored_manifest["execution_mode"],
        dataset_id=stored_manifest["dataset"]["dataset_id"],
        cache_mode=cache_mode,
        repository_root=repository_root,
        registry_path=registry_path,
        corpus_path=selected_corpus,
        top_k=summary["top_k"],
        concurrency=runtime["concurrency"],
        max_retries=runtime["max_retries"],
        random_seed=runtime["random_seed"],
        judge_enabled=summary["judge_enabled"],
        allow_external_calls=False,
        created_at=(
            stored_manifest["created_at"] if preserve_manifest_envelope else None
        ),
        manifest_environment=(
            stored_manifest["environment"] if preserve_manifest_envelope else None
        ),
    )
    corpus = build_corpus_snapshot(
        selected_corpus,
        repository_root=repository_root,
        require_offline_fixture=stored_manifest["execution_mode"] == "offline",
    )
    return plan["manifest"], corpus


def _runtime_factory(
    manifest: Mapping[str, Any],
    *,
    corpus: CorpusSnapshot,
    cache_directory: Path,
) -> LegalEvaluationRuntimeFactory:
    if manifest["config"]["summary"]["generate"]:
        raise ExperimentLifecycleError(
            "generation execution is disabled until a budgeted provider is explicitly configured"
        )
    if manifest["execution_policy"]["external_calls_allowed"]:
        raise ExperimentLifecycleError(
            "provider-free lifecycle cannot execute an external-call manifest"
        )
    retriever = BM25Retriever(list(corpus.chunks))
    summary = manifest["config"]["summary"]
    model = manifest["contracts"]["generation"]["model"]

    def assistant_factory() -> LegalChatAssistant:
        return LegalChatAssistant(
            retriever,
            model=model,
            top_k=summary["top_k"],
            memory_token_limit=summary["memory_token_limit"],
            adaptive_enabled=summary["adaptive_enabled"],
            adaptive_use_llm=summary["adaptive_use_llm"],
            adaptive_max_queries=summary["adaptive_max_queries"],
            adaptive_per_plan_top_k=summary["adaptive_per_plan_top_k"],
            normalizer_retries=summary["adaptive_normalizer_retries"],
            condense_with_llm=summary["condense_with_llm"],
            completion_client=_ProviderForbiddenCompletionClient(),
        )

    return LegalEvaluationRuntimeFactory(
        EvaluationRuntimeSpec(
            manifest=manifest,
            cache=ExactStageCache(cache_directory),
            retriever=retriever,
            assistant_factory=assistant_factory,
            model=model,
            chunk_strategy=manifest["contracts"]["chunking"]["strategy"],
            generate=False,
            trace_metadata={
                "lifecycle": {
                    "schema_version": LIFECYCLE_PLAN_SCHEMA_VERSION,
                    "provider_policy": "forbidden",
                }
            },
        )
    )


def _experiment_root(value: str | Path | None, repository_root: Path) -> Path:
    return _resolve_input_path(value or DEFAULT_EXPERIMENT_ROOT, repository_root)


def _cache_root(
    value: str | Path | None,
    experiment_root: Path,
    repository_root: Path,
) -> Path:
    if value is None:
        return (experiment_root / DEFAULT_CACHE_DIRECTORY_NAME).resolve()
    return _resolve_input_path(value, repository_root)


def _run_with_manifest(
    *,
    store: ExperimentStore,
    manifest: Mapping[str, Any],
    corpus: CorpusSnapshot,
    cache_directory: Path,
    cache_mode: str,
    stop_after_completed: int | None,
) -> LifecycleExecution:
    factory = _runtime_factory(
        manifest,
        corpus=corpus,
        cache_directory=cache_directory,
    )
    runner = ExperimentRunner(
        store=store,
        requested_manifest=manifest,
        runtime_factory=factory,
        cache_mode=cache_mode,
        execution_environment=execution_environment(),
    )
    summary = runner.run(stop_after_completed=stop_after_completed)
    return LifecycleExecution(
        experiment_id=manifest["experiment_id"],
        manifest_hash=manifest["identity"]["manifest_hash"],
        resume_compatibility_hash=manifest["identity"]["resume_compatibility_hash"],
        experiment_directory=store.directory,
        cache_directory=cache_directory,
        summary=summary,
    )


def run_experiment(
    *,
    experiment_id: str,
    mode: str = "offline",
    dataset_id: str | None = None,
    cache_mode: str = "fresh",
    repository_root: str | Path | None = None,
    experiment_root: str | Path | None = None,
    cache_root: str | Path | None = None,
    registry_path: str | Path | None = None,
    corpus_path: str | Path | None = None,
    top_k: int = 3,
    concurrency: int = 1,
    max_retries: int = 0,
    random_seed: int = 42,
    stop_after_completed: int | None = None,
) -> LifecycleExecution:
    root = resolve_repository_root(repository_root)
    plan = build_lifecycle_plan(
        experiment_id=experiment_id,
        mode=mode,
        dataset_id=dataset_id,
        cache_mode=cache_mode,
        repository_root=root,
        registry_path=registry_path,
        corpus_path=corpus_path,
        top_k=top_k,
        concurrency=concurrency,
        max_retries=max_retries,
        random_seed=random_seed,
        judge_enabled=False,
        allow_external_calls=False,
    )
    if not plan["runnable"]:
        raise ExperimentLifecycleError(plan["blocked_reason"])
    manifest = plan["manifest"]
    selected_corpus = _resolve_input_path(
        corpus_path or DEFAULT_OFFLINE_CORPUS,
        root,
    )
    corpus = build_corpus_snapshot(
        selected_corpus,
        repository_root=root,
        require_offline_fixture=mode == "offline",
    )
    experiments = _experiment_root(experiment_root, root)
    target = experiments / experiment_id
    if os.path.lexists(target):
        raise ExperimentLifecycleError(
            f"experiment already exists; use resume instead: {experiment_id}"
        )
    store = ExperimentStore.create(experiments, manifest)
    cache_directory = _cache_root(cache_root, experiments, root)
    return _run_with_manifest(
        store=store,
        manifest=manifest,
        corpus=corpus,
        cache_directory=cache_directory,
        cache_mode=cache_mode,
        stop_after_completed=stop_after_completed,
    )


def resume_experiment(
    *,
    experiment_id: str,
    cache_mode: str = "cache",
    repository_root: str | Path | None = None,
    experiment_root: str | Path | None = None,
    cache_root: str | Path | None = None,
    registry_path: str | Path | None = None,
    corpus_path: str | Path | None = None,
    stop_after_completed: int | None = None,
) -> LifecycleExecution:
    root = resolve_repository_root(repository_root)
    experiments = _experiment_root(experiment_root, root)
    store = ExperimentStore.open(experiments, experiment_id)
    stored_manifest = store.load_manifest()
    manifest, corpus = _rebuild_from_manifest(
        stored_manifest,
        experiment_id=experiment_id,
        cache_mode=stored_manifest["cache_policy"]["default_mode"],
        repository_root=root,
        registry_path=registry_path,
        corpus_path=corpus_path,
        preserve_manifest_envelope=True,
    )
    cache_directory = _cache_root(cache_root, experiments, root)
    return _run_with_manifest(
        store=store,
        manifest=manifest,
        corpus=corpus,
        cache_directory=cache_directory,
        cache_mode=cache_mode,
        stop_after_completed=stop_after_completed,
    )


def replay_experiment(
    *,
    source_experiment_id: str,
    experiment_id: str,
    repository_root: str | Path | None = None,
    experiment_root: str | Path | None = None,
    cache_root: str | Path | None = None,
    registry_path: str | Path | None = None,
    corpus_path: str | Path | None = None,
) -> LifecycleExecution:
    if source_experiment_id == experiment_id:
        raise ExperimentLifecycleError("replay requires a new experiment_id")
    root = resolve_repository_root(repository_root)
    experiments = _experiment_root(experiment_root, root)
    source_store = ExperimentStore.open(experiments, source_experiment_id)
    source_manifest = source_store.load_manifest()
    if source_manifest["config"]["summary"]["allow_external_calls"]:
        raise ExperimentLifecycleError(
            "replay source must have external calls disabled in its manifest"
        )
    target = experiments / experiment_id
    if os.path.lexists(target):
        raise ExperimentLifecycleError(f"replay target already exists: {experiment_id}")
    manifest, corpus = _rebuild_from_manifest(
        source_manifest,
        experiment_id=experiment_id,
        cache_mode="replay",
        repository_root=root,
        registry_path=registry_path,
        corpus_path=corpus_path,
        preserve_manifest_envelope=False,
    )
    if manifest["execution_policy"]["external_calls_allowed"]:
        raise ExperimentLifecycleError("replay manifest must prohibit external calls")
    store = ExperimentStore.create(experiments, manifest)
    cache_directory = _cache_root(cache_root, experiments, root)
    execution = _run_with_manifest(
        store=store,
        manifest=manifest,
        corpus=corpus,
        cache_directory=cache_directory,
        cache_mode="replay",
        stop_after_completed=None,
    )
    if execution.summary.status != "succeeded":
        raise ExperimentLifecycleError(
            "exact replay did not complete; inspect persisted case failures for cache misses or corruption"
        )
    return execution


def _path_matches_bytes(path: Path, data: bytes) -> bool:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            size += len(block)
            digest.update(block)
    return size == len(data) and digest.digest() == hashlib.sha256(data).digest()


def _write_immutable_bytes(path: Path, data: bytes) -> None:

    if os.path.lexists(path):
        if _is_reparse_point(path) or not path.is_file():
            raise ArtifactConflictError(f"unsafe immutable publication path: {path}")
        if not _path_matches_bytes(path, data):
            raise ArtifactConflictError(
                f"immutable publication already contains different bytes: {path}"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_name, path)
        except FileExistsError:
            if not _path_matches_bytes(path, data):
                raise ArtifactConflictError(
                    f"concurrent publication wrote different bytes: {path}"
                )
    finally:
        if temp_name and os.path.lexists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _publication_file_evidence(files: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    return {
        name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(files.items())
    }


def publish_aggregation(store: ExperimentStore) -> AggregationPublication:
    """Publish deterministic aggregate files under their content-addressed key."""

    bundle = aggregate_experiment(store)
    validated = validate_aggregation_bundle(
        bundle,
        expected_manifest=store.load_manifest(),
    )
    aggregation_key = validated["identity"]["aggregation_key"]
    bundle_hash = validated["bundle_hash"]
    if not _SHA256.fullmatch(aggregation_key) or not _SHA256.fullmatch(bundle_hash):
        raise ExperimentLifecycleError("aggregation identity is not a SHA-256 digest")
    aggregations_root = store.directory / "aggregations"
    if os.path.lexists(aggregations_root) and (
        _is_reparse_point(aggregations_root)
        or not aggregations_root.is_dir()
        or not aggregations_root.resolve().is_relative_to(store.directory.resolve())
    ):
        raise ArtifactConflictError(f"unsafe aggregation root: {aggregations_root}")
    aggregations_root.mkdir(parents=True, exist_ok=True)
    directory = aggregations_root / aggregation_key
    if os.path.lexists(directory) and (
        _is_reparse_point(directory)
        or not directory.is_dir()
        or not directory.resolve().is_relative_to(aggregations_root.resolve())
    ):
        raise ArtifactConflictError(f"unsafe aggregation directory: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "summary.json": aggregation_json_bytes(validated),
        "cases.jsonl": render_aggregation_jsonl(validated).encode("utf-8"),
        "cases.csv": render_aggregation_csv(validated).encode("utf-8"),
        "report.md": render_aggregation_markdown(validated).encode("utf-8"),
    }
    evidence = _publication_file_evidence(files)
    marker = {
        "publication_schema_version": AGGREGATION_PUBLICATION_SCHEMA_VERSION,
        "artifact_kind": "experiment_aggregation_publication",
        "experiment_id": store.experiment_id,
        "manifest_hash": validated["identity"]["manifest_hash"],
        "aggregation_key": aggregation_key,
        "bundle_hash": bundle_hash,
        "files": evidence,
    }
    marker_bytes = canonical_json_bytes(marker)
    complete_path = directory / "complete.json"
    if os.path.lexists(complete_path):
        if (
            _is_reparse_point(complete_path)
            or not complete_path.is_file()
            or not _path_matches_bytes(complete_path, marker_bytes)
        ):
            raise ArtifactConflictError(
                f"aggregation completion proof is invalid: {complete_path}"
            )
        for name, data in files.items():
            path = directory / name
            if (
                not os.path.lexists(path)
                or _is_reparse_point(path)
                or not path.is_file()
                or not _path_matches_bytes(path, data)
            ):
                raise ArtifactConflictError(
                    f"completed aggregation contains an invalid file: {path}"
                )
    else:
        for name, data in files.items():
            _write_immutable_bytes(directory / name, data)
        _write_immutable_bytes(complete_path, marker_bytes)
    complete_evidence = {
        **evidence,
        "complete.json": {
            "size": len(marker_bytes),
            "sha256": hashlib.sha256(marker_bytes).hexdigest(),
        },
    }
    return AggregationPublication(
        experiment_id=store.experiment_id,
        aggregation_key=aggregation_key,
        bundle_hash=bundle_hash,
        directory=directory,
        files=complete_evidence,
    )


def aggregate_experiment_by_id(
    *,
    experiment_id: str,
    repository_root: str | Path | None = None,
    experiment_root: str | Path | None = None,
) -> AggregationPublication:
    root = resolve_repository_root(repository_root)
    experiments = _experiment_root(experiment_root, root)
    return publish_aggregation(ExperimentStore.open(experiments, experiment_id))


def plan_json(plan: Mapping[str, Any]) -> str:
    return json.dumps(
        json.loads(canonical_json_bytes(dict(plan)).decode("utf-8")),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


def execution_json(execution: LifecycleExecution) -> str:
    return json.dumps(
        execution.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


def publication_json(publication: AggregationPublication) -> str:
    return json.dumps(
        publication.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


def validate_no_external_calls(executions: Sequence[LifecycleExecution]) -> None:
    for execution in executions:
        audit = aggregate_experiment(
            ExperimentStore.open(
                execution.experiment_directory.parent,
                execution.experiment_id,
            )
        )["attempt_audit"]
        actual = audit["actual_calls"]["by_kind"]
        if any(actual[kind]["attempted"] != 0 for kind in EXTERNAL_CALL_KINDS):
            raise ExperimentLifecycleError(
                f"experiment recorded an external call: {execution.experiment_id}"
            )


__all__ = [
    "AggregationPublication",
    "CorpusSnapshot",
    "ExperimentLifecycleError",
    "LifecycleExecution",
    "aggregate_experiment_by_id",
    "build_corpus_snapshot",
    "build_lifecycle_plan",
    "execution_json",
    "execution_environment",
    "plan_json",
    "publication_json",
    "publish_aggregation",
    "replay_experiment",
    "repository_code_identity",
    "resolve_repository_root",
    "resume_experiment",
    "run_experiment",
    "stage_implementation_fingerprints",
    "validate_no_external_calls",
]
