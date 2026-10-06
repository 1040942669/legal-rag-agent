"""Explicit, synthetic integration tests for the experimental Java bridge.

This file is outside the default testpaths. Run it explicitly only after the
pinned public Lucene jars have been obtained and verified.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import queue
import subprocess
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "java" / "SmartCnBridge.java"
DEPENDENCIES = ROOT / ".tmp" / "smartcn-deps" / "9.12.3"
HASHES = {
    "lucene-core-9.12.3.jar": "b64a3f8098a7572034fb30085cdee01b34ec81fb0e5a31b471536af58dc6c01b",
    "lucene-analysis-common-9.12.3.jar": "fa571bd7caf0f0b4faf46a72ca004a7836f348c31d92bd522dddcc3d128d287e",
    "lucene-analysis-smartcn-9.12.3.jar": "06db5436be801787738b9735ebe81622e263826a39dedc66402a99a1c33fab36",
}


def encoded(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def decoded(value: str) -> str:
    return base64.b64decode(value, validate=True).decode("utf-8")


@pytest.fixture(scope="session")
def bridge_classpath(tmp_path_factory):
    assert SOURCE.is_file(), "Experimental Java bridge is not implemented"
    jars = [DEPENDENCIES / name for name in HASHES]
    for jar in jars:
        assert jar.is_file(), f"Missing pinned dependency: {jar.name}"
        assert hashlib.sha256(jar.read_bytes()).hexdigest() == HASHES[jar.name]
    classes = tmp_path_factory.mktemp("smartcn-classes")
    compilation = subprocess.run(
        ["javac", "-encoding", "UTF-8", "-cp", os.pathsep.join(map(str, jars)),
         "-d", str(classes), str(SOURCE)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert compilation.returncode == 0, compilation.stderr
    return os.pathsep.join(map(str, [classes, *jars]))


class Process:
    def __init__(self, classpath: str, mode: str, docs: Path, tokens: Path,
                 k1: str = "1.5", b: str = "0.75"):
        self.child = subprocess.Popen(
            ["java", "-cp", classpath, "SmartCnBridge", mode, str(docs), str(tokens), k1, b],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="strict", bufsize=1,
        )
        self.lines: queue.Queue[str] = queue.Queue()

        def read_lines():
            for line in self.child.stdout:
                self.lines.put(line.rstrip("\r\n"))
            self.lines.put("<EOF>")

        threading.Thread(target=read_lines, daemon=True).start()

    def read(self) -> list[str]:
        try:
            return self.lines.get(timeout=10).split("\t")
        except queue.Empty:
            self.child.kill()
            self.child.wait(timeout=5)
            pytest.fail("Java bridge did not produce a bounded protocol response")

    def send(self, command: str) -> list[str]:
        self.child.stdin.write(command + "\n")
        self.child.stdin.flush()
        return self.read()

    def cleanup(self):
        if self.child.poll() is None:
            assert self.send("QUIT") == ["BYE"]
            assert self.child.wait(timeout=5) == 0
        for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
            stream.close()


@contextmanager
def bridge(classpath, tmp_path, mode="lucene", documents=(), *, raw_docs=None,
           k1="1.5", b="0.75", existing_tokens=False):
    docs = tmp_path / "docs.tsv"
    tokens = tmp_path / "tokens.tsv"
    rows = "".join(encoded(chunk_id) + "\t" + encoded(text) + "\n"
                   for chunk_id, text in documents)
    docs.write_text(rows if raw_docs is None else raw_docs, encoding="utf-8")
    if existing_tokens:
        tokens.write_text("sentinel", encoding="utf-8")
    process = Process(classpath, mode, docs, tokens, k1, b)
    try:
        yield process, tokens
    finally:
        process.cleanup()


def ready(process, count):
    frame = process.read()
    assert frame[0:2] == ["READY", str(count)]
    assert len(frame) == 5 and int(frame[2]) >= 0
    assert int(frame[3]) >= 0 and 0 <= int(frame[4]) <= count
    return int(frame[3]), int(frame[4])


def analysis(process, text):
    frame = process.send("A\t" + encoded(text))
    assert frame[0] == "T" and int(frame[1]) >= 0
    return [decoded(token) for token in frame[2:]]


def search(process, query, top_k=5):
    frame = process.send(f"Q\t{top_k}\t" + encoded(query))
    assert frame[0] == "R" and int(frame[1]) >= 0 and len(frame) % 2 == 0
    pairs = [(decoded(frame[index]), float(frame[index + 1]))
             for index in range(2, len(frame), 2)]
    assert all(math.isfinite(score) and score > 0 for _, score in pairs)
    assert len({chunk_id for chunk_id, _ in pairs}) == len(pairs)
    return pairs


def test_token_export_matches_actual_analysis_preserving_tf(bridge_classpath, tmp_path):
    text = "合成法律 ABC123 2026 alpha alpha 条文 条文。"
    with bridge(bridge_classpath, tmp_path, "tokenize", [("来源甲/条一", text), ("空", "")]) as (process, tokens):
        ready(process, 2)
        rows = [row.split("\t") for row in tokens.read_text(encoding="utf-8").splitlines()]
        assert [decoded(row[0]) for row in rows] == ["来源甲/条一", "空"]
        exported = [decoded(value) for value in rows[0][1:]]
        assert exported == analysis(process, text)
        assert exported and len(exported) > len(set(exported))
        assert len(rows[1]) == 1


@pytest.mark.parametrize("mode", ["lucene", "tokenize"])
def test_zero_docs_and_empty_punctuation_analysis(bridge_classpath, tmp_path, mode):
    with bridge(bridge_classpath, tmp_path, mode) as (process, tokens):
        ready(process, 0)
        assert analysis(process, "") == []
        assert analysis(process, " ，。！？ ") == []
        if mode == "lucene":
            assert search(process, "合成法律") == []
        else:
            assert tokens.read_bytes() == b""


@pytest.mark.parametrize("query", ["", "，。！？", "zzzxxyyunknown"])
def test_empty_and_oov_search_has_no_zero_score_fill(bridge_classpath, tmp_path, query):
    with bridge(bridge_classpath, tmp_path, documents=[("甲", "合成法律 apple")]) as (process, _):
        ready(process, 1)
        assert search(process, query) == []


def test_duplicate_query_terms_do_not_boost_score(bridge_classpath, tmp_path):
    with bridge(bridge_classpath, tmp_path, documents=[("乙", "alpha beta"), ("甲", "alpha alpha beta")]) as (process, _):
        ready(process, 2)
        assert search(process, "alpha") == search(process, "alpha alpha alpha")


def test_tie_resolved_by_full_utf8_chunk_id_before_topk(bridge_classpath, tmp_path):
    ids = ["乙完整来源", "甲完整来源", "a完整来源", "😀完整来源", "𐀀完整来源", "z完整来源", "b完整来源"]
    with bridge(bridge_classpath, tmp_path, documents=[(chunk_id, "alpha 合成法") for chunk_id in ids]) as (process, _):
        ready(process, len(ids))
        all_pairs = search(process, "alpha", len(ids))
        assert [chunk_id for chunk_id, _ in all_pairs] == sorted(ids)
        assert len({score for _, score in all_pairs}) == 1
        assert search(process, "alpha", 2) == all_pairs[:2]


@pytest.mark.parametrize("k1,b", [("NaN", "0.75"), ("Infinity", "0.75"), ("-1", "0.75"),
                                ("1.5", "NaN"), ("1.5", "-0.1"), ("1.5", "1.1")])
def test_nonfinite_or_invalid_bm25_configuration_is_fatal(bridge_classpath, tmp_path, k1, b):
    with bridge(bridge_classpath, tmp_path, k1=k1, b=b) as (process, _):
        assert process.read() == ["ERR", "invalid_parameters"]
        assert process.child.wait(timeout=5) != 0
        assert process.child.stderr.read() == ""


@pytest.mark.parametrize("documents,raw_docs,code", [
    ([("same", "alpha"), ("same", "beta")], None, "duplicate_id"),
    ([("", "alpha")], None, "invalid_document"),
    ([], "@@@\tYWxwaGE=\n", "invalid_encoding"),
    ([], "YWxwaGE=\n", "invalid_document"),
    ([], "/w==\tYWxwaGE=\n", "invalid_encoding"),
])
def test_invalid_document_is_safe_fatal_error(bridge_classpath, tmp_path, documents, raw_docs, code):
    with bridge(bridge_classpath, tmp_path, documents=documents, raw_docs=raw_docs) as (process, _):
        assert process.read() == ["ERR", code]
        assert process.child.wait(timeout=5) != 0
        assert process.child.stderr.read() == ""


def test_token_output_is_exclusive_not_overwritten(bridge_classpath, tmp_path):
    with bridge(bridge_classpath, tmp_path, "tokenize", existing_tokens=True) as (process, tokens):
        assert process.read() == ["ERR", "output_exists"]
        assert process.child.wait(timeout=5) != 0
        assert tokens.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.parametrize("command,code", [("Q\t0\tYWxwaGE=", "invalid_top_k"),
                                         ("Q\t2\t@@@", "invalid_encoding"),
                                         ("UNKNOWN", "invalid_command")])
def test_invalid_command_is_safe_fatal_error(bridge_classpath, tmp_path, command, code):
    with bridge(bridge_classpath, tmp_path, documents=[("私有合成标记", "alpha")]) as (process, _):
        ready(process, 1)
        assert process.send(command) == ["ERR", code]
        assert process.child.wait(timeout=5) != 0
        assert process.child.stderr.read() == ""


def test_input_pipe_eof_terminates_without_orphan(bridge_classpath, tmp_path):
    with bridge(bridge_classpath, tmp_path) as (process, _):
        ready(process, 0)
        process.child.stdin.close()
        assert process.child.wait(timeout=5) == 0
        assert process.read() == ["<EOF>"]


def test_normal_quit_is_acknowledged(bridge_classpath, tmp_path):
    with bridge(bridge_classpath, tmp_path) as (process, _):
        ready(process, 0)
        assert process.send("QUIT") == ["BYE"]
        assert process.child.wait(timeout=5) == 0


def test_native_statistics_equal_exported_token_stream(bridge_classpath, tmp_path):
    documents = [("有词", "alpha alpha 合成法 ABC123 2026"), ("空", ""), ("标点", "，。！？")]
    export_dir = tmp_path / "export"
    lucene_dir = tmp_path / "lucene"
    export_dir.mkdir()
    lucene_dir.mkdir()
    with bridge(bridge_classpath, export_dir, "tokenize", documents) as (process, tokens):
        exported_counts = ready(process, len(documents))
        rows = [row.split("\t") for row in tokens.read_text(encoding="utf-8").splitlines()]
        assert exported_counts == (sum(len(row) - 1 for row in rows), sum(len(row) > 1 for row in rows))
    with bridge(bridge_classpath, lucene_dir, "lucene", documents) as (process, _):
        assert ready(process, len(documents)) == exported_counts


def test_tokenize_mode_does_not_pretend_to_search(bridge_classpath, tmp_path):
    with bridge(bridge_classpath, tmp_path, "tokenize") as (process, _):
        ready(process, 0)
        assert process.send("Q\t1\t" + encoded("alpha")) == ["ERR", "mode_command"]
        assert process.child.wait(timeout=5) != 0


def test_closed_output_pipe_terminates_without_orphan(bridge_classpath, tmp_path):
    docs = tmp_path / "docs.tsv"
    docs.write_text("", encoding="utf-8")
    child = subprocess.Popen(
        ["java", "-cp", bridge_classpath, "SmartCnBridge", "lucene", str(docs),
         str(tmp_path / "unused.tsv"), "1.5", "0.75"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="strict", bufsize=1,
    )
    try:
        assert child.stdout.readline().startswith("READY\t0\t")
        child.stdout.close()
        child.stdin.write("A\t" + encoded("alpha") + "\n")
        child.stdin.flush()
        assert child.wait(timeout=5) != 0
        assert child.stderr.read() == ""
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()


def distinct_query_terms(count):
    terms = []
    for index in range(count):
        value = index
        letters = []
        for _ in range(4):
            letters.append(chr(ord("a") + value % 26))
            value //= 26
        terms.append("q" + "".join(letters) + "q")
    # Sentence delimiters keep the analyzer's bounded input buffer from splitting
    # one artificial long sentence across a word. The cap concerns actual tokens.
    return ". ".join(terms) + "."


@pytest.mark.parametrize("command", ["A", "Q"])
def test_query_unique_overflow_is_rejected_without_truncation(bridge_classpath, tmp_path, command):
    with bridge(bridge_classpath, tmp_path) as (process, _):
        ready(process, 0)
        request = ("A\t" if command == "A" else "Q\t1\t") + encoded(distinct_query_terms(65_537))
        assert process.send(request)[:2] == ["ERR", "too_many_query_terms"]
        assert process.child.wait(timeout=5) != 0


def test_native_query_above_old_default_clause_limit_is_supported(bridge_classpath, tmp_path):
    query = distinct_query_terms(1_100)
    with bridge(bridge_classpath, tmp_path, documents=[("hit", "qaaaaqq alpha")]) as (process, _):
        ready(process, 1)
        assert search(process, query) == []


def test_query_unique_boundary_preserves_analysis_tokens(bridge_classpath, tmp_path):
    with bridge(bridge_classpath, tmp_path, "tokenize") as (process, _):
        ready(process, 0)
        assert len(set(analysis(process, distinct_query_terms(65_536)))) == 65_536
