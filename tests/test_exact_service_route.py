from dataclasses import replace
from types import SimpleNamespace

import pytest

from legal_rag.models import SearchResult
from legal_rag.retrieval_contracts import RetrievalBoundary, RetrievalProvenance, RetrievedArticleProvenance, chunk_payload_fingerprint
from legal_rag.models import Chunk
from legal_rag.services.exact_retrieval import ExactReferenceRetriever
from legal_rag.storage.retrieval import BoundCorpus, BoundCorpusEntry, BoundaryBoundRetriever


def _boundary():
    return RetrievalBoundary("scope-a", "snapshot-a", "a" * 64)


def _entry(*, title="测试法", number="第一条", suffix="a", ordinal=0):
    boundary = _boundary()
    article = RetrievedArticleProvenance(f"article-{suffix}", f"law-{suffix}", f"version-{suffix}", number, title,
                                         None, None, "fixtures/test.txt", 1, "verified")
    chunk = Chunk(f"chunk-{suffix}", f"{number} 原始分块正文", [title], [number],
                  ["fixtures/test.txt"], [1], "article", {"scope_id": "scope-a",
                  "snapshot_id": "snapshot-a", "profile_id": "a" * 64,
                  "access_scope_ids": ["scope-a"], "law_ids": [article.law_id], "version_ids": [article.version_id],
                  "article_ids": [article.article_id], "article_refs": [article.to_metadata()],
                  "boundary_fingerprint": boundary.fingerprint})
    provenance = RetrievalProvenance(boundary, "scope-a", "snapshot-a", "a" * 64,
                                    chunk.chunk_id, "b" * 64, chunk_payload_fingerprint(chunk), ordinal, None, (article,))
    return BoundCorpusEntry(chunk, provenance)


class Lexical:
    name = "fake_lexical"
    def __init__(self, entries):
        self.entries, self.calls = entries, 0
    def retrieve(self, query, top_k=5):
        self.calls += 1
        return [SearchResult(item.chunk, 0.5, rank + 1, self.name)
                for rank, item in enumerate(self.entries[:top_k])]


class Catalog:
    def __init__(self, entries, *, status="found"):
        self.entries, self.status, self.requests = entries, status, []
    def lookup_article(self, request):
        self.requests.append(request)
        matching = [entry for entry in self.entries for article in entry.provenance.articles
                    if article.title == request.law_title and article.article_number == request.article_number]
        if self.status != "found" or not matching:
            return SimpleNamespace(status=self.status if self.status != "found" else "not_found", reason="no_exact_match", match=None)
        entry = matching[0]
        article = entry.provenance.articles[0]
        match = SimpleNamespace(law_id=article.law_id, version_id=article.version_id,
                                article_id=article.article_id, title=article.title,
                                article_number=article.article_number,
                                provenance=SimpleNamespace(boundary=request.boundary,
                                    memberships=tuple(SimpleNamespace(chunk_id=item.chunk.chunk_id,
                                    chunk_content_hash=item.provenance.chunk_content_hash,
                                    snapshot_ordinal=item.provenance.snapshot_ordinal) for item in matching)))
        return SimpleNamespace(status="found", reason="exact_match", match=match)


def make_route(entries=None, **kwargs):
    entries = entries or [_entry()]
    corpus = BoundCorpus(_boundary(), tuple(entries))
    lexical = Lexical(entries)
    catalog = Catalog(entries, **kwargs)
    router = ExactReferenceRetriever(corpus=corpus,
        lexical=BoundaryBoundRetriever(lexical, corpus=corpus), catalog=catalog,
        pointer_revision=1, activation_id="activation-a")
    return router, lexical, catalog


def test_exact_reference_uses_catalog_and_preserves_original_chunk_and_typed_boundary():
    router, lexical, catalog = make_route()
    outcome = router.retrieve_outcome("《测试法》第一条", top_k=5)
    assert outcome.status == "found" and outcome.route == "exact_reference"
    assert lexical.calls == 0
    assert catalog.requests[0].boundary.snapshot_id == _boundary().snapshot_id
    assert outcome.results[0].chunk == _entry().chunk
    assert outcome.results[0].provenance == _entry().provenance
    assert outcome.results[0].trace["score_kind"] == "exact_key_match"


def test_exact_miss_does_not_fall_back_to_similar_lexical_hit():
    router, lexical, _ = make_route()
    outcome = router.retrieve_outcome("《测试法》第九十九条")
    assert outcome.status == "not_found" and not outcome.results
    assert lexical.calls == 0


def test_unresolved_article_is_clarification_without_guessing_law():
    router, lexical, catalog = make_route()
    outcome = router.retrieve_outcome("第一条有什么要求？")
    assert outcome.status == "needs_disambiguation" and not outcome.results
    assert lexical.calls == 0 and not catalog.requests


@pytest.mark.parametrize("unpaired_prefix", ["", "第三百条；"])
def test_reference_overflow_requests_clarification_without_truncation_or_dispatch(unpaired_prefix):
    from legal_rag.chat import validate_reference_route
    from legal_rag.legal_references import parse_legal_references

    query = unpaired_prefix + "；".join(f"《测试法》第{number}条" for number in range(1, 18))
    analysis = parse_legal_references(query)
    assert len(analysis.requirements) == 17
    assert bool(analysis.unresolved) is bool(unpaired_prefix)
    router, lexical, catalog = make_route()
    outcome = router.retrieve_outcome(query)
    assert outcome.status == "needs_disambiguation"
    assert outcome.route == "exact_reference"
    assert outcome.reason_codes == ("too_many_references",)
    assert not outcome.results and not outcome.requested_pairs and not outcome.resolved_pairs
    assert lexical.calls == 0 and not catalog.requests
    validate_reference_route(outcome, query)


def test_catalog_ambiguity_does_not_rank_one_version_as_exact_match():
    router, lexical, _ = make_route(status="needs_disambiguation")
    outcome = router.retrieve_outcome("《测试法》第一条")
    assert outcome.status == "needs_disambiguation" and not outcome.results
    assert lexical.calls == 0


def test_general_query_stays_lexical_and_pair_requirements_are_empty():
    router, lexical, _ = make_route()
    outcome = router.retrieve_outcome("一般规则是什么？")
    assert outcome.route == "lexical" and lexical.calls == 1
    assert not outcome.requested_pairs


def test_snapshot_membership_mismatch_is_not_accepted():
    router, lexical, catalog = make_route()
    original = catalog.lookup_article
    def mismatched(request):
        outcome = original(request)
        outcome.match.provenance.memberships[0].chunk_content_hash = "f" * 64
        return outcome
    catalog.lookup_article = mismatched
    outcome = router.retrieve_outcome("《测试法》第一条")
    assert outcome.status == "not_found" and not outcome.results
    assert lexical.calls == 0


def test_multiple_law_article_pairs_are_covered_before_extra_parts():
    entries = [_entry(title="甲法", number="第十条"),
               _entry(title="乙法", number="第二十条", suffix="b", ordinal=1)]
    router, lexical, _ = make_route(entries)
    result = router.retrieve_outcome("《甲法》第10条和《乙法》第20条", top_k=2)
    assert result.status == "found" and len(result.results) == 2
    assert result.resolved_pairs == (("甲法", "第十条"), ("乙法", "第二十条"))
    clipped = router.retrieve_outcome("《甲法》第10条和《乙法》第20条", top_k=1)
    assert clipped.status == "not_found" and len(clipped.resolved_pairs) == 1
    assert "top_k_cannot_cover_references" in clipped.reason_codes
    swapped = router.retrieve_outcome("《甲法》第20条和《乙法》第10条", top_k=2)
    assert swapped.status == "not_found" and not swapped.results
    assert lexical.calls == 0


NESTED_CATALOG_TITLE = "合成机关关于适用《合成甲法》的规定"


def _nested_catalog_route():
    return make_route([
        _entry(title="合成甲法", number="第十条", suffix="plain"),
        _entry(title=NESTED_CATALOG_TITLE, number="第二条", suffix="outer", ordinal=1),
    ])


def test_nested_catalog_does_not_block_unrelated_general_query():
    router, lexical, catalog = _nested_catalog_route()
    outcome = router.retrieve_outcome("原始分块正文的一般登记事项")
    assert outcome.route == "lexical" and outcome.status == "found"
    assert lexical.calls == 1 and not catalog.requests
    assert set(router.known_law_hints) == {"合成甲法", NESTED_CATALOG_TITLE}


def test_nested_catalog_keeps_plain_law_exact_authority():
    router, lexical, catalog = _nested_catalog_route()
    outcome = router.retrieve_outcome("《合成甲法》第十条是什么？")
    assert outcome.status == "found" and lexical.calls == 0
    assert outcome.resolved_pairs == (("合成甲法", "第十条"),)
    assert [result.chunk.chunk_id for result in outcome.results] == ["chunk-plain"]
    assert catalog.requests[0].law_title == "合成甲法"


@pytest.mark.parametrize("wrapped", [False, True])
def test_nested_catalog_outer_title_keeps_full_exact_pair(wrapped):
    from legal_rag.chat import validate_reference_route
    router, lexical, catalog = _nested_catalog_route()
    title = f"《{NESTED_CATALOG_TITLE}》" if wrapped else NESTED_CATALOG_TITLE
    query = title + "第二条是什么？"
    outcome = router.retrieve_outcome(query)
    assert outcome.status == "found" and lexical.calls == 0
    assert outcome.requested_pairs == outcome.resolved_pairs == ((NESTED_CATALOG_TITLE, "第二条"),)
    assert [result.chunk.chunk_id for result in outcome.results] == ["chunk-outer"]
    assert catalog.requests[0].law_title == NESTED_CATALOG_TITLE
    validate_reference_route(outcome, query)


def test_nested_catalog_article_cannot_be_stolen_by_inner_law_reference():
    router, lexical, catalog = _nested_catalog_route()
    outcome = router.retrieve_outcome("《合成甲法》第二条是什么？")
    assert outcome.status == "not_found" and not outcome.results
    assert outcome.requested_pairs == (("合成甲法", "第二条"),)
    assert not outcome.resolved_pairs and lexical.calls == 0
    assert catalog.requests[0].law_title == "合成甲法"


def test_split_chunk_text_and_membership_are_not_replaced_by_full_article():
    first = _entry()
    second = _entry(suffix="split", ordinal=1)
    second_article = replace(second.provenance.articles[0], article_id="article-a", law_id="law-a", version_id="version-a")
    metadata = {**second.chunk.metadata, "article_ids": ["article-a"], "law_ids": ["law-a"],
                "version_ids": ["version-a"], "article_refs": [second_article.to_metadata()]}
    chunk = replace(second.chunk, text="第二个原始片段", metadata=metadata)
    second = BoundCorpusEntry(chunk, replace(second.provenance, articles=(second_article,), chunk_payload_hash=chunk_payload_fingerprint(chunk)))
    router, _, _ = make_route([first, second])
    result = router.retrieve_outcome("《测试法》第一条", top_k=5)
    assert [item.chunk.text for item in result.results] == [first.chunk.text, "第二个原始片段"]
    assert result.status == "found"


def test_factory_wires_frozen_generic_profile_to_real_bm25_without_model_calls(monkeypatch):
    from legal_rag.embedding_contracts import EmbeddingProfileIdentity
    from legal_rag.services.execution_policy import ServiceExecutionPolicy
    from legal_rag.services.run_executor import RunExecutionInput
    import legal_rag.services.service_retrieval as module
    profile = EmbeddingProfileIdentity("fixture", "fixture/model", "r1", 3, True, "", "", False)
    fields = {"provider": profile.provider, "model": profile.model, "revision": profile.revision,
              "dimensions": profile.dimensions, "normalization": profile.normalization,
              "query_prefix": "", "document_prefix": "", "embed_with_metadata": False}
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, _): return self
        def mappings(self): return self
        def one_or_none(self): return fields
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: Connection())
    boundary = replace(_boundary(), profile_id=profile.profile_id)
    entry = _entry()
    chunk = replace(entry.chunk, metadata={**entry.chunk.metadata, "profile_id": profile.profile_id,
                    "boundary_fingerprint": boundary.fingerprint})
    entry = BoundCorpusEntry(chunk, replace(entry.provenance, boundary=boundary,
                    profile_id=profile.profile_id, chunk_payload_hash=chunk_payload_fingerprint(chunk)))
    corpus = BoundCorpus(boundary, (entry,))
    repository = SimpleNamespace(load_bound_corpus=lambda **_: corpus)
    monkeypatch.setattr(module, "PostgresExactRetrievalRepository", lambda _: repository)
    monkeypatch.setattr(module, "PostgresLegalCatalogRepository", lambda _: Catalog(corpus.entries))
    policy = ServiceExecutionPolicy(lexical_profile="generic-v3")
    execution = RunExecutionInput("run-a", "原始分块正文", (), boundary.scope_id, boundary.snapshot_id,
        1, "activation-a", profile.profile_id, boundary.fingerprint, {"top_k": 1}, policy.to_dict())
    assistant = module.PostgresAssistantFactory(engine)(execution)
    result = assistant.retriever.retrieve_outcome("原始分块正文", top_k=1)
    assert result.route == "lexical" and result.results[0].trace["lexical_profile"] == "generic-v3"
    assert assistant.model == "service-provider-disabled"
    import pytest
    with pytest.raises(RuntimeError, match="generation is disabled"):
        assistant.llm.complete("offline")
