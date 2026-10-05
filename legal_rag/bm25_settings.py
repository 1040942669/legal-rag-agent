"""Frozen effective retrieval settings, independent of any corpus or index."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from numbers import Real
from typing import Mapping, Sequence

from .chinese_bm25 import ChineseBM25Retriever, chinese_bm25_identity
from .models import Chunk


LEGACY_BM25_PROFILES = ("legacy-v1", "local-lexical-v2", "generic-v3")
MODERN_BM25_PROFILES = ("bm25s-jieba-precise-v1", "bm25s-jieba-search-v1",
                       "bm25s-sklearn-char-v1", "bm25s-sklearn-char-bigram-v1")
_MODERN_MODES = dict(zip(MODERN_BM25_PROFILES, ("precise", "search", "char", "char-bigram")))
BM25_PROFILES = (*LEGACY_BM25_PROFILES, *MODERN_BM25_PROFILES)
# Selection is explicit until the pre-registered comparison has completed.
DEFAULT_BM25_SETTINGS_PROFILE = "legacy-v1"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value: object, name: str, *, minimum: float = 0, maximum: float = 1_000_000) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum or name == "k1" and result == 0:
        raise ValueError(f"invalid {name}")
    return result


@dataclass(frozen=True, slots=True)
class BM25Settings:
    lexical_profile: str = DEFAULT_BM25_SETTINGS_PROFILE
    k1: float = 1.5
    b: float = 0.75
    hmm: bool | None = None
    law_boost: float | None = None
    article_boost: float | None = None
    deprecated_penalty: float | None = None
    _identity_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.lexical_profile not in BM25_PROFILES or not isinstance(self.lexical_profile, str):
            raise ValueError("unapproved BM25 profile")
        character = self.lexical_profile in {"bm25s-sklearn-char-v1", "bm25s-sklearn-char-bigram-v1"}
        if character:
            if self.hmm is not None:
                raise ValueError("character analyzers do not support HMM configuration")
        else:
            object.__setattr__(self, "hmm", True if self.hmm is None else self.hmm)
            if type(self.hmm) is not bool:
                raise ValueError("hmm must be boolean")
        object.__setattr__(self, "k1", _number(self.k1, "k1"))
        object.__setattr__(self, "b", _number(self.b, "b", maximum=1))
        modern = self.lexical_profile in MODERN_BM25_PROFILES
        for name, default in (("law_boost", 0 if modern else 40),
                              ("article_boost", 0 if modern else 80), ("deprecated_penalty", 1)):
            value = default if getattr(self, name) is None else getattr(self, name)
            object.__setattr__(self, name, _number(value, name, maximum=1 if name == "deprecated_penalty" else 1_000_000))
        if modern:
            if self.law_boost != 0 or self.article_boost != 0 or self.deprecated_penalty != 1:
                raise ValueError("modern BM25 does not apply legacy boosts or deprecated multipliers")
            identity = chinese_bm25_identity(mode=_MODERN_MODES[self.lexical_profile],
                                             k1=self.k1, b=self.b, hmm=self.hmm)
        else:
            if not self.hmm:
                raise ValueError("historical BM25 does not have a configurable jieba analyzer")
            from .retrieval import bm25_text_versions
            query_version, document_version = bm25_text_versions(self.lexical_profile)
            identity = {"schema_version": 1,
                        "engine": {"name": "legacy-custom-bm25", "version": "legacy-formula-v1", "k1": self.k1, "b": self.b,
                                   "law_boost": self.law_boost, "article_boost": self.article_boost,
                                   "deprecated_penalty": self.deprecated_penalty},
                        "lexical_profile": self.lexical_profile,
                        "query_text_version": query_version, "document_text_version": document_version}
        object.__setattr__(self, "_identity_json", _json(identity))

    @property
    def identity(self) -> dict:
        return json.loads(self._identity_json)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_json(self.to_dict()).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {"schema_version": 1, "lexical_profile": self.lexical_profile,
                "parameters": {"k1": self.k1, "b": self.b, "hmm": self.hmm,
                               "law_boost": self.law_boost, "article_boost": self.article_boost,
                               "deprecated_penalty": self.deprecated_penalty}, "identity": self.identity}

    @classmethod
    def from_dict(cls, value: dict) -> BM25Settings:
        if not isinstance(value, dict) or set(value) != {"schema_version", "lexical_profile", "parameters", "identity"} \
                or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("invalid BM25 settings fields")
        parameters = value["parameters"]
        if not isinstance(parameters, dict) or set(parameters) != {
            "k1", "b", "hmm", "law_boost", "article_boost", "deprecated_penalty",
        }:
            raise ValueError("invalid BM25 parameter fields")
        settings = cls(lexical_profile=value["lexical_profile"], **parameters)
        if _json(settings.to_dict()) != _json(value):
            raise ValueError("BM25 configuration identity drift")
        return settings

    @classmethod
    def from_config(cls, retrieval: Mapping[str, object]) -> BM25Settings:
        if not isinstance(retrieval, Mapping):
            raise ValueError("retrieval configuration must be a mapping")
        return cls(lexical_profile=retrieval.get("bm25_lexical_profile", DEFAULT_BM25_SETTINGS_PROFILE),
                   k1=retrieval.get("bm25_k1", 1.5), b=retrieval.get("bm25_b", 0.75),
                   hmm=retrieval.get("bm25_hmm"), law_boost=retrieval.get("bm25_law_boost"),
                   article_boost=retrieval.get("bm25_article_boost"),
                   deprecated_penalty=retrieval.get("deprecated_penalty"))

    def for_profile(self, profile: str) -> BM25Settings:
        if profile == self.lexical_profile:
            return self
        if profile in MODERN_BM25_PROFILES:
            hmm = None if profile.startswith("bm25s-sklearn-") else self.hmm
            return BM25Settings(lexical_profile=profile, k1=self.k1, b=self.b, hmm=hmm)
        return BM25Settings(lexical_profile=profile, k1=self.k1, b=self.b,
                            law_boost=self.law_boost if self.lexical_profile in LEGACY_BM25_PROFILES else None,
                            article_boost=self.article_boost if self.lexical_profile in LEGACY_BM25_PROFILES else None,
                            deprecated_penalty=self.deprecated_penalty if self.lexical_profile in LEGACY_BM25_PROFILES else None)


def build_bm25_retriever(chunks: Sequence[Chunk], settings: BM25Settings):
    if not isinstance(settings, BM25Settings):
        raise ValueError("BM25 builder requires frozen settings")
    # Decode checks the actual installed dependency/resource identity before
    # constructing an index; no stored profile silently gains a new analyzer.
    settings = BM25Settings.from_dict(settings.to_dict())
    if settings.lexical_profile in MODERN_BM25_PROFILES:
        return ChineseBM25Retriever(chunks, mode=settings.identity["tokenizer"]["mode"],
                                     k1=settings.k1, b=settings.b, hmm=settings.hmm)
    from .retrieval import BM25Retriever
    return BM25Retriever(list(chunks), k1=settings.k1, b=settings.b, law_boost=settings.law_boost,
                         article_boost=settings.article_boost, deprecated_penalty=settings.deprecated_penalty,
                         lexical_profile=settings.lexical_profile)


def resolve_bm25_settings(retrieval: Mapping[str, object] | None = None) -> BM25Settings:
    """One public effective source for new runs, not historical replay defaults.

    Reading the in-code public configuration never loads dotenv or credentials.
    Explicit retrieval mappings remain authoritative and are strictly checked.
    """
    if retrieval is None:
        from .config import load_config
        retrieval = load_config()["retrieval"]
    return BM25Settings.from_config(retrieval)
