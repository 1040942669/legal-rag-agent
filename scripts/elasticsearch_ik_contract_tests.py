"""Explicit real-JVM synthetic contracts; never part of default offline pytest."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from legal_rag.models import Chunk
from scripts import benchmark_chinese_bm25 as common
from scripts.benchmark_elasticsearch_ik import free_port, worker_environment
from scripts.elasticsearch_ik_adapter import ElasticsearchIKRetriever, install_loopback_socket_audit
from scripts.isolated_elasticsearch import IsolatedElasticsearch, verify_home


def run(home, directory):
    os.environ.update(worker_environment())
    directory.mkdir(parents=True, exist_ok=False)
    runtime = verify_home(home)
    port, transport_port = free_port(), free_port()
    while transport_port == port:
        transport_port = free_port()
    service = IsolatedElasticsearch(home, directory / "service", port, transport_port)
    install_loopback_socket_audit("http://127.0.0.1:" + str(port))
    checks = []
    started = time.perf_counter()
    retriever = None
    try:
        service.start()
        chunks = [Chunk(f"synthetic-{index:02d}", "劳动合同 劳动报酬", ["合成法"], ["第一条"],
                        ["synthetic.txt"], [1], "synthetic") for index in reversed(range(8))]
        try:
            retriever = ElasticsearchIKRetriever(chunks, service.endpoint, "synthetic-contracts")
        except Exception as exc:
            from scripts.elasticsearch_ik_adapter import _LoopbackJSONTransport
            transport = _LoopbackJSONTransport(service.endpoint)
            common.write_new(directory / "constructor-failure.json", {
                "error_type": type(exc).__name__, "detail": str(exc),
                "actual_mapping": transport.request("GET", "/synthetic-contracts/_mapping"),
                "actual_settings": transport.request("GET", "/synthetic-contracts/_settings?flat_settings=true")})
            raise
        hits = retriever.retrieve("劳动", top_k=5)
        assert [hit.chunk.chunk_id for hit in hits] == [f"synthetic-{index:02d}" for index in range(5)]
        assert all(hit.chunk.text == "劳动合同 劳动报酬" for hit in hits)
        checks.append("server_global_tie_order_and_original_sources")
        duplicate = retriever.retrieve("劳动 劳动", top_k=5)
        assert [(hit.chunk.chunk_id, hit.score) for hit in duplicate] == [(hit.chunk.chunk_id, hit.score) for hit in hits]
        checks.append("deduplicated_query_terms")
        assert retriever.retrieve("", top_k=5) == []
        assert retriever.retrieve("qzxvnotindictionary000000", top_k=5) == []
        checks.append("empty_and_oov_are_valid_empty_results")
        assert retriever.retrieve("劳动", top_k=0) == []
        checks.append("zero_top_k")
        present = retriever.diagnose_query("劳动")
        absent = retriever.diagnose_query("qzxvnotindictionary000000")
        assert present["token_count"] > 0 and present["oov_count"] == 0
        assert absent["token_count"] > 0 and absent["oov_count"] == absent["token_count"]
        checks.append("real_text_field_exact_term_presence_diagnostics")
        retriever.close()
        retriever = None
        checks.append("frozen_instance_and_index_identity")
    finally:
        try:
            if retriever is not None:
                retriever.close()
        finally:
            service.close()
    checks.append("owned_service_cleanup")
    assert verify_home(home) == runtime
    checks.append("installed_runtime_content_stable")
    common.write_new(directory / "result.json", {"status": "passed", "checks": checks,
                     "elapsed_ms": (time.perf_counter()-started)*1000,
                     "service_identity": service.identity, "actual_provider_calls": 0})
    print({"status": "passed", "checks": len(checks), "directory": str(directory)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-home", type=Path, default=ROOT / ".tmp/ik-deps/9.1.4/elasticsearch-9.1.4")
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    run(args.engine_home.resolve(), args.directory.resolve())
