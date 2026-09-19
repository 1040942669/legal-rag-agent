from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .env import load_dotenv

SILICONFLOW_MODEL_PREFIX = "siliconflow:"


@dataclass
class CompletionUsage:
    calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    token_usage_calls: int = 0
    latency_ms: float = 0.0

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
        total = max(0, int(total_tokens if total_tokens is not None else prompt + completion))
        self.input_tokens += prompt
        self.output_tokens += completion
        self.total_tokens += total
        self.token_usage_calls += 1

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
    model: str
    base_url: str = "http://localhost:11434"
    request_timeout: int = 180
    _llamaindex_llm: Any = field(default=None, repr=False, compare=False)
    usage: CompletionUsage = field(default_factory=CompletionUsage, init=False, repr=False, compare=False)

    def complete(self, prompt: str) -> str:
        self.usage.calls += 1
        started = time.perf_counter()
        try:
            try:
                text, token_counts = self._complete_with_llamaindex(prompt)
            except ModuleNotFoundError:
                text, token_counts = self._complete_with_http(prompt)
            self.usage.record_tokens(**token_counts)
            return text
        except Exception:
            self.usage.failed_calls += 1
            raise
        finally:
            self.usage.latency_ms += (time.perf_counter() - started) * 1000

    def _complete_with_llamaindex(self, prompt: str) -> tuple[str, dict[str, int | None]]:
        if self._llamaindex_llm is None:
            from llama_index.llms.ollama import Ollama

            self._llamaindex_llm = Ollama(
                model=self.model,
                base_url=self.base_url,
                request_timeout=self.request_timeout,
            )
        response = self._llamaindex_llm.complete(prompt)
        raw = getattr(response, "raw", None) or getattr(response, "additional_kwargs", None)
        return str(getattr(response, "text", response)).strip(), extract_token_counts(raw)

    def _complete_with_http(self, prompt: str) -> tuple[str, dict[str, int | None]]:
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
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama request failed: {exc}") from exc
        return str(body.get("response", "")).strip(), extract_token_counts(body)


@dataclass
class SiliconFlowClient:
    """OpenAI-compatible chat completion client for SiliconFlow API models."""

    model: str
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key_env: str = "SILICONFLOW_API_KEY"
    request_timeout: int = 120
    temperature: float = 0.1
    _client: Any = field(default=None, repr=False, compare=False)
    usage: CompletionUsage = field(default_factory=CompletionUsage, init=False, repr=False, compare=False)

    def complete(self, prompt: str) -> str:
        self.usage.calls += 1
        started = time.perf_counter()
        try:
            if self._client is None:
                try:
                    from openai import OpenAI
                except ModuleNotFoundError as exc:
                    raise RuntimeError("SiliconFlow client requires openai. Run `uv sync` first.") from exc
                load_dotenv()
                api_key = os.environ.get(self.api_key_env)
                if not api_key:
                    raise RuntimeError(
                        f"Missing SiliconFlow API key. Set `{self.api_key_env}` in your environment or .env."
                    )
                self._client = OpenAI(
                    api_key=api_key,
                    base_url=self.base_url,
                    timeout=self.request_timeout,
                )
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=self.temperature,
            )
        except RuntimeError:
            self.usage.failed_calls += 1
            raise
        except Exception as exc:  # Network/API errors surface as RuntimeError like OllamaClient.
            self.usage.failed_calls += 1
            raise RuntimeError(f"SiliconFlow request failed: {exc}") from exc
        finally:
            self.usage.latency_ms += (time.perf_counter() - started) * 1000
        self.usage.record_tokens(**extract_token_counts(getattr(response, "usage", None)))
        return str(response.choices[0].message.content or "").strip()


def build_completion_client(
    model: str,
    *,
    ollama_base_url: str = "http://localhost:11434",
    request_timeout: int = 180,
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
    return OllamaClient(model=model, base_url=ollama_base_url, request_timeout=request_timeout)


def extract_token_counts(payload: Any) -> dict[str, int | None]:
    if payload is None:
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump()

    def read(*keys: str) -> int | None:
        for key in keys:
            if isinstance(payload, dict) and payload.get(key) is not None:
                return int(payload[key])
            value = getattr(payload, key, None)
            if value is not None:
                return int(value)
        return None

    input_tokens = read("prompt_tokens", "prompt_eval_count", "input_tokens", "prompt_token_count")
    output_tokens = read(
        "completion_tokens",
        "eval_count",
        "output_tokens",
        "candidates_token_count",
    )
    total_tokens = read("total_tokens", "total_token_count")
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
