"""Real-library contracts using fictional text, not legal quality targets."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import math

import bm25s
import jieba
import pytest

from legal_rag.chinese_bm25 import ChineseBM25Retriever, chinese_bm25_identity
from legal_rag.models import Chunk
from legal_rag.retrieval import BM25Retriever
from legal_rag.retrieval_contracts import RetrievalContractError


def chunk(key, text, *, laws=(), articles=(), metadata=None):
    return Chunk(key, text, list(laws), list(articles), ["fictional.txt"], [1],
                 "article", metadata or {})


@pytest.mark.parametrize("mode", ["precise", "search"])
def test_real_chinese_library_matches_subsentence_and_preserves_original_identity(mode):
    target = chunk("target", "商品质量不合格可以退货。")
    other = chunk("other", "工资支付应当及时。")
    retriever = ChineseBM25Retriever([target, other], mode=mode)
    rows = retriever.retrieve("退货", top_k=8)
    assert [row.chunk.chunk_id for row in rows] == ["target"]
    assert rows[0].chunk is target
    assert rows[0].provenance is None
    assert rows[0].retriever == "bm25"
    assert math.isfinite(rows[0].score) and rows[0].score > 0
    assert rows[0].trace["score_kind"] == "bm25"
    assert rows[0].trace["config_fingerprint"] == retriever.fingerprint


def test_fixed_index_text_uses_titles_article_labels_and_body_without_mutation():
    original = chunk("target", "退货流程。", laws=("合成甲法", "合成乙法"), articles=("第十条",))
    before = deepcopy(original)
    retriever = ChineseBM25Retriever([original])
    assert retriever.index_texts == ("合成甲法\n合成乙法\n第十条\n退货流程。",)
    assert retriever.retrieve("合成乙法")
    assert original == before
    assert isinstance(retriever.known_law_hints, tuple)
    assert set(retriever.known_law_hints) == {"合成甲法", "合成乙法"}


@pytest.mark.parametrize("mode", ["precise", "search"])
def test_document_tf_is_preserved_query_is_unique_and_no_dictionary_growth(mode):
    # Explicit boundaries make repetition independent of the analyzer's choice
    # to keep the dictionary word 生态环境 whole in precise mode.
    retriever = ChineseBM25Retriever([chunk("x", "生态，生态；环境，环境。")], mode=mode)
    assert retriever.document_tokens[0].count("生态") == 2
    before = dict(retriever._segmenter.FREQ)
    assert retriever.retrieve("生态")[0].score == retriever.retrieve("生态，生态，生态")[0].score
    assert retriever.retrieve("zyxwvu_missing_feature_20261006") == []
    assert retriever._segmenter.FREQ == before


@pytest.mark.parametrize("query", ["", " \n\t", "，。！", "zyxwvu_missing_feature_20261006"])
def test_empty_punctuation_and_unknown_queries_do_not_return_zero_score_candidates(query):
    assert ChineseBM25Retriever([]).retrieve(query) == []
    assert ChineseBM25Retriever([chunk("a", "商品退货。")]).retrieve(query) == []


def test_empty_documents_are_safe():
    assert ChineseBM25Retriever([chunk("a", ""), chunk("b", "，。")]).retrieve("退货") == []


@pytest.mark.parametrize("top_k", [True, False, 0, -1, 1.5, "1", None, 10001])
def test_invalid_top_k_is_rejected_even_for_empty_corpus(top_k):
    with pytest.raises(RetrievalContractError):
        ChineseBM25Retriever([]).retrieve("", top_k=top_k)


@pytest.mark.parametrize("query", [None, 1, True, b"abc"])
def test_query_must_be_text(query):
    with pytest.raises(ValueError, match="query"):
        ChineseBM25Retriever([]).retrieve(query)


def test_ties_use_chunk_identity_before_top_k_and_ignore_input_order():
    a, b, c = [chunk(key, "商品退货。") for key in ("a", "b", "c")]
    for source in ([c, b, a], [a, c, b]):
        rows = ChineseBM25Retriever(source).retrieve("退货", top_k=2)
        assert [row.chunk.chunk_id for row in rows] == ["a", "b"]
        assert [row.rank for row in rows] == [1, 2]


def test_no_manual_boost_or_deprecated_multiplier():
    a = chunk("a", "商品退货。", laws=("合成甲法",), articles=("第十条",))
    b = chunk("b", "商品退货。", laws=("合成甲法",), articles=("第十条",),
              metadata={"deprecated": True})
    rows = ChineseBM25Retriever([a, b]).retrieve("合成甲法第十条")
    assert rows[0].score == rows[1].score
    assert all("metadata_boost" not in row.trace and "deprecated_penalty" not in row.trace for row in rows)


def test_modes_use_independent_default_segmenters():
    precise = ChineseBM25Retriever([chunk("a", "中国科学院计算所。")], mode="precise")
    search = ChineseBM25Retriever([chunk("a", "中国科学院计算所。")], mode="search")
    assert precise._segmenter is not search._segmenter
    assert precise._segmenter is not jieba.dt
    assert "中国科学院" in precise.document_tokens[0]
    assert "科学" not in precise.document_tokens[0]
    assert "科学" in search.document_tokens[0]
    assert precise.fingerprint != search.fingerprint


def test_configuration_is_complete_stable_canonical_and_defensive():
    one = ChineseBM25Retriever([chunk("a", "退货。")])
    two = ChineseBM25Retriever([chunk("b", "工资。")])
    assert one.config_identity == two.config_identity
    assert one.fingerprint == two.fingerprint
    identity = one.config_identity
    assert identity["engine"]["name"] == "bm25s"
    assert identity["engine"]["version"] == "0.3.9"
    assert identity["engine"]["method"] == "lucene"
    assert identity["tokenizer"]["name"] == "jieba"
    assert identity["tokenizer"]["version"] == "0.42.1"
    assert identity["tokenizer"]["mode"] == "search"
    assert identity["tokenizer"]["hmm"] is True
    assert len(identity["tokenizer"]["dictionary_sha256"]) == 64
    assert set(identity["tokenizer"]["hmm_resource_sha256"]) == {
        "prob_start.py", "prob_trans.py", "prob_emit.py"}
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert hashlib.sha256(payload.encode("utf-8")).hexdigest() == one.fingerprint
    identity["tokenizer"]["hmm"] = False
    identity["engine"]["k1"] = 99
    assert one.config_identity == two.config_identity
    assert ChineseBM25Retriever([], hmm=False).fingerprint != one.fingerprint
    assert ChineseBM25Retriever([], k1=1.2).fingerprint != one.fingerprint
    with pytest.raises(AttributeError):
        one.mode = "precise"
    with pytest.raises(AttributeError):
        one.hmm = False


def test_identity_function_does_not_build_an_index_or_a_prefix_dictionary(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("identity must not construct a retriever/tokenizer")
    monkeypatch.setattr(jieba, "Tokenizer", forbidden)
    monkeypatch.setattr(bm25s, "BM25", forbidden)
    result = chinese_bm25_identity(mode="precise", k1=1.2, b=0.6, hmm=False)
    assert result["tokenizer"]["mode"] == "precise"
    assert result["engine"]["k1"] == 1.2
    assert result["engine"]["b"] == 0.6
    assert result["tokenizer"]["hmm"] is False


def test_concurrent_queries_are_repeatable_without_mutating_vocabulary():
    retriever = ChineseBM25Retriever([chunk("b", "商品退货。"), chunk("a", "工资支付。")])
    before = dict(retriever._engine.vocab_dict)
    queries = ["退货", "工资", "unknown_zyxwvu", "退货退货"] * 6
    expected = [[(row.chunk.chunk_id, row.score) for row in retriever.retrieve(query)] for query in queries]
    with ThreadPoolExecutor(max_workers=4) as pool:
        actual = list(pool.map(lambda query: [(r.chunk.chunk_id, r.score) for r in retriever.retrieve(query)], queries))
    assert actual == expected
    assert retriever._engine.vocab_dict == before


@pytest.mark.parametrize("kwargs", [
    {"mode": "full"}, {"mode": None}, {"mode": []}, {"hmm": 1}, {"k1": True}, {"k1": 0},
    {"k1": float("nan")}, {"k1": float("inf")},
    {"b": False}, {"b": -0.1}, {"b": 1.1}, {"b": float("nan")},
])
def test_invalid_configuration_fails_closed(kwargs):
    with pytest.raises(ValueError):
        ChineseBM25Retriever([], **kwargs)


def test_duplicate_or_invalid_chunk_identity_is_rejected():
    for chunks in ([chunk("a", "退货"), chunk("a", "工资")], [chunk("", "退货")]):
        with pytest.raises(ValueError, match="chunk"):
            ChineseBM25Retriever(chunks)


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), -1.0, True, "1.0"])
def test_invalid_library_scores_are_not_silently_hidden(monkeypatch, score):
    retriever = ChineseBM25Retriever([chunk("a", "退货。")])
    monkeypatch.setattr(retriever._engine, "get_scores", lambda terms: [score])
    with pytest.raises(ValueError, match="score"):
        retriever.retrieve("退货")


def test_real_lucene_library_scores_not_an_adapter_formula():
    retriever = ChineseBM25Retriever([chunk("a", "商品退货退货。"), chunk("b", "工资。")])
    expected = retriever._engine.get_scores(["退货"])
    row = retriever.retrieve("退货")[0]
    assert row.score == float(expected[0])
    assert retriever._engine.method == "lucene"


def test_engine_only_formula_diagnostic_matches_historical_atire_lucene_combination():
    # This is an engineering diagnostic, not an allowed modern runtime variant.
    chunks = [chunk("a", "生态生态环境。"), chunk("b", "环境保护。"), chunk("c", "工资支付。")]
    old = BM25Retriever(chunks, law_boost=0, article_boost=0)
    library = bm25s.BM25(method="atire", idf_method="lucene", k1=old.k1, b=old.b, dtype="float64")
    library.index(old.doc_tokens, show_progress=False)
    for query in ("生态", "生态生态", "环境保护", "工资", "absent_zyxwvu"):
        old_rows = {r.chunk.chunk_id: r.score for r in old.retrieve(query, top_k=10)}
        terms, _ = old._query_features(query)
        scores = library.get_scores(terms)
        for index, item in enumerate(chunks):
            assert float(scores[index]) == pytest.approx(old_rows.get(item.chunk_id, 0.0), rel=1e-12, abs=1e-12)
