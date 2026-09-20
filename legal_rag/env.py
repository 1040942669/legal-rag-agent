from __future__ import annotations

import os
from pathlib import Path

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def env_flag_enabled(name: str) -> bool:
    """Return True only when an environment flag is explicitly enabled."""

    return os.environ.get(name, "").strip().casefold() in _TRUE_VALUES


def live_model_calls_allowed() -> bool:
    """Allow live model calls only after an explicit affirmative opt-in."""

    return env_flag_enabled("ALLOW_LIVE_MODEL_CALLS")


def require_live_model_calls_allowed(operation: str) -> None:
    """Fail before provider initialization when an offline run forbids model calls."""

    if not live_model_calls_allowed():
        raise RuntimeError(
            f"{operation} is disabled because ALLOW_LIVE_MODEL_CALLS is not explicitly true."
        )


def load_dotenv(
    path: str | Path | None = None, *, override: bool = False
) -> Path | None:
    if env_flag_enabled("LEGAL_RAG_DISABLE_DOTENV"):
        return None
    env_path = Path(path) if path else find_project_dotenv()
    if not env_path or not env_path.exists():
        return None

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = clean_env_value(value.strip())
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
    return env_path


def find_project_dotenv() -> Path | None:
    current = Path.cwd().resolve()
    for directory in [current, *current.parents]:
        candidate = directory / ".env"
        if candidate.exists():
            return candidate
    return None


def clean_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
