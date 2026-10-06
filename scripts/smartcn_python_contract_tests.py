"""Explicit real-JVM synthetic tests; not collected by the offline CI suite.

Run: uv run python -m pytest -q scripts/smartcn_python_contract_tests.py
Requires the fixed three verified Lucene 9.12.3 JARs and Java/Javac 17. It never
loads an evaluation dataset, a law corpus, environment file or model provider.
Retained compiler/protocol files are under .tmp, not a shared index cache.
"""

from pathlib import Path
import uuid

import psutil
import pytest

from legal_rag.models import Chunk
from scripts.smartcn_bridge import SmartCnRetriever, prepare_bridge, verify_prepared_bridge


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def runtime():
    dependency_dir = ROOT / ".tmp/smartcn-deps/9.12.3"
    work = ROOT / ".tmp" / ("smartcn-python-contract-" + uuid.uuid4().hex)
    prepared = work / "prepared"
    identity = prepare_bridge(dependency_dir, prepared)
    yield dependency_dir, prepared, work, identity
    assert verify_prepared_bridge(dependency_dir, prepared) == identity


def fictional_chunk(key, text, laws=(), articles=()):
    return Chunk(key, text, list(laws), list(articles), ["fictional.txt"], [1], "article")


def test_real_python_bridges_share_statistics_and_keep_independent_scoring(runtime):
    dep, prepared, work, identity = runtime
    chunks = [fictional_chunk("b", "生态事项，生态事项。", ("合成甲法",), ("第十条",)),
              fictional_chunk("a", "生态事项，生态事项。", ("合成甲法",), ("第十条",)),
              fictional_chunk("blank", "")]
    stats = {}
    for backend in ("lucene", "bm25s"):
        with SmartCnRetriever(chunks, backend=backend, dependency_dir=dep, work_dir=work / backend,
                              prepared_build_dir=prepared) as retriever:
            single = retriever.retrieve("生态", 2)
            repeated = retriever.retrieve("生态，生态", 2)
            assert [(row.chunk.chunk_id, row.score) for row in single] == [
                (row.chunk.chunk_id, row.score) for row in repeated]
            assert [row.chunk.chunk_id for row in single] == ["a", "b"]
            assert single[0].chunk is chunks[1] and single[1].chunk is chunks[0]
            assert retriever.retrieve("qwertynotpresent_20261006") == []
            assert retriever.retrieve("") == []
            assert retriever.timings["compile_ms"] == 0
            assert retriever.timings["prepared_compile_ms"] == identity["compile_ms"]
            assert retriever.timings["java_startup_and_build_ms"] > 0
            assert retriever.index_stats["zero_token_document_count"] == 1
            stats[backend] = retriever.index_stats
            pids = retriever.subprocess_pids
        assert not any(psutil.pid_exists(pid) for pid in pids)
        assert (retriever.run_dir / "docs.tsv").exists()
        assert (retriever.run_dir / "java.stderr.log").exists()
    assert stats["lucene"] == stats["bm25s"]
    assert verify_prepared_bridge(dep, prepared) == identity


@pytest.mark.parametrize("backend", ["lucene", "bm25s"])
@pytest.mark.parametrize("chunks", [[], [fictional_chunk("blank", "")]])
def test_real_python_empty_corpus_and_zero_token_document_are_safe(runtime, backend, chunks):
    dep, prepared, work, _ = runtime
    with SmartCnRetriever(chunks, backend=backend, dependency_dir=dep, work_dir=work / "empty",
                          prepared_build_dir=prepared) as retriever:
        assert retriever.index_stats == {
            "document_count": len(chunks), "total_tokens": 0,
            "nonempty_document_count": 0, "zero_token_document_count": len(chunks),
            "average_nonempty_field_length": 0,
        }
        assert retriever.retrieve("合成", 5) == []
        pids = retriever.subprocess_pids
    assert not any(psutil.pid_exists(pid) for pid in pids)
