"""A catalog identity is not required to fit the bounded query grammar."""

import pytest

from legal_rag.services.exact_retrieval import ExactReferenceRetriever
from legal_rag.legal_references import MAX_REFERENCE_QUERY_CHARS
from legal_rag.retrieval_contracts import MAX_RETRIEVAL_TOP_K
from legal_rag.storage.retrieval import BoundCorpus, BoundaryBoundRetriever
from test_exact_service_route import Catalog, Lexical, _boundary, _entry, make_route


PLAIN_TITLE = "合成甲法"
UNSUPPORTED_TITLES = (
    "合成机关" + "补充规定" * 64 + "关于适用《合成甲法》的规定",
    "合成机关关于适用《合成甲法的规定",
    "合成机关关于适用《合成甲法》的规定》",
    "合成机关关于适用" + "《" * 9 + "合成甲法" + "》" * 9 + "的规定",
)


def _mixed_route(title):
    return make_route([
        _entry(title=PLAIN_TITLE, number="第十条", suffix="plain"),
        _entry(title=title, number="第二条", suffix="unsupported", ordinal=1),
    ])


@pytest.mark.parametrize("title", UNSUPPORTED_TITLES)
def test_unsupported_catalog_title_keeps_normal_lexical_route_and_full_identity(title):
    router, lexical, catalog = _mixed_route(title)
    assert router.catalog_law_titles == tuple(sorted((PLAIN_TITLE, title)))
    assert router.unsupported_law_titles == (title,)
    assert router.known_law_hints == (PLAIN_TITLE,)
    outcome = router.retrieve_outcome("原始分块正文的一般登记事项")
    assert outcome.route == "lexical" and outcome.status == "found"
    assert lexical.calls == 1 and not catalog.requests
    assert [result.chunk.chunk_id for result in outcome.results] == ["chunk-plain", "chunk-unsupported"]
    assert outcome.results[1].provenance.articles[0].title == title
    assert all(result.provenance.boundary == _boundary() for result in outcome.results)


@pytest.mark.parametrize("title", UNSUPPORTED_TITLES)
def test_unsupported_catalog_title_does_not_disable_other_exact_law(title):
    router, lexical, catalog = _mixed_route(title)
    outcome = router.retrieve_outcome("《合成甲法》第十条是什么？")
    assert outcome.route == "exact_reference" and outcome.status == "found"
    assert outcome.resolved_pairs == ((PLAIN_TITLE, "第十条"),)
    assert [result.chunk.chunk_id for result in outcome.results] == ["chunk-plain"]
    assert lexical.calls == 0 and catalog.requests[0].law_title == PLAIN_TITLE


@pytest.mark.parametrize("title", UNSUPPORTED_TITLES)
@pytest.mark.parametrize("wrapped", [False, True])
def test_complete_unsupported_reference_is_empty_unknown_without_inner_fallback(title, wrapped):
    router, lexical, catalog = _mixed_route(title)
    spelling = f"《{title}》" if wrapped else title
    query = spelling + "第二条是什么？"
    assert router.unsupported_reference_titles(query) == (title,)
    outcome = router.retrieve_outcome(query)
    assert outcome.route == "exact_reference" and outcome.status == "needs_disambiguation"
    assert outcome.reason_codes == ("unsupported_catalog_title",)
    assert not outcome.results and not outcome.requested_pairs and not outcome.resolved_pairs
    assert not catalog.requests and lexical.calls == 0


def test_short_unsupported_spelling_inside_valid_quote_does_not_steal_reference():
    router, lexical, catalog = _mixed_route("甲法》")
    query = "《合成甲法》第十条是什么？"
    assert router.unsupported_reference_titles(query) == ()
    outcome = router.retrieve_outcome(query)
    assert outcome.status == "found" and outcome.resolved_pairs == ((PLAIN_TITLE, "第十条"),)
    assert lexical.calls == 0 and catalog.requests[0].law_title == PLAIN_TITLE


def test_unsupported_spelling_without_explicit_title_or_article_is_not_a_substring_gate():
    router, _, _ = _mixed_route("甲法》")
    assert router.unsupported_reference_titles("资料中的甲法》字样是如何排版的？") == ()
    assert router.unsupported_reference_titles("登记事项的一般规则") == ()
    assert router.unsupported_reference_titles("《甲法》》是什么？") == ("甲法》",)


def test_unsupported_title_capability_is_detached_from_mutable_caller_entries():
    title = UNSUPPORTED_TITLES[0]
    entries = [_entry(title=title)]
    corpus = BoundCorpus(_boundary(), tuple(entries))
    router = ExactReferenceRetriever(corpus=corpus,
        lexical=BoundaryBoundRetriever(Lexical(entries), corpus=corpus), catalog=Catalog(entries),
        pointer_revision=1, activation_id="activation-a")
    entries[0].chunk.law_names[:] = [PLAIN_TITLE]
    assert router.catalog_law_titles == router.unsupported_law_titles == (title,)
    with pytest.raises(AttributeError):
        router.unsupported_law_titles = ()


def test_unbound_catalog_identity_does_not_enter_supported_or_unsupported_capabilities():
    entry = _entry(title=PLAIN_TITLE)
    corpus = BoundCorpus(_boundary(), (entry,))
    foreign = _entry(title=UNSUPPORTED_TITLES[0], suffix="foreign")
    router = ExactReferenceRetriever(corpus=corpus,
        lexical=BoundaryBoundRetriever(Lexical([entry]), corpus=corpus), catalog=Catalog([entry, foreign]),
        pointer_revision=1, activation_id="activation-a")
    assert router.catalog_law_titles == router.known_law_hints == (PLAIN_TITLE,)
    assert router.unsupported_law_titles == ()


@pytest.mark.parametrize("top_k", [0, True, MAX_RETRIEVAL_TOP_K + 1])
def test_title_capability_guard_does_not_bypass_top_k_contract(top_k):
    router, lexical, catalog = _mixed_route(UNSUPPORTED_TITLES[0])
    with pytest.raises(ValueError):
        router.retrieve_outcome(f"《{UNSUPPORTED_TITLES[0]}》第二条", top_k=top_k)
    assert lexical.calls == 0 and not catalog.requests


def test_unsupported_title_guard_does_not_erase_reference_overflow_contract():
    router, lexical, catalog = _mixed_route(UNSUPPORTED_TITLES[0])
    query = "；".join(f"《合成甲法》第{number}条" for number in range(1, 18))
    query += f"；《{UNSUPPORTED_TITLES[0]}》第二条"
    outcome = router.retrieve_outcome(query)
    assert outcome.reason_codes == ("too_many_references",)
    assert outcome.status == "needs_disambiguation" and not outcome.results
    assert not outcome.requested_pairs and not outcome.resolved_pairs
    assert not catalog.requests and lexical.calls == 0


@pytest.mark.parametrize("query", [None, " " * (MAX_REFERENCE_QUERY_CHARS + 1)])
def test_unsupported_title_matcher_keeps_original_query_shape_bound(query):
    router, _, _ = _mixed_route(UNSUPPORTED_TITLES[0])
    with pytest.raises(ValueError):
        router.unsupported_reference_titles(query)


@pytest.mark.parametrize("title", UNSUPPORTED_TITLES)
def test_chat_ordinary_query_survives_unsupported_catalog_result(title):
    from legal_rag.chat import LegalChatAssistant
    router, lexical, catalog = _mixed_route(title)
    assistant = LegalChatAssistant(router, model="offline", top_k=2, adaptive_enabled=False,
                                   completion_client=object())
    retrieved = assistant.retrieve_turn(assistant.prepare_question("原始分块正文的一般登记事项"),
                                        max_followup_rounds=0)
    assert retrieved.results and retrieved.evidence_check.sufficient
    assert retrieved.route_outcome.route == "lexical" and retrieved.route_outcome.status == "found"
    assert lexical.calls == 1 and not catalog.requests


@pytest.mark.parametrize("title", UNSUPPORTED_TITLES)
def test_chat_complete_unsupported_reference_has_no_inner_exact_pair(title):
    from legal_rag.chat import LegalChatAssistant
    router, lexical, catalog = _mixed_route(title)
    assistant = LegalChatAssistant(router, model="offline", top_k=2, adaptive_enabled=False,
                                   completion_client=object())
    retrieved = assistant.retrieve_turn(assistant.prepare_question(title + "第二条是什么？"),
                                        max_followup_rounds=0)
    assert retrieved.route_outcome.reason_codes == ("unsupported_catalog_title",)
    assert retrieved.route_outcome.status == "needs_disambiguation"
    assert not retrieved.results and not retrieved.route_outcome.requested_pairs
    assert not retrieved.evidence_check.sufficient and not retrieved.evidence_check.followup_queries
    assert not catalog.requests and lexical.calls == 0
