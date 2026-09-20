import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from legal_rag.chunking import build_chunks
from legal_rag.config import load_config
from legal_rag.data import parse_law_file, profile_dataset
from legal_rag.diagnostics import build_chunk_diagnostics
from legal_rag.embeddings import (
    EmbeddingModelConfig,
    SentenceTransformerEncoder,
    SiliconFlowEmbeddingEncoder,
    resolve_embedding_model,
)
from legal_rag.env import clean_env_value, live_model_calls_allowed, load_dotenv
from legal_rag.evaluation import render_eval_report
from legal_rag.failure_analysis import label_retrieval_failure
from legal_rag.indexing import build_index
from legal_rag.manifest import write_artifact_manifest
from legal_rag.llm import OllamaClient, SiliconFlowClient
from legal_rag.models import EvalCase, EvalRecord, SearchResult
from legal_rag.query import analyze_query
from legal_rag.retrieval import BM25Retriever, RRFHybridRetriever, format_sources
from legal_rag.tracing import JsonlTraceWriter, build_retrieval_trace_record


class CoreTest(unittest.TestCase):
    def make_dataset(self) -> Path:
        return Path(__file__).parent / "fixtures" / "Chinese-Laws"

    def make_workspace_temp(self) -> Path:
        path = Path(".tmp") / "tests" / uuid4().hex
        path.mkdir(parents=True, exist_ok=True)
        return path

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

    def test_neighbor_chunks_support_sliding_stride(self) -> None:
        data_dir = self.make_dataset()
        articles = parse_law_file(data_dir / "中华人民共和国民法典.txt")
        chunks = build_chunks(
            articles,
            "neighbor",
            neighbor_window=2,
            neighbor_stride=1,
        )

        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0].article_numbers, ["第八条", "第九条"])
        self.assertEqual(chunks[1].article_numbers, ["第九条", "第十九条"])
        self.assertEqual(chunks[0].metadata["neighbor_stride"], 1)

    def test_chunk_diagnostics_summarizes_lengths_and_anomalies(self) -> None:
        data_dir = self.make_dataset()
        articles = parse_law_file(data_dir / "中华人民共和国民法典.txt")
        chunks = build_chunks(
            articles, "neighbor", neighbor_window=2, neighbor_stride=1
        )
        diagnostics = build_chunk_diagnostics(chunks, strategy="neighbor")

        self.assertEqual(diagnostics["strategy"], "neighbor")
        self.assertEqual(diagnostics["chunk_count"], 3)
        self.assertIn("char_length", diagnostics)
        self.assertIn("article_span_length", diagnostics)

    def test_query_analyzer_extracts_law_article_and_risk_flags(self) -> None:
        analysis = analyze_query(
            "《中华人民共和国民法典》第一百一十九条规定了什么？我很急！"
        )

        self.assertIn("中华人民共和国民法典", analysis.law_names)
        self.assertIn("第一百一十九条", analysis.article_numbers)
        self.assertIn("emotional", analysis.risk_flags)
        self.assertIn("article_lookup", analysis.case_type_hints)

    def test_bm25_boost_parameters_are_configurable(self) -> None:
        data_dir = self.make_dataset()
        articles = parse_law_file(data_dir / "中华人民共和国民法典.txt")
        chunks = build_chunks(articles, "article")
        retriever = BM25Retriever(chunks, law_boost=0.0, article_boost=7.0)
        results = retriever.retrieve("民法典 第九条", top_k=3)

        self.assertTrue(results)
        self.assertEqual(results[0].trace["bm25_article_boost"], 7.0)
        self.assertIn("metadata_boost", results[0].trace)

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
                    SearchResult(
                        chunk=chunk, score=1.0 / rank, rank=rank, retriever=self.name
                    )
                    for rank, chunk in enumerate(self.ordered_chunks[:top_k], start=1)
                ]

        bm25 = StaticRetriever("bm25", [chunks[0], chunks[1], chunks[2]])
        dense = StaticRetriever("dense", [chunks[1], chunks[0], chunks[2]])
        rrf = RRFHybridRetriever(bm25, dense, rrf_k=60)
        results = rrf.retrieve("生态环境", top_k=2)

        self.assertEqual(results[0].retriever, "rrf")
        self.assertIn(results[0].chunk, [chunks[0], chunks[1]])
        self.assertIn("fused_score", results[0].trace)
        self.assertTrue(
            "bm25_rank" in results[0].trace or "dense_rank" in results[0].trace
        )
        self.assertIn("trace=", format_sources(results))

    def test_failure_labeler_marks_wrong_law_and_hit(self) -> None:
        data_dir = self.make_dataset()
        articles = []
        for path in data_dir.glob("*.txt"):
            articles.extend(parse_law_file(path))
        chunks = build_chunks(articles, "article")
        results = [SearchResult(chunk=chunks[0], score=1.0, rank=1, retriever="bm25")]

        hit_case = EvalCase(
            case_id="hit",
            question="",
            case_type="article_lookup",
            expected_law=chunks[0].law_names[0],
            expected_articles=[chunks[0].article_numbers[0]],
            keywords=[],
        )
        wrong_law_case = EvalCase(
            case_id="wrong-law",
            question="",
            case_type="article_lookup",
            expected_law="不存在的法律",
            expected_articles=["第一条"],
            keywords=[],
        )

        self.assertEqual(label_retrieval_failure(results, hit_case).label, "hit")
        self.assertEqual(
            label_retrieval_failure(results, wrong_law_case).label, "wrong_law"
        )

    def test_trace_writer_records_retrieval_schema(self) -> None:
        data_dir = self.make_dataset()
        articles = parse_law_file(data_dir / "中华人民共和国民法典.txt")
        chunks = build_chunks(articles, "article")
        result = SearchResult(
            chunk=chunks[0],
            score=1.0,
            rank=1,
            retriever="bm25",
            trace={"metadata_boost": 80.0},
        )
        trace_path = self.make_workspace_temp() / "trace.jsonl"
        writer = JsonlTraceWriter(trace_path, run_id="run_trace_test")
        writer.write(
            build_retrieval_trace_record(
                query="民法典第八条",
                retriever="bm25",
                top_k=1,
                results=[result],
                latency_ms=3,
                analyzer=analyze_query("民法典第八条").to_dict(),
            )
        )
        record = json.loads(trace_path.read_text(encoding="utf-8").strip())

        self.assertEqual(record["run_id"], "run_trace_test")
        self.assertEqual(record["results"][0]["chunk_id"], chunks[0].chunk_id)
        self.assertEqual(record["results"][0]["ranking_trace"]["metadata_boost"], 80.0)

    def test_qwen3_embedding_uses_siliconflow_provider(self) -> None:
        config = load_config()
        model = resolve_embedding_model(config, "qwen3_embedding_4b")
        self.assertEqual(model.provider, "siliconflow")
        self.assertEqual(model.api_key_env, "SILICONFLOW_API_KEY")
        self.assertIn("Qwen3-Embedding-4B", model.model_name)

    def test_clean_env_value_strips_matching_quotes(self) -> None:
        self.assertEqual(clean_env_value('"abc"'), "abc")
        self.assertEqual(clean_env_value("'abc'"), "abc")
        self.assertEqual(clean_env_value("abc"), "abc")

    def test_dotenv_loading_can_be_disabled_for_offline_execution(self) -> None:
        root = self.make_workspace_temp()
        dotenv_path = root / ".env"
        dotenv_path.write_text("OFFLINE_TEST_VALUE=must-not-load\n", encoding="utf-8")

        with patch.dict(os.environ, {"LEGAL_RAG_DISABLE_DOTENV": "1"}, clear=False):
            os.environ.pop("OFFLINE_TEST_VALUE", None)
            self.assertIsNone(load_dotenv(dotenv_path))
            self.assertNotIn("OFFLINE_TEST_VALUE", os.environ)

    def test_live_provider_clients_fail_closed_when_disabled(self) -> None:
        embedding_config = EmbeddingModelConfig(
            key="remote-test",
            provider="siliconflow",
            model_name="remote-test-model",
            role="document",
        )

        with patch.dict(os.environ, {"ALLOW_LIVE_MODEL_CALLS": "false"}, clear=False):
            for label, invoke in (
                ("ollama", lambda: OllamaClient(model="test").complete("prompt")),
                (
                    "siliconflow",
                    lambda: SiliconFlowClient(model="test").complete("prompt"),
                ),
                (
                    "sentence-transformer",
                    lambda: SentenceTransformerEncoder(embedding_config),
                ),
                ("embedding", lambda: SiliconFlowEmbeddingEncoder(embedding_config)),
            ):
                with self.subTest(provider=label):
                    with self.assertRaisesRegex(RuntimeError, "ALLOW_LIVE_MODEL_CALLS"):
                        invoke()

    def test_live_model_calls_default_to_fail_closed(self) -> None:
        for value in (None, "", "false", "0", "unexpected"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("ALLOW_LIVE_MODEL_CALLS", None)
                    if value is not None:
                        os.environ["ALLOW_LIVE_MODEL_CALLS"] = value
                    self.assertFalse(live_model_calls_allowed())

        for value in ("1", "true", "YES", " on "):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ,
                    {"ALLOW_LIVE_MODEL_CALLS": value},
                    clear=False,
                ):
                    self.assertTrue(live_model_calls_allowed())

    def test_manifest_writer_records_reproducibility_fields(self) -> None:
        manifest_path = self.make_workspace_temp() / "manifest.json"
        manifest = write_artifact_manifest(
            manifest_path,
            artifact_type="index",
            run_id="run_test",
            inputs={"dataset_dir": "dataset"},
            config={"strategy": "article"},
            outputs={"chunks_path": "chunks.jsonl"},
            metrics={"chunk_count": 2},
        )
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["run_id"], "run_test")
        self.assertEqual(saved["manifest_version"], 1)
        self.assertEqual(saved["config"]["strategy"], "article")
        self.assertEqual(saved["metrics"]["chunk_count"], 2)

    def test_build_index_writes_manifest(self) -> None:
        data_dir = self.make_dataset()
        root = self.make_workspace_temp()
        profile_path = root / "profile.json"
        profile_path.write_text("{}", encoding="utf-8")
        metadata = build_index(
            dataset_dir=data_dir,
            profile_path=profile_path,
            output_root=root / "indexes",
            strategy="article",
            chunking_config=load_config()["chunking"],
            run_id="run_index_test",
        )
        manifest_path = Path(metadata["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(metadata["run_id"], "run_index_test")
        self.assertEqual(manifest["run_id"], "run_index_test")
        self.assertEqual(manifest["metrics"]["chunk_count"], metadata["chunk_count"])
        self.assertTrue(Path(metadata["diagnostics_path"]).exists())

    def test_eval_report_includes_run_metadata(self) -> None:
        record = EvalRecord(
            case_id="case-1",
            case_type="article_lookup",
            model="retrieval-only",
            retriever="bm25",
            chunk_strategy="article",
            hit_at_3=1,
            hit_at_5=1,
            mrr=1.0,
            target_coverage=1.0,
            keyword_coverage=1.0,
            citation_hit=1,
            sufficiency_pass=1,
            citation_valid=1,
            verifier_pass=1,
            refusal_correctness=1,
            latency_ms=1,
            answer="answer",
            sources="[S1] source",
        )
        report = render_eval_report(
            [record],
            metadata={
                "run_id": "run_eval_test",
                "case_path": "eval_cases/legal_eval_cases_v2.jsonl",
                "top_k": 5,
                "chunk_count": 4,
                "index_manifest_path": "indexes/article/manifest.json",
            },
        )

        self.assertIn("run_eval_test", report)
        self.assertIn("legal_eval_cases_v2.jsonl", report)
        self.assertIn("Chunk 数: 4", report)
        self.assertIn("失败归因", report)
        self.assertIn("Evidence sufficiency pass", report)


if __name__ == "__main__":
    unittest.main()
