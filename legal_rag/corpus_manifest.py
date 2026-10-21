"""Pinned corpus manifest: the exact law files the frozen version uses.

The repository never ships law text.  It ships this manifest instead: one
Hugging Face dataset revision plus the size and SHA-256 of every file, so a
local copy can be fetched and verified byte for byte.  A directory that
differs in any file is not the frozen corpus, whatever its name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

MANIFEST_SCHEMA_VERSION = 1
DEFAULT_MANIFEST_PATH = "configs/corpus/kuugo_chinese_law_a829f47d.manifest.json"
DEFAULT_ENDPOINT = "https://huggingface.co"
FETCH_TIMEOUT_SECONDS = 60
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REVISION_RE = re.compile(r"[0-9a-f]{40}")

Fetcher = Callable[[str], bytes]


class CorpusManifestError(ValueError):
    """The manifest is malformed or a corpus copy cannot be made to match it."""


@dataclass(frozen=True)
class CorpusFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class CorpusManifest:
    corpus_id: str
    repo_id: str
    revision: str
    files: tuple[CorpusFile, ...]
    source: Mapping[str, Any]

    def download_url(self, path: str, endpoint: str = DEFAULT_ENDPOINT) -> str:
        return (
            f"{endpoint.rstrip('/')}/datasets/{self.repo_id}/resolve/"
            f"{self.revision}/{urllib.parse.quote(path)}"
        )


@dataclass(frozen=True)
class CorpusVerification:
    corpus_id: str
    revision: str
    file_count: int
    missing: tuple[str, ...]
    modified: tuple[str, ...]
    extra: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (self.missing or self.modified or self.extra)

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_id": self.corpus_id,
            "revision": self.revision,
            "ok": self.ok,
            "file_count": self.file_count,
            "missing": list(self.missing),
            "modified": list(self.modified),
            "extra": list(self.extra),
        }


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_corpus_manifest(path: str | Path) -> CorpusManifest:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusManifestError(f"cannot read corpus manifest {path}: {exc}") from exc
    return parse_corpus_manifest(payload)


def parse_corpus_manifest(payload: Any) -> CorpusManifest:
    if not isinstance(payload, dict):
        raise CorpusManifestError("corpus manifest must be a JSON object")
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise CorpusManifestError("unsupported corpus manifest schema_version")
    corpus_id = payload.get("corpus_id")
    source = payload.get("source")
    if not isinstance(corpus_id, str) or not corpus_id:
        raise CorpusManifestError("corpus_id must be a non-empty string")
    if not isinstance(source, dict):
        raise CorpusManifestError("source must be an object")
    if source.get("provider") != "huggingface" or source.get("repo_type") != "dataset":
        raise CorpusManifestError("only Hugging Face dataset sources are supported")
    repo_id = source.get("repo_id")
    revision = source.get("revision")
    if not isinstance(repo_id, str) or repo_id.count("/") != 1:
        raise CorpusManifestError("source.repo_id must look like owner/name")
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise CorpusManifestError("source.revision must be a full 40-character commit")
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        raise CorpusManifestError("files must be a non-empty list")
    files: list[CorpusFile] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise CorpusManifestError("every file entry must be an object")
        name, size, digest = entry.get("path"), entry.get("size"), entry.get("sha256")
        if not _is_plain_file_name(name):
            raise CorpusManifestError(f"file path must be a plain .txt name: {name!r}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise CorpusManifestError(f"file size must be a non-negative integer: {name}")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise CorpusManifestError(f"file sha256 must be 64 lowercase hex: {name}")
        files.append(CorpusFile(name, size, digest))
    if len({item.path for item in files}) != len(files):
        raise CorpusManifestError("file paths must be unique")
    if payload.get("file_count") != len(files):
        raise CorpusManifestError("file_count does not match the file list")
    return CorpusManifest(corpus_id, repo_id, revision, tuple(files), dict(source))


def _is_plain_file_name(name: Any) -> bool:
    return (
        isinstance(name, str)
        and name.endswith(".txt")
        and not name.startswith(".")
        and Path(name).name == name
        and "/" not in name
        and "\\" not in name
    )


def build_corpus_manifest(
    data_dir: str | Path, *, corpus_id: str, source: Mapping[str, Any]
) -> dict[str, Any]:
    """Describe every ``*.txt`` in ``data_dir``; used to pin a verified copy."""

    paths = sorted(path for path in Path(data_dir).glob("*.txt") if path.is_file())
    files = [
        {"path": path.name, "size": path.stat().st_size, "sha256": file_sha256(path)}
        for path in paths
    ]
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "corpus_id": corpus_id,
        "source": dict(source),
        "file_count": len(files),
        "total_bytes": sum(item["size"] for item in files),
        "files": files,
    }
    parse_corpus_manifest(payload)
    return payload


def verify_corpus(data_dir: str | Path, manifest: CorpusManifest) -> CorpusVerification:
    """Compare the ``*.txt`` files the loader would read with the manifest."""

    root = Path(data_dir)
    present = (
        {path.name: path for path in root.glob("*.txt") if path.is_file()}
        if root.is_dir()
        else {}
    )
    missing: list[str] = []
    modified: list[str] = []
    for entry in manifest.files:
        path = present.get(entry.path)
        if path is None:
            missing.append(entry.path)
        elif path.stat().st_size != entry.size or file_sha256(path) != entry.sha256:
            modified.append(entry.path)
    expected = {entry.path for entry in manifest.files}
    return CorpusVerification(
        corpus_id=manifest.corpus_id,
        revision=manifest.revision,
        file_count=len(manifest.files),
        missing=tuple(missing),
        modified=tuple(modified),
        extra=tuple(sorted(name for name in present if name not in expected)),
    )


def fetch_corpus(
    manifest: CorpusManifest,
    output_dir: str | Path,
    *,
    endpoint: str | None = None,
    fetch: Fetcher | None = None,
    overwrite: bool = False,
) -> CorpusVerification:
    """Download missing files of the pinned revision and verify each one.

    A local file that differs from the manifest is only replaced with
    ``overwrite=True``; extra files are never touched and keep the copy from
    verifying until the caller removes them.
    """

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    before = verify_corpus(root, manifest)
    if before.modified and not overwrite:
        raise CorpusManifestError(
            f"{len(before.modified)} files differ from the manifest, e.g. "
            f"{before.modified[0]}; use a fresh directory or overwrite=True"
        )
    resolved_endpoint = endpoint or os.environ.get("HF_ENDPOINT") or DEFAULT_ENDPOINT
    resolved_fetch = fetch or _urlopen_fetch
    wanted = set(before.missing) | set(before.modified)
    for entry in manifest.files:
        if entry.path not in wanted:
            continue
        data = resolved_fetch(manifest.download_url(entry.path, resolved_endpoint))
        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise CorpusManifestError(f"downloaded {entry.path} does not match the manifest")
        partial = root / f"{entry.path}.part"
        try:
            partial.write_bytes(data)
            os.replace(partial, root / entry.path)
        finally:
            partial.unlink(missing_ok=True)
    return verify_corpus(root, manifest)


def _urlopen_fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
        return response.read()
