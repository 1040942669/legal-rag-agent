from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .env import load_dotenv

SILICONFLOW_MODEL_PREFIX = "siliconflow:"


@dataclass
class OllamaClient:
    model: str
    base_url: str = "http://localhost:11434"
    request_timeout: int = 180
    _llamaindex_llm: Any = field(default=None, repr=False, compare=False)

    def complete(self, prompt: str) -> str:
        try:
            return self._complete_with_llamaindex(prompt)
        except ModuleNotFoundError:
            return self._complete_with_http(prompt)

    def _complete_with_llamaindex(self, prompt: str) -> str:
        if self._llamaindex_llm is None:
            from llama_index.llms.ollama import Ollama

            self._llamaindex_llm = Ollama(
                model=self.model,
                base_url=self.base_url,
                request_timeout=self.request_timeout,
            )
        response = self._llamaindex_llm.complete(prompt)
        return str(getattr(response, "text", response)).strip()

    def _complete_with_http(self, prompt: str) -> str:
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
        return str(body.get("response", "")).strip()


@dataclass
class SiliconFlowClient:
    """OpenAI-compatible chat completion client for SiliconFlow API models."""

    model: str
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key_env: str = "SILICONFLOW_API_KEY"
    request_timeout: int = 120
    temperature: float = 0.1
    _client: Any = field(default=None, repr=False, compare=False)

    def complete(self, prompt: str) -> str:
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
            self._client = OpenAI(api_key=api_key, base_url=self.base_url, timeout=self.request_timeout)
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=self.temperature,
            )
        except Exception as exc:  # Network/API errors surface as RuntimeError like OllamaClient.
            raise RuntimeError(f"SiliconFlow request failed: {exc}") from exc
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
