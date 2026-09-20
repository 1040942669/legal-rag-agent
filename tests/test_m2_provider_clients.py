from __future__ import annotations

import json
import socket
import traceback
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any

import pytest

import legal_rag.retrieval as retrieval_module
from legal_rag.embeddings import EmbeddingModelConfig, SiliconFlowEmbeddingEncoder
from legal_rag.llm import OllamaClient, SiliconFlowClient
from legal_rag.provider_errors import (
    MAX_PROVIDER_TIMEOUT_SECONDS,
    ProviderCallError,
    classify_provider_error,
    provider_call_error,
    should_propagate_controlled_error,
    should_propagate_provider_error,
    validate_timeout_seconds,
)
from legal_rag.retrieval import CachedDenseRetriever


class _StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__("secret provider body must never escape")
        self.status_code = status_code


class _CompletionEndpoint:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _EmbeddingEndpoint(_CompletionEndpoint):
    pass


def _completion_client(outcome: Any) -> tuple[Any, _CompletionEndpoint]:
    endpoint = _CompletionEndpoint(outcome)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=endpoint),
    )
    return client, endpoint


def _embedding_client(outcome: Any) -> tuple[Any, _EmbeddingEndpoint]:
    endpoint = _EmbeddingEndpoint(outcome)
    client = SimpleNamespace(embeddings=endpoint)
    return client, endpoint


def _completion_response(content: str = " answer ") -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    )


def _embedding_response(*vectors: list[float]) -> Any:
    return SimpleNamespace(
        data=[
            SimpleNamespace(index=index, embedding=vector)
            for index, vector in enumerate(vectors)
        ]
    )


@pytest.fixture(autouse=True)
def _offline_fake_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Calls are allowed through the production guard, but every provider object in
    # this file is a local fake. Disabling dotenv prevents ambient credentials from
    # changing configuration-error tests.
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    monkeypatch.setenv("LEGAL_RAG_DISABLE_DOTENV", "1")


@pytest.mark.parametrize(
    ("status_code", "expected_code", "retryable"),
    [
        (400, "invalid_request", False),
        (401, "auth", False),
        (403, "auth", False),
        (404, "invalid_request", False),
        (408, "timeout", True),
        (413, "invalid_request", False),
        (422, "invalid_request", False),
        (429, "rate_limited", True),
        (500, "unavailable", True),
        (501, "unknown", False),
        (502, "unavailable", True),
        (503, "unavailable", True),
        (504, "unavailable", True),
        (505, "unknown", False),
        (507, "unavailable", True),
        (520, "unavailable", True),
        (522, "unavailable", True),
        (524, "unavailable", True),
        (599, "unavailable", True),
        (418, "unknown", False),
    ],
)
def test_provider_status_classification_is_stable(
    status_code: int,
    expected_code: str,
    retryable: bool,
) -> None:
    classification = classify_provider_error(_StatusError(status_code))

    assert classification.error_code == expected_code
    assert classification.retryable is retryable
    assert classification.status_code == status_code
    assert classification.classification_source == "status"


def test_status_classification_precedes_misleading_exception_type() -> None:
    class TimeoutLookingError(TimeoutError):
        status_code = 429

    classification = classify_provider_error(
        TimeoutLookingError("secret rate-limit response")
    )

    assert classification.error_code == "rate_limited"
    assert classification.retryable is True
    assert classification.status_code == 429


def test_typed_and_chained_transport_errors_are_classified() -> None:
    outer = RuntimeError("outer secret")
    outer.__cause__ = TimeoutError("inner secret")

    assert classify_provider_error(outer).error_code == "timeout"
    assert (
        classify_provider_error(
            urllib.error.URLError(TimeoutError("secret"))
        ).error_code
        == "timeout"
    )
    assert classify_provider_error(ConnectionError("secret")).error_code == "network"
    assert classify_provider_error(socket.gaierror("secret dns")).error_code == (
        "network"
    )


def test_provider_classification_follows_visible_chain_and_nearest_candidate() -> None:
    root_timeout = TimeoutError("visible timeout")
    root_timeout.__cause__ = _StatusError(401)
    assert classify_provider_error(root_timeout).error_code == "timeout"

    try:
        raise _StatusError(401)
    except _StatusError:
        try:
            raise RuntimeError("outer") from TimeoutError("explicit timeout")
        except RuntimeError as error:
            assert error.__suppress_context__ is True
            assert classify_provider_error(error).error_code == "timeout"

    try:
        raise TimeoutError("suppressed timeout")
    except TimeoutError:
        try:
            raise ValueError("deterministic outer") from None
        except ValueError as error:
            classification = classify_provider_error(error)
            assert classification.error_code == "unknown"
            assert classification.retryable is False


def test_real_openai_typed_errors_are_classified_without_network() -> None:
    import httpx
    import openai

    request = httpx.Request("POST", "https://secret.invalid/v1/chat/completions")
    timeout = openai.APITimeoutError(request)
    response = httpx.Response(429, request=request)
    rate_limited = openai.RateLimitError(
        "secret provider response",
        response=response,
        body={"secret": "must-not-escape"},
    )

    assert classify_provider_error(timeout).error_code == "timeout"
    assert classify_provider_error(rate_limited).error_code == "rate_limited"

    fake, _ = _completion_client(rate_limited)
    client = SiliconFlowClient(model="fake", _client=fake)
    with pytest.raises(ProviderCallError) as captured:
        client.complete("prompt")
    rendered = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert "secret provider response" not in rendered
    assert "must-not-escape" not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_explicit_provider_error_rejects_status_code_disagreement() -> None:
    with pytest.raises(ValueError, match="disagrees"):
        ProviderCallError(
            "timeout",
            provider="siliconflow",
            operation="completion",
            status_code=401,
        )


def test_provider_error_is_safe_and_existing_typed_error_is_not_flattened() -> None:
    raw = RuntimeError("provider-body-super-secret")
    wrapped = provider_call_error(
        raw,
        provider="siliconflow",
        operation="completion",
    )

    assert wrapped.error_code == "unknown"
    assert wrapped.retryable is False
    assert "super-secret" not in str(wrapped)
    assert "super-secret" not in repr(wrapped)
    assert "super-secret" not in json.dumps(wrapped.to_safe_dict())
    assert (
        provider_call_error(
            wrapped,
            provider="ollama",
            operation="completion",
        )
        is wrapped
    )


def test_provider_boundary_suppresses_raw_exception_traceback() -> None:
    fake, _ = _completion_client(
        TimeoutError("SECRET_PROVIDER_BODY must not appear in a traceback")
    )
    client = SiliconFlowClient(model="fake", _client=fake)

    with pytest.raises(ProviderCallError) as captured:
        client.complete("prompt")

    rendered = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert "SECRET_PROVIDER_BODY" not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True


def test_provider_boundary_redacts_prompt_and_response_from_traceback_locals() -> None:
    fake, _ = _completion_client(
        SimpleNamespace(
            choices=[],
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            provider_body="TRACEBACK_LOCAL_RESPONSE_SECRET",
        )
    )
    client = SiliconFlowClient(model="fake", _client=fake)

    with pytest.raises(ProviderCallError) as captured:
        client.complete("TRACEBACK_LOCAL_PROMPT_SECRET")

    captured_traceback = traceback.TracebackException.from_exception(
        captured.value,
        capture_locals=True,
    )
    boundary_locals = "\n".join(
        repr(frame.locals)
        for frame in captured_traceback.stack
        if "legal_rag" in frame.filename
    )
    assert "TRACEBACK_LOCAL_RESPONSE_SECRET" not in boundary_locals
    assert "TRACEBACK_LOCAL_PROMPT_SECRET" not in boundary_locals
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_provider_client_initialization_redacts_api_key_from_traceback_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    def fail_client_initialization(**kwargs: Any) -> Any:
        raise RuntimeError("constructor failed")

    monkeypatch.setenv("SILICONFLOW_API_KEY", "TRACEBACK_LOCAL_API_KEY_SECRET")
    monkeypatch.setattr(openai, "OpenAI", fail_client_initialization)
    client = SiliconFlowClient(model="fake")

    with pytest.raises(ProviderCallError) as captured:
        client.complete("safe prompt")

    rendered = "".join(
        traceback.TracebackException.from_exception(
            captured.value,
            capture_locals=True,
        ).format()
    )
    assert "TRACEBACK_LOCAL_API_KEY_SECRET" not in rendered


@pytest.mark.parametrize(
    "value",
    [False, 0, -1, float("nan"), float("inf"), "30", 3600.0001],
)
def test_timeout_validation_rejects_unsafe_values(value: Any) -> None:
    with pytest.raises(ProviderCallError) as captured:
        validate_timeout_seconds(
            value,
            provider="siliconflow",
            operation="completion",
        )

    assert captured.value.error_code == "configuration"
    assert captured.value.retryable is False


def test_timeout_validation_accepts_the_explicit_upper_bound() -> None:
    assert (
        validate_timeout_seconds(
            MAX_PROVIDER_TIMEOUT_SECONDS,
            provider="siliconflow",
            operation="completion",
        )
        == MAX_PROVIDER_TIMEOUT_SECONDS
    )


def test_provider_error_propagation_is_an_explicit_client_opt_in() -> None:
    error = ProviderCallError(
        "timeout",
        provider="siliconflow",
        operation="completion",
    )

    assert should_propagate_provider_error(
        error,
        SimpleNamespace(propagate_provider_errors=True),
    )
    assert not should_propagate_provider_error(
        error,
        SimpleNamespace(propagate_provider_errors=1),
    )
    assert not should_propagate_provider_error(
        RuntimeError("not typed"),
        SimpleNamespace(propagate_provider_errors=True),
    )
    assert should_propagate_controlled_error(
        RuntimeError("runner stop or contract failure"),
        SimpleNamespace(propagate_control_errors=True),
    )
    assert not should_propagate_controlled_error(
        RuntimeError("legacy failure"),
        SimpleNamespace(propagate_provider_errors=True),
    )


def test_siliconflow_completion_success_counts_one_physical_request() -> None:
    fake, endpoint = _completion_client(_completion_response())
    client = SiliconFlowClient(model="fake", _client=fake)

    assert client.complete("prompt") == "answer"
    assert len(endpoint.calls) == 1
    assert client.usage.calls == 1
    assert client.usage.failed_calls == 0
    assert client.usage.input_tokens == 3
    assert client.usage.output_tokens == 2
    assert client.usage.total_tokens == 5
    assert client.usage.token_usage_calls == 1


def test_siliconflow_completion_preserves_typed_errors_and_usage() -> None:
    expected = ProviderCallError(
        "timeout",
        provider="siliconflow",
        operation="completion",
    )
    fake, endpoint = _completion_client(expected)
    client = SiliconFlowClient(model="fake", _client=fake)

    with pytest.raises(ProviderCallError) as captured:
        client.complete("prompt")

    assert captured.value is expected
    assert len(endpoint.calls) == 1
    assert client.usage.calls == 1
    assert client.usage.failed_calls == 1
    assert client.usage.token_usage_calls == 0


def test_siliconflow_completion_classifies_transport_and_invalid_response() -> None:
    timeout_fake, timeout_endpoint = _completion_client(
        TimeoutError("secret transport detail")
    )
    timeout_client = SiliconFlowClient(model="fake", _client=timeout_fake)
    invalid_response = _completion_response()
    invalid_response.choices = []
    invalid_fake, invalid_endpoint = _completion_client(invalid_response)
    invalid_client = SiliconFlowClient(model="fake", _client=invalid_fake)

    with pytest.raises(ProviderCallError) as timeout_captured:
        timeout_client.complete("prompt")
    with pytest.raises(ProviderCallError) as invalid_captured:
        invalid_client.complete("prompt")

    assert timeout_captured.value.error_code == "timeout"
    assert timeout_captured.value.retryable is True
    assert invalid_captured.value.error_code == "invalid_response"
    assert invalid_captured.value.retryable is False
    assert "secret transport detail" not in str(timeout_captured.value)
    assert len(timeout_endpoint.calls) == 1
    assert len(invalid_endpoint.calls) == 1
    assert timeout_client.usage.snapshot()["failed_calls"] == 1
    assert invalid_client.usage.snapshot()["failed_calls"] == 1
    assert invalid_client.usage.input_tokens == 3
    assert invalid_client.usage.output_tokens == 2
    assert invalid_client.usage.total_tokens == 5
    assert invalid_client.usage.token_usage_calls == 1


def test_siliconflow_invalid_content_still_preserves_valid_token_usage() -> None:
    response = _completion_response()
    response.choices[0].message.content = {"not": "text"}
    fake, endpoint = _completion_client(response)
    client = SiliconFlowClient(model="fake", _client=fake)

    with pytest.raises(ProviderCallError, match="invalid_response"):
        client.complete("prompt")

    assert len(endpoint.calls) == 1
    assert client.usage.failed_calls == 1
    assert client.usage.input_tokens == 3
    assert client.usage.output_tokens == 2
    assert client.usage.total_tokens == 5
    assert client.usage.token_usage_calls == 1


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3},
        {"prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1},
        {"prompt_tokens": "1", "completion_tokens": 2, "total_tokens": 3},
        {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 4},
    ],
)
def test_siliconflow_completion_rejects_invalid_token_usage(usage: Any) -> None:
    response = _completion_response()
    response.usage = usage
    fake, endpoint = _completion_client(response)
    client = SiliconFlowClient(model="fake", _client=fake)

    with pytest.raises(ProviderCallError) as captured:
        client.complete("prompt")

    assert captured.value.error_code == "invalid_response"
    assert captured.value.retryable is False
    assert len(endpoint.calls) == 1
    assert client.usage.calls == 1
    assert client.usage.failed_calls == 1
    assert client.usage.token_usage_calls == 0


def test_completion_configuration_failure_counts_one_controlled_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    client = SiliconFlowClient(model="fake")

    with pytest.raises(ProviderCallError) as captured:
        client.complete("prompt")

    assert captured.value.error_code == "configuration"
    assert client.usage.calls == 1
    assert client.usage.failed_calls == 1


def test_ollama_lazy_initialization_failure_counts_one_controlled_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ProviderCallError(
        "configuration",
        provider="ollama",
        operation="completion",
        cause_type="InvalidClientConfig",
    )
    client = OllamaClient(model="fake")

    def fail_initialization() -> bool:
        raise expected

    monkeypatch.setattr(client, "_load_llamaindex_llm", fail_initialization)

    with pytest.raises(ProviderCallError) as captured:
        client.complete("prompt")

    assert captured.value is expected
    assert client.usage.calls == 1
    assert client.usage.failed_calls == 1


@pytest.mark.parametrize("client_type", [OllamaClient, SiliconFlowClient])
def test_live_call_gate_fails_before_usage_accounting(
    client_type: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, _ = _completion_client(_completion_response())
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "false")
    if client_type is OllamaClient:
        client = client_type(model="fake", _llamaindex_llm=fake)
    else:
        client = client_type(model="fake", _client=fake)

    with pytest.raises(RuntimeError, match="ALLOW_LIVE_MODEL_CALLS"):
        client.complete("prompt")

    assert client.usage.calls == 0
    assert client.usage.failed_calls == 0


def test_siliconflow_openai_client_disables_hidden_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    captured: dict[str, Any] = {}
    fake, _ = _completion_client(_completion_response())

    def build_fake_openai(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return fake

    monkeypatch.setenv("SILICONFLOW_API_KEY", "fake-offline-key")
    monkeypatch.setattr(openai, "OpenAI", build_fake_openai)
    client = SiliconFlowClient(model="fake", request_timeout=12.5)

    assert client._get_client() is fake
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 12.5
    assert client.usage.calls == 0


def test_ollama_http_error_uses_status_before_urlerror_network_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fail_request(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            "https://secret.invalid/path",
            429,
            "secret response body",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", fail_request)
    client = OllamaClient(model="fake", request_timeout=4)

    with pytest.raises(ProviderCallError) as captured:
        client._complete_with_http("prompt")

    assert captured.value.error_code == "rate_limited"
    assert captured.value.status_code == 429
    assert "secret.invalid" not in str(captured.value)
    assert calls == 1
    assert client.usage.calls == 1
    assert client.usage.failed_calls == 1


def test_ollama_successful_http_with_invalid_body_is_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b'{"done": true, "prompt_eval_count": 3, "eval_count": 2}'

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeResponse(),
    )
    client = OllamaClient(model="fake")

    with pytest.raises(ProviderCallError) as captured:
        client._complete_with_http("prompt")

    assert captured.value.error_code == "invalid_response"
    assert captured.value.retryable is False
    assert client.usage.calls == 1
    assert client.usage.failed_calls == 1
    assert client.usage.input_tokens == 3
    assert client.usage.output_tokens == 2
    assert client.usage.total_tokens == 5
    assert client.usage.token_usage_calls == 1


@pytest.mark.parametrize("client_type", [OllamaClient, SiliconFlowClient])
def test_completion_clients_enforce_timeout_upper_bound(client_type: Any) -> None:
    with pytest.raises(ProviderCallError, match="configuration"):
        client_type(
            model="fake",
            request_timeout=MAX_PROVIDER_TIMEOUT_SECONDS + 1,
        )


def test_embedding_success_batches_one_physical_request_per_batch() -> None:
    fake, endpoint = _embedding_client(_embedding_response([1.0, 0.0]))
    config = EmbeddingModelConfig(
        key="fake",
        provider="siliconflow",
        model_name="fake",
        role="document",
        dimensions=2,
    )
    encoder = SiliconFlowEmbeddingEncoder(config, client=fake)

    assert encoder.encode_documents(["a", "b"], batch_size=1) == [
        [1.0, 0.0],
        [1.0, 0.0],
    ]
    assert len(endpoint.calls) == 2


def test_embedding_preserves_typed_error_and_rejects_invalid_response() -> None:
    expected = ProviderCallError(
        "unavailable",
        provider="siliconflow",
        operation="embedding",
        status_code=503,
    )
    failure_fake, failure_endpoint = _embedding_client(expected)
    invalid_fake, invalid_endpoint = _embedding_client(
        _embedding_response([float("nan"), 0.0])
    )
    config = EmbeddingModelConfig(
        key="fake",
        provider="siliconflow",
        model_name="fake",
        role="query",
        dimensions=2,
    )
    failure_encoder = SiliconFlowEmbeddingEncoder(config, client=failure_fake)
    invalid_encoder = SiliconFlowEmbeddingEncoder(config, client=invalid_fake)

    with pytest.raises(ProviderCallError) as failure_captured:
        failure_encoder.encode_query("question")
    with pytest.raises(ProviderCallError) as invalid_captured:
        invalid_encoder.encode_query("question")

    assert failure_captured.value is expected
    assert invalid_captured.value.error_code == "invalid_response"
    assert invalid_captured.value.retryable is False
    assert len(failure_endpoint.calls) == 1
    assert len(invalid_endpoint.calls) == 1


def test_embedding_response_is_reordered_by_provider_index() -> None:
    response = SimpleNamespace(
        data=[
            SimpleNamespace(index=1, embedding=[0.0, 1.0]),
            SimpleNamespace(index=0, embedding=[1.0, 0.0]),
        ]
    )
    fake, endpoint = _embedding_client(response)
    config = EmbeddingModelConfig(
        key="fake",
        provider="siliconflow",
        model_name="fake",
        role="document",
        dimensions=2,
    )

    assert SiliconFlowEmbeddingEncoder(config, client=fake).encode_documents(
        ["first", "second"], batch_size=2
    ) == [[1.0, 0.0], [0.0, 1.0]]
    assert len(endpoint.calls) == 1


@pytest.mark.parametrize(
    "data",
    [
        [
            SimpleNamespace(index=0, embedding=[1.0, 0.0]),
            SimpleNamespace(index=0, embedding=[0.0, 1.0]),
        ],
        [
            SimpleNamespace(index=0, embedding=[1.0, 0.0]),
            SimpleNamespace(index=2, embedding=[0.0, 1.0]),
        ],
        [
            SimpleNamespace(index=True, embedding=[1.0, 0.0]),
            SimpleNamespace(index=1, embedding=[0.0, 1.0]),
        ],
        [
            SimpleNamespace(embedding=[1.0, 0.0]),
            SimpleNamespace(index=1, embedding=[0.0, 1.0]),
        ],
    ],
)
def test_embedding_response_rejects_unsafe_indices(data: list[Any]) -> None:
    fake, _ = _embedding_client(SimpleNamespace(data=data))
    config = EmbeddingModelConfig(
        key="fake",
        provider="siliconflow",
        model_name="fake",
        role="document",
        dimensions=2,
    )

    with pytest.raises(ProviderCallError, match="invalid_response"):
        SiliconFlowEmbeddingEncoder(config, client=fake).encode_documents(
            ["first", "second"], batch_size=2
        )


def test_embedding_dimension_is_stable_across_batches() -> None:
    responses = iter(
        [
            _embedding_response([1.0, 0.0]),
            _embedding_response([1.0, 0.0, 0.5]),
        ]
    )
    endpoint = SimpleNamespace(create=lambda **kwargs: next(responses))
    fake = SimpleNamespace(embeddings=endpoint)
    config = EmbeddingModelConfig(
        key="fake",
        provider="siliconflow",
        model_name="fake",
        role="document",
    )

    with pytest.raises(ProviderCallError, match="invalid_response"):
        SiliconFlowEmbeddingEncoder(config, client=fake).encode_documents(
            ["first", "second"], batch_size=1
        )


def test_embedding_query_dimension_is_bound_to_loaded_index_contract() -> None:
    fake, endpoint = _embedding_client(_embedding_response([1.0, 0.0, 0.5]))
    config = EmbeddingModelConfig(
        key="fake",
        provider="siliconflow",
        model_name="fake",
        role="query",
    )
    encoder = SiliconFlowEmbeddingEncoder(
        config,
        client=fake,
        expected_dimension=2,
    )

    with pytest.raises(ProviderCallError) as captured:
        encoder.encode_query("question")

    assert captured.value.error_code == "invalid_response"
    assert len(endpoint.calls) == 1


def test_cached_dense_retriever_binds_encoder_to_cache_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import numpy as np

    captured: dict[str, Any] = {}
    cache = SimpleNamespace(vectors=np.empty((0, 7), dtype="float32"))
    config = EmbeddingModelConfig(
        key="fake",
        provider="siliconflow",
        model_name="fake",
        role="query",
    )

    monkeypatch.setattr(retrieval_module, "load_embedding_cache", lambda _: cache)
    monkeypatch.setattr(
        retrieval_module,
        "validate_cache_matches_chunks",
        lambda *args, **kwargs: None,
    )

    def capture_encoder(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(retrieval_module, "build_encoder", capture_encoder)

    CachedDenseRetriever([], cache_dir="unused", model_config=config)

    assert captured["expected_dimension"] == 7


def test_cached_dense_retriever_rejects_bad_query_vector_before_matmul() -> None:
    import numpy as np

    config = EmbeddingModelConfig(
        key="fake",
        provider="siliconflow",
        model_name="fake",
        role="query",
    )
    retriever = object.__new__(CachedDenseRetriever)
    retriever.model_config = config
    retriever.cache = SimpleNamespace(vectors=np.zeros((1, 2), dtype="float32"))
    retriever.encoder = SimpleNamespace(encode_query=lambda _: [1.0, 2.0, 3.0])

    with pytest.raises(ProviderCallError) as captured:
        retriever.retrieve("dimension mismatch")

    assert captured.value.error_code == "invalid_response"


def test_embedding_boundary_redacts_query_and_response_from_traceback_locals() -> None:
    fake, _ = _embedding_client(
        SimpleNamespace(
            data=[],
            provider_body="EMBEDDING_TRACEBACK_RESPONSE_SECRET",
        )
    )
    config = EmbeddingModelConfig(
        key="fake",
        provider="siliconflow",
        model_name="fake",
        role="query",
        dimensions=2,
    )
    encoder = SiliconFlowEmbeddingEncoder(config, client=fake)

    with pytest.raises(ProviderCallError) as captured:
        encoder.encode_query("EMBEDDING_TRACEBACK_QUERY_SECRET")

    captured_traceback = traceback.TracebackException.from_exception(
        captured.value,
        capture_locals=True,
    )
    boundary_locals = "\n".join(
        repr(frame.locals)
        for frame in captured_traceback.stack
        if "legal_rag" in frame.filename
    )
    assert "EMBEDDING_TRACEBACK_RESPONSE_SECRET" not in boundary_locals
    assert "EMBEDDING_TRACEBACK_QUERY_SECRET" not in boundary_locals


def test_embedding_openai_client_disables_hidden_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    captured: dict[str, Any] = {}
    fake, _ = _embedding_client(_embedding_response([1.0, 0.0]))

    def build_fake_openai(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return fake

    monkeypatch.setenv("SILICONFLOW_API_KEY", "fake-offline-key")
    monkeypatch.setattr(openai, "OpenAI", build_fake_openai)
    config = EmbeddingModelConfig(
        key="fake",
        provider="siliconflow",
        model_name="fake",
        role="query",
        max_retries=0,
        request_timeout=7.5,
    )

    encoder = SiliconFlowEmbeddingEncoder(config)

    assert encoder.client is fake
    assert config.max_retries == 0
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 7.5


def test_embedding_config_enforces_timeout_upper_bound() -> None:
    with pytest.raises(ProviderCallError, match="configuration"):
        EmbeddingModelConfig(
            key="fake",
            provider="siliconflow",
            model_name="fake",
            role="query",
            request_timeout=MAX_PROVIDER_TIMEOUT_SECONDS + 1,
        )


@pytest.mark.parametrize("dimensions", [True, 0, -1, 1.5, "2"])
def test_embedding_config_requires_a_positive_integer_dimension(
    dimensions: Any,
) -> None:
    with pytest.raises(ProviderCallError, match="configuration"):
        EmbeddingModelConfig(
            key="fake",
            provider="siliconflow",
            model_name="fake",
            role="query",
            dimensions=dimensions,
        )


@pytest.mark.parametrize("max_retries", [True, -1, 1, 99, 1.5, "3"])
def test_embedding_config_strictly_validates_legacy_retry_field(
    max_retries: Any,
) -> None:
    with pytest.raises(ProviderCallError, match="configuration"):
        EmbeddingModelConfig(
            key="fake",
            provider="siliconflow",
            model_name="fake",
            role="query",
            max_retries=max_retries,
        )
