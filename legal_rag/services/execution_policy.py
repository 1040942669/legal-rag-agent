"""Server-owned, serializable execution authority. Never contains credentials."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, fields, replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from legal_rag.bm25_settings import BM25Settings, BM25_PROFILES, LEGACY_BM25_PROFILES, MODERN_BM25_PROFILES, resolve_bm25_settings


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, (float, bool)):
        raise ValueError(f"{name} must be an exact decimal")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"invalid {name}") from None
    if not result.is_finite() or result < 0 or result > Decimal("1000000"):
        raise ValueError(f"invalid {name}")
    if result.as_tuple().exponent < -6:
        raise ValueError(f"{name} supports at most six decimal places")
    return result


def policy_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class GenerationPolicy:
    enabled: bool = False
    provider: str = "siliconflow"
    model: str = ""
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key_env: str = "SILICONFLOW_API_KEY"
    allowed_scope_ids: tuple[str, ...] = ()
    pricing_acknowledged: bool = False
    egress_acknowledged: bool = False
    price_revision: str = ""
    currency: str = "CNY"
    input_rate: Decimal | str = "0"
    output_rate: Decimal | str = "0"
    budget: Decimal | str = "0"
    max_prompt_bytes: int = 16000
    prompt_overhead_tokens: int = 256
    max_output_tokens: int = 512

    def __post_init__(self) -> None:
        for name in ("enabled", "pricing_acknowledged", "egress_acknowledged"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        for name in ("input_rate", "output_rate", "budget"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        for name, maximum in (("max_prompt_bytes", 100000), ("prompt_overhead_tokens", 4096),
                              ("max_output_tokens", 32768)):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= maximum:
                raise ValueError(f"invalid {name}")
        if self.provider != "siliconflow" or self.base_url != "https://api.siliconflow.cn/v1" or self.currency != "CNY":
            raise ValueError("unapproved completion destination or currency")
        if not isinstance(self.api_key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", self.api_key_env):
            raise ValueError("invalid credential environment reference")
        if not isinstance(self.allowed_scope_ids, tuple) or any(
            not isinstance(scope, str) or not scope or scope != scope.strip()
            for scope in self.allowed_scope_ids
        ) or len(set(self.allowed_scope_ids)) != len(self.allowed_scope_ids):
            raise ValueError("invalid egress scope allowlist")
        if any(not isinstance(value, str) or len(value) > 255
               or any(ord(character) < 32 or ord(character) == 127 for character in value)
               for value in (self.model, self.price_revision)):
            raise ValueError("invalid pricing identity")
        if self.enabled and not (
            self.model and self.model == self.model.strip() and self.price_revision
            and self.allowed_scope_ids and self.pricing_acknowledged and self.egress_acknowledged
            and self.budget > 0 and self.input_rate > 0 and self.output_rate > 0
        ):
            raise ValueError("enabled generation requires complete acknowledged policy")

    def reservation(self, prompt: str) -> Decimal:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > self.max_prompt_bytes:
            raise ValueError("prompt preflight rejected")
        return self.cost(len(prompt.encode("utf-8")) + self.prompt_overhead_tokens, self.max_output_tokens).quantize(
            Decimal("0.000000000001"), rounding=ROUND_CEILING,
        )

    def cost(self, inputs: int, outputs: int) -> Decimal:
        if type(inputs) is not int or type(outputs) is not int or min(inputs, outputs) < 0:
            raise ValueError("invalid usage")
        return (Decimal(inputs) * self.input_rate + Decimal(outputs) * self.output_rate) / Decimal(1000000)

    def to_dict(self) -> dict:
        value = {item.name: getattr(self, item.name) for item in fields(self)}
        for name in ("input_rate", "output_rate", "budget"):
            value[name] = str(value[name])
        value["allowed_scope_ids"] = list(self.allowed_scope_ids)
        return value

    @classmethod
    def from_dict(cls, value: dict) -> GenerationPolicy:
        if not isinstance(value, dict) or set(value) != {item.name for item in fields(cls)}:
            raise ValueError("invalid generation policy fields")
        copied = dict(value)
        if not isinstance(copied["allowed_scope_ids"], list):
            raise ValueError("invalid egress scope list")
        copied["allowed_scope_ids"] = tuple(copied["allowed_scope_ids"])
        return cls(**copied)


@dataclass(frozen=True, slots=True)
class ServiceExecutionPolicy:
    lexical_profile: str | None = None
    allowed_lexical_profiles: tuple[str, ...] | None = None
    exact_reference_routing: bool = True
    generation: GenerationPolicy = GenerationPolicy()
    semantic_policy: object | None = None
    schema_version: int = 2
    bm25_settings: BM25Settings | None = None
    evidence_rules_version: str | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in {1, 2} or type(self.exact_reference_routing) is not bool:
            raise ValueError("invalid execution policy contract")
        public_settings = None
        if self.lexical_profile is None:
            if self.schema_version == 1:
                object.__setattr__(self, "lexical_profile", "legacy-v1")
            elif self.bm25_settings is not None:
                if not isinstance(self.bm25_settings, BM25Settings):
                    raise ValueError("invalid explicit BM25 settings")
                object.__setattr__(self, "lexical_profile", self.bm25_settings.lexical_profile)
            else:
                public_settings = resolve_bm25_settings()
                object.__setattr__(self, "lexical_profile", public_settings.lexical_profile)
        profiles = LEGACY_BM25_PROFILES if self.schema_version == 1 else BM25_PROFILES
        if self.allowed_lexical_profiles is None:
            object.__setattr__(self, "allowed_lexical_profiles", ("legacy-v1", "generic-v3") if self.schema_version == 1
                               else ("legacy-v1", "generic-v3", *MODERN_BM25_PROFILES))
        if self.lexical_profile not in profiles:
            raise ValueError("unapproved lexical profile")
        if not isinstance(self.allowed_lexical_profiles, tuple) or self.lexical_profile not in self.allowed_lexical_profiles or any(
            profile not in profiles for profile in self.allowed_lexical_profiles
        ) or len(set(self.allowed_lexical_profiles)) != len(self.allowed_lexical_profiles):
            raise ValueError("invalid lexical selector allowlist")
        if self.schema_version == 1:
            if self.bm25_settings is not None or self.evidence_rules_version not in {None, "general-reference-v2"}:
                raise ValueError("historical policy cannot acquire new retrieval authority")
            object.__setattr__(self, "evidence_rules_version", "general-reference-v2")
        else:
            settings = self.bm25_settings or (public_settings or resolve_bm25_settings()).for_profile(self.lexical_profile)
            if not isinstance(settings, BM25Settings) or settings.lexical_profile != self.lexical_profile:
                raise ValueError("BM25 settings must match the selected profile")
            object.__setattr__(self, "bm25_settings", settings)
            rules = self.evidence_rules_version or "general-reference-v3"
            if rules not in {"general-reference-v2", "general-reference-v3"}:
                raise ValueError("unapproved evidence rules")
            object.__setattr__(self, "evidence_rules_version", rules)
        if not isinstance(self.generation, GenerationPolicy):
            raise ValueError("invalid generation policy")
        if self.semantic_policy is not None:
            from legal_rag.semantic import SemanticPolicy
            policy = self.semantic_policy if isinstance(self.semantic_policy, SemanticPolicy) else SemanticPolicy.from_dict(self.semantic_policy)
            if not self.generation.enabled:
                raise ValueError("semantic checking requires acknowledged generation authority")
            # Store the immutable typed contract internally, not an aliased caller dict.
            object.__setattr__(self, "semantic_policy", policy)

    def to_dict(self) -> dict:
        value = {"schema_version": self.schema_version, "lexical_profile": self.lexical_profile,
                "allowed_lexical_profiles": list(self.allowed_lexical_profiles),
                "exact_reference_routing": self.exact_reference_routing,
                "generation": self.generation.to_dict(),
                "semantic_policy": self.semantic_policy.to_dict() if self.semantic_policy else None}
        if self.schema_version == 2:
            value.update(bm25_settings=self.resolved_bm25_settings.to_dict(),
                         evidence_rules_version=self.evidence_rules_version)
        return value

    @property
    def resolved_bm25_settings(self) -> BM25Settings:
        # The old service factory used the BM25 constructor multiplier 1.0,
        # not CLI YAML's historical 0.5. Never import current defaults here.
        return self.bm25_settings or BM25Settings(lexical_profile=self.lexical_profile, deprecated_penalty=1)

    def with_lexical_profile(self, selector: str) -> ServiceExecutionPolicy:
        if selector not in self.allowed_lexical_profiles:
            raise ValueError("lexical selector is not allowed by deployment")
        return replace(self, lexical_profile=selector,
                       bm25_settings=self.resolved_bm25_settings.for_profile(selector) if self.schema_version == 2 else None)

    @property
    def fingerprint(self) -> str:
        return policy_hash(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict) -> ServiceExecutionPolicy:
        original_fields = {"schema_version", "lexical_profile", "allowed_lexical_profiles", "exact_reference_routing", "generation", "semantic_policy"}
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] not in {1, 2}:
            raise ValueError("invalid execution policy fields")
        if not isinstance(value.get("lexical_profile"), str):
            raise ValueError("serialized lexical profile identity must be explicit")
        modern = value["schema_version"] == 2
        if set(value) != (original_fields | {"bm25_settings", "evidence_rules_version"} if modern else original_fields):
            raise ValueError("invalid execution policy fields")
        if not isinstance(value["allowed_lexical_profiles"], list):
            raise ValueError("invalid lexical selector list")
        return cls(lexical_profile=value["lexical_profile"], exact_reference_routing=value["exact_reference_routing"],
                   allowed_lexical_profiles=tuple(value["allowed_lexical_profiles"]),
                   generation=GenerationPolicy.from_dict(value["generation"]),
                   semantic_policy=value["semantic_policy"], schema_version=value["schema_version"],
                   bm25_settings=BM25Settings.from_dict(value["bm25_settings"]) if modern else None,
                   evidence_rules_version=value["evidence_rules_version"] if modern else None)

    @classmethod
    def historical(cls) -> ServiceExecutionPolicy:
        return cls(schema_version=1, exact_reference_routing=False, allowed_lexical_profiles=("legacy-v1",))

    @classmethod
    def from_environment(cls) -> ServiceExecutionPolicy:
        # Explicit process environment only. No dotenv traversal or Key reads.
        raw = os.environ.get("LEGAL_RAG_EXECUTION_POLICY_JSON")
        from legal_rag.json_utils import reject_duplicate_object_pairs, reject_non_finite_json_constant
        return cls.from_dict(json.loads(raw, object_pairs_hook=reject_duplicate_object_pairs,
            parse_constant=reject_non_finite_json_constant)) if raw else cls()
