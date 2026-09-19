#!/usr/bin/env python3
"""Milestone quality gates with machine-readable, offline-first evidence.

The M0 gate deliberately reads only files that Git considers commit candidates:
tracked files plus untracked files that are not ignored. This keeps local secrets,
private corpora, and generated artifacts outside the documentation and secret
checks by construction. The secret scan is a small high-confidence safety net,
not a substitute for reviewing the candidate diff.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_MILESTONES = frozenset({"M0", "M1"})
SUPPORTED_MODES = frozenset({"offline"})
REPORT_SCHEMA_VERSION = 1
MILESTONE_PREREQUISITES: dict[str, tuple[str, ...]] = {
    "M0": (),
    "M1": ("M0",),
}

MANDATORY_M0_CHECK_IDS = frozenset(
    {
        "M0-T02-offline-pytest",
        "M0-T03-synthetic-offline-smoke",
        "M0-T04-package-version",
        "M0-T04-cli-help",
        "M0-Q01-markdown-links",
        "M0-Q02-state-manifests",
        "M0-Q03-secret-scan",
    }
)

M1_TEST_SELECTORS: dict[str, tuple[str, ...]] = {
    "M1-T01": (
        "tests/test_m1_verification.py::M1VerificationTest::test_m1_t01_disclaimer_is_not_a_refusal",
        "tests/test_m1_synthetic_examples.py::test_fixture_is_explicitly_synthetic_non_legal_and_not_a_quality_claim",
        "tests/test_m1_synthetic_examples.py::test_disclaimer_regression_example_is_not_counted_as_refusal",
    ),
    "M1-T02": (
        "tests/test_m1_verification.py::M1VerificationTest::test_m1_t02_legal_prohibition_words_are_not_a_refusal",
    ),
    "M1-T03": (
        "tests/test_m1_verification.py::M1VerificationTest::test_m1_t03_unknown_source_id_fails_and_falls_back",
        "tests/test_m1_verification.py::M1VerificationTest::test_invalid_generated_source_is_reverified_after_fallback",
    ),
    "M1-T04": (
        "tests/test_m1_verification.py::M1VerificationTest::test_m1_t04_real_id_does_not_imply_semantic_support",
        "tests/test_m1_synthetic_examples.py::test_fixture_is_explicitly_synthetic_non_legal_and_not_a_quality_claim",
        "tests/test_m1_synthetic_examples.py::test_real_but_irrelevant_source_id_never_implies_semantic_support",
    ),
    "M1-T05": (
        "tests/test_m1_verification.py::M1VerificationTest::test_m1_t05_insufficient_evidence_without_sources_is_valid_mode",
    ),
    "M1-T06": (
        "tests/test_m1_evaluation.py::M1EvaluationTest::test_m1_t06_ab_and_ba_are_deterministic_and_case_isolated",
    ),
    "M1-T07": (
        "tests/test_m1_evaluation.py::M1EvaluationTest::test_m1_t07_retrieval_only_skips_generation_verifier_and_judge",
        "tests/test_m1_evaluation.py::M1EvaluationTest::test_m1_t07_retrieval_only_uses_explicit_na_in_record_and_trace",
    ),
    "M1-T08": (
        "tests/test_m1_evaluation.py::M1EvaluationTest::test_m1_t08_judge_errors_are_null_and_excluded_from_quality_denominator",
        "tests/test_m1_evaluation.py::M1EvaluationTest::test_m1_t08_judge_errors_have_nullable_scores_and_canonical_codes",
        "tests/test_m1_evaluation.py::M1EvaluationTest::test_m1_t08_judge_three_way_counts_and_success_mean_share_one_summary",
    ),
    "M1-T09": (
        "tests/test_m1_evaluation.py::M1EvaluationTest::test_m1_t09_behavior_denominators_and_over_refusal_are_explicit",
    ),
    "M1-T10": (
        "tests/test_m1_verification.py::M1VerificationTest::test_m1_t10_cross_snapshot_and_scope_sources_are_rejected",
        "tests/test_m1_verification.py::M1VerificationTest::test_cross_snapshot_generation_is_rejected_without_leaking_evidence",
    ),
}

MANDATORY_M1_CHECK_IDS = frozenset(
    {
        *MANDATORY_M0_CHECK_IDS,
        *M1_TEST_SELECTORS,
    }
)

REQUIRED_RECORD_FIELDS = frozenset(
    {
        "test_id",
        "command",
        "environment",
        "executed_at",
        "exit_code",
        "status",
        "output_summary",
        "artifact_path",
    }
)

MAX_SUMMARY_CHARS = 6000
MAX_SCANNED_FILE_BYTES = 2 * 1024 * 1024

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_INLINE_LINK_RE = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)\n]+)",
)
_REFERENCE_LINK_RE = re.compile(
    r"^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)",
)
_PROJECT_HEADER_RE = re.compile(r"^\s*\[project\]\s*$")
_TOML_SECTION_RE = re.compile(r"^\s*\[[^]]+\]\s*$")
_TOML_VERSION_RE = re.compile(r'^\s*version\s*=\s*["\']([^"\']+)["\']\s*$')

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b")),
    ("stripe-live-secret", re.compile(r"\bsk_live_[0-9A-Za-z]{16,}\b")),
    ("openai-style-secret", re.compile(r"\bsk-(?:proj-)?[0-9A-Za-z_-]{20,}\b")),
    (
        "credential-in-url",
        re.compile(r"\bhttps?://[^\s/:@]+:[^\s/@]{8,}@", re.IGNORECASE),
    ),
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret(?:[_-]?key)?|password|passwd)\b"
    r"\s*[:=]\s*[\"']?([^\s\"',;#]{12,})",
)
_PLACEHOLDER_MARKERS = (
    "example",
    "dummy",
    "placeholder",
    "redacted",
    "changeme",
    "replace_me",
    "replace-me",
    "your_",
    "your-",
    "not-set",
    "not_set",
    "<",
    ">",
    "${",
    "{{",
)


class GateConfigurationError(RuntimeError):
    """Raised when a required repository input cannot be inspected safely."""


def utc_now() -> str:
    """Return an RFC 3339 timestamp in UTC."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def environment_summary() -> dict[str, Any]:
    """Describe the execution environment without exposing environment values."""

    return {
        "dotenv_loading_disabled": True,
        "live_model_calls_allowed": False,
        "mode": "offline",
        "os": platform.system() or os.name,
        "python": platform.python_version(),
        "sanitized_environment": True,
    }


def sanitized_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a minimal subprocess environment with offline controls and no credentials."""

    original = dict(os.environ if source is None else source)
    allowed_names = {
        "APPDATA",
        "CI",
        "COMSPEC",
        "GITHUB_ACTIONS",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "RUNNER_OS",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
    clean = {key: value for key, value in original.items() if key.upper() in allowed_names}
    clean.update(
        {
            "ALLOW_LIVE_MODEL_CALLS": "false",
            "HF_HUB_OFFLINE": "1",
            "LEGAL_RAG_DISABLE_DOTENV": "1",
            "NO_PROXY": "*",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "TRANSFORMERS_OFFLINE": "1",
            "UV_FROZEN": "1",
            "UV_NO_SYNC": "1",
            "UV_OFFLINE": "1",
            "no_proxy": "*",
        }
    )
    return clean


def uv_run_command(*arguments: str) -> list[str]:
    """Return a frozen, no-sync, offline uv command."""

    return ["uv", "run", "--offline", "--frozen", "--no-sync", *arguments]


def read_project_version(repo_root: Path) -> str:
    """Read project.version without requiring tomllib on Python 3.10."""

    pyproject_path = repo_root / "pyproject.toml"
    try:
        lines = pyproject_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GateConfigurationError(f"cannot read pyproject.toml: {exc}") from exc

    in_project = False
    for line in lines:
        if _PROJECT_HEADER_RE.match(line):
            in_project = True
            continue
        if in_project and _TOML_SECTION_RE.match(line):
            break
        if in_project:
            match = _TOML_VERSION_RE.match(line)
            if match:
                return match.group(1)
    raise GateConfigurationError("pyproject.toml has no [project].version")


def _clean_summary(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", text).replace("\x00", "")
    cleaned = cleaned.strip()
    if len(cleaned) <= limit:
        return cleaned
    omitted = len(cleaned) - limit
    return f"{cleaned[:limit]}\n... [{omitted} characters omitted]"


def _process_summary(stdout: str, stderr: str) -> str:
    parts: list[str] = []
    if stdout.strip():
        parts.append(f"stdout:\n{stdout.strip()}")
    if stderr.strip():
        parts.append(f"stderr:\n{stderr.strip()}")
    return _clean_summary("\n".join(parts) or "command produced no output")


def result_record(
    *,
    test_id: str,
    command: str | Sequence[str],
    exit_code: int,
    status: str,
    output_summary: str,
    artifact_path: str | None = None,
    executed_at: str | None = None,
) -> dict[str, Any]:
    """Create one result record with the required stable schema."""

    return {
        "test_id": test_id,
        "command": list(command) if not isinstance(command, str) else command,
        "environment": environment_summary(),
        "executed_at": executed_at or utc_now(),
        "exit_code": int(exit_code),
        "status": status,
        "output_summary": _clean_summary(output_summary),
        "artifact_path": artifact_path,
    }


def run_subprocess_check(
    *,
    test_id: str,
    command: Sequence[str],
    repo_root: Path,
    timeout_seconds: int,
    artifact_path: str | None = None,
) -> dict[str, Any]:
    """Run one subprocess check and always return a result record."""

    executed_at = utc_now()
    try:
        completed = subprocess.run(
            list(command),
            cwd=repo_root,
            env=sanitized_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        exit_code = completed.returncode
        summary = _process_summary(completed.stdout, completed.stderr)
    except FileNotFoundError as exc:
        exit_code = 127
        summary = f"command unavailable: {exc.filename or command[0]}"
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        summary = _process_summary(stdout, stderr)
        summary = f"timed out after {timeout_seconds} seconds\n{summary}"
    except OSError as exc:
        exit_code = 126
        summary = f"command could not start: {exc}"

    return result_record(
        test_id=test_id,
        command=command,
        exit_code=exit_code,
        status="passed" if exit_code == 0 else "failed",
        output_summary=summary,
        artifact_path=artifact_path,
        executed_at=executed_at,
    )


def _junit_counts(path: Path) -> dict[str, int]:
    """Read aggregate pytest JUnit counts without trusting process exit alone."""

    root = ET.parse(path).getroot()
    local_name = root.tag.rsplit("}", maxsplit=1)[-1]
    if local_name == "testsuite":
        suites = [root]
    else:
        suites = [
            child
            for child in root
            if child.tag.rsplit("}", maxsplit=1)[-1] == "testsuite"
        ]
    if not suites:
        raise ValueError("JUnit report contains no testsuite")

    counts = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        for name in counts:
            raw_value = suite.attrib.get(name, "0")
            value = int(raw_value)
            if value < 0:
                raise ValueError(f"JUnit {name} count must be non-negative")
            counts[name] += value
    return counts


def run_pytest_check(
    *,
    test_id: str,
    selectors: Sequence[str],
    repo_root: Path,
    timeout_seconds: int,
    artifact_path: str | None = None,
) -> dict[str, Any]:
    """Run mandatory pytest selectors and fail closed on skip, xfail, or no tests."""

    with tempfile.TemporaryDirectory(prefix="legal-rag-quality-gate-") as temp_dir:
        junit_path = Path(temp_dir) / "pytest-junit.xml"
        command = uv_run_command(
            "pytest",
            "-q",
            "-o",
            "xfail_strict=true",
            "--junitxml",
            str(junit_path),
            *selectors,
        )
        record = run_subprocess_check(
            test_id=test_id,
            command=command,
            repo_root=repo_root,
            timeout_seconds=timeout_seconds,
            artifact_path=artifact_path,
        )
        if record["exit_code"] != 0:
            return record

        try:
            counts = _junit_counts(junit_path)
        except (OSError, ET.ParseError, ValueError) as exc:
            record["status"] = "failed"
            record["exit_code"] = 1
            record["output_summary"] = _clean_summary(
                f"{record['output_summary']}\nmandatory pytest JUnit validation failed: {exc}"
            )
            return record

        problems: list[str] = []
        if counts["tests"] == 0:
            problems.append("no tests were executed")
        if counts["skipped"]:
            problems.append(
                f"{counts['skipped']} mandatory test(s) were skipped or xfailed"
            )
        if counts["failures"] or counts["errors"]:
            problems.append(
                f"JUnit recorded {counts['failures']} failure(s) and {counts['errors']} error(s)"
            )
        junit_summary = (
            "JUnit: "
            f"tests={counts['tests']}, failures={counts['failures']}, "
            f"errors={counts['errors']}, skipped={counts['skipped']}"
        )
        if problems:
            record["status"] = "failed"
            record["exit_code"] = 1
            junit_summary += "; " + "; ".join(problems)
        record["output_summary"] = _clean_summary(
            f"{record['output_summary']}\n{junit_summary}"
        )
        return record


def _safe_candidate_path(repo_root: Path, relative_path: Path) -> Path | None:
    if relative_path.is_absolute():
        return None
    root = repo_root.resolve()
    candidate = repo_root / relative_path
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, ValueError):
        return None
    if candidate.is_symlink():
        return None
    return candidate


def git_candidate_files(repo_root: Path) -> list[Path]:
    """List tracked and non-ignored untracked regular files, relative to the repo."""

    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--"],
            cwd=repo_root,
            env=sanitized_environment(),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        raise GateConfigurationError(f"git candidate discovery failed: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GateConfigurationError("git candidate discovery timed out") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GateConfigurationError(
            f"git candidate discovery exited {completed.returncode}: {_clean_summary(stderr, 500)}"
        )

    paths: set[Path] = set()
    for raw_path in completed.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative_path = Path(os.fsdecode(raw_path))
        candidate = _safe_candidate_path(repo_root, relative_path)
        if candidate is not None and candidate.is_file():
            paths.add(relative_path)
    return sorted(paths, key=lambda item: item.as_posix())


def _read_candidate_text(repo_root: Path, relative_path: Path) -> tuple[str | None, str | None]:
    """Read a bounded UTF-8 candidate; return a skip reason for unsafe/binary files."""

    candidate = _safe_candidate_path(repo_root, relative_path)
    if candidate is None:
        return None, "unsafe path or symlink"
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        return None, f"stat failed: {exc}"
    if size > MAX_SCANNED_FILE_BYTES:
        return None, f"larger than {MAX_SCANNED_FILE_BYTES} bytes"
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        return None, f"read failed: {exc}"
    if b"\0" in raw[:8192]:
        return None, "binary content"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "not UTF-8 text"


def _extract_markdown_targets(text: str) -> list[tuple[int, str]]:
    targets: list[tuple[int, str]] = []
    fence_marker: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if fence_marker is None:
                fence_marker = marker
            elif fence_marker == marker:
                fence_marker = None
            continue
        if fence_marker is not None:
            continue
        for match in _INLINE_LINK_RE.finditer(line):
            targets.append((line_number, match.group(1)))
        reference = _REFERENCE_LINK_RE.match(line)
        if reference:
            targets.append((line_number, reference.group(1)))
    return targets


def _relative_link_target(
    source_path: Path,
    raw_target: str,
) -> tuple[Path | None, str | None]:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if not target or target.startswith("#") or target.startswith("//"):
        return None, None
    if re.match(r"^[A-Za-z]:[\\/]", target) or target.startswith(("/", "\\")):
        return None, "local link must be relative"

    parsed = urlsplit(target)
    if parsed.scheme:
        return None, None
    decoded_path = unquote(parsed.path)
    if not decoded_path:
        return None, None
    if "\\" in decoded_path:
        return None, "local Markdown links must use forward slashes"

    posix_path = PurePosixPath(decoded_path)
    return source_path.parent.joinpath(*posix_path.parts), None


def validate_markdown_links(repo_root: Path, candidate_files: Iterable[Path]) -> list[str]:
    """Return broken/unsafe relative links in candidate Markdown files."""

    candidates = {path.as_posix(): path for path in candidate_files}
    candidate_keys = set(candidates)
    root = repo_root.resolve()
    errors: list[str] = []

    for source_key in sorted(key for key in candidate_keys if key.lower().endswith(".md")):
        source_path = candidates[source_key]
        text, skip_reason = _read_candidate_text(repo_root, source_path)
        if text is None:
            errors.append(f"{source_key}: cannot inspect Markdown ({skip_reason})")
            continue
        for line_number, raw_target in _extract_markdown_targets(text):
            unresolved, target_error = _relative_link_target(source_path, raw_target)
            if target_error:
                errors.append(f"{source_key}:{line_number}: {target_error}: {raw_target}")
                continue
            if unresolved is None:
                continue
            full_target = repo_root / unresolved
            try:
                target_relative = full_target.resolve(strict=False).relative_to(root)
            except (OSError, ValueError):
                errors.append(
                    f"{source_key}:{line_number}: relative link escapes repository: {raw_target}"
                )
                continue
            target_key = target_relative.as_posix()
            directory_prefix = target_key.rstrip("/") + "/"
            if target_key not in candidate_keys and not any(
                key.startswith(directory_prefix) for key in candidate_keys
            ):
                errors.append(
                    f"{source_key}:{line_number}: target is not a Git candidate: {raw_target}"
                )
    return errors


def validate_state_payload(payload: Any, milestone: str = "M0") -> list[str]:
    """Validate STATE.json structure and release-sensitive invariants."""

    if not isinstance(payload, dict):
        return ["STATE root must be an object"]
    errors: list[str] = []

    schema_version = payload.get("document_schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 1:
        errors.append("document_schema_version must be a positive integer")

    status_values = payload.get("stage_status_values")
    if not isinstance(status_values, list) or not status_values or not all(
        isinstance(item, str) and item for item in status_values
    ):
        errors.append("stage_status_values must be a non-empty string list")
        allowed_statuses: set[str] = set()
    else:
        allowed_statuses = set(status_values)
        if len(allowed_statuses) != len(status_values):
            errors.append("stage_status_values must not contain duplicates")

    active_milestone = payload.get("active_milestone")
    if not isinstance(active_milestone, str) or not active_milestone:
        errors.append("active_milestone must be a non-empty string")

    milestones = payload.get("milestones")
    milestone_entries: dict[str, dict[str, Any]] = {}
    if not isinstance(milestones, list) or not milestones:
        errors.append("milestones must be a non-empty list")
    else:
        for index, entry in enumerate(milestones):
            if not isinstance(entry, dict):
                errors.append(f"milestones[{index}] must be an object")
                continue
            milestone_id = entry.get("id")
            if not isinstance(milestone_id, str) or not milestone_id:
                errors.append(f"milestones[{index}].id must be a non-empty string")
                continue
            if milestone_id in milestone_entries:
                errors.append(f"duplicate milestone id: {milestone_id}")
            milestone_entries[milestone_id] = entry
            status = entry.get("status")
            if status not in allowed_statuses:
                errors.append(f"milestone {milestone_id} has unknown status: {status!r}")
            tests = entry.get("tests")
            if not isinstance(tests, dict) or not isinstance(tests.get("status"), str):
                errors.append(f"milestone {milestone_id} must have tests.status")

    gate_entry = milestone_entries.get(milestone)
    if gate_entry is None:
        errors.append(f"milestones must contain {milestone}")
    else:
        if gate_entry.get("required") is not True:
            errors.append(f"milestone {milestone} must be required")
        if active_milestone != milestone and gate_entry.get("status") != "released":
            errors.append(
                f"non-active gated milestone {milestone} must already be released"
            )

    for prerequisite_id in MILESTONE_PREREQUISITES.get(milestone, ()):
        prerequisite = milestone_entries.get(prerequisite_id)
        if prerequisite is None:
            errors.append(f"milestones must contain prerequisite {prerequisite_id}")
        elif prerequisite.get("status") != "released":
            errors.append(
                f"prerequisite milestone {prerequisite_id} must already be released"
            )

    active_entry = (
        milestone_entries.get(active_milestone)
        if isinstance(active_milestone, str)
        else None
    )
    if isinstance(active_milestone, str) and active_entry is None:
        errors.append(f"milestones must contain active_milestone {active_milestone}")
    elif (
        active_entry is not None
        and payload.get("execution_status") != active_entry.get("status")
    ):
        errors.append("execution_status must equal the active milestone status")

    for milestone_id, entry in milestone_entries.items():
        if entry.get("status") != "released":
            continue
        if not entry.get("tag"):
            errors.append(f"released milestone {milestone_id} must record a tag")
        if not entry.get("release_url"):
            errors.append(f"released milestone {milestone_id} must record a release_url")
        if entry.get("remote_release_verified") is not True:
            errors.append(f"released milestone {milestone_id} must verify the remote release")
        tests = entry.get("tests", {})
        if tests.get("status") != "passed":
            errors.append(f"released milestone {milestone_id} tests.status must be passed")

    repository = payload.get("repository")
    if not isinstance(repository, dict):
        errors.append("repository must be an object")
    else:
        if not isinstance(repository.get("full_name"), str) or not repository.get("full_name"):
            errors.append("repository.full_name must be a non-empty string")
        head = repository.get("workspace_head")
        if head is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", str(head)):
            errors.append("repository.workspace_head must be null or a 40-character commit SHA")

    return errors


def _positive_schema_version(payload: Mapping[str, Any]) -> bool:
    for key in ("schema_version", "manifest_version"):
        if key in payload:
            value = payload[key]
            return isinstance(value, int) and not isinstance(value, bool) and value > 0
    return False


def validate_manifest_payload(payload: Any, path: Path | None = None) -> list[str]:
    """Validate generic manifests and the checked-in run-manifest contract."""

    label = path.as_posix() if path is not None else "manifest"
    if not isinstance(payload, dict):
        return [f"{label}: root must be an object"]
    errors: list[str] = []
    if not _positive_schema_version(payload):
        errors.append(f"{label}: schema_version or manifest_version must be a positive integer")

    is_run_manifest = path is not None and "run_manifest" in path.name.lower()
    if is_run_manifest:
        execution = payload.get("execution")
        if not isinstance(execution, dict):
            errors.append(f"{label}: execution must be an object")
        else:
            if execution.get("mode") not in {
                "offline",
                "retrieval",
                "smoke-generation",
                "full-regression",
            }:
                errors.append(f"{label}: execution.mode is invalid")
            if not isinstance(execution.get("live_model_calls_allowed"), bool):
                errors.append(f"{label}: execution.live_model_calls_allowed must be boolean")
        for section_name in ("generation", "judge"):
            section = payload.get(section_name)
            if not isinstance(section, dict) or not isinstance(section.get("enabled"), bool):
                errors.append(f"{label}: {section_name}.enabled must be boolean")
        counts = payload.get("result_counts")
        if not isinstance(counts, dict):
            errors.append(f"{label}: result_counts must be an object")
        else:
            for name in ("succeeded", "failed", "not_run"):
                value = counts.get(name)
                if value is not None and (
                    not isinstance(value, int) or isinstance(value, bool) or value < 0
                ):
                    errors.append(f"{label}: result_counts.{name} must be null or non-negative")
    return errors


def validate_state_and_manifests(
    repo_root: Path,
    candidate_files: Iterable[Path],
    milestone: str = "M0",
) -> tuple[list[str], int]:
    """Parse and validate STATE plus every candidate JSON manifest."""

    candidates = {path.as_posix(): path for path in candidate_files}
    errors: list[str] = []
    parsed_count = 0
    state_key = "docs/refactor/STATE.json"
    state_path = candidates.get(state_key)
    if state_path is None:
        errors.append(f"missing Git candidate: {state_key}")
    else:
        text, skip_reason = _read_candidate_text(repo_root, state_path)
        if text is None:
            errors.append(f"{state_key}: cannot inspect JSON ({skip_reason})")
        else:
            try:
                state_payload = json.loads(text)
                parsed_count += 1
            except json.JSONDecodeError as exc:
                errors.append(f"{state_key}:{exc.lineno}:{exc.colno}: invalid JSON")
            else:
                errors.extend(f"{state_key}: {error}" for error in validate_state_payload(state_payload, milestone))

    manifest_paths = sorted(
        (
            path
            for key, path in candidates.items()
            if key.lower().endswith(".json") and "manifest" in path.name.lower()
        ),
        key=lambda item: item.as_posix(),
    )
    if not manifest_paths:
        errors.append("no Git-candidate manifest JSON files found")
    for manifest_path in manifest_paths:
        key = manifest_path.as_posix()
        text, skip_reason = _read_candidate_text(repo_root, manifest_path)
        if text is None:
            errors.append(f"{key}: cannot inspect JSON ({skip_reason})")
            continue
        try:
            payload = json.loads(text)
            parsed_count += 1
        except json.JSONDecodeError as exc:
            errors.append(f"{key}:{exc.lineno}:{exc.colno}: invalid JSON")
            continue
        errors.extend(validate_manifest_payload(payload, manifest_path))
    return errors, parsed_count


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.strip().strip("\"'").lower()
    if normalized in {"none", "null", "false", "true", "unknown"}:
        return True
    if re.search(r"[(){}\[\]]", normalized):
        return True
    if normalized.startswith(("os.", "self.", "config.", "settings.", "env.")):
        return True
    if not normalized or len(set(normalized)) <= 2:
        return True
    return any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


def find_high_confidence_secrets(
    repo_root: Path,
    candidate_files: Iterable[Path],
) -> tuple[list[str], int]:
    """Find high-confidence secret shapes without returning the matching values."""

    findings: list[str] = []
    skipped_count = 0
    for relative_path in sorted(candidate_files, key=lambda item: item.as_posix()):
        text, skip_reason = _read_candidate_text(repo_root, relative_path)
        if text is None:
            skipped_count += 1
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            detectors: set[str] = set()
            for detector_name, pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    detectors.add(detector_name)
            for assignment in _SECRET_ASSIGNMENT_RE.finditer(line):
                if not _looks_like_placeholder(assignment.group(1)):
                    detectors.add("assigned-credential")
            for detector_name in sorted(detectors):
                findings.append(f"{relative_path.as_posix()}:{line_number}:{detector_name}")
    return findings, skipped_count


def _static_check_record(
    *,
    test_id: str,
    command: str,
    errors: Sequence[str],
    success_summary: str,
    artifact_path: str | None = None,
) -> dict[str, Any]:
    if errors:
        summary = "\n".join(errors)
        exit_code = 1
        status = "failed"
    else:
        summary = success_summary
        exit_code = 0
        status = "passed"
    return result_record(
        test_id=test_id,
        command=command,
        exit_code=exit_code,
        status=status,
        output_summary=summary,
        artifact_path=artifact_path,
    )


def gate_succeeded(
    records: Sequence[Mapping[str, Any]],
    mandatory_ids: Iterable[str] = MANDATORY_M0_CHECK_IDS,
) -> bool:
    """Return true only when every mandatory check occurs exactly once and passes."""

    required = set(mandatory_ids)
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        test_id = record.get("test_id")
        if isinstance(test_id, str):
            by_id.setdefault(test_id, []).append(record)
        if not REQUIRED_RECORD_FIELDS.issubset(record):
            return False
    for test_id in required:
        matches = by_id.get(test_id, [])
        if len(matches) != 1:
            return False
        record = matches[0]
        if record.get("status") != "passed" or record.get("exit_code") != 0:
            return False
    return True


def _candidate_static_records(repo_root: Path, milestone: str) -> list[dict[str, Any]]:
    try:
        candidates = git_candidate_files(repo_root)
    except GateConfigurationError as exc:
        error = str(exc)
        return [
            _static_check_record(
                test_id="M0-Q01-markdown-links",
                command="quality_gate:validate_markdown_links(git_candidates)",
                errors=[error],
                success_summary="",
            ),
            _static_check_record(
                test_id="M0-Q02-state-manifests",
                command="quality_gate:validate_state_and_manifests(git_candidates)",
                errors=[error],
                success_summary="",
                artifact_path="docs/refactor/STATE.json",
            ),
            _static_check_record(
                test_id="M0-Q03-secret-scan",
                command="quality_gate:find_high_confidence_secrets(git_candidates)",
                errors=[error],
                success_summary="",
            ),
        ]

    markdown_count = sum(path.suffix.lower() == ".md" for path in candidates)
    markdown_errors = validate_markdown_links(repo_root, candidates)
    markdown_record = _static_check_record(
        test_id="M0-Q01-markdown-links",
        command="quality_gate:validate_markdown_links(git_candidates)",
        errors=markdown_errors,
        success_summary=(
            f"validated relative links in {markdown_count} Git-candidate Markdown files"
        ),
    )

    json_errors, parsed_count = validate_state_and_manifests(repo_root, candidates, milestone)
    json_record = _static_check_record(
        test_id="M0-Q02-state-manifests",
        command="quality_gate:validate_state_and_manifests(git_candidates)",
        errors=json_errors,
        success_summary=f"parsed and validated STATE plus manifests ({parsed_count} JSON files)",
        artifact_path="docs/refactor/STATE.json",
    )

    secret_findings, skipped_count = find_high_confidence_secrets(repo_root, candidates)
    secret_record = _static_check_record(
        test_id="M0-Q03-secret-scan",
        command="quality_gate:find_high_confidence_secrets(git_candidates)",
        errors=secret_findings,
        success_summary=(
            f"scanned {len(candidates) - skipped_count} Git-candidate text files; "
            f"safely omitted {skipped_count} binary, non-UTF-8, oversized, or symlink files; "
            "no high-confidence credential shapes found"
        ),
    )
    return [markdown_record, json_record, secret_record]


def _m0_offline_records(
    repo_root: Path,
    *,
    state_milestone: str,
) -> list[dict[str, Any]]:
    """Run the cumulative M0 checks while validating the requested active stage."""

    records: list[dict[str, Any]] = []

    records.append(
        run_pytest_check(
            test_id="M0-T02-offline-pytest",
            selectors=(),
            repo_root=repo_root,
            timeout_seconds=900,
        )
    )

    records.append(
        run_pytest_check(
            test_id="M0-T03-synthetic-offline-smoke",
            selectors=("tests/test_m0_smoke.py",),
            repo_root=repo_root,
            timeout_seconds=120,
            artifact_path="tests/test_m0_smoke.py",
        )
    )

    try:
        expected_version = read_project_version(repo_root)
    except GateConfigurationError as exc:
        records.append(
            _static_check_record(
                test_id="M0-T04-package-version",
                command="quality_gate:read_project_version + package import",
                errors=[str(exc)],
                success_summary="",
                artifact_path="pyproject.toml",
            )
        )
    else:
        version_probe = (
            "import importlib.metadata as m, json, sys; "
            "import legal_rag; "
            "expected=sys.argv[1]; installed=m.version('legal-rag-assistant'); "
            "module=getattr(legal_rag, '__version__', None); "
            "print(json.dumps({'expected': expected, 'installed': installed, 'module': module})); "
            "raise SystemExit(0 if expected == installed == module else 1)"
        )
        records.append(
            run_subprocess_check(
                test_id="M0-T04-package-version",
                command=uv_run_command("python", "-c", version_probe, expected_version),
                repo_root=repo_root,
                timeout_seconds=120,
                artifact_path="pyproject.toml",
            )
        )

    records.append(
        run_subprocess_check(
            test_id="M0-T04-cli-help",
            command=uv_run_command("python", "-m", "legal_rag.cli", "--help"),
            repo_root=repo_root,
            timeout_seconds=120,
        )
    )
    records.extend(_candidate_static_records(repo_root, state_milestone))

    return records


def _gate_report(
    *,
    milestone: str,
    started_at: str,
    records: list[dict[str, Any]],
    mandatory_check_ids: frozenset[str],
) -> dict[str, Any]:
    passed = gate_succeeded(records, mandatory_check_ids)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "milestone": milestone,
        "mode": "offline",
        "started_at": started_at,
        "finished_at": utc_now(),
        "status": "passed" if passed else "failed",
        "exit_code": 0 if passed else 1,
        "mandatory_check_ids": sorted(mandatory_check_ids),
        "checks": records,
        "artifact_path": None,
    }


def run_m0_offline(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Execute the complete M0 offline gate and return its report."""

    started_at = utc_now()
    records = _m0_offline_records(repo_root, state_milestone="M0")
    return _gate_report(
        milestone="M0",
        started_at=started_at,
        records=records,
        mandatory_check_ids=MANDATORY_M0_CHECK_IDS,
    )


def _m1_acceptance_records(repo_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for test_id, selectors in M1_TEST_SELECTORS.items():
        records.append(
            run_pytest_check(
                test_id=test_id,
                selectors=selectors,
                repo_root=repo_root,
                timeout_seconds=180,
                artifact_path=selectors[0].split("::", maxsplit=1)[0],
            )
        )
    return records


def run_m1_offline(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Execute cumulative M0 checks plus every named M1 acceptance test."""

    started_at = utc_now()
    records = _m0_offline_records(repo_root, state_milestone="M1")
    records.extend(_m1_acceptance_records(repo_root))
    return _gate_report(
        milestone="M1",
        started_at=started_at,
        records=records,
        mandatory_check_ids=MANDATORY_M1_CHECK_IDS,
    )


def validate_request(milestone: str | None, mode: str | None) -> list[str]:
    errors: list[str] = []
    if milestone not in SUPPORTED_MILESTONES:
        errors.append(
            f"unsupported milestone {milestone!r}; supported: {', '.join(sorted(SUPPORTED_MILESTONES))}"
        )
    if mode not in SUPPORTED_MODES:
        errors.append(f"unsupported mode {mode!r}; supported: {', '.join(sorted(SUPPORTED_MODES))}")
    return errors


def _invalid_request_report(
    milestone: str | None,
    mode: str | None,
    errors: Sequence[str],
) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "milestone": milestone,
        "mode": mode,
        "started_at": now,
        "finished_at": now,
        "status": "failed",
        "exit_code": 2,
        "mandatory_check_ids": [],
        "checks": [],
        "errors": list(errors),
        "artifact_path": None,
    }


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a milestone quality gate.")
    parser.add_argument("--milestone", help="Milestone identifier (M0 or M1).")
    parser.add_argument("--mode", help="Gate mode (currently offline).")
    parser.add_argument("--output", help="Optional path for the JSON report.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request_errors = validate_request(args.milestone, args.mode)
    if request_errors:
        report = _invalid_request_report(args.milestone, args.mode, request_errors)
    elif args.milestone == "M1":
        report = run_m1_offline(REPO_ROOT)
    else:
        report = run_m0_offline(REPO_ROOT)

    exit_code = int(report["exit_code"])
    if args.output:
        output_path = Path(args.output).expanduser()
        if not output_path.is_absolute():
            output_path = Path.cwd() / output_path
        report["artifact_path"] = str(output_path.resolve(strict=False))
        try:
            _write_report(output_path, report)
        except OSError as exc:
            report["status"] = "failed"
            report["exit_code"] = 1
            report.setdefault("errors", []).append(f"could not write --output report: {exc}")
            exit_code = 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
