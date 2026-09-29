"""Optional, redacted OTLP/HTTP JSON export for Langfuse.

Only values assembled from the typed Observation allowlist enter the bounded
queue.  Remote export is disabled unless explicitly enabled and acknowledged.
The legacy Langfuse batch ingestion endpoint is intentionally not used.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import queue
import secrets
import threading
import time
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit

from .events import Observation


_OTLP_TRACE_PATH = "/api/public/otel/v1/traces"


def _redacted_id(value: str, key: bytes, kind: str, length: int = 32) -> str:
    digest = hmac.new(
        key, f"{kind}:{value}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest[:length]


def _attribute(key: str, value: str | int | float | bool) -> dict[str, object]:
    if isinstance(value, bool):
        encoded: dict[str, object] = {"boolValue": value}
    elif isinstance(value, int):
        # OTLP JSON encodes 64-bit integer fields as decimal strings.
        encoded = {"intValue": str(value)}
    elif isinstance(value, float):
        encoded = {"doubleValue": value}
    else:
        encoded = {"stringValue": value}
    return {"key": key, "value": encoded}


def build_langfuse_payload(
    observation: Observation, *, redaction_key: bytes
) -> dict[str, object]:
    """Build a strict allowlisted OTLP span with pseudonymous correlations.

    The remote payload omits all free-form text, evidence IDs, token counts,
    prompts, answers, credentials, and user identifiers.  The same local key
    makes correlation stable for one installation without exporting raw IDs.
    """

    if not isinstance(observation, Observation):
        raise TypeError("observation must be an Observation")
    if not isinstance(redaction_key, bytes) or not redaction_key:
        raise ValueError("redaction_key must be non-empty bytes")

    context = observation.context
    attributes = [
        _attribute("langfuse.observation.type", "event"),
        _attribute("langfuse.trace.name", "legal-rag-m6"),
        _attribute("m6.status", observation.status),
        _attribute("m6.retry_count", observation.retry_count),
        _attribute("m6.evidence_count", len(observation.evidence_ids)),
    ]
    if context.session_id is not None:
        attributes.append(
            _attribute(
                "langfuse.session.id",
                _redacted_id(context.session_id, redaction_key, "session"),
            )
        )
    for name in ("run_id", "experiment_id", "job_id"):
        value = getattr(context, name)
        if value is not None:
            attributes.append(
                _attribute(
                    f"langfuse.trace.metadata.{name}",
                    _redacted_id(value, redaction_key, name),
                )
            )
    for name in ("node", "tool", "cache_status", "error_category"):
        value = getattr(observation, name)
        if value is not None:
            attributes.append(_attribute(f"m6.{name}", value))
    for name in ("duration_ms", "queue_wait_ms", "cost_usd"):
        value = getattr(observation, name)
        if value is not None:
            attributes.append(_attribute(f"m6.{name}", float(value)))
    for name, count in sorted(observation.counts.items()):
        attributes.append(_attribute(f"m6.count.{name}", count))
    for name, count in sorted(observation.budget_used.items()):
        attributes.append(_attribute(f"m6.budget.{name}", count))

    end_ns = int(observation.occurred_at.timestamp() * 1_000_000_000)
    duration_ns = int((observation.duration_ms or 0) * 1_000_000)
    span = {
        "traceId": _redacted_id(context.trace_id, redaction_key, "trace"),
        "spanId": secrets.token_hex(8),
        "name": observation.name,
        "startTimeUnixNano": str(max(0, end_ns - duration_ns)),
        "endTimeUnixNano": str(end_ns),
        "attributes": attributes,
    }
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attribute("service.name", "legal-rag")]},
                "scopeSpans": [
                    {"scope": {"name": "legal_rag.observability"}, "spans": [span]}
                ],
            }
        ]
    }


def _endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Langfuse base_url must be an absolute HTTP URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Langfuse base_url must not include credentials or query")
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("remote Langfuse base_url must use HTTPS")
    return base_url.rstrip("/") + _OTLP_TRACE_PATH


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # Never forward Basic authentication to a redirected host.
        return None


Transport = Callable[[bytes, dict[str, str], float], None]


class LangfuseExporter:
    """Best-effort nonblocking exporter with bounded memory and I/O time.

    A configured exporter sends sanitized OTLP JSON to the Langfuse OTEL
    endpoint.  It never exports a raw Observation, and records no exception
    message because transport failures may contain credentials.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        remote_export_acknowledged: bool = False,
        base_url: str | None = None,
        public_key: str | None = None,
        secret_key: str | None = None,
        timeout_seconds: float = 0.5,
        max_queue_size: int = 128,
        transport: Transport | None = None,
    ) -> None:
        self.enabled = enabled
        self.failed_count = 0
        self.dropped_count = 0
        self.last_error_category: str | None = None
        if type(max_queue_size) is not int or not 1 <= max_queue_size <= 10_000:
            raise ValueError("max_queue_size must be between 1 and 10000")
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float)
        ):
            raise ValueError("timeout_seconds must be a finite positive number")
        if not 0.01 <= timeout_seconds <= 2:
            raise ValueError("timeout_seconds must be between 0.01 and 2 seconds")
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=max_queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._transport = transport
        self._endpoint: str | None = None
        self._headers: dict[str, str] = {}
        self._redaction_key = b""
        self._timeout_seconds = float(timeout_seconds)
        if not enabled:
            return
        if not remote_export_acknowledged:
            raise ValueError("remote export must be explicitly acknowledged")
        if not base_url or not public_key or not secret_key:
            raise ValueError("Langfuse export requires host and both API keys")
        self._endpoint = _endpoint(base_url)
        self._redaction_key = hashlib.sha256(
            b"legal-rag-m6-redaction:" + secret_key.encode("utf-8")
        ).digest()
        credentials = base64.b64encode(
            f"{public_key}:{secret_key}".encode("utf-8")
        ).decode("ascii")
        self._headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
            "x-langfuse-ingestion-version": "4",
        }
        self._thread = threading.Thread(
            target=self._run, name="legal-rag-langfuse-export", daemon=True
        )
        self._thread.start()

    def record(self, observation: Observation) -> None:
        if not self.enabled:
            return
        try:
            payload = build_langfuse_payload(
                observation, redaction_key=self._redaction_key
            )
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self._queue.put_nowait(body)
        except queue.Full:
            self.dropped_count += 1
            self.last_error_category = "queue_full"
        except Exception:
            self.failed_count += 1
            self.last_error_category = "export_failed"

    def flush(self, *, timeout_seconds: float = 1.0) -> bool:
        """Wait briefly for tests/shutdown; never used on the request path."""

        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return self._queue.unfinished_tasks == 0

    def close(self, *, timeout_seconds: float = 1.0) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout_seconds))
            return not self._thread.is_alive()
        return True

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                body = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if self._transport is not None:
                    self._transport(body, dict(self._headers), self._timeout_seconds)
                else:
                    self._send_http(body)
            except Exception:
                self.failed_count += 1
                self.last_error_category = "export_failed"
            finally:
                self._queue.task_done()

    def _send_http(self, body: bytes) -> None:
        assert self._endpoint is not None
        request = urllib.request.Request(
            self._endpoint, data=body, headers=self._headers, method="POST"
        )
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=self._timeout_seconds) as response:
            # Consuming this small response closes the connection promptly.
            response.read(1024)


__all__ = ["LangfuseExporter", "build_langfuse_payload"]
