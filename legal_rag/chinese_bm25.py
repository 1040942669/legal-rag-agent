"""Thin, local Chinese analysis and mature BM25S scoring adapter.

The caller owns corpus authorization, snapshot identity and typed provenance.
This module neither interprets law/article pairs nor certifies legal support.
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
from importlib.metadata import version
import json
import math
from numbers import Real
from pathlib import Path
from typing import Sequence

from .models import Chunk, SearchResult
from .retrieval_contracts import validate_retrieval_top_k


QUERY_TEXT_VERSION = "jieba-unique-lower-alnum-v1"
DOCUMENT_TEXT_VERSION = "jieba-tf-lower-alnum-v1"
INDEX_TEXT_VERSION = "law-titles-article-labels-body-newlines-v1"
CHAR_QUERY_TEXT_VERSION = "sklearn-char-ngrams-unique-lower-alnum-v1"
CHAR_DOCUMENT_TEXT_VERSION = "sklearn-char-ngrams-tf-lower-alnum-v1"
_DEFAULT_HMM = object()


def _parameter(value: Real, name: str, *, lower: float, upper: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < lower or (upper is not None and result > upper):
        raise ValueError(f"{name} is outside its supported range")
    if name == "k1" and result == 0:
        raise ValueError("k1 must be positive")
    return result


@lru_cache(maxsize=1)
def _dependency_identity() -> dict:
    # Installed package resources are fixed for this process. This is not an
    # index/query cache; upgrading dependencies requires a fresh process.
    import jieba

    resources = Path(jieba.__file__).resolve().parent
    return {
        "bm25s_version": version("bm25s"), "jieba_version": version("jieba"),
        "dictionary_sha256": hashlib.sha256((resources / "dict.txt").read_bytes()).hexdigest(),
        "hmm_resource_sha256": {
            name: hashlib.sha256((resources / "finalseg" / name).read_bytes()).hexdigest()
            for name in ("prob_start.py", "prob_trans.py", "prob_emit.py")
        },
    }


def chinese_bm25_identity(
    *, mode: str = "search", k1: float = 1.5, b: float = 0.75,
    hmm: bool | None | object = _DEFAULT_HMM,
) -> dict:
    """Freeze analyzer/engine settings without building an index or reading cases."""
    if not isinstance(mode, str) or mode not in {"precise", "search", "char", "char-bigram"}:
        raise ValueError("mode must be precise, search, char or char-bigram")
    character_mode = mode in {"char", "char-bigram"}
    if hmm is _DEFAULT_HMM:
        hmm = None if character_mode else True
    if character_mode and hmm is not None:
        raise ValueError("hmm is not applicable to character analyzers; use None")
    if not character_mode and type(hmm) is not bool:
        raise ValueError("hmm must be a boolean")
    k1 = _parameter(k1, "k1", lower=0)
    b = _parameter(b, "b", lower=0, upper=1)
    if character_mode:
        return {
            "schema_version": 1,
            "engine": {"name": "bm25s", "version": version("bm25s"),
                       "method": "lucene", "idf_method": "lucene", "k1": k1, "b": b,
                       "backend": "numpy", "dtype": "float64"},
            "tokenizer": {"name": "sklearn", "version": version("scikit-learn"),
                          "implementation": "CountVectorizer.build_analyzer",
                          "mode": mode, "analyzer": "char",
                          "ngram_range": [1, 1] if mode == "char" else [1, 2],
                          "lowercase": True, "strip_accents": None,
                          "preprocessor": None, "tokenizer": None,
                          "stop_words": None, "stop_words_applicable": False,
                          "hmm": None, "hmm_applicable": False,
                          "normalization": "CountVectorizer defaults; keep tokens containing alnum"},
            "index_text_version": INDEX_TEXT_VERSION,
            "query_text_version": CHAR_QUERY_TEXT_VERSION,
            "document_text_version": CHAR_DOCUMENT_TEXT_VERSION,
        }
    resources = _dependency_identity()
    return {
        "schema_version": 1,
        "engine": {"name": "bm25s", "version": resources["bm25s_version"],
                   "method": "lucene", "idf_method": "lucene", "k1": k1, "b": b,
                   "backend": "numpy", "dtype": "float64"},
        "tokenizer": {"name": "jieba", "version": resources["jieba_version"],
                      "mode": mode, "hmm": hmm,
                      "dictionary": "jieba/dict.txt", "dictionary_sha256": resources["dictionary_sha256"],
                      "hmm_resource_sha256": deepcopy(resources["hmm_resource_sha256"]),
                      "stopwords": [], "normalization": "lower; keep tokens containing alnum"},
        "index_text_version": INDEX_TEXT_VERSION,
        "query_text_version": QUERY_TEXT_VERSION,
        "document_text_version": DOCUMENT_TEXT_VERSION,
    }


class ChineseBM25Retriever:
    """Immutable-index retriever; query analysis never grows the dictionary.

    Resource hashes describe installed dependency resources, not a reviewed law
    corpus or a legal-quality guarantee. The fingerprint is configuration-only;
    cache callers must additionally bind their corpus and authorization identity.
    """

    name = "bm25"

    def __init__(
        self,
        chunks: Sequence[Chunk],
        *,
        mode: str = "search",
        k1: float = 1.5,
        b: float = 0.75,
        hmm: bool | None | object = _DEFAULT_HMM,
    ) -> None:
        self._config_identity = chinese_bm25_identity(mode=mode, k1=k1, b=b, hmm=hmm)
        k1 = self._config_identity["engine"]["k1"]
        b = self._config_identity["engine"]["b"]
        self.chunks = tuple(chunks)
        identities = [item.chunk_id for item in self.chunks]
        if any(not isinstance(item, str) or not item.strip() for item in identities) \
                or len(set(identities)) != len(identities):
            raise ValueError("chunk identities must be non-empty and unique")

        # Lazy dependency imports keep historical retrievers independently usable.
        import bm25s

        self._mode = mode
        self._hmm = self._config_identity["tokenizer"]["hmm"]
        self.query_text_version = self._config_identity["query_text_version"]
        self.document_text_version = self._config_identity["document_text_version"]
        canonical = json.dumps(self._config_identity, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        self._fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self._segmenter = None
        self._analyzer = None
        if mode in {"char", "char-bigram"}:
            from sklearn.feature_extraction.text import CountVectorizer

            # Use the mature library's analyzer directly. It is never fitted on
            # questions and has no custom tokenizer, word list or stopword set.
            self._analyzer = CountVectorizer(
                analyzer="char", ngram_range=tuple(self._config_identity["tokenizer"]["ngram_range"]),
            ).build_analyzer()
        else:
            import jieba

            resources = Path(jieba.__file__).resolve().parent
            dictionary = resources / "dict.txt"
            # This is the packaged default dictionary, not a corpus-derived userdict.
            # An explicit path avoids jieba's version-agnostic default jieba.cache.
            self._segmenter = jieba.Tokenizer(dictionary=str(dictionary))
            self._segmenter.initialize()
        titles = {
            alias for item in self.chunks for title in item.law_names
            for alias in (title.strip(), title.strip().removeprefix("中华人民共和国").strip())
            if len(alias) >= 2
        }
        self.known_law_hints = tuple(sorted(titles, key=lambda item: (-len(item), item)))
        self.index_texts = tuple(
            "\n".join((*item.law_names, *item.article_numbers, item.text))
            for item in self.chunks
        )
        self.document_tokens = tuple(tuple(self._tokens(text)) for text in self.index_texts)
        self._engine = None
        if any(self.document_tokens):
            self._engine = bm25s.BM25(method="lucene", idf_method="lucene", k1=k1, b=b,
                                      backend="numpy", dtype="float64")
            self._engine.index([list(tokens) for tokens in self.document_tokens], show_progress=False)

    @property
    def config_identity(self) -> dict:
        return deepcopy(self._config_identity)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def hmm(self) -> bool | None:
        return self._hmm

    @property
    def lexical_profile(self) -> str:
        if self.mode in {"char", "char-bigram"}:
            return f"bm25s-sklearn-{self.mode}-v1"
        return f"bm25s-jieba-{self.mode}-v1"

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def _tokens(self, text: str) -> list[str]:
        if self._analyzer is not None:
            words = self._analyzer(text)
        elif self.mode == "search":
            words = self._segmenter.cut_for_search(text.lower(), HMM=self.hmm)
        else:
            words = self._segmenter.cut(text.lower(), cut_all=False, HMM=self.hmm)
        return [word for word in words if any(character.isalnum() for character in word)]

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        resolved_top_k = validate_retrieval_top_k(top_k)
        if not isinstance(query, str):
            raise ValueError("query must be text")
        if self._engine is None:
            return []
        # Repetition in a query cannot silently multiply a feature's importance.
        # Document repetitions remain intact in the library's TF statistics.
        terms = list(dict.fromkeys(self._tokens(query)))
        if not terms or not any(term in self._engine.vocab_dict for term in terms):
            return []
        scores = self._engine.get_scores(terms)
        if len(scores) != len(self.chunks):
            raise ValueError("BM25 library score count does not match corpus")
        if any(isinstance(score, bool) or not isinstance(score, Real) for score in scores):
            raise ValueError("BM25 library returned an invalid score type")
        values = [float(score) for score in scores]
        if any(not math.isfinite(score) or score < 0 for score in values):
            raise ValueError("BM25 library returned an invalid score")
        # Library scores are authoritative. This ordering adds no scoring formula
        # and resolves ties before truncation, not after an arbitrary top-k cut.
        ranked = sorted((index for index, score in enumerate(values) if score > 0),
                        key=lambda index: (-values[index], self.chunks[index].chunk_id))[:resolved_top_k]
        return [
            SearchResult(
                chunk=self.chunks[index], score=values[index], rank=rank, retriever=self.name,
                trace={"score_kind": "bm25", "engine": "bm25s", "method": "lucene",
                       "lexical_profile": self.lexical_profile,
                       "tokenizer": self._config_identity["tokenizer"]["name"],
                       "tokenizer_mode": self.mode, "hmm": self.hmm,
                       "config_fingerprint": self.fingerprint,
                       "query_text_version": self.query_text_version,
                       "document_text_version": self.document_text_version,
                       "index_text_version": INDEX_TEXT_VERSION},
            )
            for rank, index in enumerate(ranked, start=1)
        ]
