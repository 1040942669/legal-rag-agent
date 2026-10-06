"""Experiment-only Lucene/SmartCN bridge; no downloads or production wiring.

Compilation may be reused after byte-identity checks. Every retriever builds a
fresh index and retains its input/logs in a caller-owned, non-tracked run folder.
The bridge is not a security sandbox, corpus authorization layer or legal judge.
"""

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
from importlib.metadata import version
import json
import math
from numbers import Real
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Sequence

from legal_rag.chinese_bm25 import INDEX_TEXT_VERSION
from legal_rag.models import Chunk, SearchResult
from legal_rag.retrieval_contracts import validate_retrieval_top_k


ROOT = Path(__file__).resolve().parents[1]
JAVA_SOURCE = ROOT / "scripts/java/SmartCnBridge.java"
LUCENE_VERSION = "9.12.3"
MAX_QUERY_TERMS = 65536
DEPENDENCIES = {
    "lucene-core-9.12.3.jar": "b64a3f8098a7572034fb30085cdee01b34ec81fb0e5a31b471536af58dc6c01b",
    "lucene-analysis-common-9.12.3.jar": "fa571bd7caf0f0b4faf46a72ca004a7836f348c31d92bd522dddcc3d128d287e",
    "lucene-analysis-smartcn-9.12.3.jar": "06db5436be801787738b9735ebe81622e263826a39dedc66402a99a1c33fab36",
}
JAVA_FLAGS = ("-Dfile.encoding=UTF-8", "-Djava.net.useSystemProxies=false")


class SmartCnBridgeError(RuntimeError):
    """Only stable codes escape the subprocess; raw diagnostics stay local."""


def _digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise SmartCnBridgeError("invalid_dependency_file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dependency_files(directory: Path) -> tuple[Path, ...]:
    if directory.is_symlink() or not directory.is_dir():
        raise SmartCnBridgeError("dependency_directory_missing")
    paths = tuple(directory / name for name in DEPENDENCIES)
    if any(_digest(path) != DEPENDENCIES[path.name] for path in paths):
        raise SmartCnBridgeError("dependency_hash_mismatch")
    return paths


def _environment() -> dict[str, str]:
    # No key/private application environment is forwarded, and no Java options
    # or CLASSPATH can inject additional arguments/classes into either tool.
    allowed = {"PATH", "JAVA_HOME", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME",
               "USERPROFILE", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "PATHEXT", "SYSTEMDRIVE"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def _tool(name: str) -> str:
    executable = shutil.which(name, path=_environment().get("PATH"))
    if executable is None:
        raise SmartCnBridgeError("missing_" + name)
    return executable


def _safe_work_path(path: Path) -> Path:
    path = Path(path).resolve()
    if path.is_relative_to(ROOT) and not any(path.is_relative_to(base) for base in
                                           (ROOT / ".tmp", ROOT / "artifacts/experiments")):
        raise ValueError("bridge work must be outside tracked repository paths")
    return path


def _stop_process(process) -> None:
    """Stop descendants even on a successful parent exit; never recursive-delete."""
    import psutil

    records = list(getattr(process, "_smartcn_descendants", ()))
    try:
        parent = getattr(process, "_smartcn_record", None) or psutil.Process(process.pid)
        records.extend(parent.children(recursive=True))
        records.append(parent)
    except psutil.NoSuchProcess:
        pass
    records = list({record.pid: record for record in records}.values())
    for record in records:
        try:
            record.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(records, timeout=2)
    for record in alive:
        try:
            record.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=2)
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        raise SmartCnBridgeError("process_cleanup_failed") from None
    if alive:
        raise SmartCnBridgeError("process_cleanup_failed")


def _remember_process_tree(process):
    import psutil

    try:
        parent = getattr(process, "_smartcn_record", None) or psutil.Process(process.pid)
        process._smartcn_record = parent
        process._smartcn_descendants = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        pass


def _run_logged(args: list[str], log_path: Path, timeout: float) -> None:
    process = None
    with log_path.open("xb") as log:
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, shell=False, env=_environment())
            _remember_process_tree(process)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                raise SmartCnBridgeError("tool_timeout") from None
            if code != 0:
                raise SmartCnBridgeError("tool_failed")
        except OSError:
            raise SmartCnBridgeError("tool_start_failed") from None
        finally:
            if process is not None:
                _stop_process(process)


def _class_hashes(directory: Path) -> dict[str, str]:
    if directory.is_symlink() or not directory.is_dir():
        raise SmartCnBridgeError("compiled_classes_missing")
    paths = sorted(directory.rglob("*"))
    if any(path.is_symlink() or (path.is_file() and path.suffix != ".class") for path in paths):
        raise SmartCnBridgeError("invalid_compiled_classes")
    values = {path.relative_to(directory).as_posix(): _digest(path) for path in paths if path.is_file()}
    if "SmartCnBridge.class" not in values:
        raise SmartCnBridgeError("compiled_classes_missing")
    return values


def _tool_hashes():
    return {name: _digest(Path(_tool(name)).resolve()) for name in ("java", "javac")}


def _version_line(path: Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    if not lines or not re.search(r'(?:version\s+"|javac\s+)17(?:[.\s"]|$)', lines[0]):
        raise SmartCnBridgeError("java17_required")
    return lines[0]


def prepare_bridge(dependency_dir: Path, build_dir: Path, *, timeout: float = 60) -> dict:
    """Compile once into a new local directory, before experiment manifest freeze."""
    timeout = _parameter(timeout, "timeout", positive=True)
    jars = _dependency_files(Path(dependency_dir).resolve())
    source_hash = _digest(JAVA_SOURCE)
    build_dir = _safe_work_path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=False)
    classes = build_dir / "classes"
    classes.mkdir()
    java, javac = _tool("java"), _tool("javac")
    tool_hashes = _tool_hashes()
    _run_logged([java, "-version"], build_dir / "java-version.log", timeout)
    _run_logged([javac, "-version"], build_dir / "javac-version.log", timeout)
    java_version = _version_line(build_dir / "java-version.log")
    javac_version = _version_line(build_dir / "javac-version.log")
    started = time.perf_counter()
    _run_logged([javac, "-encoding", "UTF-8", "-cp", os.pathsep.join(map(str, jars)),
                 "-d", str(classes), str(JAVA_SOURCE)], build_dir / "compile.log", timeout)
    compile_ms = (time.perf_counter() - started) * 1000
    if _digest(JAVA_SOURCE) != source_hash or _dependency_files(Path(dependency_dir).resolve()) != jars \
            or _tool_hashes() != tool_hashes:
        raise SmartCnBridgeError("compile_inputs_changed")
    identity = {
        "schema_version": 1, "protocol": "smartcn-tsv-base64-v1",
        "lucene_version": LUCENE_VERSION, "jar_sha256": dict(DEPENDENCIES),
        "source_sha256": source_hash, "classes_sha256": _class_hashes(classes),
        "java_version": java_version,
        "javac_version": javac_version,
        "tool_executable_sha256": tool_hashes,
        "java_flags": list(JAVA_FLAGS), "compile_ms": compile_ms,
        "environment_policy": "minimal-system-allowlist-no-java-options-classpath-or-application-keys",
    }
    with (build_dir / "runtime.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(identity, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return deepcopy(identity)


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _prepared_identity(dependency_dir: Path, build_dir: Path) -> tuple[dict, tuple[Path, ...]]:
    jars = _dependency_files(dependency_dir)
    receipt = build_dir / "runtime.json"
    try:
        if receipt.is_symlink():
            raise ValueError("linked receipt")
        identity = json.loads(receipt.read_text(encoding="utf-8"), object_pairs_hook=_unique_json)
        valid = (identity["schema_version"] == 1 and identity["protocol"] == "smartcn-tsv-base64-v1"
                 and identity["lucene_version"] == LUCENE_VERSION
                 and identity["jar_sha256"] == DEPENDENCIES
                 and identity["source_sha256"] == _digest(JAVA_SOURCE)
                 and identity["classes_sha256"] == _class_hashes(build_dir / "classes")
                 and identity["tool_executable_sha256"] == _tool_hashes()
                 and identity["java_flags"] == list(JAVA_FLAGS)
                 and identity["environment_policy"] == "minimal-system-allowlist-no-java-options-classpath-or-application-keys"
                 and identity["java_version"] == _version_line(build_dir / "java-version.log")
                 and identity["javac_version"] == _version_line(build_dir / "javac-version.log"))
        _parameter(identity["compile_ms"], "compile_ms")
        if not valid:
            raise ValueError("identity changed")
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        raise SmartCnBridgeError("prepared_identity_mismatch") from None
    return identity, jars


def verify_prepared_bridge(dependency_dir: Path, build_dir: Path) -> dict:
    """Read-only source/JAR/class/JDK validation; never compiles or starts Java."""
    return deepcopy(_prepared_identity(Path(dependency_dir).resolve(), Path(build_dir).resolve())[0])


def _parameter(value, name, *, positive=False, maximum=None):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ValueError(name + " must be finite")
    value = float(value)
    if value < 0 or (positive and value == 0) or (maximum is not None and value > maximum):
        raise ValueError(name + " out of range")
    return value


def _encode(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _decode(value: str) -> str:
    try:
        return base64.b64decode(value, validate=True).decode("utf-8", errors="strict")
    except (ValueError, UnicodeError):
        raise SmartCnBridgeError("invalid_base64_utf8") from None


def _integer(value: str, code: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise SmartCnBridgeError(code)
    return int(value)


class SmartCnRetriever:
    name = "bm25"

    def __init__(self, chunks: Sequence[Chunk], *, backend: str = "lucene",
                 dependency_dir: Path, work_dir: Path, k1: float = 1.5, b: float = .75,
                 prepared_build_dir: Path | None = None, ready_timeout: float = 60,
                 query_timeout: float = 30) -> None:
        if backend not in {"lucene", "bm25s"}:
            raise ValueError("unsupported SmartCN backend")
        k1 = _parameter(k1, "k1", positive=True)
        b = _parameter(b, "b", maximum=1)
        ready_timeout = _parameter(ready_timeout, "ready_timeout", positive=True)
        self.query_timeout = _parameter(query_timeout, "query_timeout", positive=True)
        self.chunks = tuple(chunks)
        ids = [item.chunk_id for item in self.chunks]
        if any(not isinstance(key, str) or not key.strip() for key in ids) or len(set(ids)) != len(ids):
            raise ValueError("chunk identities must be nonempty and unique")
        self._chunks_by_id = dict(zip(ids, self.chunks))
        self._backend = backend
        self._lock = threading.Lock()
        self._responses = queue.Queue()
        self._commands = queue.Queue()
        self._process = None
        self._reader = None
        self._writer = None
        self._stderr = None
        self._closed = False
        self._engine = None
        self.document_tokens = ()
        dependency_dir = Path(dependency_dir).resolve()
        # Validate before creating retained inputs or starting any child process.
        _dependency_files(dependency_dir)
        if prepared_build_dir is not None:
            prepared_build_dir = Path(prepared_build_dir).resolve()
            runtime, jars = _prepared_identity(dependency_dir, prepared_build_dir)
        work_dir = _safe_work_path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir = Path(tempfile.mkdtemp(prefix="smartcn-", dir=work_dir))
        if prepared_build_dir is None:
            prepared_build_dir = self.run_dir / "compiled"
            runtime = prepare_bridge(dependency_dir, prepared_build_dir, timeout=ready_timeout)
            runtime, jars = _prepared_identity(dependency_dir, prepared_build_dir)
            compile_ms = runtime["compile_ms"]
        else:
            compile_ms = 0
        self.timings = {"compile_ms": compile_ms, "prepared_compile_ms": runtime["compile_ms"],
                        "java_startup_and_build_ms": None, "java_build_ns": None,
                        "python_index_ms": 0, "last_engine_ns": None}
        self._config_identity = {
            "schema_version": 1, "runtime": runtime,
            "engine": {"name": "lucene" if backend == "lucene" else "bm25s",
                       "version": LUCENE_VERSION if backend == "lucene" else version("bm25s"),
                       "method": "lucene", "idf_method": "lucene", "k1": k1, "b": b,
                       "backend": "jvm" if backend == "lucene" else "numpy",
                       "dtype": "float32" if backend == "lucene" else "float64",
                       "average_length_denominator": "nonempty-body-documents" if backend == "lucene" else "all-documents",
                       "length_norm": "lucene-intToByte4" if backend == "lucene" else "exact-token-count"},
            "tokenizer": {"name": "SmartChineseAnalyzer", "version": LUCENE_VERSION,
                          "default_stopwords": True, "additional_filter": None,
                          "document_tf_preserved": True, "query_tokens_unique": True,
                          "max_unique_query_terms": MAX_QUERY_TERMS, "overflow": "reject-no-truncation"},
            "index_text_version": INDEX_TEXT_VERSION,
        }
        fingerprint_payload = deepcopy(self._config_identity)
        fingerprint_payload["runtime"].pop("compile_ms")
        self._fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, ensure_ascii=False,
                                                     sort_keys=True, separators=(",", ":"),
                                                     allow_nan=False).encode("utf-8")).hexdigest()
        self.known_law_hints = tuple(sorted({name for item in self.chunks for title in item.law_names
                                          for name in (title.strip(), title.strip().removeprefix("中华人民共和国").strip())
                                          if len(name) >= 2}, key=lambda name: (-len(name), name)))
        self.index_texts = tuple("\n".join((*item.law_names, *item.article_numbers, item.text)) for item in self.chunks)
        docs, tokens = self.run_dir / "docs.tsv", self.run_dir / "tokens.tsv"
        with docs.open("x", encoding="utf-8", newline="\n") as stream:
            for item, text in zip(self.chunks, self.index_texts):
                stream.write(_encode(item.chunk_id) + "\t" + _encode(text) + "\n")
        mode = "lucene" if backend == "lucene" else "tokenize"
        command = [_tool("java"), *JAVA_FLAGS, "-cp", os.pathsep.join((str(prepared_build_dir / "classes"), *map(str, jars))),
                   "SmartCnBridge", mode, str(docs), str(tokens), str(k1), str(b)]
        started = time.perf_counter()
        try:
            self._stderr = (self.run_dir / "java.stderr.log").open("xb")
            self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=self._stderr, text=True, encoding="utf-8", errors="strict",
                                             bufsize=1, shell=False, env=_environment())
            _remember_process_tree(self._process)
            self._reader = threading.Thread(target=self._read_stdout, daemon=True)
            self._reader.start()
            self._writer = threading.Thread(target=self._write_stdin, daemon=True)
            self._writer.start()
            ready = self._read(ready_timeout, "ready_timeout")
            if len(ready) != 5 or ready[0] != "READY":
                raise SmartCnBridgeError("invalid_ready")
            count, build_ns, total, nonempty = [_integer(value, "invalid_ready") for value in ready[1:]]
            if count != len(self.chunks):
                raise SmartCnBridgeError("ready_count_mismatch")
            if nonempty > count or total < nonempty or (not nonempty and total):
                raise SmartCnBridgeError("invalid_ready")
            self.index_stats = {"document_count": count, "total_tokens": total,
                                "nonempty_document_count": nonempty, "zero_token_document_count": count - nonempty,
                                "average_nonempty_field_length": total / nonempty if nonempty else 0}
            self.timings["java_startup_and_build_ms"] = (time.perf_counter() - started) * 1000
            self.timings["java_build_ns"] = build_ns
            if backend == "bm25s":
                self._build_bm25s(tokens, k1, b)
        except BaseException as exc:
            self._abort()
            if isinstance(exc, (SmartCnBridgeError, KeyboardInterrupt, SystemExit)):
                raise
            raise SmartCnBridgeError("bridge_start_failed") from None

    @property
    def config_identity(self):
        return deepcopy(self._config_identity)

    @property
    def fingerprint(self):
        return self._fingerprint

    @property
    def subprocess_pids(self):
        return (self._process.pid,) if self._process is not None else ()

    def _read_stdout(self):
        try:
            for line in self._process.stdout:
                self._responses.put(line.rstrip("\r\n").split("\t"))
        except (UnicodeError, OSError, ValueError):
            self._responses.put(["ERR", "stdout_read_failed"])
        finally:
            self._responses.put(None)

    def _write_stdin(self):
        while True:
            request = self._commands.get()
            if request is None:
                return
            command, completion = request
            try:
                self._process.stdin.write(command + "\n")
                self._process.stdin.flush()
                completion.put(True)
            except (OSError, ValueError):
                completion.put(False)

    def _send(self, command, timeout):
        completion = queue.Queue(maxsize=1)
        self._commands.put((command, completion))
        try:
            if not completion.get(timeout=timeout):
                raise SmartCnBridgeError("stdin_write_failed")
        except queue.Empty:
            raise SmartCnBridgeError("query_timeout") from None

    def _read(self, timeout, timeout_code):
        try:
            row = self._responses.get(timeout=timeout)
        except queue.Empty:
            raise SmartCnBridgeError(timeout_code) from None
        if row is None:
            raise SmartCnBridgeError("unexpected_eof")
        if row and row[0] == "ERR":
            code = row[1] if len(row) == 2 and re.fullmatch(r"[a-z0-9_]{1,64}", row[1]) else "invalid_error"
            raise SmartCnBridgeError("java_error_" + code)
        return row

    def _build_bm25s(self, path, k1, b):
        if path.is_symlink() or not path.is_file():
            raise SmartCnBridgeError("invalid_tokens_file")
        documents = []
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                parts = line.rstrip("\r\n").split("\t")
                index = len(documents)
                if index >= len(self.chunks) or _decode(parts[0]) != self.chunks[index].chunk_id:
                    raise SmartCnBridgeError("token_identity_mismatch")
                document = tuple(_decode(token) for token in parts[1:])
                if any(not token for token in document):
                    raise SmartCnBridgeError("invalid_token")
                documents.append(document)
        if len(documents) != len(self.chunks) or sum(map(len, documents)) != self.index_stats["total_tokens"] \
                or sum(bool(tokens) for tokens in documents) != self.index_stats["nonempty_document_count"]:
            raise SmartCnBridgeError("token_statistics_mismatch")
        self.document_tokens = tuple(documents)
        started = time.perf_counter()
        if any(documents):
            import bm25s

            if version("bm25s") != "0.3.9":
                raise SmartCnBridgeError("bm25s_version_mismatch")
            self._engine = bm25s.BM25(method="lucene", idf_method="lucene", k1=k1, b=b,
                                      backend="numpy", dtype="float64")
            self._engine.index([list(document) for document in documents], show_progress=False)
        self.timings["python_index_ms"] = (time.perf_counter() - started) * 1000

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        top_k = validate_retrieval_top_k(top_k)
        if not isinstance(query, str):
            raise ValueError("query must be text")
        with self._lock:
            if self._closed:
                raise SmartCnBridgeError("closed")
            try:
                command = f"Q\t{top_k}\t{_encode(query)}" if self._backend == "lucene" else "A\t" + _encode(query)
                deadline = time.monotonic() + self.query_timeout
                self._send(command, self.query_timeout)
                response = self._read(max(0, deadline - time.monotonic()), "query_timeout")
                expected = "R" if self._backend == "lucene" else "T"
                if len(response) < 2 or response[0] != expected:
                    raise SmartCnBridgeError("invalid_query_response")
                self.timings["last_engine_ns"] = _integer(response[1], "invalid_engine_time")
                if self._backend == "lucene":
                    if len(response[2:]) % 2:
                        raise SmartCnBridgeError("invalid_query_response")
                    pairs = [(_decode(response[index]), float(response[index + 1])) for index in range(2, len(response), 2)]
                    if len(pairs) > top_k or len({key for key, _ in pairs}) != len(pairs) \
                            or any(key not in self._chunks_by_id or not math.isfinite(score) or score <= 0 for key, score in pairs):
                        raise SmartCnBridgeError("invalid_result")
                    if pairs != sorted(pairs, key=lambda item: (-item[1], item[0].encode("utf-8"))):
                        raise SmartCnBridgeError("invalid_result_sort")
                else:
                    terms = list(dict.fromkeys(_decode(token) for token in response[2:]))
                    if len(terms) > MAX_QUERY_TERMS:
                        raise SmartCnBridgeError("too_many_query_terms")
                    if any(not token for token in terms):
                        raise SmartCnBridgeError("invalid_token")
                    if not terms or self._engine is None or not any(term in self._engine.vocab_dict for term in terms):
                        return []
                    started = time.perf_counter_ns()
                    raw_scores = self._engine.get_scores(terms)
                    self.timings["last_python_score_ns"] = time.perf_counter_ns() - started
                    if len(raw_scores) != len(self.chunks) or any(isinstance(score, bool) or not isinstance(score, Real)
                                                               or not math.isfinite(float(score)) or score < 0 for score in raw_scores):
                        raise SmartCnBridgeError("invalid_bm25s_score")
                    pairs = sorted(((item.chunk_id, float(score)) for item, score in zip(self.chunks, raw_scores) if score > 0),
                                   key=lambda item: (-item[1], item[0].encode("utf-8")))[:top_k]
                return [SearchResult(self._chunks_by_id[key], score, rank, self.name,
                                     trace={"score_kind": "bm25", "engine": self._config_identity["engine"]["name"],
                                            "tokenizer": "SmartChineseAnalyzer", "config_fingerprint": self.fingerprint,
                                            "index_text_version": INDEX_TEXT_VERSION})
                        for rank, (key, score) in enumerate(pairs, 1)]
            except BaseException as exc:
                self._abort()
                if isinstance(exc, (SmartCnBridgeError, KeyboardInterrupt, SystemExit)):
                    raise
                raise SmartCnBridgeError("query_protocol_failed") from None

    def _abort(self):
        self._closed = True
        try:
            if self._process is not None:
                _stop_process(self._process)
        finally:
            self._commands.put(None)
            if self._process is not None:
                for stream in (self._process.stdin, self._process.stdout):
                    if stream is not None:
                        stream.close()
            if self._stderr is not None:
                self._stderr.close()
            if self._reader is not None:
                self._reader.join(timeout=1)
            if self._writer is not None:
                self._writer.join(timeout=1)

    def close(self):
        with self._lock:
            if self._closed:
                return
            try:
                _remember_process_tree(self._process)
                self._send("QUIT", min(self.query_timeout, 2))
                if self._read(min(self.query_timeout, 2), "close_timeout") != ["BYE"]:
                    raise SmartCnBridgeError("invalid_close_response")
                if self._process.wait(timeout=2) != 0:
                    raise SmartCnBridgeError("java_exit_failed")
            except (OSError, ValueError, subprocess.TimeoutExpired):
                raise SmartCnBridgeError("close_failed") from None
            finally:
                self._abort()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.close()
        else:
            self._abort()
