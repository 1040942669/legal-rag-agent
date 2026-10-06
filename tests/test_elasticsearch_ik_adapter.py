"""Public synthetic ES transport contracts; no real ES or corpus is loaded."""
from copy import deepcopy
import json
import math
import socket
from types import SimpleNamespace

import pytest

from legal_rag.models import Chunk
from scripts import elasticsearch_ik_adapter as adapter


def chunk(key="a", text="合成中文正文"):
    return Chunk(key, text, ["合成法"], ["第一条"], [], [], "article")


def tcp_socket():
    return SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM)


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.cluster_uuid = "synthetic-cluster"
        self.index_uuid = "synthetic-index"
        self.version = "9.1.4"
        self.plugin_version = "9.1.4"
        self.settings = {}
        self.mapping = {}
        self.ids = []
        self.tokens = ["合成", "中文", "合成"]
        self.search_override = None
        self.bulk_error = False
        self.max_seq_no = -1
        self.available_terms = {"合成"}

    def request(self, method, path, payload=None, *, raw_body=None):
        self.calls.append((method, path, deepcopy(payload), raw_body))
        if path == "/":
            return {"cluster_uuid": self.cluster_uuid, "cluster_name": "synthetic",
                    "version": {"number": self.version, "build_hash": "synthetic-build", "lucene_version": "synthetic-lucene"}}
        if path == "/_nodes/plugins":
            return {"nodes": {"node": {"version": self.version, "plugins": [
                {"name": "analysis-ik", "version": self.plugin_version, "classname": "SyntheticIK"}]}}}
        if method == "PUT" and path == "/synthetic-ik":
            self.mapping = deepcopy(payload["mappings"])
            self.settings = adapter.flatten_settings(payload["settings"])
            self.settings["index.uuid"] = self.index_uuid
            return {"acknowledged": True, "shards_acknowledged": True, "index": "synthetic-ik"}
        if path == "/_bulk":
            lines = raw_body.decode("utf-8").splitlines()
            items = []
            for line in lines[::2]:
                item = json.loads(line)["create"]
                self.ids.append(item["_id"])
                self.max_seq_no += 1
                items.append({"create": {"_index": item["_index"], "_id": item["_id"], "status": 201}})
            return {"errors": self.bulk_error, "items": items}
        if method == "PUT" and path.endswith("/_settings"):
            self.settings.update(adapter.flatten_settings(payload))
            return {"acknowledged": True}
        if path.endswith("/_refresh"):
            return {"_shards": {"total": 1, "successful": 1, "failed": 0}}
        if path.endswith("/_count"):
            return {"count": len(self.ids), "_shards": {"total": 1, "successful": 1, "failed": 0}}
        if "/_settings?" in path:
            self.settings["index.uuid"] = self.index_uuid
            return {"synthetic-ik": {"settings": deepcopy(self.settings)}}
        if path.endswith("/_mapping"):
            return {"synthetic-ik": {"mappings": deepcopy(self.mapping)}}
        if "/_stats?" in path:
            return {"indices": {"synthetic-ik": {"shards": {"0": [
                {"routing": {"primary": True, "state": "STARTED"}, "seq_no": {"max_seq_no": self.max_seq_no}}]}}}}
        if path.endswith("/_analyze"):
            return {"tokens": [{"token": token} for token in self.tokens]}
        if "/_search?" in path:
            if self.search_override is not None:
                return deepcopy(self.search_override)
            return search_result(self.ids[:payload["size"]])
        if path.endswith("/_terms_enum"):
            token = payload["string"]
            return {"complete": True, "terms": [token] if token in self.available_terms else [],
                    "_shards": {"total": 1, "successful": 1, "failed": 0}}
        raise AssertionError((method, path))


def search_result(ids, *, total=None):
    return {"took": 1, "timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0},
            "hits": {"total": {"value": len(ids) if total is None else total, "relation": "eq"},
                     "hits": [{"_index": "synthetic-ik", "_id": key, "_score": 1.0, "sort": [1.0, key]}
                              for key in sorted(ids, key=lambda value: value.encode("utf-8"))]}}


@pytest.fixture
def fake(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(adapter, "_LoopbackJSONTransport", lambda endpoint: transport)
    return transport


def build(fake, chunks=None):
    return adapter.ElasticsearchIKRetriever(chunks or [chunk()], "http://127.0.0.1:19200", "synthetic-ik")


def test_build_freezes_real_engine_mapping_read_only_and_original_chunk_identity(fake):
    source = chunk()
    retriever = build(fake, [source])
    assert retriever.retrieve("合成问题")[0].chunk is source
    identity = retriever.config_identity
    assert identity["engine"]["version"] == "9.1.4"
    assert identity["engine"]["k1"] == 1.5 and identity["engine"]["b"] == .75
    assert identity["tokenizer"]["index_analyzer"] == "ik_max_word"
    assert identity["tokenizer"]["query_analyzer"] == "ik_smart"
    assert identity["query"]["max_unique_terms"] == 1024
    assert fake.settings["index.blocks.write"] == "true"
    assert fake.settings["index.number_of_shards"] == "1"
    assert fake.settings["index.number_of_replicas"] == "0"
    assert fake.mapping["properties"]["chunk_id"]["type"] == "keyword"
    assert retriever.index_stats["document_count"] == 1
    identity["engine"]["k1"] = 77
    assert retriever.config_identity["engine"]["k1"] == 1.5


def test_query_is_analyzed_deduplicated_or_terms_with_server_global_tie(fake):
    retriever = build(fake, [chunk(str(number)) for number in range(9, -1, -1)])
    fake.search_override = search_result([str(number) for number in range(5)], total=10)
    results = retriever.retrieve("合成问题", top_k=5)
    assert [item.chunk.chunk_id for item in results] == ["0", "1", "2", "3", "4"]
    request = next(call for call in reversed(fake.calls) if "/_search?" in call[1])
    assert request[2]["sort"] == [{"_score": "desc"}, {"chunk_id": "asc"}]
    assert request[2]["query"] == {"bool": {"should": [{"term": {"body": "合成"}}, {"term": {"body": "中文"}}], "minimum_should_match": 1}}
    assert request[2]["track_scores"] is True and request[2]["_source"] is False
    assert "allow_partial_search_results=false" in request[1]
    assert retriever.last_query_tokens == ("合成", "中文")
    assert not any("_terms_enum" in call[1] for call in fake.calls)


def test_empty_and_oov_are_legal_empty_results(fake):
    retriever = build(fake)
    fake.tokens = []
    before = len(fake.calls)
    assert retriever.retrieve("。") == []
    assert not any("/_search" in call[1] for call in fake.calls[before:])
    fake.tokens = ["未收录合成词"]
    fake.search_override = search_result([])
    assert retriever.retrieve("未收录合成词") == []


@pytest.mark.parametrize("mutation", ["timeout", "shard-failed", "unknown-id", "duplicate-id", "nan", "zero", "bool-score", "null-score", "bad-sort", "wrong-index", "partial-count", "missing-hits"])
def test_partial_or_malformed_search_never_becomes_quality_miss(fake, mutation):
    retriever = build(fake)
    result = search_result(["a"])
    if mutation == "timeout": result["timed_out"] = True
    elif mutation == "shard-failed": result["_shards"]["failed"] = 1
    elif mutation == "unknown-id": result["hits"]["hits"][0]["_id"] = "not-owned"
    elif mutation == "duplicate-id": result["hits"]["hits"] *= 2; result["hits"]["total"]["value"] = 2
    elif mutation == "nan": result["hits"]["hits"][0]["_score"] = math.nan
    elif mutation == "zero": result["hits"]["hits"][0]["_score"] = 0
    elif mutation == "bool-score": result["hits"]["hits"][0]["_score"] = True
    elif mutation == "null-score": result["hits"]["hits"][0]["_score"] = None
    elif mutation == "bad-sort": result["hits"]["hits"][0]["sort"] = [1, "wrong"]
    elif mutation == "wrong-index": result["hits"]["hits"][0]["_index"] = "other"
    elif mutation == "partial-count": result["hits"]["total"]["value"] = 2
    elif mutation == "missing-hits": result["hits"].pop("hits")
    fake.search_override = result
    with pytest.raises(adapter.ElasticsearchIKError):
        retriever.retrieve("合成问题")


def test_1024_unique_bound_rejects_overflow_without_truncation(fake):
    retriever = build(fake)
    fake.tokens = [f"token-{number}" for number in range(1025)]
    before = len(fake.calls)
    with pytest.raises(adapter.ElasticsearchIKError, match="too_many_query_terms"):
        retriever.retrieve("合成超长问题")
    assert not any("/_search" in call[1] for call in fake.calls[before:])


def test_diagnostic_is_explicit_and_does_not_mutate_query_timing(fake):
    retriever = build(fake)
    retriever.retrieve("合成问题")
    prior = deepcopy(retriever.timings)
    diagnosis = retriever.diagnose_query("合成问题")
    assert diagnosis["token_count"] == 2 and diagnosis["oov_count"] == 1
    assert retriever.timings == prior


@pytest.mark.parametrize("drift", ["cluster", "index", "write-block", "count", "sequence", "mapping"])
def test_close_checks_instance_and_index_but_never_deletes_or_stops(fake, drift):
    retriever = build(fake)
    if drift == "cluster": fake.cluster_uuid = "replaced-cluster"
    elif drift == "index": fake.index_uuid = "replaced-index"
    elif drift == "write-block": fake.settings["index.blocks.write"] = "false"
    elif drift == "count": fake.ids.append("unexpected")
    elif drift == "sequence": fake.max_seq_no += 2
    elif drift == "mapping": fake.mapping["properties"]["body"]["analyzer"] = "standard"
    with pytest.raises(adapter.ElasticsearchIKError):
        retriever.close()
    assert all(method != "DELETE" for method, *_ in fake.calls)


def test_successful_close_is_idempotent_and_prevents_further_query(fake):
    retriever = build(fake)
    retriever.close()
    before = len(fake.calls)
    retriever.close()
    assert len(fake.calls) == before
    with pytest.raises(adapter.ElasticsearchIKError, match="closed"):
        retriever.retrieve("合成问题")


@pytest.mark.parametrize("bad", ["https://127.0.0.1:19200", "http://localhost:19200", "http://127.0.0.2:19200", "http://example.com:19200", "http://127.0.0.1:19200/path", "http://user:pass@127.0.0.1:19200", "http://127.0.0.1:0", "http://127.0.0.1:65536"])
def test_endpoint_is_literal_single_loopback_without_credentials(bad):
    with pytest.raises(ValueError):
        adapter._LoopbackJSONTransport(bad)


@pytest.mark.parametrize("event,args", [("socket.connect", (None, ("8.8.8.8", 19200))), ("socket.connect", (None, ("127.0.0.1", 19201))), ("socket.getaddrinfo", ("example.com", 19200, 0, 0, 0)), ("socket.sendto", (None, ("127.0.0.1", 19200)))])
def test_socket_guard_denies_other_host_port_dns_and_udp(event, args):
    guard = adapter.loopback_socket_guard("http://127.0.0.1:19200")
    with pytest.raises(adapter.ElasticsearchIKError, match="network_forbidden"):
        guard(event, args)


def test_socket_guard_allows_only_exact_literal_endpoint_and_is_not_auto_installed(fake, monkeypatch):
    installed = []
    monkeypatch.setattr(adapter.sys, "addaudithook", installed.append)
    build(fake)
    assert installed == []
    guard = adapter.loopback_socket_guard("http://127.0.0.1:19200")
    guard("socket.connect", (tcp_socket(), ("127.0.0.1", 19200)))
    guard("socket.getaddrinfo", ("127.0.0.1", 19200, 0, 0, 0))
    adapter.install_loopback_socket_audit("http://127.0.0.1:19200")
    assert len(installed) == 1


@pytest.mark.parametrize("bad", ["version", "plugin", "bulk"])
def test_readiness_or_bulk_failure_stops_construction(fake, bad):
    if bad == "version": fake.version = "9.1.3"
    elif bad == "plugin": fake.plugin_version = "different"
    else: fake.bulk_error = True
    with pytest.raises(adapter.ElasticsearchIKError):
        build(fake)


@pytest.mark.parametrize("ids", [["a", "a"], [""], [" a"], ["中" * 200]])
def test_invalid_chunk_identity_is_rejected_before_any_http(fake, ids):
    with pytest.raises(ValueError):
        build(fake, [chunk(key) for key in ids])
    assert fake.calls == []


def test_zero_top_k_is_empty_without_http_but_invalid_types_are_rejected(fake):
    retriever = build(fake)
    before = len(fake.calls)
    assert retriever.retrieve("合成问题", 0) == []
    assert len(fake.calls) == before
    for value in (True, False, -1, 1.5, "5", 10001):
        with pytest.raises(ValueError):
            retriever.retrieve("合成问题", value)


def test_empty_corpus_is_frozen_and_returns_no_results(fake):
    retriever = adapter.ElasticsearchIKRetriever([], "http://127.0.0.1:19200", "synthetic-ik")
    assert retriever.index_stats["document_count"] == 0
    assert retriever.retrieve("合成问题") == []
    retriever.close()


def test_1024_unique_tokens_and_arbitrarily_repeated_tokens_are_not_truncated(fake):
    retriever = build(fake)
    fake.tokens = [f"token-{number}" for number in range(1024)] * 2
    assert len(retriever.retrieve("合成问题")) == 1
    request = next(call for call in reversed(fake.calls) if "/_search?" in call[1])
    assert len(request[2]["query"]["bool"]["should"]) == 1024
    assert len(retriever.last_query_tokens) == 1024


@pytest.mark.parametrize("k1,b", [(True, .75), (math.nan, .75), (0, .75), (-1, .75), (1.5, True), (1.5, -1), (1.5, 1.1), (1.5, math.inf)])
def test_bm25_parameters_are_explicit_finite_valid_numbers(fake, k1, b):
    with pytest.raises(ValueError):
        adapter.ElasticsearchIKRetriever([chunk()], "http://127.0.0.1:19200", "synthetic-ik", k1=k1, b=b)
    assert fake.calls == []


class HTTPResponse:
    def __init__(self, status=200, content=b'{"ok":true}'):
        self.status, self.content = status, content

    def read(self, amount):
        return self.content[:amount]


class HTTPConnection:
    instances = []
    response = HTTPResponse()
    fail = False

    def __init__(self, host, port, *, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.closed, self.calls = False, []
        self.instances.append(self)

    def request(self, method, path, *, body, headers):
        if self.fail:
            raise TimeoutError("PRIVATE SERVER ERROR MUST NOT ESCAPE")
        self.calls.append((method, path, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


@pytest.fixture
def connection(monkeypatch):
    HTTPConnection.instances = []
    HTTPConnection.response, HTTPConnection.fail = HTTPResponse(), False
    monkeypatch.setattr(adapter.http.client, "HTTPConnection", HTTPConnection)
    return HTTPConnection


def test_direct_transport_never_uses_proxy_env_and_sends_utf8(connection, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://external.invalid:8888")
    monkeypatch.setenv("ALL_PROXY", "http://external.invalid:8888")
    transport = adapter._LoopbackJSONTransport("http://127.0.0.1:19200")
    assert transport.request("POST", "/synthetic/_analyze", {"text": "合成中文"}) == {"ok": True}
    actual = connection.instances[0]
    assert (actual.host, actual.port, actual.timeout) == ("127.0.0.1", 19200, 30)
    assert json.loads(actual.calls[0][2]) == {"text": "合成中文"}
    assert actual.calls[0][3]["Content-Type"] == "application/json"
    assert actual.closed


@pytest.mark.parametrize("status,code", [(301, "http_redirect_forbidden"), (302, "http_redirect_forbidden"), (307, "http_redirect_forbidden"), (400, "http_status_400"), (503, "http_status_503")])
def test_http_error_does_not_follow_redirect_or_expose_server_body(connection, status, code):
    connection.response = HTTPResponse(status, b'{"private":"PRIVATE SERVER ERROR MUST NOT ESCAPE"}')
    with pytest.raises(adapter.ElasticsearchIKError, match=code) as error:
        adapter._LoopbackJSONTransport("http://127.0.0.1:19200").request("GET", "/")
    assert "PRIVATE" not in str(error.value)
    assert len(connection.instances) == 1 and connection.instances[0].closed


@pytest.mark.parametrize("content", [b'[]', b'{"x":NaN}', b'{"x":1,"x":2}', b'not-json', b'\xff'])
def test_malformed_http_json_is_rejected_with_stable_private_safe_code(connection, content):
    connection.response = HTTPResponse(content=content)
    with pytest.raises(adapter.ElasticsearchIKError, match="malformed_http_json"):
        adapter._LoopbackJSONTransport("http://127.0.0.1:19200").request("GET", "/")
    assert connection.instances[0].closed


def test_http_response_limit_timeout_no_retry_and_ndjson_content_type(connection, monkeypatch):
    transport = adapter._LoopbackJSONTransport("http://127.0.0.1:19200")
    transport.request("POST", "/_bulk", raw_body=b'{}\n')
    assert connection.instances[-1].calls[0][3]["Content-Type"] == "application/x-ndjson"
    monkeypatch.setattr(adapter, "MAX_RESPONSE_BYTES", 4)
    with pytest.raises(adapter.ElasticsearchIKError, match="http_response_too_large"):
        transport.request("GET", "/")
    connection.fail = True
    before = len(connection.instances)
    with pytest.raises(adapter.ElasticsearchIKError, match="http_transport_failed") as error:
        transport.request("GET", "/")
    assert len(connection.instances) == before + 1 and connection.instances[-1].closed
    assert "PRIVATE" not in str(error.value)


def test_server_sorted_subset_cannot_be_resorted_to_hide_global_tie_error(fake):
    retriever = build(fake, [chunk("a"), chunk("b")])
    fake.search_override = search_result(["a", "b"])
    fake.search_override["hits"]["hits"].reverse()
    with pytest.raises(adapter.ElasticsearchIKError, match="noncanonical_hit_order"):
        retriever.retrieve("合成问题")


def test_bad_sort_score_type_is_not_accepted_as_equal_numeric_score(fake):
    retriever = build(fake)
    fake.search_override = search_result(["a"])
    fake.search_override["hits"]["hits"][0]["sort"] = [True, "a"]
    with pytest.raises(adapter.ElasticsearchIKError, match="invalid_hit_sort"):
        retriever.retrieve("合成问题")


def test_es_omitted_default_keyword_doc_values_is_canonicalized_only_at_that_field(fake, monkeypatch):
    original = fake.request

    def without_default(method, path, payload=None, *, raw_body=None):
        result = original(method, path, payload, raw_body=raw_body)
        if path.endswith("/_mapping"):
            result["synthetic-ik"]["mappings"]["properties"]["chunk_id"].pop("doc_values", None)
        return result

    monkeypatch.setattr(fake, "request", without_default)
    retriever = build(fake)
    assert retriever.retrieve("合成问题")[0].chunk.chunk_id == "a"
    retriever.close()


@pytest.mark.parametrize("field,value", [("doc_values", False), ("normalizer", "lowercase"), ("ignore_above", 256)])
def test_keyword_mapping_nondefault_or_unknown_changes_remain_blockers(fake, field, value):
    retriever = build(fake)
    fake.mapping["properties"]["chunk_id"][field] = value
    with pytest.raises(adapter.ElasticsearchIKError, match="index_mapping_drift"):
        retriever.close()


def test_index_level_analysis_cannot_override_frozen_plugin_analyzers(fake):
    retriever = build(fake)
    fake.settings["index.analysis.analyzer.ik_smart.type"] = "keyword"
    with pytest.raises(adapter.ElasticsearchIKError, match="unexpected_index_analysis"):
        retriever.close()


@pytest.mark.parametrize("family,kind", [(socket.AF_INET, socket.SOCK_DGRAM), (socket.AF_INET6, socket.SOCK_STREAM), (socket.AF_INET, socket.SOCK_RAW), (socket.AF_INET, True)])
def test_exact_endpoint_is_not_enough_without_ipv4_tcp(family, kind):
    guard = adapter.loopback_socket_guard("http://127.0.0.1:19200")
    endpoint = ("127.0.0.1", 19200)
    with pytest.raises(adapter.ElasticsearchIKError, match="network_forbidden"):
        guard("socket.connect", (SimpleNamespace(family=family, type=kind), endpoint))


def test_ipv4_tcp_with_platform_socket_flags_remains_allowed():
    flags = getattr(socket, "SOCK_NONBLOCK", 0) | getattr(socket, "SOCK_CLOEXEC", 0)
    guard = adapter.loopback_socket_guard("http://127.0.0.1:19200")
    guard("socket.connect", (SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM | flags), ("127.0.0.1", 19200)))
