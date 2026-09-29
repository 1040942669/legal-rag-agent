"""Explicit, default-off wiring for local and optional remote M6 observations."""

from __future__ import annotations

import os
from pathlib import Path

from .events import CompositeObserver, LocalJsonlObserver, Observer
from .langfuse import LangfuseExporter


def _enabled(name: str) -> bool:
    value = os.environ.get(name, "0")
    if value not in {"0", "1"}:
        raise ValueError(f"{name} must be 0 or 1")
    return value == "1"


def observer_from_environment() -> Observer | None:
    """Load sinks without enabling any remote data flow by default.

    Remote export requires both a feature switch and explicit operator
    acknowledgement of the data destination. Credentials are never included
    in validation messages or local records.
    """

    local_path = os.environ.get("LEGAL_RAG_OBSERVATION_JSONL_PATH", "").strip()
    remote_enabled = _enabled("LEGAL_RAG_LANGFUSE_ENABLED")
    remote_acknowledged = _enabled("LEGAL_RAG_LANGFUSE_EXPORT_ACK")
    if local_path and not Path(local_path).is_absolute():
        raise ValueError("LEGAL_RAG_OBSERVATION_JSONL_PATH must be absolute")
    if remote_enabled and not remote_acknowledged:
        raise ValueError("Langfuse remote export must be explicitly acknowledged")

    observers: list[Observer] = []
    if local_path:
        observers.append(LocalJsonlObserver(local_path))
    if remote_enabled:
        observers.append(
            LangfuseExporter(
                enabled=True,
                remote_export_acknowledged=True,
                base_url=os.environ.get("LANGFUSE_BASE_URL"),
                public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
                secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
            )
        )
    if not observers:
        return None
    if len(observers) == 1:
        return observers[0]
    return CompositeObserver(*observers)


def close_observer(observer: Observer | None) -> None:
    """Drain an optional remote sink briefly during graceful process shutdown."""

    if isinstance(observer, CompositeObserver):
        for sink in observer.observers:
            close_observer(sink)
    elif isinstance(observer, LangfuseExporter):
        observer.close(timeout_seconds=1.0)


__all__ = ["close_observer", "observer_from_environment"]
