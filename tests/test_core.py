import unittest
from pathlib import Path

from legal_rag.chunking import build_chunks
from legal_rag.config import load_config
from legal_rag.data import parse_law_file, profile_dataset
from legal_rag.embeddings import resolve_embedding_model
from legal_rag.models import SearchResult
from legal_rag.retrieval import BM25Retriever, RRFHybridRetriever


class CoreTest(unittest.TestCase):
    def make_dataset(self) -> Path:
        return Path(__file__).parent / "fixtures" / "Chinese-Laws"

    def test_parse_with_law_metadata(self) -> None:
        data_dir = self.make_dataset()
        articles = parse_law_file(data_dir / "中华人民共和国民法典.txt")
        self.assertEqual(articles[0].law_name, "中华人民共和国民法典")
        self.assertEqual(articles[0].article_number, "第八条")
        self.assertEqual(articles[0].parse_status, "with_law")

    def test_parse_from_filename_metadata(self) -> None:
        data_dir = self.make_dataset()
        articles = parse_law_file(data_dir / "中华人民共和国商业银行法.txt")
        self.assertEqual(articles[0].law_name, "中华人民共和国商业银行法")
        self.assertEqual(articles[0].article_number, "第一条")
        self.assertEqual(articles[0].parse_status, "from_filename")

    def test_profile_and_chunk_candidates(self) -> None:
        data_dir = self.make_dataset()
        profile = profile_dataset(data_dir)
        self.assertEqual(profile["file_count"], 2)
        self.assertEqual(profile["article_count"], 4)
        self.assertGreaterEqual(profile["parse_rate"], 1.0)
        candidate_names = [item["name"] for item in profile["chunk_candidates"]]
        self.assertIn("article", candidate_names)
        self.assertIn("fixed_chars", candidate_names)

    def test_chunk_and_bm25_retrieval(self) -> None:
        data_dir = self.make_dataset()
        articles = []
        for path in data_dir.glob("*.txt"):
            articles.extend(parse_law_file(path))
        chunks = build_chunks(articles, "article")
        retriever = BM25Retriever(chunks)
        results = retriever.retrieve("民法典 第九条 生态环境", top_k=3)
        self.assertTrue(results)
        self.assertIn("第九条", results[0].chunk.article_numbers)

    def test_rrf_fuses_bm25_and_dense_rankings(self) -> None:
        data_dir = self.make_dataset()
        articles = []
        for path in data_dir.glob("*.txt"):
            articles.extend(parse_law_file(path))
        chunks = build_chunks(articles, "article")

        class StaticRetriever:
            def __init__(self, name, ordered_chunks):
                self.name = name
                self.ordered_chunks = ordered_chunks

            def retrieve(self, query: str, top_k: int = 5):
                return [
                    SearchResult(chunk=chunk, score=1.0 / rank, rank=rank, retriever=self.name)
                    for rank, chunk in enumerate(self.ordered_chunks[:top_k], start=1)
                ]

        bm25 = StaticRetriever("bm25", [chunks[0], chunks[1], chunks[2]])
        dense = StaticRetriever("dense", [chunks[1], chunks[0], chunks[2]])
        rrf = RRFHybridRetriever(bm25, dense, rrf_k=60)
        results = rrf.retrieve("生态环境", top_k=2)

        self.assertEqual(results[0].retriever, "rrf")
        self.assertIn(results[0].chunk, [chunks[0], chunks[1]])

    def test_qwen3_embedding_uses_siliconflow_provider(self) -> None:
        config = load_config()
        model = resolve_embedding_model(config, "qwen3_embedding_4b")
        self.assertEqual(model.provider, "siliconflow")
        self.assertEqual(model.api_key_env, "SILICONFLOW_API_KEY")
        self.assertIn("Qwen3-Embedding-4B", model.model_name)


if __name__ == "__main__":
    unittest.main()
