"""Experiment-only, single-loopback Elasticsearch + IK lexical adapter.

The owner prepares and stops Elasticsearch. This adapter creates only its fresh
index and never deletes an index or stops a service. It does not install a
process-global network policy until the isolated worker explicitly asks for it.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import http.client
import json
import math
from numbers import Real
import re
import socket
import sys
import time
from typing import Sequence

from legal_rag.chinese_bm25 import INDEX_TEXT_VERSION
from legal_rag.models import Chunk, SearchResult
from legal_rag.retrieval_contracts import MAX_RETRIEVAL_TOP_K


EXPECTED_VERSION = "9.1.4"
MAX_UNIQUE_QUERY_TERMS = 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ElasticsearchIKError(RuntimeError):
    """Stable protocol error; never contains server, document or query text."""


def _endpoint_port(endpoint: str) -> int:
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})/?", endpoint) if isinstance(endpoint, str) else None
    if match is None or not 1 <= int(match.group(1)) <= 65535:
        raise ValueError("endpoint must be a literal http://127.0.0.1:port")
    return int(match.group(1))


def loopback_socket_guard(endpoint: str):
    """An audit hook for one worker's exact TCP endpoint, not a sandbox."""
    port = _endpoint_port(endpoint)

    def guard(event, args):
        if event == "socket.connect":
            address = args[1] if len(args) > 1 else None
            source = args[0] if args else None
            kind = getattr(source, "type", None)
            flags = getattr(socket, "SOCK_NONBLOCK", 0) | getattr(socket, "SOCK_CLOEXEC", 0)
            tcp = (getattr(source, "family", None) == socket.AF_INET
                   and isinstance(kind, int) and not isinstance(kind, bool)
                   and kind & ~flags == socket.SOCK_STREAM)
            if not tcp or not isinstance(address, tuple) or address != ("127.0.0.1", port):
                raise ElasticsearchIKError("network_forbidden")
        elif event == "socket.getaddrinfo":
            if len(args) < 2 or args[0] != "127.0.0.1" or args[1] != port:
                raise ElasticsearchIKError("network_forbidden")
        elif event in {"socket.sendto", "socket.sendmsg", "socket.gethostbyname", "socket.gethostbyaddr"}:
            raise ElasticsearchIKError("network_forbidden")

    return guard


def install_loopback_socket_audit(endpoint: str) -> None:
    sys.addaudithook(loopback_socket_guard(endpoint))


def _reject_constant(_):
    raise ValueError("nonfinite_json")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


class _LoopbackJSONTransport:
    """Direct stdlib TCP HTTP: ignores proxy env, redirects and retry policies."""
    def __init__(self, endpoint: str):
        self.port = _endpoint_port(endpoint)

    def request(self, method: str, path: str, payload=None, *, raw_body=None) -> dict:
        if not path.startswith("/") or path.startswith("//"):
            raise ElasticsearchIKError("invalid_http_path")
        if raw_body is not None and payload is not None:
            raise ElasticsearchIKError("ambiguous_http_body")
        body = raw_body
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/x-ndjson" if raw_body is not None else "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ElasticsearchIKError("http_redirect_forbidden")
            if not 200 <= response.status < 300:
                raise ElasticsearchIKError(f"http_status_{response.status}")
            content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise ElasticsearchIKError("http_response_too_large")
            value = json.loads(content.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object)
            if not isinstance(value, dict):
                raise ElasticsearchIKError("malformed_http_json")
            return value
        except ElasticsearchIKError:
            raise
        except (OSError, http.client.HTTPException):
            raise ElasticsearchIKError("http_transport_failed") from None
        except (ValueError, UnicodeError):
            raise ElasticsearchIKError("malformed_http_json") from None
        finally:
            connection.close()


def flatten_settings(value: dict) -> dict[str, str]:
    """Normalize nested ES index settings without silently dropping fields."""
    result = {}

    def visit(item, prefix):
        if isinstance(item, dict):
            for key, child in item.items():
                visit(child, f"{prefix}.{key}" if prefix else key)
        else:
            key = prefix if prefix.startswith("index.") else f"index.{prefix}"
            result[key] = str(item).lower() if isinstance(item, bool) else str(item)

    visit(value, "")
    return result


def _number(value, name, *, maximum=None):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (name == "k1" and value == 0) or (maximum is not None and value > maximum):
        raise ValueError(f"invalid {name}")
    return value


def _shards(value):
    if not isinstance(value, dict) or any(type(value.get(key)) is not int for key in ("total", "successful", "failed")):
        raise ElasticsearchIKError("malformed_shards")
    if value["total"] != 1 or value["successful"] != 1 or value["failed"] != 0:
        raise ElasticsearchIKError("incomplete_shards")


class ElasticsearchIKRetriever:
    name = "bm25"
    lexical_profile = "elasticsearch-ik-maxword-smart-v1"

    def __init__(self, chunks: Sequence[Chunk], endpoint: str, index_name: str, k1=1.5, b=.75):
        _endpoint_port(endpoint)
        if not isinstance(index_name, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,199}", index_name) is None:
            raise ValueError("invalid fresh index_name")
        self.k1, self.b = _number(k1, "k1"), _number(b, "b", maximum=1)
        self.chunks = tuple(chunks)
        self._chunks = {}
        for chunk in self.chunks:
            key = chunk.chunk_id
            if not isinstance(key, str) or not key or key != key.strip() or len(key.encode("utf-8")) > 512 or key in self._chunks:
                raise ValueError("invalid or duplicate chunk_id")
            if not isinstance(chunk.text, str) or any(not isinstance(value, str) for value in (*chunk.law_names, *chunk.article_numbers)):
                raise ValueError("invalid chunk index text")
            self._chunks[key] = chunk
        self.index_name = index_name
        self._path = f"/{index_name}"
        self._transport = _LoopbackJSONTransport(endpoint)
        self._closed = False
        self._failed = False
        self.last_query_tokens = ()
        self.timings = {}
        self.diagnostics = {"retrieval_count": 0, "diagnostic_count": 0, "oov_collection": "explicit_post_query_only"}
        started = time.perf_counter_ns()
        self._instance = self._ready_instance()
        self._mapping = {"dynamic": "strict", "properties": {
            "chunk_id": {"type": "keyword", "doc_values": True},
            "body": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart", "similarity": "benchmark_bm25"}}}
        settings = {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "-1", "similarity": {
            "benchmark_bm25": {"type": "BM25", "k1": self.k1, "b": self.b, "discount_overlaps": True}}}
        self._expected_settings = flatten_settings(settings)
        created = self._transport.request("PUT", self._path, {"settings": settings, "mappings": self._mapping})
        if created.get("acknowledged") is not True or created.get("shards_acknowledged") is not True or created.get("index") != index_name:
            raise ElasticsearchIKError("index_create_incomplete")
        for offset in range(0, len(self.chunks), 500):
            batch = self.chunks[offset:offset + 500]
            lines = []
            for chunk in batch:
                lines.append({"create": {"_index": index_name, "_id": chunk.chunk_id}})
                lines.append({"chunk_id": chunk.chunk_id, "body": "\n".join((*chunk.law_names, *chunk.article_numbers, chunk.text))})
            raw = ("\n".join(json.dumps(line, ensure_ascii=False, allow_nan=False, separators=(",", ":")) for line in lines) + "\n").encode("utf-8")
            response = self._transport.request("POST", "/_bulk", raw_body=raw)
            items = response.get("items")
            if response.get("errors") is not False or not isinstance(items, list) or len(items) != len(batch):
                raise ElasticsearchIKError("bulk_incomplete")
            for chunk, item in zip(batch, items):
                result = item.get("create") if isinstance(item, dict) else None
                if not isinstance(result, dict) or result.get("status") != 201 or result.get("_id") != chunk.chunk_id or result.get("_index") != index_name or "error" in result:
                    raise ElasticsearchIKError("bulk_item_failed")
        blocked = self._transport.request("PUT", self._path + "/_settings", {"index": {"blocks": {"write": True}}})
        if blocked.get("acknowledged") is not True:
            raise ElasticsearchIKError("write_block_incomplete")
        self._expected_settings["index.blocks.write"] = "true"
        _shards(self._transport.request("POST", self._path + "/_refresh").get("_shards"))
        self._frozen_index = self._snapshot_index()
        self.index_stats = {"document_count": len(self.chunks), "primary_shards": 1, "replicas": 0,
                            "total_token_count": None, "token_stats_status": "not_collected"}
        self.diagnostics.update({"cluster_uuid": self._instance["cluster_uuid"], "index_uuid": self._frozen_index["index_uuid"]})
        self._identity = {"schema": "elasticsearch-ik-experiment-v1", "index_text_version": INDEX_TEXT_VERSION,
            "engine": {**self._instance["version"], "name": "elasticsearch", "version": EXPECTED_VERSION,
                       "k1": self.k1, "b": self.b, "discount_overlaps": True, "similarity": "BM25", "shards": 1, "replicas": 0},
            "tokenizer": {"name": "analysis-ik", "plugin": self._instance["plugin"], "index_analyzer": "ik_max_word", "query_analyzer": "ik_smart"},
            "query": {"kind": "analyze_unique_bool_should_term", "minimum_should_match": 1,
                      "max_unique_terms": MAX_UNIQUE_QUERY_TERMS, "overflow": "reject", "deduplication": "first-occurrence",
                      "extra_boosts": False, "query_parser": False, "empty_oov": "empty_results"},
            "ranking": {"sort": ["score-desc", "chunk_id-keyword-utf8-asc"], "tie_before_top_k": True, "score_kind": "bm25"},
            "http": {"host": "127.0.0.1", "proxy": False, "redirect": "reject", "retry": False}, "mapping": deepcopy(self._mapping)}
        self.config_fingerprint = hashlib.sha256(json.dumps(self._identity, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.timings["index_build_ms"] = (time.perf_counter_ns() - started) / 1e6

    @property
    def config_identity(self):
        return deepcopy(self._identity)

    def _ready_instance(self):
        root = self._transport.request("GET", "/")
        version = root.get("version")
        if not isinstance(version, dict) or version.get("number") != EXPECTED_VERSION or not isinstance(root.get("cluster_uuid"), str) or not root["cluster_uuid"] or root["cluster_uuid"] == "_na_":
            raise ElasticsearchIKError("engine_identity_mismatch")
        if any(not isinstance(version.get(key), str) or not version[key] for key in ("build_hash", "lucene_version")):
            raise ElasticsearchIKError("engine_identity_incomplete")
        nodes = self._transport.request("GET", "/_nodes/plugins").get("nodes")
        if not isinstance(nodes, dict) or len(nodes) != 1:
            raise ElasticsearchIKError("single_node_required")
        node = next(iter(nodes.values()))
        if not isinstance(node, dict) or node.get("version") != EXPECTED_VERSION or not isinstance(node.get("plugins"), list):
            raise ElasticsearchIKError("plugin_identity_incomplete")
        plugins = [entry for entry in node["plugins"] if isinstance(entry, dict) and entry.get("name") == "analysis-ik"]
        if len(plugins) != 1 or plugins[0].get("version") != EXPECTED_VERSION or not isinstance(plugins[0].get("classname"), str):
            raise ElasticsearchIKError("plugin_identity_mismatch")
        return {"cluster_uuid": root["cluster_uuid"], "version": {"build_hash": version["build_hash"], "lucene_version": version["lucene_version"]},
                "plugin": {key: plugins[0][key] for key in ("name", "version", "classname")}}

    def _snapshot_index(self):
        settings = self._transport.request("GET", self._path + "/_settings?flat_settings=true")
        mapping = self._transport.request("GET", self._path + "/_mapping")
        count = self._transport.request("GET", self._path + "/_count")
        stats = self._transport.request("GET", self._path + "/_stats?level=shards&filter_path=indices.*.shards.*.seq_no,indices.*.shards.*.routing")
        try:
            current = settings[self.index_name]["settings"]
            # No index-level analysis is part of this frozen experiment. A
            # same-name override could otherwise change tokens without changing
            # the mapping, document count or sequence number.
            if any(key == "index.analysis" or key.startswith("index.analysis.") for key in current):
                raise ElasticsearchIKError("unexpected_index_analysis")
            current_mapping = deepcopy(mapping[self.index_name]["mappings"])
            # ES omits keyword's default doc_values=true on GET _mapping.
            # Normalize only this field/default, never false or extra options.
            keyword = current_mapping["properties"]["chunk_id"]
            if isinstance(keyword, dict) and keyword.get("type") == "keyword" and "doc_values" not in keyword:
                keyword["doc_values"] = True
            shards = stats["indices"][self.index_name]["shards"]
            primary = shards["0"]
            if set(shards) != {"0"} or len(primary) != 1 or primary[0]["routing"]["primary"] is not True or primary[0]["routing"]["state"] != "STARTED":
                raise ElasticsearchIKError("index_shard_identity_mismatch")
            sequence = primary[0]["seq_no"]["max_seq_no"]
            if type(sequence) is not int or sequence < -1:
                raise ElasticsearchIKError("index_sequence_invalid")
            if not isinstance(current.get("index.uuid"), str) or not current["index.uuid"]:
                raise ElasticsearchIKError("index_identity_incomplete")
            for key, expected in self._expected_settings.items():
                if key.endswith((".k1", ".b")):
                    valid = float(current.get(key)) == float(expected)
                else:
                    valid = current.get(key) == expected
                if not valid:
                    raise ElasticsearchIKError("index_settings_drift")
            if current_mapping != self._mapping:
                raise ElasticsearchIKError("index_mapping_drift")
            _shards(count.get("_shards"))
            if type(count.get("count")) is not int or count["count"] != len(self.chunks):
                raise ElasticsearchIKError("index_count_mismatch")
            return {"index_uuid": current["index.uuid"], "mapping": current_mapping,
                    "settings": {key: current[key] for key in self._expected_settings}, "count": count["count"], "max_seq_no": sequence}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ElasticsearchIKError("malformed_index_snapshot") from None

    def _usable(self):
        if self._closed:
            raise ElasticsearchIKError("closed")
        if self._failed:
            raise ElasticsearchIKError("failed")

    def _analyze(self, query):
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        response = self._transport.request("POST", self._path + "/_analyze", {"analyzer": "ik_smart", "text": query})
        tokens = response.get("tokens")
        if not isinstance(tokens, list) or any(not isinstance(item, dict) or not isinstance(item.get("token"), str) or not item["token"] for item in tokens):
            raise ElasticsearchIKError("malformed_analysis")
        unique = tuple(dict.fromkeys(item["token"] for item in tokens))
        if len(unique) > MAX_UNIQUE_QUERY_TERMS:
            raise ElasticsearchIKError("too_many_query_terms")
        return unique

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        self._usable()
        if type(top_k) is not int or top_k < 0 or top_k > MAX_RETRIEVAL_TOP_K:
            raise ValueError("top_k must be an integer within 0..10000")
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        if top_k == 0:
            return []
        started = time.perf_counter_ns()
        try:
            tokens = self._analyze(query)
            analyzed = time.perf_counter_ns()
            self.last_query_tokens = tokens
            self.diagnostics["retrieval_count"] += 1
            if not tokens:
                response, results = None, []
            else:
                payload = {"size": top_k, "_source": False, "track_scores": True, "track_total_hits": True,
                           "sort": [{"_score": "desc"}, {"chunk_id": "asc"}],
                           "query": {"bool": {"should": [{"term": {"body": token}} for token in tokens], "minimum_should_match": 1}}}
                response = self._transport.request("POST", self._path + "/_search?allow_partial_search_results=false", payload)
                results = self._decode_results(response, top_k)
            ended = time.perf_counter_ns()
            self.timings.update({"last_query_ms": (ended - started) / 1e6, "last_analyze_ms": (analyzed - started) / 1e6,
                                 "last_search_ms": (ended - analyzed) / 1e6 if tokens else 0.0,
                                 "last_es_took_ms": response.get("took") if response is not None else None})
            return results
        except ElasticsearchIKError:
            self._failed = True
            raise

    def _decode_results(self, response, top_k):
        if response.get("timed_out") is not False or response.get("terminated_early") is True:
            raise ElasticsearchIKError("incomplete_search")
        _shards(response.get("_shards"))
        try:
            total, hits = response["hits"]["total"], response["hits"]["hits"]
            if not isinstance(total, dict) or total.get("relation") != "eq" or type(total.get("value")) is not int or not 0 <= total["value"] <= len(self.chunks) or not isinstance(hits, list) or len(hits) != min(top_k, total["value"]):
                raise ElasticsearchIKError("incomplete_hit_count")
            results, seen, keys = [], set(), []
            for rank, hit in enumerate(hits, 1):
                key, score = hit["_id"], hit["_score"]
                if not isinstance(key, str) or key not in self._chunks or key in seen or hit.get("_index") != self.index_name:
                    raise ElasticsearchIKError("hit_identity_mismatch")
                if isinstance(score, bool) or not isinstance(score, Real) or not math.isfinite(score) or score <= 0:
                    raise ElasticsearchIKError("invalid_score")
                ordering = hit.get("sort")
                if not isinstance(ordering, list) or len(ordering) != 2 or isinstance(ordering[0], bool) or not isinstance(ordering[0], Real) or not math.isfinite(ordering[0]) or ordering != [score, key]:
                    raise ElasticsearchIKError("invalid_hit_sort")
                seen.add(key)
                keys.append((-float(score), key.encode("utf-8")))
                results.append(SearchResult(self._chunks[key], float(score), rank, self.name, {
                    "score_kind": "bm25", "backend": "elasticsearch", "lexical_profile": self.lexical_profile,
                    "config_fingerprint": self.config_fingerprint, "query_token_count": len(self.last_query_tokens)}))
            if keys != sorted(keys):
                raise ElasticsearchIKError("noncanonical_hit_order")
            return results
        except (KeyError, TypeError, AttributeError):
            raise ElasticsearchIKError("malformed_search_results") from None

    def diagnose_query(self, query: str) -> dict:
        """Extra analysis/term checks, excluded from retrieve's wall timings."""
        self._usable()
        try:
            tokens = self._analyze(query)
            oov, frequencies = [], []
            for token in tokens:
                # ES9.1.4 text fields do not implement getTerms: _terms_enum may
                # return complete empty even when a body term is indexed. Use
                # the same exact term semantics as retrieval, without scoring.
                response = self._transport.request("POST", self._path + "/_count", {"query": {"term": {"body": token}}})
                if response.get("timed_out") is True or response.get("terminated_early") is True:
                    raise ElasticsearchIKError("incomplete_term_diagnostic")
                _shards(response.get("_shards"))
                count = response.get("count")
                if type(count) is not int or not 0 <= count <= len(self.chunks):
                    raise ElasticsearchIKError("invalid_term_document_count")
                frequencies.append(count)
                if count == 0:
                    oov.append(token)
            self.diagnostics["diagnostic_count"] += 1
            return {"token_count": len(tokens), "tokens": list(tokens), "oov_count": len(oov), "oov_tokens": oov,
                    "document_frequencies": frequencies, "method": "body-exact-term-count-v1", "status": "completed"}
        except ElasticsearchIKError:
            self._failed = True
            raise

    def close(self) -> None:
        """Verify frozen identity only; the runtime owner stops/deletes later."""
        if self._closed:
            return
        try:
            if self._ready_instance() != self._instance or self._snapshot_index() != self._frozen_index:
                raise ElasticsearchIKError("instance_or_index_drift")
            if self._failed:
                raise ElasticsearchIKError("failed")
        finally:
            self._closed = True
