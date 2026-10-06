"""Fictional protocol/process contracts; no JVM or legal corpus required."""

import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import queue
import subprocess

import pytest

from legal_rag.models import Chunk
from scripts import smartcn_bridge as bridge


def b64(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def ready(count):
    return f"READY\t{count}\t17\t{count * 2}\t{count}\n"


def chunk(key, text="合成事项。", laws=(), articles=()):
    return Chunk(key, text, list(laws), list(articles), ["fictional.txt"], [1], "article")


class FakeProcess:
    def __init__(self, stdout="", returncode=None):
        self.stdout = io.StringIO(stdout)
        self.stdin = io.StringIO()
        self.pid = 876543
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminated = True
        self.returncode = -9


@pytest.fixture
def environment(tmp_path, monkeypatch):
    dep = tmp_path / "deps"
    dep.mkdir()
    pins = {}
    for name in ("core", "analysis-common", "analysis-smartcn"):
        filename = f"lucene-{name}-9.12.3.jar"
        payload = ("fictional-" + name).encode()
        (dep / filename).write_bytes(payload)
        pins[filename] = hashlib.sha256(payload).hexdigest()
    source = tmp_path / "SmartCnBridge.java"
    source.write_text("class SmartCnBridge {}", encoding="utf-8")
    monkeypatch.setattr(bridge, "DEPENDENCIES", pins)
    monkeypatch.setattr(bridge, "JAVA_SOURCE", source)
    tools = {}
    for name in ("java", "javac"):
        tools[name] = tmp_path / (name + ".exe")
        tools[name].write_bytes(("fictional-" + name).encode())
    monkeypatch.setattr(bridge.shutil, "which", lambda value, **kwargs: str(tools[value]))
    calls = []
    process_holder = {}

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        if "-version" in args:
            kwargs["stdout"].write(b'openjdk version "17.0.18"\n' if args[0] == str(tools["java"]) else b'javac 17.0.18\n')
            return FakeProcess(returncode=0)
        if "-d" in args:
            classes = Path(args[args.index("-d") + 1])
            (classes / "SmartCnBridge.class").write_bytes(b"fictional-class")
            return FakeProcess(returncode=0)
        process = process_holder.get("process", FakeProcess(ready(0) + "BYE\n"))
        if args[-5] == "tokenize":
            docs = Path(args[-4])
            tokens = Path(args[-3])
            records = [line.split("\t")[0] + "\t" + b64("合成") + "\t" + b64("合成")
                       for line in docs.read_text(encoding="utf-8").splitlines()]
            tokens.write_text("\n".join(records) + ("\n" if records else ""), encoding="utf-8")
        return process

    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    monkeypatch.setattr(bridge, "_stop_process", lambda process: process.terminate())
    monkeypatch.setattr(bridge, "_remember_process_tree", lambda process: None)
    prepared = tmp_path / "prepared"
    identity = bridge.prepare_bridge(dep, prepared)
    calls.clear()
    return dep, prepared, identity, calls, process_holder, tmp_path


def construct(environment, chunks=(), backend="lucene", response=None, **kwargs):
    dep, prepared, _, _, holder, tmp = environment
    holder["process"] = FakeProcess(response or ready(len(chunks)) + "BYE\n")
    return bridge.SmartCnRetriever(chunks, backend=backend, dependency_dir=dep,
                                  prepared_build_dir=prepared, work_dir=tmp / "runs", **kwargs)


def test_prepared_identity_has_real_hashes_separate_compile_time_and_no_mutable_alias(environment):
    dep, prepared, identity, calls, holder, tmp = environment
    assert identity["source_sha256"] == hashlib.sha256(bridge.JAVA_SOURCE.read_bytes()).hexdigest()
    assert identity["classes_sha256"] == {"SmartCnBridge.class": hashlib.sha256(b"fictional-class").hexdigest()}
    assert identity["jar_sha256"] == bridge.DEPENDENCIES
    assert identity["java_version"] == 'openjdk version "17.0.18"'
    assert identity["compile_ms"] >= 0
    retriever = construct(environment)
    assert len(calls) == 1  # Prepared workers do not compile or probe versions.
    assert retriever.timings["compile_ms"] == 0
    assert retriever.timings["prepared_compile_ms"] == identity["compile_ms"]
    copied = retriever.config_identity
    copied["engine"]["k1"] = 999
    assert retriever.config_identity["engine"]["k1"] == 1.5
    retriever.close()
    retriever.close()


def test_lucene_only_requests_topk_preserves_chunk_and_protocol_text(environment, monkeypatch):
    for name in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH", "JDK_JAVAC_OPTIONS"):
        monkeypatch.setenv(name, "not-forwarded")
    monkeypatch.setenv("FICTIONAL_MODEL_API_KEY", "not-forwarded")
    original = chunk("甲\t\n", "原文\t下一行\n正文", ("合成甲法",), ("第十条",))
    before = deepcopy(original)
    retriever = construct(environment, [original], response=ready(1) + f"R\t22\t{b64(original.chunk_id)}\t0.2\nBYE\n")
    process = environment[4]["process"]
    rows = retriever.retrieve("合成问题\t\n", top_k=3)
    assert len(rows) == 1 and rows[0].chunk is original and original == before
    assert rows[0].score == .2 and rows[0].rank == 1 and rows[0].trace["score_kind"] == "bm25"
    assert process.stdin.getvalue() == f"Q\t3\t{b64('合成问题' + chr(9) + chr(10))}\n"
    args, options = environment[3][-1]
    assert options["shell"] is False and options["encoding"] == "utf-8"
    assert not any("OPTIONS" in key or "CLASSPATH" == key or "API_KEY" in key for key in options["env"])
    lines = (retriever.run_dir / "docs.tsv").read_text(encoding="utf-8").splitlines()
    assert lines == [b64(original.chunk_id) + "\t" + b64("合成甲法\n第十条\n原文\t下一行\n正文")]
    retriever.close()
    assert retriever.run_dir.exists() and (retriever.run_dir / "java.stderr.log").exists()


@pytest.mark.parametrize("target", ["jar", "source", "classes", "receipt"])
def test_prepared_inputs_drift_is_rejected_before_launch(environment, target):
    dep, prepared, _, calls, _, _ = environment
    path = {"jar": dep / next(iter(bridge.DEPENDENCIES)), "source": bridge.JAVA_SOURCE,
            "classes": prepared / "classes/SmartCnBridge.class", "receipt": prepared / "runtime.json"}[target]
    path.write_bytes(b"changed")
    with pytest.raises(bridge.SmartCnBridgeError):
        construct(environment)
    assert not calls


@pytest.mark.parametrize("response,code", [
    (ready(99), "ready_count_mismatch"),
    ("READY\t1\t-1\t2\t1\n", "invalid_ready"),
    ("ERR\tinvalid_input\n", "java_error_invalid_input"),
    ("", "unexpected_eof"),
    ("unframed private content\n", "invalid_ready"),
])
def test_ready_errors_close_process_and_do_not_expose_raw_stdout(environment, response, code):
    environment[4]["process"] = FakeProcess(response)
    with pytest.raises(bridge.SmartCnBridgeError, match=code) as caught:
        dep, prepared, _, _, _, tmp = environment
        bridge.SmartCnRetriever([chunk("x")], dependency_dir=dep,
                               prepared_build_dir=prepared, work_dir=tmp / "runs")
    assert environment[4]["process"].terminated
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("payload", [
    "R\t4\t{a}\tNaN", "R\t4\t{a}\t-1", "R\t4\t{a}\t0",
    "R\t4\t{a}\t1\t{a}\t1", "R\t4\t{z}\t1", "R\t4\tbad%\t1",
    "R\t4\t{a}", "R\t-1\t{a}\t1", "R\t4\t{b}\t1\t{a}\t1",
])
def test_lucene_protocol_score_identity_and_sort_fail_closed(environment, payload):
    payload = payload.format(a=b64("a"), b=b64("b"), z=b64("unknown"))
    retriever = construct(environment, [chunk("a"), chunk("b")], response=ready(2) + payload + "\n")
    with pytest.raises(bridge.SmartCnBridgeError):
        retriever.retrieve("合成", top_k=2)
    assert environment[4]["process"].terminated


def test_query_timeout_closes_process_and_closed_retriever_never_reuses_stream(environment):
    retriever = construct(environment)
    retriever._responses = queue.Queue()
    retriever.query_timeout = .01
    with pytest.raises(bridge.SmartCnBridgeError, match="query_timeout"):
        retriever.retrieve("合成")
    assert environment[4]["process"].terminated
    with pytest.raises(bridge.SmartCnBridgeError, match="closed"):
        retriever.retrieve("合成")


def test_bm25s_uses_same_java_tf_tokens_query_unique_and_stable_ties(environment):
    response = ready(2) + "T\t2\t" + b64("合成") + "\t" + b64("合成") + "\nT\t3\t" + b64("missing") + "\nBYE\n"
    retriever = construct(environment, [chunk("b"), chunk("a")], backend="bm25s", response=response)
    assert retriever.document_tokens == (("合成", "合成"), ("合成", "合成"))
    rows = retriever.retrieve("合成重复", top_k=1)
    assert [row.chunk.chunk_id for row in rows] == ["a"]
    assert retriever.retrieve("OOV") == []
    assert retriever.config_identity["engine"]["name"] == "bm25s"
    assert retriever.config_identity["engine"]["dtype"] == "float64"
    assert retriever.config_identity["tokenizer"]["default_stopwords"] is True
    retriever.close()


@pytest.mark.parametrize("kwargs", [{"backend": "other"}, {"k1": True}, {"k1": 0}, {"k1": float("nan")}, {"b": 1.1}, {"b": -1}, {"ready_timeout": 0}, {"query_timeout": float("inf")}])
def test_bad_configuration_fails_before_process(environment, kwargs):
    with pytest.raises(ValueError):
        construct(environment, **kwargs)
    assert not environment[3]


def test_duplicate_ids_rejected_before_compile(environment):
    with pytest.raises(ValueError, match="chunk"):
        construct(environment, [chunk("x"), chunk("x")])
    assert not environment[3]


def test_process_subtree_cleanup_terminates_children_before_parent(monkeypatch):
    import psutil
    events = []
    class Record:
        def __init__(self, pid):
            self.pid = pid
        def children(self, recursive):
            assert recursive
            return [Record(2)]
        def terminate(self):
            events.append(self.pid)
        def kill(self):
            events.append(-self.pid)
    process = FakeProcess()
    monkeypatch.setattr(psutil, "Process", Record)
    monkeypatch.setattr(psutil, "wait_procs", lambda rows, timeout: (rows, []))
    bridge._stop_process(process)
    assert events == [2, 876543]


def test_public_prepared_validator_is_readonly_and_tool_drift_is_rejected(environment):
    dep, prepared, identity, calls, _, _ = environment
    assert bridge.verify_prepared_bridge(dep, prepared) == identity
    assert not calls
    Path(bridge._tool("java")).write_bytes(b"different-java")
    with pytest.raises(bridge.SmartCnBridgeError, match="prepared_identity_mismatch"):
        bridge.verify_prepared_bridge(dep, prepared)
    assert not calls


def test_ready_timeout_closes_started_process(environment, monkeypatch):
    original = bridge.SmartCnRetriever._read
    monkeypatch.setattr(bridge.SmartCnRetriever, "_read", lambda self, timeout, code: (_ for _ in ()).throw(bridge.SmartCnBridgeError(code)))
    with pytest.raises(bridge.SmartCnBridgeError, match="ready_timeout"):
        construct(environment)
    assert environment[4]["process"].terminated


def test_compiler_timeout_kills_process_and_preserves_log(environment, monkeypatch):
    process = FakeProcess()
    stopped = []
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda args, **kwargs: process)
    monkeypatch.setattr(process, "wait", lambda timeout: (_ for _ in ()).throw(subprocess.TimeoutExpired("fake", timeout)))
    monkeypatch.setattr(bridge, "_stop_process", lambda item: stopped.append(item))
    log = environment[-1] / "timeout.log"
    with pytest.raises(bridge.SmartCnBridgeError, match="tool_timeout"):
        bridge._run_logged(["fake"], log, .01)
    assert stopped == [process] and log.exists()


@pytest.mark.parametrize("record", ["unknown", "duplicate", "statistics", "empty_token", "malformed"])
def test_token_stream_identity_statistics_and_encoding_fail_closed(environment, monkeypatch, record):
    original = bridge.SmartCnRetriever._build_bm25s
    def corrupt(self, path, k1, b):
        rows = {"unknown": b64("different") + "\t" + b64("合成") + "\n",
                "duplicate": b64("x") + "\t" + b64("合成") + "\n" + b64("x") + "\n",
                "statistics": b64("x") + "\t" + b64("合成") + "\n",
                "empty_token": b64("x") + "\t\n", "malformed": b64("x") + "\t%invalid\n"}
        path.write_text(rows[record], encoding="utf-8")
        return original(self, path, k1, b)
    monkeypatch.setattr(bridge.SmartCnRetriever, "_build_bm25s", corrupt)
    with pytest.raises(bridge.SmartCnBridgeError):
        construct(environment, [chunk("x")], backend="bm25s")
    assert environment[4]["process"].terminated


def test_positive_ties_remain_native_order_and_unknown_queries_empty(environment):
    response = ready(2) + f"R\t2\t{b64('a')}\t.5\t{b64('b')}\t.5\nR\t3\nBYE\n"
    with construct(environment, [chunk("b"), chunk("a")], response=response) as retriever:
        assert [row.chunk.chunk_id for row in retriever.retrieve("合成", 2)] == ["a", "b"]
        assert retriever.retrieve("OOV") == []
        assert retriever.index_stats == {"document_count": 2, "total_tokens": 4,
                                         "nonempty_document_count": 2, "zero_token_document_count": 0,
                                         "average_nonempty_field_length": 2}


def test_close_error_still_closes_process(environment):
    retriever = construct(environment, response=ready(0) + "ERR\tclose_failure\n")
    with pytest.raises(bridge.SmartCnBridgeError, match="close_failure"):
        retriever.close()
    assert environment[4]["process"].terminated


def test_no_tracked_directory_may_be_used_as_run_output(environment):
    with pytest.raises(ValueError, match="tracked"):
        bridge.SmartCnRetriever([], dependency_dir=environment[0], prepared_build_dir=environment[1], work_dir=bridge.ROOT)
    assert not environment[3]


def test_prepared_receipt_duplicate_keys_rejected(environment):
    path = environment[1] / "runtime.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(bridge.SmartCnBridgeError):
        bridge.verify_prepared_bridge(environment[0], environment[1])


@pytest.mark.parametrize("tokens,overflow", [(["合成", "甲", "乙"], True), (["合成"] * 5, False)])
def test_query_unique_token_bound_rejects_without_truncating_or_limiting_tf(environment, monkeypatch, tokens, overflow):
    monkeypatch.setattr(bridge, "MAX_QUERY_TERMS", 2, raising=False)
    response = ready(1) + "T\t2\t" + "\t".join(map(b64, tokens)) + "\nBYE\n"
    retriever = construct(environment, [chunk("x")], backend="bm25s", response=response)
    if overflow:
        with pytest.raises(bridge.SmartCnBridgeError, match="too_many_query_terms"):
            retriever.retrieve("合成")
        assert environment[4]["process"].terminated
    else:
        assert retriever.retrieve("合成")
        retriever.close()
