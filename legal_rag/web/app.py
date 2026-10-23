"""FastAPI app for the local web demo: one page, one options call, one SSE call."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .errors import describe_error
from .pipeline import AskOptions, PipelineRun, json_default
from .runtime import MAX_WEB_TOP_K, WebRuntime

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_PATH = STATIC_DIR / "index.html"
MAX_QUESTION_CHARS = 2000
LOGGER = logging.getLogger(__name__)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(max_length=MAX_QUESTION_CHARS)
    retriever: str | None = None
    rerank: bool = False
    generate: bool = True
    top_k: int | None = Field(default=None, ge=1, le=MAX_WEB_TOP_K)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def format_sse(event: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, default=json_default)
    return f"event: {event}\ndata: {data}\n\n"


def create_app(runtime: WebRuntime, *, max_workers: int = 4) -> FastAPI:
    # Pipeline runs are synchronous and spend most of their time waiting on
    # providers, so they get a small dedicated pool instead of the event loop.
    executor = ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="legal-rag-web"
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(
        title="Legal RAG web demo",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(INDEX_PATH.read_text(encoding="utf-8"))

    @app.get("/api/options")
    def options() -> dict[str, Any]:
        return runtime.options()

    @app.post("/api/ask", response_model=None)
    async def ask(request: AskRequest) -> StreamingResponse | JSONResponse:
        question = request.question.strip()
        if not question:
            return _error(400, "empty_question", "请输入问题。")
        retriever = request.retriever or runtime.default_retriever
        slot = runtime.retrievers.get(retriever)
        if slot is None:
            return _error(400, "unknown_retriever", f"未知检索器：{retriever}")
        if not slot.available:
            return _error(
                409,
                "retriever_unavailable",
                f"{slot.label} 不可用：{slot.unavailable_reason}",
            )
        if request.rerank and runtime.reranker is None:
            return _error(
                409,
                "reranker_unavailable",
                f"重排不可用：{runtime.reranker_error or '配置中没有重排模型'}",
            )
        ask_options = AskOptions(
            question=question,
            retriever=retriever,
            rerank=request.rerank,
            generate=request.generate,
            top_k=request.top_k or runtime.default_top_k,
        )

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

        def put(item: tuple[str, dict[str, Any]] | None) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                # The loop closed (server shutdown); the run still finishes and
                # appends its record, there is just nobody left to stream to.
                pass

        def emit(event: str, data: dict[str, Any]) -> None:
            put((event, data))

        def work() -> None:
            try:
                PipelineRun(runtime, ask_options, emit).run()
            except Exception as exc:  # noqa: BLE001 - run() reports stage errors.
                LOGGER.exception("web pipeline run crashed")
                put(("error", {**describe_error(exc), "fatal": True}))
            finally:
                put(None)

        loop.run_in_executor(executor, work)

        async def stream() -> AsyncIterator[str]:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield format_sse(*item)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
