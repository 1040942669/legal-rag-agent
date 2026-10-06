from __future__ import annotations

import json
import os
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar

from .env import load_dotenv, require_live_model_calls_allowed
from .provider_errors import (
    ProviderCallError,
    provider_call_error,
    raise_sanitized_provider_error,
    validate_timeout_seconds,
)

SILICONFLOW_MODEL_PREFIX = "siliconflow:"
_T = TypeVar("_T")


@dataclass
class CompletionUsage:
    calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    token_usage_calls: int = 0
    latency_ms: float = 0.0
    # Component coverage is separate from aggregate usage. Missing provider
    # fields must not become a falsely complete zero in execution telemetry.
    input_usage_calls: int = 0
    output_usage_calls: int = 0
    total_usage_calls: int = 0

    def record_tokens(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
    ) -> None:
        if input_tokens is None and output_tokens is None and total_tokens is None:
            return
        prompt = max(0, int(input_tokens or 0))
        completion = max(0, int(output_tokens or 0))
        total = max(
            0, int(total_tokens if total_tokens is not None else prompt + completion)
        )
        self.input_tokens += prompt
        self.output_tokens += completion
        self.total_tokens += total
        self.token_usage_calls += 1
        self.input_usage_calls += int(input_tokens is not None)
        self.output_usage_calls += int(output_tokens is not None)
        self.total_usage_calls += int(total_tokens is not None)

    def snapshot(self) -> dict[str, int | float]:
        return {
            "calls": self.calls,
            "failed_calls": self.failed_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "token_usage_calls": self.token_usage_calls,
            "latency_ms": round(self.latency_ms, 3),
        }


@dataclass
class OllamaClient:
    propagate_provider_errors: ClassVar[bool] = False
    hidden_retries_disabled: ClassVar[bool] = True

    model: str
    base_url: str = "http://localhost:11434"
    request_timeout: float = 180
    _llamaindex_llm: Any = field(default=None, repr=False, compare=False)
    usage: CompletionUsage = field(
        default_factory=CompletionUsage, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.request_timeout = validate_timeout_seconds(
            self.request_timeout,
            provider="ollama",
            operation="completion",
        )

    def complete(self, prompt: str) -> str:
        require_live_model_calls_allowed("Ollama completion")
        try:
            return _run_completion_attempt(
                usage=self.usage,
                provider="ollama",
                operation="completion",
                attempt=lambda: self._complete_once(prompt),
            )
        except ProviderCallError as exc:
            error = exc
        prompt = "<redacted>"
        raise_sanitized_provider_error(error)

    def _complete_once(self, prompt: str) -> tuple[str, dict[str, int | None]]:
        if self._llamaindex_llm is None and not self._load_llamaindex_llm():
            return self._complete_with_http_once(prompt)
        return self._complete_with_llamaindex_once(prompt)

    def _load_llamaindex_llm(self) -> bool:
        try:
            from llama_index.llms.ollama import Ollama
        except ModuleNotFoundError:
            return False
        try:
            self._llamaindex_llm = Ollama(
                model=self.model,
                base_url=self.base_url,
                request_timeout=self.request_timeout,
            )
        except ProviderCallError as exc:
            error = exc
        except Exception as exc:
            error = ProviderCallError(
                "configuration",
                provider="ollama",
                operation="completion",
                cause_type=type(exc).__name__,
            )
        else:
            return True
        raise error from None

    def _complete_with_llamaindex(self, prompt: str) -> str:
        return _run_completion_attempt(
            usage=self.usage,
            provider="ollama",
            operation="completion",
            attempt=lambda: self._complete_with_llamaindex_once(prompt),
        )

    def _complete_with_llamaindex_once(
        self, prompt: str
    ) -> tuple[str, dict[str, int | None]]:
        if self._llamaindex_llm is None:
            raise ProviderCallError(
                "configuration",
                provider="ollama",
                operation="completion",
                cause_type="MissingClient",
            )

        def decode(response: Any) -> tuple[str, dict[str, int | None]]:
            raw = getattr(response, "raw", None) or getattr(
                response, "additional_kwargs", None
            )
            token_counts = extract_token_counts(raw)
            text = getattr(
                response, "text", response if isinstance(response, str) else None
            )
            if not isinstance(text, str):
                self.usage.record_tokens(**token_counts)
                raise TypeError("completion response text is missing")
            return text.strip(), token_counts

        return _call_and_decode(
            provider="ollama",
            operation="completion",
            request=lambda: self._llamaindex_llm.complete(prompt),
            decode=decode,
        )

    def _complete_with_http(self, prompt: str) -> str:
        return _run_completion_attempt(
            usage=self.usage,
            provider="ollama",
            operation="completion",
            attempt=lambda: self._complete_with_http_once(prompt),
        )

    def _complete_with_http_once(
        self, prompt: str
    ) -> tuple[str, dict[str, int | None]]:
        url = self.base_url.rstrip("/") + "/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1},
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def send() -> bytes:
            with urllib.request.urlopen(
                request, timeout=self.request_timeout
            ) as response:
                return response.read()

        def decode(raw_body: bytes) -> tuple[str, dict[str, int | None]]:
            body = json.loads(raw_body.decode("utf-8"))
            token_counts = extract_token_counts(body)
            if not isinstance(body, dict) or not isinstance(body.get("response"), str):
                self.usage.record_tokens(**token_counts)
                raise TypeError("completion response body is invalid")
            return body["response"].strip(), token_counts

        return _call_and_decode(
            provider="ollama",
            operation="completion",
            request=send,
            decode=decode,
        )


@dataclass
class SiliconFlowClient:
    """OpenAI-compatible chat completion client for SiliconFlow API models."""

    propagate_provider_errors: ClassVar[bool] = False
    hidden_retries_disabled: ClassVar[bool] = True

    model: str
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key_env: str = "SILICONFLOW_API_KEY"
    request_timeout: float = 120
    temperature: float = 0.1
    _client: Any = field(default=None, repr=False, compare=False)
    usage: CompletionUsage = field(
        default_factory=CompletionUsage, init=False, repr=False, compare=False
    )
    # Keyword-only opt-ins preserve legacy requests and positional construction.
    max_tokens: int | None = field(default=None, kw_only=True)
    enable_thinking: bool | None = field(default=None, kw_only=True)
    response_format: str | None = field(default=None, kw_only=True)
    follow_redirects: bool | None = field(default=None, kw_only=True)
    load_environment_file: bool = field(default=True, kw_only=True)
    last_response_metadata: dict[str, Any] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.request_timeout = validate_timeout_seconds(
            self.request_timeout,
            provider="siliconflow",
            operation="completion",
        )
        valid = (
            (self.max_tokens is None or (
                type(self.max_tokens) is int and 1 <= self.max_tokens <= 32768
            ))
            and (self.enable_thinking is None or type(self.enable_thinking) is bool)
            and (self.response_format is None or self.response_format == "json_object")
            and (self.follow_redirects is None or type(self.follow_redirects) is bool)
            and type(self.load_environment_file) is bool
        )
        if not valid:
            raise ProviderCallError(
                "configuration", provider="siliconflow", operation="completion",
                cause_type="InvalidCompletionControls",
            )

    def complete(self, prompt: str) -> str:
        require_live_model_calls_allowed("SiliconFlow completion")
        try:
            return _run_completion_attempt(
                usage=self.usage,
                provider="siliconflow",
                operation="completion",
                attempt=lambda: self._complete_once(prompt),
            )
        except ProviderCallError as exc:
            error = exc
        prompt = "<redacted>"
        raise_sanitized_provider_error(error)

    def _complete_once(self, prompt: str) -> tuple[str, dict[str, int | None]]:
        self.last_response_metadata = {}
        client = self._get_client()

        def decode(response: Any) -> tuple[str, dict[str, int | None]]:
            token_counts = extract_token_counts(getattr(response, "usage", None))
            choices = getattr(response, "choices", None)
            if not isinstance(choices, (list, tuple)) or not choices:
                self.usage.record_tokens(**token_counts)
                raise TypeError("completion response choices are missing")
            message = getattr(choices[0], "message", None)
            self.last_response_metadata = _completion_response_metadata(
                response, choices[0], message, token_counts, self.model,
            )
            content = getattr(message, "content", None)
            if content is not None and not isinstance(content, str):
                self.usage.record_tokens(**token_counts)
                raise TypeError("completion response content is invalid")
            return (content or "").strip(), token_counts

        request_options: dict[str, Any] = {}
        if self.max_tokens is not None:
            request_options["max_tokens"] = self.max_tokens
        if self.enable_thinking is not None:
            request_options["extra_body"] = {"enable_thinking": self.enable_thinking}
        if self.response_format is not None:
            request_options["response_format"] = {"type": self.response_format}
        return _call_and_decode(
            provider="siliconflow",
            operation="completion",
            request=lambda: client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=self.temperature,
                **request_options,
            ),
            decode=decode,
        )

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:
            error = ProviderCallError(
                "configuration",
                provider="siliconflow",
                operation="completion",
                cause_type=type(exc).__name__,
            )
        else:
            error = None
        if error is not None:
            raise error from None
        if self.load_environment_file:
            load_dotenv()
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise ProviderCallError(
                "configuration",
                provider="siliconflow",
                operation="completion",
                cause_type="MissingApiKey",
            )
        try:
            transport_options: dict[str, Any] = {}
            if self.follow_redirects is not None:
                from openai import DefaultHttpxClient

                transport_options["http_client"] = DefaultHttpxClient(
                    follow_redirects=self.follow_redirects
                )
            self._client = OpenAI(
                api_key=api_key,
                base_url=self.base_url,
                timeout=self.request_timeout,
                max_retries=0,
                **transport_options,
            )
        except ProviderCallError as exc:
            error = exc
        except Exception as exc:
            error = ProviderCallError(
                "configuration",
                provider="siliconflow",
                operation="completion",
                cause_type=type(exc).__name__,
            )
        else:
            return self._client
        api_key = "<redacted>"
        http_client = transport_options.get("http_client")
        if http_client is not None:
            try:
                http_client.close()
            except Exception:
                pass
        raise_sanitized_provider_error(error)


def _completion_response_metadata(
    response: Any,
    choice: Any,
    message: Any,
    counts: dict[str, int | None],
    requested_model: str,
) -> dict[str, Any]:
    """Retain only bounded safety facts, never reasoning or response text."""

    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    if isinstance(usage, dict):
        details = usage.get("completion_tokens_details")
    reasoning_tokens = (
        details.get("reasoning_tokens") if isinstance(details, dict)
        else getattr(details, "reasoning_tokens", None)
    )
    if reasoning_tokens is not None and (
        type(reasoning_tokens) is not int or reasoning_tokens < 0
    ):
        raise ValueError("provider reasoning token usage is invalid")
    reasoning_reported = hasattr(message, "reasoning_content")
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning is not None and not isinstance(reasoning, str):
        raise TypeError("provider reasoning content is invalid")
    finish = getattr(choice, "finish_reason", None)
    if finish is not None and finish not in (
        "stop", "length", "tool_calls", "content_filter", "function_call",
    ):
        finish = "unknown"
    return {
        "finish_reason": finish,
        # Null is not an observed empty string. Missing facts stay unknown.
        "reasoning_content_reported": reasoning_reported and isinstance(reasoning, str),
        "reasoning_content_nonempty": bool(reasoning),
        "reasoning_tokens": reasoning_tokens,
        "returned_model_matches": getattr(response, "model", None) == requested_model,
        "prompt_tokens": counts["input_tokens"],
        "completion_tokens": counts["output_tokens"],
        "total_tokens": counts["total_tokens"],
    }


def _run_completion_attempt(
    *,
    usage: CompletionUsage,
    provider: str,
    operation: str,
    attempt: Callable[[], tuple[str, dict[str, int | None]]],
) -> str:
    """Account for one controlled completion attempt with at most one request."""

    usage.calls += 1
    started = time.perf_counter()
    try:
        text, token_counts = attempt()
        usage.record_tokens(**token_counts)
        return text
    except ProviderCallError:
        usage.failed_calls += 1
        raise
    except Exception as exc:
        usage.failed_calls += 1
        error = provider_call_error(
            exc,
            provider=provider,
            operation=operation,
        )
    finally:
        usage.latency_ms += (time.perf_counter() - started) * 1000
    raise error from None


def _call_and_decode(
    *,
    provider: str,
    operation: str,
    request: Callable[[], _T],
    decode: Callable[[_T], tuple[str, dict[str, int | None]]],
) -> tuple[str, dict[str, int | None]]:
    try:
        response = request()
    except ProviderCallError:
        raise
    except Exception as exc:
        error = provider_call_error(
            exc,
            provider=provider,
            operation=operation,
        )
    else:
        error = None
    if error is not None:
        raise error from None
    try:
        return decode(response)
    except ProviderCallError:
        raise
    except Exception as exc:
        error = ProviderCallError(
            "invalid_response",
            provider=provider,
            operation=operation,
            cause_type=type(exc).__name__,
        )
    raise error from None


def build_completion_client(
    model: str,
    *,
    ollama_base_url: str = "http://localhost:11434",
    request_timeout: float = 180,
):
    """Build the right completion client for a model name.

    Model names prefixed with `siliconflow:` route to the SiliconFlow API,
    everything else goes to the local Ollama server.
    """
    if model.startswith(SILICONFLOW_MODEL_PREFIX):
        return SiliconFlowClient(
            model=model.removeprefix(SILICONFLOW_MODEL_PREFIX),
            request_timeout=request_timeout,
        )
    return OllamaClient(
        model=model, base_url=ollama_base_url, request_timeout=request_timeout
    )


def extract_token_counts(payload: Any) -> dict[str, int | None]:
    if payload is None:
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump()

    def read(*keys: str) -> int | None:
        for key in keys:
            value = (
                payload.get(key)
                if isinstance(payload, dict)
                else getattr(payload, key, None)
            )
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("provider token usage is invalid")
                return value
        return None

    input_tokens = read(
        "prompt_tokens", "prompt_eval_count", "input_tokens", "prompt_token_count"
    )
    output_tokens = read(
        "completion_tokens",
        "eval_count",
        "output_tokens",
        "candidates_token_count",
    )
    total_tokens = read("total_tokens", "total_token_count")
    if (
        total_tokens is not None
        and input_tokens is not None
        and output_tokens is not None
        and total_tokens < input_tokens + output_tokens
    ):
        raise ValueError("provider total token usage is inconsistent")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def usage_snapshot(client: Any | None) -> dict[str, int | float]:
    usage = getattr(client, "usage", None)
    if isinstance(usage, CompletionUsage):
        return usage.snapshot()
    return {
        "calls": 0,
        "failed_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "token_usage_calls": 0,
        "latency_ms": 0.0,
    }


def usage_delta(
    before: dict[str, int | float],
    after: dict[str, int | float],
) -> dict[str, int | float]:
    return {key: after.get(key, 0) - before.get(key, 0) for key in after}
