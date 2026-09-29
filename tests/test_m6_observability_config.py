from __future__ import annotations

import pytest

from legal_rag.observability.config import observer_from_environment
from legal_rag.observability.events import LocalJsonlObserver


def test_observer_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LEGAL_RAG_OBSERVATION_JSONL_PATH",
        "LEGAL_RAG_LANGFUSE_ENABLED",
        "LEGAL_RAG_LANGFUSE_EXPORT_ACK",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    assert observer_from_environment() is None


def test_local_observer_requires_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("LEGAL_RAG_OBSERVATION_JSONL_PATH", "relative/events.jsonl")
    with pytest.raises(ValueError, match="absolute"):
        observer_from_environment()
    monkeypatch.setenv(
        "LEGAL_RAG_OBSERVATION_JSONL_PATH", str(tmp_path / "events.jsonl")
    )
    assert isinstance(observer_from_environment(), LocalJsonlObserver)


def test_remote_export_requires_explicit_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEGAL_RAG_OBSERVATION_JSONL_PATH", raising=False)
    monkeypatch.setenv("LEGAL_RAG_LANGFUSE_ENABLED", "1")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://example.com")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    monkeypatch.delenv("LEGAL_RAG_LANGFUSE_EXPORT_ACK", raising=False)
    with pytest.raises(ValueError, match="acknowledged"):
        observer_from_environment()
