from __future__ import annotations

import re
import socket
import traceback
import urllib.error
from dataclasses import dataclass
from math import isfinite
from typing import Any, NoReturn


PROVIDER_ERROR_CODES = frozenset(
    {
        "timeout",
        "rate_limited",
        "network",
        "unavailable",
        "auth",
        "invalid_request",
        "configuration",
        "invalid_response",
        "unknown",
    }
)
RETRYABLE_PROVIDER_ERROR_CODES = frozenset(
    {"timeout", "rate_limited", "network", "unavailable"}
)
NON_RETRYABLE_PROVIDER_ERROR_CODES = (
    PROVIDER_ERROR_CODES - RETRYABLE_PROVIDER_ERROR_CODES
)
MAX_PROVIDER_TIMEOUT_SECONDS = 3600.0

_STATUS_ERROR_CODES = {
    400: "invalid_request",
    401: "auth",
    403: "auth",
    404: "invalid_request",
    408: "timeout",
    413: "invalid_request",
    422: "invalid_request",
    429: "rate_limited",
    500: "unavailable",
    502: "unavailable",
    503: "unavailable",
    504: "unavailable",
    501: "unknown",
    505: "unknown",
}
_SAFE_BOUNDARY_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_CAUSE_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_CLASSIFICATION_SOURCES = frozenset(
    {"explicit", "provider", "status", "type", "cause", "fallback"}
)


@dataclass(frozen=True)
class ProviderErrorClassification:
    """Safe, stable classification facts for one provider failure."""

    error_code: str
    retryable: bool
    status_code: int | None
    cause_type: str
    classification_source: str

    def to_safe_dict(self) -> dict[str, str | int | bool | None]:
        return {
            "error_code": self.error_code,
            "retryable": self.retryable,
            "status_code": self.status_code,
            "cause_type": self.cause_type,
            "classification_source": self.classification_source,
        }


class ProviderCallError(RuntimeError):
    """A provider failure with stable retry semantics and no raw response data."""

    def __init__(
        self,
        error_code: str,
        *,
        provider: str,
        operation: str,
        status_code: int | None = None,
        cause_type: str = "ProviderError",
        classification_source: str = "explicit",
    ) -> None:
        if error_code not in PROVIDER_ERROR_CODES:
            raise ValueError(f"unsupported provider error code: {error_code!r}")
        self.provider = _validate_boundary_name("provider", provider)
        self.operation = _validate_boundary_name("operation", operation)
        self.error_code = error_code
        self.retryable = error_code in RETRYABLE_PROVIDER_ERROR_CODES
        self.status_code = _validate_status_code(status_code)
        if self.status_code is not None and (
            _status_error_code(self.status_code) != error_code
        ):
            raise ValueError("provider error code disagrees with HTTP status")
        self.cause_type = _safe_cause_type(cause_type)
        if classification_source not in _CLASSIFICATION_SOURCES:
            raise ValueError("unsupported provider error classification source")
        self.classification_source = classification_source
        status = f", status={self.status_code}" if self.status_code is not None else ""
        super().__init__(
            f"{self.provider} {self.operation} failed ({self.error_code}{status})"
        )

    @property
    def code(self) -> str:
        """Alias used by callers that expose a generic structured-error API."""

        return self.error_code

    def to_safe_dict(self) -> dict[str, str | int | bool | None]:
        """Return only bounded classification metadata, never provider text/body."""

        return {
            "provider": self.provider,
            "operation": self.operation,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "status_code": self.status_code,
            "cause_type": self.cause_type,
            "classification_source": self.classification_source,
        }


def validate_timeout_seconds(
    value: Any,
    *,
    provider: str,
    operation: str,
) -> float:
    """Validate a single-request timeout without accepting bool/NaN/infinity."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value <= 0
        or value > MAX_PROVIDER_TIMEOUT_SECONDS
    ):
        raise ProviderCallError(
            "configuration",
            provider=provider,
            operation=operation,
            cause_type="InvalidTimeout",
        )
    return float(value)


def classify_provider_error(error: BaseException) -> ProviderErrorClassification:
    """Classify typed, HTTP-status, and chained provider exceptions."""

    if isinstance(error, ProviderCallError):
        return ProviderErrorClassification(
            error_code=error.error_code,
            retryable=error.retryable,
            status_code=error.status_code,
            cause_type=error.cause_type,
            classification_source="provider",
        )

    for candidate, depth in _exception_chain(error):
        status_code = _extract_status_code(candidate)
        if status_code is not None:
            error_code = _status_error_code(status_code)
            return _classification(
                error_code,
                status_code=status_code,
                cause=candidate,
                source="status" if depth == 0 else "cause",
            )
        error_code = _typed_error_code(candidate)
        if error_code is not None:
            return _classification(
                error_code,
                status_code=None,
                cause=candidate,
                source="type" if depth == 0 else "cause",
            )

    return _classification(
        "unknown",
        status_code=None,
        cause=error,
        source="fallback",
    )


def raise_sanitized_provider_error(error: ProviderCallError) -> NoReturn:
    """Raise a typed boundary error without retaining sensitive inner frames.

    Provider SDK response objects, prompts, and credentials can otherwise remain
    reachable through traceback frame locals even when ``str(error)`` and the
    visible exception chain are safe.  Callers must overwrite any sensitive
    locals in their currently executing public-boundary frame before invoking
    this helper; completed inner frames are cleared and detached here.
    """

    if error.__traceback__ is not None:
        traceback.clear_frames(error.__traceback__)
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    raise error from None


def provider_call_error(
    error: BaseException,
    *,
    provider: str,
    operation: str,
) -> ProviderCallError:
    """Convert an exception once; preserve an existing typed boundary verbatim."""

    if isinstance(error, ProviderCallError):
        return error
    classified = classify_provider_error(error)
    return ProviderCallError(
        classified.error_code,
        provider=provider,
        operation=operation,
        status_code=classified.status_code,
        cause_type=classified.cause_type,
        classification_source=classified.classification_source,
    )


def should_propagate_provider_error(error: BaseException, client: Any) -> bool:
    """Let controlled experiment clients opt out of legacy graceful degradation."""

    return isinstance(error, ProviderCallError) and (
        getattr(client, "propagate_provider_errors", None) is True
    )


def should_propagate_controlled_error(error: BaseException, client: Any) -> bool:
    """Keep runner control/contract failures out of legacy fallback paths.

    Production clients retain their existing graceful-degradation behavior.  The
    experiment adapter opts its wrapper into strict propagation so stop signals,
    ledger mismatches, parsing failures, and typed provider errors all abort the
    cache producer before an artifact can be written.
    """

    return getattr(client, "propagate_control_errors", None) is True or (
        should_propagate_provider_error(error, client)
    )


def _classification(
    error_code: str,
    *,
    status_code: int | None,
    cause: BaseException,
    source: str,
) -> ProviderErrorClassification:
    return ProviderErrorClassification(
        error_code=error_code,
        retryable=error_code in RETRYABLE_PROVIDER_ERROR_CODES,
        status_code=status_code,
        cause_type=_safe_cause_type(type(cause).__name__),
        classification_source=source,
    )


def _status_error_code(status_code: int) -> str:
    explicit = _STATUS_ERROR_CODES.get(status_code)
    if explicit is not None:
        return explicit
    if 500 <= status_code <= 599:
        return "unavailable"
    return "unknown"


def _exception_chain(error: BaseException) -> list[tuple[BaseException, int]]:
    pending: list[tuple[BaseException, int]] = [(error, 0)]
    chain: list[tuple[BaseException, int]] = []
    seen: set[int] = set()
    while pending and len(chain) < 16:
        candidate, depth = pending.pop(0)
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))
        chain.append((candidate, depth))
        cause = candidate.__cause__
        context = candidate.__context__
        reason = getattr(candidate, "reason", None)
        visible_chain = cause
        if visible_chain is None and not candidate.__suppress_context__:
            visible_chain = context
        for nested in (visible_chain, reason):
            if isinstance(nested, BaseException):
                pending.append((nested, depth + 1))
    return chain


def _extract_status_code(error: BaseException) -> int | None:
    candidates = [
        getattr(error, "status_code", None),
        getattr(error, "code", None),
        getattr(getattr(error, "response", None), "status_code", None),
        getattr(getattr(error, "response", None), "status", None),
    ]
    for value in candidates:
        if type(value) is int and 100 <= value <= 599:
            return value
    return None


def _typed_error_code(error: BaseException) -> str | None:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(error, (ConnectionError, socket.gaierror)):
        return "network"
    if isinstance(error, urllib.error.URLError):
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            return "timeout"
        return "network"

    name = type(error).__name__.casefold()
    if "timeout" in name:
        return "timeout"
    if "ratelimit" in name or "rate_limit" in name:
        return "rate_limited"
    if "authentication" in name or "permissiondenied" in name:
        return "auth"
    if any(
        marker in name for marker in ("badrequest", "notfound", "unprocessableentity")
    ):
        return "invalid_request"
    if "responsevalidation" in name or "invalidresponse" in name:
        return "invalid_response"
    if any(
        marker in name
        for marker in (
            "apiconnection",
            "connecterror",
            "networkerror",
            "transporterror",
            "readerror",
            "writeerror",
            "remoteprotocolerror",
        )
    ):
        return "network"
    if "serviceunavailable" in name:
        return "unavailable"
    return None


def _validate_boundary_name(label: str, value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_BOUNDARY_NAME.fullmatch(value):
        raise ValueError(f"{label} must be a safe lowercase identifier")
    return value


def _validate_status_code(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 100 <= value <= 599:
        raise ValueError("provider status_code must be an HTTP status integer")
    return value


def _safe_cause_type(value: Any) -> str:
    if isinstance(value, str) and _SAFE_CAUSE_TYPE.fullmatch(value):
        return value
    return "Exception"
