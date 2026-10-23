from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import uvicorn

import legal_rag.web.runtime as runtime_module
from legal_rag.web.command import build_parser, main
from legal_rag.web.runtime import ModelInfo, RetrieverSlot, WebRuntime


def test_parser_defaults_to_loopback_and_demo_port() -> None:
    args = build_parser().parse_args([])

    assert (args.host, args.port, args.config) == (
        "127.0.0.1",
        8765,
        "configs/default.yaml",
    )


def test_main_enables_live_calls_in_process_and_serves_the_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ALLOW_LIVE_MODEL_CALLS", raising=False)
    monkeypatch.setenv("LEGAL_RAG_DISABLE_DOTENV", "1")
    seen: dict[str, Any] = {}

    def fake_build_runtime(config_path: str) -> WebRuntime:
        seen["config"] = config_path
        seen["live_calls"] = os.environ.get("ALLOW_LIVE_MODEL_CALLS")
        return WebRuntime(
            retrievers={"bm25": RetrieverSlot("bm25", "BM25 关键词", object())},
            default_retriever="bm25",
            default_top_k=5,
            generator=ModelInfo("fixture", "fixture", "ollama"),
            completion_client_factory=object,
            runs_path=tmp_path / "runs.jsonl",
        )

    def fake_run(app: Any, **kwargs: Any) -> None:
        seen["app"] = app
        seen["run"] = kwargs

    monkeypatch.setattr(runtime_module, "build_runtime", fake_build_runtime)
    monkeypatch.setattr(uvicorn, "run", fake_run)

    assert main(["--port", "9100"]) == 0
    assert seen["config"] == "configs/default.yaml"
    assert seen["live_calls"] == "true"
    assert seen["run"]["host"] == "127.0.0.1"
    assert seen["run"]["port"] == 9100
    assert seen["app"].title == "Legal RAG web demo"


def test_main_rejects_an_invalid_port() -> None:
    with pytest.raises(SystemExit):
        main(["--port", "0"])
