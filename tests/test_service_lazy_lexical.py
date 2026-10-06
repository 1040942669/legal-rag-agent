"""Run-local lazy BM25 contracts, with fictional corpus and no providers."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from legal_rag.bm25_settings import BM25Settings, BM25_PROFILES, build_bm25_retriever
from legal_rag.embedding_contracts import EmbeddingProfileIdentity
from legal_rag.retrieval_contracts import MAX_RETRIEVAL_TOP_K, RetrievalBoundary, chunk_payload_fingerprint
from legal_rag.services.execution_policy import ServiceExecutionPolicy
from legal_rag.services.exact_retrieval import ExactReferenceRetriever
from legal_rag.services.run_executor import RunExecutionInput
import legal_rag.services.service_retrieval as module
from legal_rag.storage.retrieval import BoundCorpus, BoundCorpusEntry, BoundaryBoundRetriever
from test_exact_service_route import Catalog, _entry


def _profile(revision="r1"):
    return EmbeddingProfileIdentity("fixture", "fixture/model", revision, 3, True, "", "", False)


def _corpus(*, scope="scope-a", snapshot="snapshot-a", profile=None):
    profile = profile or _profile()
    boundary = RetrievalBoundary(scope, snapshot, profile.profile_id)
    entries = []
    for ordinal, (number, text) in enumerate([
        ("第一条", "合成登记申请与合同义务"),
        ("第二条", "合成登记申请申请与通知"),
        ("第三条", "合成通知程序与材料"),
    ]):
        original = _entry(number=number, suffix=f"{scope}-{snapshot}-{ordinal}", ordinal=ordinal)
        chunk = replace(original.chunk, text=text, metadata={
            **original.chunk.metadata, "scope_id": scope, "snapshot_id": snapshot,
            "profile_id": profile.profile_id, "access_scope_ids": [scope],
            "boundary_fingerprint": boundary.fingerprint,
        })
        provenance = replace(original.provenance, boundary=boundary, scope_id=scope,
                             snapshot_id=snapshot, profile_id=profile.profile_id,
                             chunk_payload_hash=chunk_payload_fingerprint(chunk))
        entries.append(BoundCorpusEntry(chunk, provenance))
    return BoundCorpus(boundary, tuple(entries))


@pytest.fixture
def environment(monkeypatch):
    state = SimpleNamespace(corpora={}, profile=_profile(), loads=[], builds=[])

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, _): return self
        def mappings(self): return self
        def one_or_none(self):
            profile = state.profile
            return {"provider": profile.provider, "model": profile.model,
                    "revision": profile.revision, "dimensions": profile.dimensions,
                    "normalization": profile.normalization, "query_prefix": "",
                    "document_prefix": "", "embed_with_metadata": False}

    def load_bound_corpus(*, filters, expected_profile):
        state.loads.append((filters, expected_profile))
        assert filters.profile_id == expected_profile.profile_id
        return state.corpora[filters]

    def spy(chunks, settings):
        state.builds.append((deepcopy(chunks), settings.to_dict()))
        return build_bm25_retriever(chunks, settings)

    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=Connection)
    monkeypatch.setattr(module, "PostgresExactRetrievalRepository", lambda _: SimpleNamespace(
        load_bound_corpus=load_bound_corpus))
    monkeypatch.setattr(module, "PostgresLegalCatalogRepository", lambda _: Catalog(
        [entry for corpus in state.corpora.values() for entry in corpus.entries]))
    monkeypatch.setattr(module, "build_bm25_retriever", spy)
    state.factory = module.PostgresAssistantFactory(engine)

    def create(*, corpus=None, policy=None, run_id="run-a", raw_policy=False):
        corpus = corpus or _corpus(profile=state.profile)
        state.corpora[corpus.boundary] = corpus
        policy = policy or ServiceExecutionPolicy()
        execution = RunExecutionInput(
            run_id, "合成登记申请", (), corpus.boundary.scope_id, corpus.boundary.snapshot_id,
            1, "activation-a", corpus.boundary.profile_id, corpus.boundary.fingerprint,
            {"top_k": 3}, None if raw_policy else policy.to_dict(),
        )
        return state.factory(execution), execution, corpus

    state.create = create
    return state


@pytest.mark.parametrize("query,status", [
    ("《测试法》第一条", "found"),
    ("《测试法》第九十九条", "not_found"),
    ("第一条有什么要求？", "needs_disambiguation"),
])
def test_exact_terminal_routes_never_construct_lexical_index(environment, query, status):
    assistant, _, corpus = environment.create()
    assert len(environment.loads) == 1  # Authorization still loads one frozen corpus.
    assert environment.builds == []
    outcome = assistant.retriever.retrieve_outcome(query, top_k=3)
    assert outcome.route == "exact_reference" and outcome.status == status
    assert environment.builds == []
    expected_router = ExactReferenceRetriever(
        corpus=corpus,
        lexical=BoundaryBoundRetriever(build_bm25_retriever(
            corpus.chunks, ServiceExecutionPolicy().resolved_bm25_settings), corpus=corpus),
        catalog=Catalog(corpus.entries), pointer_revision=1, activation_id="activation-a",
    )
    assert outcome == expected_router.retrieve_outcome(query, top_k=3)
    if status == "found":
        assert outcome.results[0].chunk == corpus.entries[0].chunk
        assert outcome.results[0].provenance == corpus.entries[0].provenance


@pytest.mark.parametrize("profile", BM25_PROFILES)
def test_first_lexical_query_constructs_once_with_identical_full_ranking(environment, profile):
    settings = BM25Settings(lexical_profile=profile, k1=1.2, b=.4)
    policy = ServiceExecutionPolicy(lexical_profile=profile, bm25_settings=settings,
                                    allowed_lexical_profiles=(profile,))
    assistant, _, corpus = environment.create(policy=policy)
    assert environment.builds == []
    eager = BoundaryBoundRetriever(build_bm25_retriever(corpus.chunks, settings), corpus=corpus)
    for query in ("合成登记申请", "合成通知程序", "合成登记申请"):
        actual = assistant.retriever.retrieve_outcome(query, top_k=3)
        assert actual.route == "lexical"
        assert list(actual.results) == eager.retrieve(query, top_k=3)
        assert len(environment.builds) == 1
    assert environment.builds[0][1] == settings.to_dict()
    assert assistant.model == "service-provider-disabled"
    with pytest.raises(RuntimeError, match="generation is disabled"):
        assistant.llm.complete("synthetic offline prompt")


@pytest.mark.parametrize("historical", [False, True, "no-policy"])
def test_non_routing_policy_retains_eager_initialization(environment, historical):
    policy = ServiceExecutionPolicy.historical() if historical else ServiceExecutionPolicy(exact_reference_routing=False)
    assistant, _, _ = environment.create(policy=policy, raw_policy=historical == "no-policy")
    assert len(environment.builds) == 1
    assert isinstance(assistant.retriever, BoundaryBoundRetriever)
    assistant.retriever.retrieve("合成登记申请")
    assert len(environment.builds) == 1


def test_failed_lazy_build_is_not_cached_and_does_not_poison_frozen_input(environment, monkeypatch):
    attempts = []

    def fail_then_build(chunks, settings):
        attempts.append(deepcopy(chunks))
        if len(attempts) == 1:
            chunks[0].law_names.append("poison")
            chunks[0].metadata["scope_id"] = "poisoned partial initialization"
            raise RuntimeError("synthetic index construction failure")
        return build_bm25_retriever(chunks, settings)

    monkeypatch.setattr(module, "build_bm25_retriever", fail_then_build)
    assistant, _, corpus = environment.create()
    assert not attempts
    assert assistant.retriever.retrieve_outcome("《测试法》第一条").status == "found"
    with pytest.raises(RuntimeError, match="synthetic index construction failure"):
        assistant.retriever.retrieve_outcome("合成登记申请")
    assert len(attempts) == 1
    first = assistant.retriever.retrieve_outcome("合成登记申请")
    second = assistant.retriever.retrieve_outcome("合成登记申请")
    assert len(attempts) == 2 and attempts[0] == attempts[1] == corpus.chunks
    assert first == second and first.results


def test_eager_build_failure_still_happens_during_factory(environment, monkeypatch):
    def fail(*_):
        raise RuntimeError("synthetic eager failure")
    monkeypatch.setattr(module, "build_bm25_retriever", fail)
    with pytest.raises(RuntimeError, match="synthetic eager failure"):
        environment.create(policy=ServiceExecutionPolicy.historical())


def test_frozen_chunks_and_settings_do_not_alias_delayed_inputs_or_results(environment):
    settings = BM25Settings(lexical_profile="generic-v3", k1=1.2, b=.3)
    policy = ServiceExecutionPolicy(lexical_profile=settings.lexical_profile, bm25_settings=settings)
    assistant, execution, corpus = environment.create(policy=policy)
    original = deepcopy(corpus)
    expected = BoundaryBoundRetriever(build_bm25_retriever(original.chunks, settings), corpus=original)
    corpus.entries[0].chunk.law_names.append("caller poison")
    corpus.entries[0].chunk.metadata["scope_id"] = "foreign-scope"
    execution.execution_policy["bm25_settings"]["parameters"]["k1"] = 9
    execution.execution_policy["lexical_profile"] = "legacy-v1"
    first = assistant.retriever.retrieve_outcome("合成登记申请", top_k=3)
    assert list(first.results) == expected.retrieve("合成登记申请", top_k=3)
    assert environment.builds[0][0] == original.chunks
    assert environment.builds[0][1] == settings.to_dict()
    first.results[0].chunk.law_names.append("caller changed returned payload")
    first.results[0].chunk.metadata.clear()
    first.results[0].trace.clear()
    again = assistant.retriever.retrieve_outcome("合成登记申请", top_k=3)
    assert list(again.results) == expected.retrieve("合成登记申请", top_k=3)
    assert len(environment.builds) == 1
    assert assistant.retriever.retrieve_outcome("《测试法》第一条").results[0].chunk == original.entries[0].chunk


@pytest.mark.parametrize("change", ["same-boundary-new-run", "scope", "snapshot", "profile", "settings"])
def test_separate_runs_never_reuse_indices_or_authority(environment, change):
    first, _, corpus_a = environment.create()
    settings_b = BM25Settings()
    if change == "profile":
        environment.profile = _profile("r2")
    corpus_b = _corpus(scope="scope-b" if change == "scope" else "scope-a",
                       snapshot="snapshot-b" if change == "snapshot" else "snapshot-a",
                       profile=environment.profile)
    if change == "settings":
        settings_b = BM25Settings(lexical_profile="generic-v3", k1=1.1, b=.5)
    second, _, _ = environment.create(corpus=corpus_b, run_id="run-b", policy=ServiceExecutionPolicy(
        lexical_profile=settings_b.lexical_profile, bm25_settings=settings_b))
    assert len(environment.loads) == 2 and environment.builds == []
    result_a = first.retriever.retrieve_outcome("合成登记申请", top_k=3)
    result_b = second.retriever.retrieve_outcome("合成登记申请", top_k=3)
    assert len(environment.builds) == 2
    assert all(item.provenance.boundary == corpus_a.boundary for item in result_a.results)
    assert all(item.provenance.boundary == corpus_b.boundary for item in result_b.results)
    expected_b = BoundaryBoundRetriever(build_bm25_retriever(corpus_b.chunks, settings_b), corpus=corpus_b)
    assert list(result_b.results) == expected_b.retrieve("合成登记申请", top_k=3)


def test_concurrent_first_queries_share_only_one_successful_build(environment, monkeypatch):
    entered, release, duplicate = Event(), Event(), Event()
    count_lock = Lock()
    calls = []

    def blocked_build(chunks, settings):
        with count_lock:
            calls.append(1)
            if len(calls) > 1:
                duplicate.set()
        entered.set()
        if not release.wait(3):
            raise RuntimeError("synthetic build release timed out")
        return build_bm25_retriever(chunks, settings)

    monkeypatch.setattr(module, "build_bm25_retriever", blocked_build)
    assistant, _, _ = environment.create()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(assistant.retriever.retrieve_outcome, "合成登记申请")
        try:
            assert entered.wait(1)
            second = pool.submit(assistant.retriever.retrieve_outcome, "合成登记申请")
            assert not duplicate.wait(.05)
        finally:
            release.set()
        assert first.result(timeout=3) == second.result(timeout=3)
    assert calls == [1]


@pytest.mark.parametrize("top_k", [True, 0, MAX_RETRIEVAL_TOP_K + 1])
def test_invalid_lexical_top_k_cannot_trigger_index_construction(environment, top_k):
    assistant, _, _ = environment.create()
    assert environment.builds == []
    with pytest.raises(ValueError):
        assistant.retriever.retrieve_outcome("合成登记申请", top_k=top_k)
    assert environment.builds == []


def test_corrupt_authority_is_rejected_before_any_deferred_build(environment):
    from legal_rag.storage.retrieval import RetrievalDataError
    corpus = _corpus()
    entry = corpus.entries[0]
    corrupt = BoundCorpus(corpus.boundary, (replace(entry, provenance=replace(
        entry.provenance, chunk_payload_hash="f" * 64)),))
    with pytest.raises(RetrievalDataError, match="provenance is inconsistent"):
        environment.create(corpus=corrupt)
    assert environment.builds == []
