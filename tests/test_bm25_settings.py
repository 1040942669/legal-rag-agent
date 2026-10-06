"""One immutable, actual BM25 configuration shared by application entrances."""

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from legal_rag.bm25_settings import BM25Settings, build_bm25_retriever
from legal_rag.chinese_bm25 import ChineseBM25Retriever, chinese_bm25_identity
from legal_rag.models import Chunk


MODERN = "bm25s-jieba-search-v1"


def test_modern_settings_freeze_actual_engine_analyzer_resources_and_all_parameters():
    settings = BM25Settings(lexical_profile=MODERN, k1=1.7, b=0.6, hmm=False)
    assert settings.identity == chinese_bm25_identity(mode="search", k1=1.7, b=0.6, hmm=False)
    assert settings.law_boost == settings.article_boost == 0
    assert settings.deprecated_penalty == 1
    assert BM25Settings.from_dict(settings.to_dict()) == settings
    assert BM25Settings.from_dict(settings.to_dict()).fingerprint == settings.fingerprint
    with pytest.raises(FrozenInstanceError):
        settings.k1 = 2


def test_identity_and_payload_are_detached_from_caller_mutation():
    settings = BM25Settings(lexical_profile=MODERN)
    fingerprint = settings.fingerprint
    identity = settings.identity
    identity["tokenizer"]["hmm_resource_sha256"].clear()
    payload = settings.to_dict()
    payload["identity"]["engine"]["version"] = "forged"
    assert settings.fingerprint == fingerprint
    assert settings.identity["tokenizer"]["hmm_resource_sha256"]


@pytest.mark.parametrize("path", [("engine", "version"), ("tokenizer", "dictionary_sha256"),
                                 ("tokenizer", "version"), ("query_text_version",)])
def test_deserialization_rejects_changed_dependencies_resources_and_text_identity(path):
    payload = BM25Settings(lexical_profile=MODERN).to_dict()
    target = payload["identity"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = "different"
    with pytest.raises(ValueError, match="identity"):
        BM25Settings.from_dict(payload)


@pytest.mark.parametrize("fields", [dict(k1=True), dict(k1=float("nan")), dict(k1=0),
                                   dict(b=1.1), dict(hmm=1), dict(law_boost=40),
                                   dict(article_boost=80), dict(deprecated_penalty=0.5)])
def test_modern_configuration_does_not_ignore_invalid_or_legacy_scoring_settings(fields):
    with pytest.raises(ValueError):
        BM25Settings(lexical_profile=MODERN, **fields)


def test_explicit_legacy_preserves_previous_service_multiplier_and_scoring_parameters():
    settings = BM25Settings(lexical_profile="generic-v3")
    assert (settings.law_boost, settings.article_boost, settings.deprecated_penalty) == (40, 80, 1)
    assert settings.identity["engine"]["name"] == "legacy-custom-bm25"
    assert BM25Settings.from_dict(settings.to_dict()) == settings
    cli_history = BM25Settings.from_config({"bm25_lexical_profile": "generic-v3", "deprecated_penalty": 0.5})
    assert cli_history.deprecated_penalty == 0.5 and cli_history.fingerprint != settings.fingerprint


def test_modern_builder_uses_actual_mature_adapter_and_frozen_settings():
    chunks = [Chunk("a", "劳动合同解除条件", ["合成劳动法"], ["第一条"], [], [], "article", {})]
    settings = BM25Settings(lexical_profile="bm25s-jieba-precise-v1", k1=1.1, b=0.4, hmm=False)
    retriever = build_bm25_retriever(chunks, settings)
    assert isinstance(retriever, ChineseBM25Retriever)
    assert retriever.config_identity == settings.identity
    assert retriever.lexical_profile == settings.lexical_profile
    assert retriever.retrieve("劳动合同")[0].chunk == chunks[0]


def test_legacy_builder_only_uses_explicit_historical_profile():
    from legal_rag.retrieval import BM25Retriever
    chunks = [Chunk("a", "合同解除", ["合成法"], ["第一条"], [], [], "article", {})]
    settings = BM25Settings(lexical_profile="legacy-v1", k1=1.2, b=0.4,
                            law_boost=3, article_boost=4, deprecated_penalty=0.8)
    retriever = build_bm25_retriever(chunks, settings)
    assert isinstance(retriever, BM25Retriever)
    assert (retriever.k1, retriever.b, retriever.law_boost, retriever.article_boost,
            retriever.deprecated_penalty) == (1.2, 0.4, 3, 4, 0.8)


def test_from_config_records_actual_effective_parameters_instead_of_profile_only():
    config = {"bm25_lexical_profile": MODERN, "bm25_k1": 1.2, "bm25_b": 0.5,
              "bm25_hmm": False, "bm25_law_boost": 0, "bm25_article_boost": 0,
              "deprecated_penalty": 1, "top_k": 3}
    before = deepcopy(config)
    settings = BM25Settings.from_config(config)
    assert (settings.k1, settings.b, settings.hmm) == (1.2, 0.5, False)
    assert config == before
    changed = BM25Settings.from_config(config | {"bm25_b": 0.7})
    assert changed.fingerprint != settings.fingerprint


def test_selector_rebuilds_real_mode_and_retains_common_parameters():
    selected = BM25Settings(lexical_profile=MODERN, k1=1.2, b=0.6, hmm=False).for_profile(
        "bm25s-jieba-precise-v1")
    assert selected.lexical_profile == "bm25s-jieba-precise-v1"
    assert selected.identity["tokenizer"]["mode"] == "precise"
    assert (selected.k1, selected.b, selected.hmm) == (1.2, 0.6, False)


def test_settings_payload_requires_closed_shape_and_no_silent_missing_fields():
    payload = BM25Settings(lexical_profile=MODERN).to_dict()
    for altered in (payload | {"unknown": True}, {key: value for key, value in payload.items() if key != "identity"},
                    payload | {"schema_version": True}):
        with pytest.raises(ValueError):
            BM25Settings.from_dict(altered)


@pytest.mark.parametrize("profile,mode", [("bm25s-sklearn-char-v1", "char"),
                                         ("bm25s-sklearn-char-bigram-v1", "char-bigram")])
def test_character_settings_freeze_mature_analyzer_with_explicit_nonapplicable_hmm(profile, mode):
    settings = BM25Settings(lexical_profile=profile, k1=1.2, b=0.6)
    assert settings.hmm is None and settings.identity == chinese_bm25_identity(mode=mode, k1=1.2, b=0.6, hmm=None)
    assert settings.identity["tokenizer"]["hmm_applicable"] is False
    assert BM25Settings.from_dict(settings.to_dict()) == settings
    chunks = [Chunk("a", "劳动合同解除条件", ["合成劳动法"], ["第一条"], [], [], "article", {})]
    retriever = build_bm25_retriever(chunks, settings)
    assert retriever.mode == mode and retriever.config_identity == settings.identity
    assert retriever.retrieve("劳动合同")[0].chunk == chunks[0]
    with pytest.raises(ValueError, match="HMM"):
        BM25Settings(lexical_profile=profile, hmm=True)


def test_profile_switch_to_character_analyzer_does_not_reuse_a_jieba_hmm_setting():
    original = BM25Settings(lexical_profile=MODERN, hmm=False)
    char = original.for_profile("bm25s-sklearn-char-bigram-v1")
    assert char.hmm is None and char.identity["tokenizer"]["mode"] == "char-bigram"
    assert char.for_profile(MODERN).hmm is True


def test_modern_service_policy_freezes_complete_settings_and_evidence_rules():
    from legal_rag.services.execution_policy import ServiceExecutionPolicy
    settings = BM25Settings(lexical_profile=MODERN, k1=1.2, b=0.5, hmm=False)
    policy = ServiceExecutionPolicy(lexical_profile=MODERN, bm25_settings=settings,
                                    allowed_lexical_profiles=(MODERN, "bm25s-jieba-precise-v1"))
    assert policy.schema_version == 2 and policy.evidence_rules_version == "general-reference-v3"
    assert policy.to_dict()["bm25_settings"] == settings.to_dict()
    assert ServiceExecutionPolicy.from_dict(policy.to_dict()) == policy
    selected = policy.with_lexical_profile("bm25s-jieba-precise-v1")
    assert selected.bm25_settings.identity["tokenizer"]["mode"] == "precise"
    assert (selected.bm25_settings.k1, selected.bm25_settings.b, selected.bm25_settings.hmm) == (1.2, 0.5, False)
    assert selected.fingerprint != policy.fingerprint


def test_old_schema_one_service_policy_has_exact_original_payload_and_multiplier():
    from legal_rag.services.execution_policy import GenerationPolicy, ServiceExecutionPolicy
    source = {"schema_version": 1, "lexical_profile": "generic-v3", "allowed_lexical_profiles": ["generic-v3"],
              "exact_reference_routing": True, "generation": GenerationPolicy().to_dict(), "semantic_policy": None}
    before = deepcopy(source)
    restored = ServiceExecutionPolicy.from_dict(source)
    assert restored.schema_version == 1 and restored.evidence_rules_version == "general-reference-v2"
    assert restored.resolved_bm25_settings.deprecated_penalty == 1
    assert restored.resolved_bm25_settings.lexical_profile == "generic-v3"
    assert restored.to_dict() == source == before
    with pytest.raises(ValueError):
        restored.with_lexical_profile(MODERN)


def test_explicit_schema_one_constructor_keeps_old_default_selector_allowlist():
    from legal_rag.services.execution_policy import ServiceExecutionPolicy
    policy = ServiceExecutionPolicy(schema_version=1)
    assert policy.allowed_lexical_profiles == ("legacy-v1", "generic-v3")
    assert "bm25_settings" not in policy.to_dict()


@pytest.mark.parametrize("profile", ["legacy-v1", "bm25s-sklearn-char-v1"])
def test_public_defaults_are_one_effective_source_for_new_service_cli_and_lifecycle(monkeypatch, profile):
    from legal_rag.config import DEFAULT_CONFIG, load_config
    from legal_rag.cli import retrieval_metadata
    from legal_rag.experiment_lifecycle import _manifest_contracts
    from legal_rag.services.execution_policy import ServiceExecutionPolicy
    configured = dict(DEFAULT_CONFIG["retrieval"], bm25_lexical_profile=profile,
                      bm25_k1=1.17, bm25_b=.36, bm25_law_boost=22,
                      bm25_article_boost=44, deprecated_penalty=.63)
    if profile != "legacy-v1":
        configured.update(bm25_hmm=None, bm25_law_boost=0, bm25_article_boost=0, deprecated_penalty=1)
    monkeypatch.setitem(DEFAULT_CONFIG, "retrieval", configured)
    expected = BM25Settings.from_config(configured)
    assert ServiceExecutionPolicy().resolved_bm25_settings == expected
    assert retrieval_metadata(load_config())["bm25_settings"] == expected.to_dict()
    assert _manifest_contracts(top_k=3, judge_enabled=False)["retrieval"]["parameters"]["bm25_settings"] == expected.to_dict()
    assert ServiceExecutionPolicy(schema_version=1).resolved_bm25_settings.deprecated_penalty == 1
    explicit = BM25Settings(lexical_profile="legacy-v1", deprecated_penalty=.9)
    assert ServiceExecutionPolicy(bm25_settings=explicit).resolved_bm25_settings == explicit
    selected = ServiceExecutionPolicy(lexical_profile="legacy-v1")
    assert selected.resolved_bm25_settings == expected.for_profile("legacy-v1")


@pytest.mark.parametrize("schema", [1, 2])
def test_serialized_policy_cannot_omit_profile_identity_through_null(schema):
    from legal_rag.services.execution_policy import ServiceExecutionPolicy
    payload = ServiceExecutionPolicy(schema_version=schema).to_dict()
    payload["lexical_profile"] = None
    with pytest.raises(ValueError):
        ServiceExecutionPolicy.from_dict(payload)


def test_service_policy_does_not_accept_missing_or_inconsistent_modern_settings():
    from legal_rag.services.execution_policy import ServiceExecutionPolicy
    policy = ServiceExecutionPolicy(lexical_profile=MODERN)
    payload = policy.to_dict()
    del payload["bm25_settings"]
    with pytest.raises(ValueError):
        ServiceExecutionPolicy.from_dict(payload)
    with pytest.raises(ValueError):
        ServiceExecutionPolicy(lexical_profile=MODERN, bm25_settings=BM25Settings(lexical_profile="legacy-v1"))
    with pytest.raises(ValueError):
        ServiceExecutionPolicy(lexical_profile=MODERN, schema_version=1)


def test_cli_builder_and_metadata_use_the_same_full_configuration():
    from legal_rag.cli import create_retriever, retrieval_metadata
    from legal_rag.config import load_config
    config = load_config()
    config["retrieval"].update(bm25_lexical_profile=MODERN, bm25_k1=1.2, bm25_b=0.6,
                              bm25_hmm=False, bm25_law_boost=0, bm25_article_boost=0,
                              deprecated_penalty=1)
    settings = BM25Settings.from_config(config["retrieval"])
    chunks = [Chunk("a", "劳动合同解除条件", ["合成劳动法"], ["第一条"], [], [], "article", {})]
    retriever = create_retriever("bm25", chunks, config, top_k=3, chunk_strategy="article")
    assert isinstance(retriever, ChineseBM25Retriever) and retriever.config_identity == settings.identity
    assert retrieval_metadata(config)["bm25_settings"] == settings.to_dict()


def test_lifecycle_manifest_and_runtime_freeze_and_apply_identical_full_settings(tmp_path):
    from pathlib import Path
    from legal_rag.experiment_lifecycle import build_lifecycle_plan, build_corpus_snapshot, _runtime_factory
    root = Path(__file__).resolve().parents[1]
    settings = BM25Settings(lexical_profile=MODERN, k1=1.2, b=0.6, hmm=False)
    manifest = build_lifecycle_plan(experiment_id="bm25-config-contract", repository_root=root,
                                    bm25_settings=settings)["manifest"]
    assert manifest["config"]["summary"]["bm25_settings"] == settings.to_dict()
    assert manifest["contracts"]["retrieval"]["parameters"]["bm25_settings"] == settings.to_dict()
    assert manifest["contracts"]["retrieval"]["query_analysis"]["reference_rules_version"] == "legal-reference-v3"
    assert manifest["contracts"]["retrieval"]["query_analysis"]["evidence_rules_version"] == "general-reference-v3"
    corpus = build_corpus_snapshot(root / "tests/fixtures/synthetic/synthetic_non_law.txt",
                                   repository_root=root, require_offline_fixture=True)
    factory = _runtime_factory(manifest, corpus=corpus, cache_directory=tmp_path / "cache")
    assert factory.spec.retriever.config_identity == settings.identity
    assert factory.spec.assistant_factory().evidence_rules_version == "general-reference-v3"


def test_lifecycle_factory_rejects_config_and_contract_disagreement(tmp_path):
    from pathlib import Path
    from legal_rag.experiment_lifecycle import build_lifecycle_plan, build_corpus_snapshot, _runtime_factory, ExperimentLifecycleError
    root = Path(__file__).resolve().parents[1]
    manifest = build_lifecycle_plan(experiment_id="bm25-mismatch", repository_root=root,
        bm25_settings=BM25Settings(lexical_profile=MODERN))["manifest"]
    manifest["contracts"]["retrieval"]["parameters"]["bm25_settings"] = BM25Settings(
        lexical_profile="bm25s-jieba-precise-v1").to_dict()
    corpus = build_corpus_snapshot(root / "tests/fixtures/synthetic/synthetic_non_law.txt",
                                   repository_root=root, require_offline_fixture=True)
    with pytest.raises(ExperimentLifecycleError, match="configuration"):
        _runtime_factory(manifest, corpus=corpus, cache_directory=tmp_path / "cache")


@pytest.mark.parametrize("profile", ["bm25s-jieba-precise-v1", "bm25s-jieba-search-v1"])
def test_service_factory_applies_same_full_settings_as_cli_and_lifecycle(monkeypatch, profile):
    from dataclasses import replace
    from types import SimpleNamespace
    from legal_rag.embedding_contracts import EmbeddingProfileIdentity
    from legal_rag.retrieval_contracts import chunk_payload_fingerprint
    from legal_rag.services.execution_policy import ServiceExecutionPolicy
    from legal_rag.services.run_executor import RunExecutionInput
    from legal_rag.storage.retrieval import BoundCorpus, BoundCorpusEntry
    from test_exact_service_route import Catalog, _boundary, _entry
    import legal_rag.services.service_retrieval as module
    embedding = EmbeddingProfileIdentity("fixture", "fixture/model", "r1", 3, True, "", "", False)
    row = {"provider": embedding.provider, "model": embedding.model, "revision": embedding.revision,
           "dimensions": 3, "normalization": True, "query_prefix": "", "document_prefix": "", "embed_with_metadata": False}

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, _): return self
        def mappings(self): return self
        def one_or_none(self): return row

    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: Connection())
    boundary = replace(_boundary(), profile_id=embedding.profile_id)
    entry = _entry()
    chunk = replace(entry.chunk, metadata={**entry.chunk.metadata, "profile_id": embedding.profile_id,
                                          "boundary_fingerprint": boundary.fingerprint})
    entry = BoundCorpusEntry(chunk, replace(entry.provenance, boundary=boundary, profile_id=embedding.profile_id,
                                           chunk_payload_hash=chunk_payload_fingerprint(chunk)))
    corpus = BoundCorpus(boundary, (entry,))
    monkeypatch.setattr(module, "PostgresExactRetrievalRepository",
                        lambda _: SimpleNamespace(load_bound_corpus=lambda **_: corpus))
    monkeypatch.setattr(module, "PostgresLegalCatalogRepository", lambda _: Catalog(corpus.entries))
    settings = BM25Settings(lexical_profile=profile, k1=1.2, b=0.6, hmm=False)
    policy = ServiceExecutionPolicy(lexical_profile=profile, bm25_settings=settings)
    execution = RunExecutionInput("run-a", "原始分块正文", (), boundary.scope_id, boundary.snapshot_id,
        1, "activation-a", embedding.profile_id, boundary.fingerprint, {"top_k": 1}, policy.to_dict())
    assistant = module.PostgresAssistantFactory(engine)(execution)
    outcome = assistant.retriever.retrieve_outcome(execution.question, top_k=1)
    expected = build_bm25_retriever(corpus.chunks, settings).retrieve(execution.question, top_k=1)
    assert [(result.chunk.chunk_id, result.score) for result in outcome.results] == [
        (result.chunk.chunk_id, result.score) for result in expected]
    assert outcome.results[0].trace["lexical_profile"] == profile
    assert outcome.results[0].provenance.boundary == boundary
    assert assistant.evidence_rules_version == "general-reference-v3"
    assert assistant.retriever.reference_rules_version == "legal-reference-v3"
    assert assistant.model == "service-provider-disabled"
