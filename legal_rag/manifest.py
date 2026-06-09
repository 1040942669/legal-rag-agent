from __future__ import annotations

import json
import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def new_run_id(prefix: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{timestamp}_{uuid.uuid4().hex[:8]}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def summarize_path(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).resolve()
    return {
        "path": str(resolved),
        "exists": resolved.exists(),
        "is_file": resolved.is_file(),
        "is_dir": resolved.is_dir(),
    }


def write_artifact_manifest(
    output_path: str | Path,
    *,
    artifact_type: str,
    run_id: str | None = None,
    inputs: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = {
        "manifest_version": 1,
        "artifact_type": artifact_type,
        "run_id": run_id or new_run_id(artifact_type),
        "created_at": utc_now(),
        "environment": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
        },
        "inputs": inputs or {},
        "config": config or {},
        "outputs": outputs or {},
        "metrics": metrics or {},
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
