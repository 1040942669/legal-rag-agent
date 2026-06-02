from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class OllamaClient:
    model: str
    base_url: str = "http://localhost:11434"
    request_timeout: int = 180

    def complete(self, prompt: str) -> str:
        try:
            return self._complete_with_llamaindex(prompt)
        except ModuleNotFoundError:
            return self._complete_with_http(prompt)

    def _complete_with_llamaindex(self, prompt: str) -> str:
        from llama_index.llms.ollama import Ollama

        llm = Ollama(
            model=self.model,
            base_url=self.base_url,
            request_timeout=self.request_timeout,
        )
        response = llm.complete(prompt)
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

