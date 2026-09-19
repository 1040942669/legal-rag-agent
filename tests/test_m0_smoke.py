from __future__ import annotations

import socket
from pathlib import Path


SYNTHETIC_FIXTURE = (
    Path(__file__).parent / "fixtures" / "synthetic" / "synthetic_non_law.txt"
)


def test_synthetic_parse_chunk_and_bm25_smoke_without_network(monkeypatch) -> None:
    def block_network(*_args, **_kwargs):
        raise AssertionError("M0 offline smoke attempted to use the network")

    monkeypatch.setattr(socket, "socket", block_network)
    monkeypatch.setattr(socket, "create_connection", block_network)

    from legal_rag.chunking import build_chunks
    from legal_rag.data import parse_law_file
    from legal_rag.retrieval import BM25Retriever

    articles = parse_law_file(SYNTHETIC_FIXTURE)
    assert any("并非真实法律条文" in article.raw_text for article in articles)
    assert [article.article_number for article in articles if article.article_number] == [
        "第一条",
        "第二条",
        "第三条",
    ]

    chunks = build_chunks(articles, "article")
    results = BM25Retriever(chunks).retrieve(
        "《星河数据测试规则（虚构）》第二条 琥珀令牌",
        top_k=1,
    )

    assert results
    assert results[0].retriever == "bm25"
    assert results[0].chunk.article_numbers == ["第二条"]
    assert "琥珀令牌" in results[0].chunk.text
