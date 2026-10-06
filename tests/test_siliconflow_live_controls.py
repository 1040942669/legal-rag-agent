"""Provider-free tests for opt-in live controls; never use a real API key."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from legal_rag.llm import SiliconFlowClient
from legal_rag.provider_errors import ProviderCallError


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    monkeypatch.setenv("LEGAL_RAG_DISABLE_DOTENV", "true")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "fake-offline-key")


def response(**changes: Any) -> SimpleNamespace:
    values = {
        "model": "Qwen/Qwen3.5-35B-A3B",
        "choices": [SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content=' {"ok": true} ', reasoning_content=""),
        )],
        "usage": SimpleNamespace(
            prompt_tokens=12, completion_tokens=5, total_tokens=17,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def fake_client(payload: Any) -> tuple[Any, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> Any:
        requests.append(kwargs)
        return payload

    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create),
    )), requests


def test_explicit_nonthinking_json_output_limit_and_metadata() -> None:
    fake, requests = fake_client(response())
    client = SiliconFlowClient(
        model="Qwen/Qwen3.5-35B-A3B", _client=fake,
        max_tokens=1536, enable_thinking=False, response_format="json_object",
        follow_redirects=False,
    )
    assert client.complete("public test") == '{"ok": true}'
    assert requests == [{
        "model": "Qwen/Qwen3.5-35B-A3B",
        "messages": [{"role": "user", "content": "public test"}],
        "temperature": 0.1, "max_tokens": 1536,
        "extra_body": {"enable_thinking": False},
        "response_format": {"type": "json_object"},
    }]
    assert client.last_response_metadata == {
        "finish_reason": "stop", "reasoning_content_reported": True,
        "reasoning_content_nonempty": False, "reasoning_tokens": 0,
        "returned_model_matches": True,
        "prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17,
    }


def test_default_request_remains_unchanged() -> None:
    fake, requests = fake_client(response())
    client = SiliconFlowClient("legacy", _client=fake)
    client.complete("prompt")
    assert requests == [{"model": "legacy", "messages": [
        {"role": "user", "content": "prompt"},
    ], "temperature": 0.1}]


@pytest.mark.parametrize("options", [
    {"max_tokens": True}, {"max_tokens": 0}, {"max_tokens": -1},
    {"max_tokens": 1.5}, {"max_tokens": 32769},
    {"enable_thinking": "false"}, {"enable_thinking": 0},
    {"response_format": "json_schema"}, {"response_format": {}},
    {"follow_redirects": "false"}, {"follow_redirects": 0},
])
def test_invalid_controls_fail_before_initialization(options: dict[str, Any]) -> None:
    with pytest.raises(ProviderCallError) as captured:
        SiliconFlowClient("fake", **options)
    assert captured.value.error_code == "configuration"


def test_missing_metadata_is_unknown_not_false_evidence() -> None:
    fake, _ = fake_client(SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))],
        usage=None,
    ))
    client = SiliconFlowClient("fake", _client=fake)
    client.complete("prompt")
    assert client.last_response_metadata == {
        "finish_reason": None, "reasoning_content_reported": False,
        "reasoning_content_nonempty": False, "reasoning_tokens": None,
        "returned_model_matches": False,
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
    }


def test_nonempty_reasoning_is_only_a_flag_and_not_saved() -> None:
    payload = response(choices=[SimpleNamespace(
        finish_reason="length",
        message=SimpleNamespace(content='{"ok": true}', reasoning_content="private reasoning"),
    )])
    fake, _ = fake_client(payload)
    client = SiliconFlowClient("Qwen/Qwen3.5-35B-A3B", _client=fake)
    client.complete("prompt")
    assert client.last_response_metadata["reasoning_content_nonempty"] is True
    assert client.last_response_metadata["finish_reason"] == "length"
    assert "private reasoning" not in str(client.last_response_metadata)


@pytest.mark.parametrize("invalid", [True, -1, "0", 1.2])
def test_invalid_reasoning_usage_rejects_response(invalid: Any) -> None:
    payload = response(usage=SimpleNamespace(
        prompt_tokens=12, completion_tokens=5, total_tokens=17,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=invalid),
    ))
    fake, _ = fake_client(payload)
    client = SiliconFlowClient("fake", _client=fake)
    with pytest.raises(ProviderCallError) as captured:
        client.complete("prompt")
    assert captured.value.error_code == "invalid_response"
    assert client.usage.calls == 1


def test_metadata_reset_before_a_failed_attempt() -> None:
    fake, _ = fake_client(response())
    client = SiliconFlowClient("fake", _client=fake)
    client.complete("prompt")

    def fail(**kwargs: Any) -> Any:
        raise TimeoutError("secret prompt and secret response")

    fake.chat.completions.create = fail
    with pytest.raises(ProviderCallError):
        client.complete("prompt")
    assert client.last_response_metadata == {}


@pytest.mark.parametrize("status", [301, 302, 307, 308, 429, 500])
def test_redirect_and_transient_status_never_send_a_second_http_request(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    import openai

    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://unapproved.invalid/steal"})

    def build_http(**kwargs: Any) -> httpx.Client:
        assert kwargs == {"follow_redirects": False}
        return httpx.Client(transport=httpx.MockTransport(transport), **kwargs)

    monkeypatch.setattr(openai, "DefaultHttpxClient", build_http)
    client = SiliconFlowClient("fake", follow_redirects=False, request_timeout=1)
    with pytest.raises(ProviderCallError):
        client.complete("public test")
    assert len(requests) == 1
    assert requests[0].url.host == "api.siliconflow.cn"
    client._client.close()


def test_null_reasoning_is_not_observed_empty_content() -> None:
    fake, _ = fake_client(response(
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content='{"ok": true}', reasoning_content=None),
        )],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=5, total_tokens=17),
    ))
    client = SiliconFlowClient("fake", _client=fake)
    client.complete("prompt")
    assert client.last_response_metadata["reasoning_content_reported"] is False
    assert client.last_response_metadata["reasoning_tokens"] is None


def test_opt_in_constructor_failure_sanitizes_key_and_closes_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai
    import traceback

    transport = SimpleNamespace(closed=False)

    def close() -> None:
        transport.closed = True

    transport.close = close

    def fail_constructor(**kwargs: Any) -> None:
        raise RuntimeError("private response " + kwargs["api_key"])

    monkeypatch.setattr(openai, "DefaultHttpxClient", lambda **kwargs: transport)
    monkeypatch.setattr(openai, "OpenAI", fail_constructor)
    with pytest.raises(ProviderCallError) as captured:
        SiliconFlowClient("fake", follow_redirects=False)._get_client()
    rendered = "".join(traceback.TracebackException.from_exception(
        captured.value, capture_locals=True,
    ).format())
    assert "fake-offline-key" not in rendered
    assert "private response" not in rendered
    assert transport.closed is True
