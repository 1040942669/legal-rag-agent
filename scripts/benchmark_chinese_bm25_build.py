"""Bounded index-construction probe, not API or production latency acceptance."""
from __future__ import annotations

import argparse
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_chinese_bm25 import build_arm, digest, disable_network, write_new

ARMS = (
    {"id": "legacy-v1", "backend": "historical", "lexical_profile": "legacy-v1", "k1": 1.5,
     "b": .75, "law_boost": 40., "article_boost": 80., "deprecated_penalty": .5},
    {"id": "bm25s-sklearn-char", "backend": "bm25s", "mode": "char", "k1": 1.5,
     "b": .75, "hmm": None, "method": "lucene"},
)
SOURCES = ("scripts/benchmark_chinese_bm25_build.py", "scripts/benchmark_chinese_bm25.py",
           "legal_rag/retrieval.py", "legal_rag/chinese_bm25.py", "legal_rag/models.py",
           "legal_rag/chunking.py", "legal_rag/retrieval_contracts.py", "pyproject.toml", "uv.lock")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    for name, value in {"LEGAL_RAG_DISABLE_DOTENV": "1", "PYTHON_DOTENV_DISABLED": "1",
                        "ALLOW_LIVE_MODEL_CALLS": "false"}.items():
        os.environ[name] = value
    disable_network()
    from legal_rag.chunking import load_chunks
    from scripts.offline_retrieval_ab import create_run_directory
    index = ROOT / "artifacts/indexes/article/chunks.jsonl"
    directory = create_run_directory(ROOT / "artifacts/experiments", args.run_id)
    before = {path: digest(ROOT / path) for path in SOURCES}
    manifest = {"protocol_id": "bm25-construction-fixed-v1", "run_id": args.run_id,
                "source_sha256": before, "index_sha256": digest(index), "arms": ARMS,
                "builds_per_arm": 4, "fixed_probe_query": "劳动合同",
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "limits": ["One process, fixed arm order, no corpus loading or PostgreSQL time in build",
                           "First build includes engine/analyzer imports; builds 2-4 use loaded libraries but recreate the full index",
                           "No shared index cache; small timing sample, not end-to-end API capacity", "No cases or gold loaded"]}
    write_new(directory / "manifest.json", manifest)
    chunks = load_chunks(index)
    records = []
    for arm in ARMS:
        for attempt in range(4):
            gc.collect()
            started = time.perf_counter()
            retriever = build_arm(arm, chunks)
            built = time.perf_counter()
            results = retriever.retrieve("劳动合同", top_k=5)
            finished = time.perf_counter()
            records.append({"arm": arm["id"], "build_number": attempt + 1,
                            "build_ms": (built - started) * 1000,
                            "query_ms": (finished - built) * 1000, "result_count": len(results)})
            del results, retriever
    stable = before == {path: digest(ROOT / path) for path in SOURCES} and digest(index) == manifest["index_sha256"]
    result = {"status": "completed" if stable else "failed", "records": records,
              "source_and_index_stable": stable, "chunk_count": len(chunks), "actual_provider_calls": 0,
              "manifest_sha256": digest(directory / "manifest.json"), "limits": manifest["limits"]}
    write_new(directory / "summary.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if stable else 1


if __name__ == "__main__":
    raise SystemExit(main())
