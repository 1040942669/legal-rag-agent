from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit


REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
RECOVERY_SELECTOR: Final = (
    "integration_tests/test_m5_fault_recovery.py::"
    "test_m5_t01_kill_after_retrieval_checkpoint_resumes_without_retrieval"
)
_SHA: Final = re.compile(r"[0-9a-f]{40}")


def _candidate_sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    candidate = completed.stdout.strip().lower()
    if _SHA.fullmatch(candidate) is None:
        raise RuntimeError("git did not return an exact candidate commit SHA")
    return candidate


def _database_url(value: str | None) -> str:
    resolved = (value or os.environ.get("LEGAL_RAG_DATABASE_URL", "")).strip()
    if not resolved:
        raise ValueError(
            "provide --database-url or LEGAL_RAG_DATABASE_URL for a disposable "
            "PostgreSQL test database"
        )
    scheme = urlsplit(resolved).scheme
    if scheme not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("the M5 recovery demo requires PostgreSQL with psycopg")
    return resolved


def _subprocess_environment(
    *,
    database_url: str,
    candidate_sha: str,
    receipt_path: Path | None,
) -> dict[str, str]:
    inherited_names = (
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "VIRTUAL_ENV",
        "WINDIR",
    )
    environment = {
        name: os.environ[name]
        for name in inherited_names
        if os.environ.get(name)
    }
    environment.update(
        {
            "ALLOW_LIVE_MODEL_CALLS": "false",
            "HF_HUB_OFFLINE": "1",
            "LEGAL_RAG_DATABASE_URL": database_url,
            "LEGAL_RAG_DISABLE_DOTENV": "1",
            "LEGAL_RAG_INTEGRATION_TEST": "1",
            "LEGAL_RAG_M5_CANDIDATE_SHA": candidate_sha,
            "NO_PROXY": "*",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "UV_OFFLINE": "1",
        }
    )
    if receipt_path is not None:
        environment["LEGAL_RAG_M5_FAULT_RECEIPT"] = str(receipt_path.resolve())
    return environment


def run_demo(
    *,
    database_url: str,
    candidate_sha: str,
    receipt_path: Path | None = None,
    timeout_seconds: int = 120,
) -> int:
    """Run the real T01 hard-kill/restart scenario in one command."""

    if type(timeout_seconds) is not int or not 10 <= timeout_seconds <= 600:
        raise ValueError("timeout_seconds must be an integer between 10 and 600")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-ra",
        RECOVERY_SELECTOR,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=_subprocess_environment(
                database_url=database_url,
                candidate_sha=candidate_sha,
                receipt_path=receipt_path,
            ),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return 124
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the M5 cross-process PostgreSQL checkpoint recovery demo. "
            "The demo never calls a live model."
        )
    )
    parser.add_argument(
        "--database-url",
        help=(
            "Disposable PostgreSQL base URL. Defaults to "
            "LEGAL_RAG_DATABASE_URL; the test creates and drops an isolated database."
        ),
    )
    parser.add_argument(
        "--candidate-sha",
        help="Exact candidate SHA. Defaults to the current Git HEAD.",
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        help="Optional path for the partial M5 fault receipt containing M5-T01.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
        help="Bound the complete hard-kill/recovery demonstration (default: 120).",
    )
    args = parser.parse_args(argv)

    try:
        database_url = _database_url(args.database_url)
        candidate_sha = (args.candidate_sha or _candidate_sha()).strip().lower()
        if _SHA.fullmatch(candidate_sha) is None:
            raise ValueError("--candidate-sha must be an exact 40-character SHA")
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as error:
        parser.error(str(error))

    return_code = run_demo(
        database_url=database_url,
        candidate_sha=candidate_sha,
        receipt_path=args.receipt,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps(
            {
                "candidate_sha": candidate_sha,
                "live_model_calls": False,
                "scenario": "M5-T01",
                "schema_version": 1,
                "status": "passed" if return_code == 0 else "failed",
            },
            sort_keys=True,
        )
    )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
