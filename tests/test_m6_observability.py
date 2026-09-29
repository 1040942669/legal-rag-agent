from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import pytest

from legal_rag.observability import (
    LangfuseExporter,
    LocalJsonlObserver,
    Observation,
    ObservationContext,
    build_langfuse_payload,
)


def _observation(**overrides: object) -> Observation:
    values: dict[str, object] = {
        "context": ObservationContext(
            trace_id="trace-private@example.com",
            session_id="session-private@example.com",
            run_id="run-123",
            experiment_id="experiment-123",
            job_id="job-123",
        ),
        "name": "job.progress",
        "status": "running",
        "occurred_at": datetime(2026, 9, 29, tzinfo=timezone.utc),
        "duration_ms": 37.5,
        "queue_wait_ms": 12.0,
        "counts": {"total": 10, "completed": 3, "failed": 1, "pending": 6},
        "budget_used": {"tool_attempts": 2, "model_attempts": 1},
        "retry_count": 1,
        "cache_status": "miss",
        "evidence_ids": ("evidence-private@example.com",),
        "model_input_tokens": 30,
        "model_output_tokens": 12,
        "cost_usd": 0.02,
    }
    values.update(overrides)
    return Observation(**values)


def test_local_observation_keeps_correlation_and_execution_facts(tmp_path) -> None:
    path = tmp_path / "m6-trace.jsonl"
    LocalJsonlObserver(path).record(_observation())

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["trace_id"] == "trace-private@example.com"
    assert record["session_id"] == "session-private@example.com"
    assert record["run_id"] == "run-123"
    assert record["experiment_id"] == "experiment-123"
    assert record["job_id"] == "job-123"
    assert record["counts"] == {
        "total": 10,
        "completed": 3,
        "failed": 1,
        "pending": 6,
    }
    assert record["queue_wait_ms"] == 12.0
    assert record["model_input_tokens"] == 30
    assert record["evidence_ids"] == ["evidence-private@example.com"]


def test_langfuse_payload_allowlist_redacts_text_secrets_and_identifiers() -> None:
    event = _observation(error_category="Authorization Bearer secret-private")
    payload = build_langfuse_payload(event, redaction_key=b"local-secret")
    serialized = json.dumps(payload, sort_keys=True)

    for forbidden in (
        "private@example.com",
        "secret-private",
        "Bearer",
        "evidence-private",
        "model_input_tokens",
        "model_output_tokens",
        "token",
        "query",
        "answer",
        "Authorization",
    ):
        assert forbidden not in serialized
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 1
    span = spans[0]
    assert len(span["traceId"]) == 32
    assert len(span["spanId"]) == 16
    attributes = {
        attribute["key"]: next(iter(attribute["value"].values()))
        for attribute in span["attributes"]
    }
    assert attributes["m6.error_category"] == "other"
    assert attributes["m6.count.completed"] == "3"
    assert attributes["m6.evidence_count"] == "1"
    assert attributes["m6.queue_wait_ms"] == 12.0


def test_observation_rejects_unknown_fields_and_non_numeric_metrics() -> None:
    with pytest.raises(TypeError):
        _observation(query="private question")
    with pytest.raises(ValueError):
        _observation(counts={"private_query": 1})
    with pytest.raises(ValueError):
        _observation(counts={"completed": "private"})


def test_disabled_exporter_never_sends() -> None:
    sent: list[bytes] = []
    exporter = LangfuseExporter(
        transport=lambda body, headers, timeout: sent.append(body)
    )
    exporter.record(_observation())
    assert exporter.flush(timeout_seconds=0.1)
    exporter.close()
    assert sent == []


def test_remote_export_requires_explicit_acknowledgement_and_https() -> None:
    with pytest.raises(ValueError, match="acknowledged"):
        LangfuseExporter(
            enabled=True,
            base_url="https://example.langfuse.test",
            public_key="pk-test",
            secret_key="sk-test",
        )
    with pytest.raises(ValueError, match="HTTPS"):
        LangfuseExporter(
            enabled=True,
            remote_export_acknowledged=True,
            base_url="http://example.langfuse.test",
            public_key="pk-test",
            secret_key="sk-test",
        )


def test_export_failure_does_not_block_or_raise_on_main_path() -> None:
    sent = threading.Event()

    def failing_transport(body: bytes, headers: dict[str, str], timeout: float) -> None:
        sent.set()
        raise OSError("connection contains Authorization Bearer private-secret")

    exporter = LangfuseExporter(
        enabled=True,
        remote_export_acknowledged=True,
        base_url="https://example.langfuse.test",
        public_key="pk-test",
        secret_key="sk-test",
        timeout_seconds=0.1,
        transport=failing_transport,
    )
    started = time.monotonic()
    exporter.record(_observation())
    elapsed = time.monotonic() - started
    assert elapsed < 0.1
    assert exporter.flush(timeout_seconds=1.0)
    assert sent.is_set()
    assert exporter.failed_count == 1
    assert exporter.last_error_category == "export_failed"
    exporter.close()


def test_transport_receives_only_redacted_otlp_json() -> None:
    sent: list[tuple[bytes, dict[str, str], float]] = []

    def capture(body: bytes, headers: dict[str, str], timeout: float) -> None:
        sent.append((body, headers, timeout))

    exporter = LangfuseExporter(
        enabled=True,
        remote_export_acknowledged=True,
        base_url="https://example.langfuse.test",
        public_key="pk-test",
        secret_key="sk-test",
        transport=capture,
    )
    exporter.record(_observation())
    assert exporter.flush(timeout_seconds=1.0)
    exporter.close()
    assert len(sent) == 1
    body, headers, timeout = sent[0]
    assert "private@example.com" not in body.decode("utf-8")
    assert "pk-test" not in body.decode("utf-8")
    assert "sk-test" not in body.decode("utf-8")
    assert headers["Content-Type"] == "application/json"
    assert headers["x-langfuse-ingestion-version"] == "4"
    assert timeout == 0.5


def test_export_queue_is_bounded_and_never_waits_for_transport() -> None:
    entered = threading.Event()
    release = threading.Event()

    def slow_transport(body: bytes, headers: dict[str, str], timeout: float) -> None:
        entered.set()
        release.wait(timeout=0.5)

    exporter = LangfuseExporter(
        enabled=True,
        remote_export_acknowledged=True,
        base_url="https://example.langfuse.test",
        public_key="pk-test",
        secret_key="sk-test",
        max_queue_size=1,
        timeout_seconds=0.1,
        transport=slow_transport,
    )
    try:
        exporter.record(_observation())
        assert entered.wait(timeout=1.0)
        started = time.monotonic()
        for _ in range(20):
            exporter.record(_observation())
        assert time.monotonic() - started < 0.2
        assert exporter.dropped_count >= 19
    finally:
        release.set()
        exporter.close()
