"""Pass-through proxies that time the calls one staged chat turn makes.

``LegalChatAssistant.retrieve_turn`` runs retrieval, reranking, the evidence
check and any follow-up retrieval inside one call.  The web demo wraps the
retriever, the reranker and the completion client so it can report each of
those steps while they happen, without changing the assistant itself.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from legal_rag.models import SearchResult
from legal_rag.retrieval import Retriever
from legal_rag.retrieval import retrieval_boundary as resolve_retrieval_boundary


@dataclass
class TimedCall:
    index: int
    started: float
    finished: float | None = None
    error: BaseException | None = None

    @property
    def elapsed_ms(self) -> float:
        end = self.finished if self.finished is not None else time.perf_counter()
        return (end - self.started) * 1000

    def ran_within(self, other: TimedCall) -> bool:
        """True when this call started while ``other`` was running."""

        end = other.finished if other.finished is not None else float("inf")
        return other.started <= self.started <= end


@dataclass
class RetrieveCall(TimedCall):
    query: str = ""
    top_k: int = 0
    results: list[SearchResult] = field(default_factory=list)


@dataclass
class RerankCall(TimedCall):
    documents: int = 0


@dataclass
class CompletionCall(TimedCall):
    prompt_chars: int = 0


Listener = Callable[[str, Any], None]


class RetrievalTap:
    """Retriever proxy that records each ``retrieve`` call.

    The listener receives ``("start", call)`` before and ``("end", call)``
    after a successful call.  The retrieval boundary is forwarded unchanged,
    so boundary checks behave exactly as they would on the wrapped retriever.
    """

    def __init__(self, retriever: Retriever, listener: Listener | None = None) -> None:
        self._retriever = retriever
        self._listener = listener
        self.name = getattr(retriever, "name", "retriever")
        self.calls: list[RetrieveCall] = []

    @property
    def retrieval_boundary(self):
        return resolve_retrieval_boundary(self._retriever)

    @property
    def boundary_fingerprint(self) -> str | None:
        boundary = resolve_retrieval_boundary(self._retriever)
        return boundary.fingerprint if boundary is not None else None

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        call = RetrieveCall(
            index=len(self.calls),
            started=time.perf_counter(),
            query=query,
            top_k=top_k,
        )
        self.calls.append(call)
        _notify(self._listener, "start", call)
        try:
            results = self._retriever.retrieve(query, top_k=top_k)
        except BaseException as exc:
            call.error = exc
            raise
        finally:
            call.finished = time.perf_counter()
        call.results = list(results)
        _notify(self._listener, "end", call)
        return results


class RerankTap:
    """Reranker proxy that records each ``score`` call."""

    def __init__(self, reranker: Any, listener: Listener | None = None) -> None:
        self._reranker = reranker
        self._listener = listener
        self.name = reranker.name
        self.calls: list[RerankCall] = []

    def score(self, query: str, results: list[SearchResult]) -> list[float]:
        call = RerankCall(
            index=len(self.calls),
            started=time.perf_counter(),
            documents=len(results),
        )
        self.calls.append(call)
        _notify(self._listener, "start", call)
        try:
            return self._reranker.score(query, results)
        except BaseException as exc:
            call.error = exc
            raise
        finally:
            call.finished = time.perf_counter()


class CompletionTap:
    """Completion client proxy that keeps the provider error a turn swallows.

    ``generate_turn`` turns a provider failure into a generic degraded answer.
    The tap records the typed error first so the page can say whether a key is
    missing, the network failed or the provider rejected the call.  Every
    other attribute, including ``usage`` and the error-propagation flags, is
    read from the wrapped client, so the assistant behaves as without the tap.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.calls: list[CompletionCall] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._client, name)

    @property
    def last_error(self) -> BaseException | None:
        return self.calls[-1].error if self.calls else None

    def complete(self, prompt: str) -> Any:
        call = CompletionCall(
            index=len(self.calls),
            started=time.perf_counter(),
            prompt_chars=len(prompt),
        )
        self.calls.append(call)
        try:
            return self._client.complete(prompt)
        except BaseException as exc:
            call.error = exc
            raise
        finally:
            call.finished = time.perf_counter()


def _notify(listener: Listener | None, event: str, call: Any) -> None:
    if listener is not None:
        listener(event, call)
