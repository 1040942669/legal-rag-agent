"""Run a fixed, provider-free engineering contract showcase into local evidence.

This is not a service launcher, production benchmark, or legal-quality gate.
Only the six selected fixture suites below can be dispatched by this entrypoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GROUPS = (
    ("lazy-lexical", "tests/test_service_lazy_lexical.py"),
    ("reference-route", "tests/test_reference_route_chat.py"),
    ("bound-semantic", "tests/test_bound_semantic_chat.py"),
    ("trace-observation", "tests/test_m6_trace_observation.py"),
    ("harness-observation", "tests/test_m6_harness_observation.py"),
    ("pgvector-optional-hints", "tests/test_pgvector_optional_hints.py"),
)
GROUP_TIMEOUT_SECONDS = 180
_INHERITED_ENVIRONMENT = frozenset(
    {"COMSPEC", "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "VIRTUAL_ENV", "WINDIR"}
)


class ShowcaseError(ValueError):
    """Stable, non-sensitive entrypoint failure code."""


def _subprocess_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _INHERITED_ENVIRONMENT
    }
    environment.update(
        ALLOW_LIVE_MODEL_CALLS="false",
        LEGAL_RAG_DISABLE_DOTENV="1",
        LEGAL_RAG_LANGFUSE_ENABLED="0",
        LEGAL_RAG_LANGFUSE_EXPORT_ACK="0",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        UV_OFFLINE="1",
        NO_PROXY="*",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
    )
    return environment


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prepare_output_dir(output_dir: Path | None) -> Path:
    temporary_root = (REPOSITORY_ROOT / ".tmp").resolve()
    target = (
        temporary_root / f"engineering-showcase-{uuid.uuid4().hex}"
        if output_dir is None
        else output_dir.resolve()
    )
    if target == temporary_root or not target.is_relative_to(temporary_root):
        raise ShowcaseError("output_must_be_a_workspace_temporary_subdirectory")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ShowcaseError("output_directory_not_empty")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _source_identity() -> dict[str, Any]:
    git_environment = _subprocess_environment()
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT,
            env=git_environment, capture_output=True, text=True, encoding="utf-8",
            check=True, timeout=10,
        ).stdout.strip().lower()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPOSITORY_ROOT,
            env=git_environment, capture_output=True, text=True, encoding="utf-8",
            check=True, timeout=10,
        ).stdout != ""
        tracked_inputs = subprocess.run(
            ["git", "ls-files", "-z", "--", "tests", "configs", "eval_cases"],
            cwd=REPOSITORY_ROOT, env=git_environment, capture_output=True,
            text=True, encoding="utf-8", check=True, timeout=10,
        ).stdout.split("\0")
    except (OSError, subprocess.SubprocessError):
        raise ShowcaseError("source_identity_unavailable") from None
    if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
        raise ShowcaseError("source_head_invalid")
    paths = set((REPOSITORY_ROOT / "legal_rag").rglob("*.py"))
    # Include tracked public test helpers, conftest, synthetic fixtures, config
    # and evaluation registries; never discover ignored/private input trees.
    paths.update(REPOSITORY_ROOT / relative for relative in tracked_inputs if relative)
    paths.update(REPOSITORY_ROOT / selector for _, selector in GROUPS)
    paths.update(
        REPOSITORY_ROOT / relative for relative in (
            "scripts/engineering_showcase.py", "tests/test_engineering_showcase.py",
            "pyproject.toml", "uv.lock",
        )
    )
    files: dict[str, str] = {}
    for path in sorted(paths):
        if not path.is_file() or not path.resolve().is_relative_to(REPOSITORY_ROOT.resolve()):
            raise ShowcaseError("selected_source_missing_or_outside_repository")
        files[path.relative_to(REPOSITORY_ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"head": head, "dirty": dirty, "source_files": files, "source_sha256": digest}


def _junit_summary(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ShowcaseError("junit_missing_or_empty")
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ShowcaseError("junit_too_large")
    try:
        root = ElementTree.parse(path).getroot()
        if root.tag == "testsuite":
            suites = [root]
        elif root.tag == "testsuites" and all(child.tag == "testsuite" for child in root):
            suites = list(root)
        else:
            raise ValueError
        if not suites:
            raise ValueError
        totals = dict(tests=0, failures=0, errors=0, skipped=0)
        cases: list[dict[str, str]] = []
        identities: set[tuple[str, str]] = set()
        for suite in suites:
            declared = {key: int(suite.attrib[key]) for key in totals}
            if any(value < 0 for value in declared.values()):
                raise ValueError
            testcase_nodes = list(suite.findall("testcase"))
            observed = {
                "tests": len(testcase_nodes),
                "failures": sum(case.find("failure") is not None for case in testcase_nodes),
                "errors": sum(case.find("error") is not None for case in testcase_nodes),
                "skipped": sum(case.find("skipped") is not None for case in testcase_nodes),
            }
            if declared != observed or observed["tests"] == 0:
                raise ValueError
            for case in testcase_nodes:
                name = case.attrib["name"]
                if not name:
                    raise ValueError
                classname = case.attrib.get("classname", "")
                identity = (classname, name)
                outcomes = [child for child in case if child.tag in {"failure", "error", "skipped"}]
                if identity in identities or len(outcomes) > 1:
                    raise ValueError
                identities.add(identity)
                cases.append({"classname": classname, "name": name})
            for key in totals:
                totals[key] += declared[key]
    except (ElementTree.ParseError, KeyError, TypeError, ValueError):
        raise ShowcaseError("junit_invalid") from None
    return {**totals, "selected_cases": cases}


def _run_group(group_id: str, selector: str, output_dir: Path) -> dict[str, Any]:
    junit = output_dir / f"{group_id}.xml"
    log = output_dir / f"{group_id}.log"
    command = [
        sys.executable, "-B", "-m", "pytest", "-q", "-ra", "--strict-markers",
        "-o", "xfail_strict=true", selector, f"--junitxml={junit}",
    ]
    record: dict[str, Any] = {
        "group_id": group_id, "selector": selector, "command": command,
        "started_at": _utc_now(), "junit": str(junit), "log": str(log),
        "timeout_seconds": GROUP_TIMEOUT_SECONDS,
    }
    started = time.perf_counter()
    errors: list[str] = []
    try:
        with log.open("x", encoding="utf-8") as handle:
            completed = subprocess.run(
                command, cwd=REPOSITORY_ROOT, env=_subprocess_environment(),
                stdout=handle, stderr=subprocess.STDOUT, shell=False,
                timeout=GROUP_TIMEOUT_SECONDS, check=False,
            )
        record["exit_code"] = completed.returncode
        if completed.returncode != 0:
            errors.append("pytest_nonzero_exit")
    except subprocess.TimeoutExpired:
        record["exit_code"] = 124
        errors.append("pytest_timeout")
    except OSError:
        record["exit_code"] = None
        errors.append("pytest_launch_failed")
    record["ended_at"] = _utc_now()
    record["elapsed_ms"] = (time.perf_counter() - started) * 1000
    try:
        record["junit_summary"] = _junit_summary(junit)
    except ShowcaseError as error:
        errors.append(str(error))
        record["junit_summary"] = None
    else:
        summary = record["junit_summary"]
        if any(summary[key] for key in ("failures", "errors", "skipped")):
            errors.append("junit_contains_nonpassing_cases")
    record["error_codes"] = errors
    record["status"] = "passed" if not errors else "failed"
    return record


def run_showcase(output_dir: Path | None = None) -> tuple[dict[str, Any], Path]:
    target = _prepare_output_dir(output_dir)
    before = _source_identity()
    started = _utc_now()
    groups = [_run_group(group_id, selector, target) for group_id, selector in GROUPS]
    after = _source_identity()
    stable = before["head"] == after["head"] and before["source_files"] == after["source_files"]
    manifest = {
        "schema_version": 1,
        "scope": "provider_free_engineering_contracts",
        "status": "passed" if stable and all(group["status"] == "passed" for group in groups) else "failed",
        "started_at": started, "ended_at": _utc_now(), "source_before": before,
        "source_after": after, "source_stable": stable,
        "source_identity_scope": "recorded_runtime_test_config_evaluation_files_not_full_environment",
        "source_context": "dirty_worktree" if before["dirty"] or after["dirty"] else "clean_commit",
        "live_model_calls_authorized": False,
        "services_started_by_entrypoint": False,
        "os_egress_sandbox": False,
        "local_only": True,
        "groups": groups,
        "not_proven": ["legal_quality", "live_provider_quality_or_billing", "postgres_recovery", "real_broker_recovery", "production_capacity"],
    }
    manifest_path = target / "manifest.json"
    with manifest_path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    return manifest, manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List the fixed offline groups without running them.")
    parser.add_argument("--output-dir", type=Path, help="Empty local directory beneath this repository's ignored .tmp directory.")
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps({"groups": [{"group_id": key, "selector": selector} for key, selector in GROUPS]}, ensure_ascii=False))
        return 0
    try:
        manifest, manifest_path = run_showcase(args.output_dir)
    except (ShowcaseError, OSError) as error:
        code = str(error) if isinstance(error, ShowcaseError) else "local_evidence_write_failed"
        print(json.dumps({"status": "failed", "error_code": code}))
        return 1
    print(json.dumps({
        "status": manifest["status"], "groups_passed": sum(group["status"] == "passed" for group in manifest["groups"]),
        "groups_total": len(manifest["groups"]), "source_stable": manifest["source_stable"],
        "source_context": manifest["source_context"], "manifest": str(manifest_path),
    }))
    return 0 if manifest["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
