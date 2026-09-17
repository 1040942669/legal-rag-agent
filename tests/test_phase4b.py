from __future__ import annotations

import json
import unittest
from pathlib import Path
from uuid import uuid4

import numpy as np

from legal_rag.cli import build_parser
from legal_rag.embeddings import (
    EMBEDDING_CACHE_SCHEMA_VERSION,
    EmbeddingModelConfig,
    chunk_corpus_fingerprint,
    embedding_contract_fingerprint,
    inspect_embedding_cache,
)
from legal_rag.evaluation import evaluate
from legal_rag.experiments import (
    ExperimentSpec,
    build_experiment_specs,
    summarize_experiment,
    write_experiment_matrix,
)
from legal_rag.llm import CompletionUsage, extract_token_counts, usage_delta
from legal_rag.models import Chunk, EvalCase, EvalRecord, SearchResult
from legal_rag.rerank import CrossEncoderReranker, RerankerConfig, RerankingRetriever


def make_chunk(chunk_id: str, text: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        text=text,
        law_names=["中华人民共和国测试法"],
        article_numbers=[f"第{chunk_id}条"],
        source_files=["test.txt"],
        line_nos=[1],
        strategy="article",
    )


def make_record(case_id: str, latency_ms: int, *, hit: int = 1, error: str = "") -> EvalRecord:
    return EvalRecord(
        case_id=case_id,
        case_type="article_lookup",
        model="retrieval-only",
        retriever="bm25",
        chunk_strategy="article",
        hit_at_3=hit,
        hit_at_5=hit,
        mrr=float(hit),
        target_coverage=float(hit),
        keyword_coverage=float(hit),
        citation_hit=hit,
        sufficiency_pass=hit,
        citation_valid=-1,
        verifier_pass=-1,
        refusal_correctness=-1,
        latency_ms=latency_ms,
        answer="answer",
        sources="sources",
        error=error,
        failure_label="hit" if hit else "missed_article",
    )


class CacheHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(".tmp") / "tests" / uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        self.chunks = [make_chunk("一", "第一条文本"), make_chunk("二", "第二条文本")]
        self.model = EmbeddingModelConfig(
            key="test_embedding",
            provider="sentence_transformers",
            model_name="test/model",
            role="test",
            query_prefix="query: ",
            document_prefix="passage: ",
        )
        vectors = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype="float32")
        np.save(self.root / "vectors.npy", vectors)
        (self.root / "chunk_ids.json").write_text(
            json.dumps([chunk.chunk_id for chunk in self.chunks], ensure_ascii=False),
            encoding="utf-8",
        )
        metadata = {
            "schema_version": EMBEDDING_CACHE_SCHEMA_VERSION,
            "embedding_key": self.model.key,
            "provider": self.model.provider,
            "model_name": self.model.model_name,
            "normalize": self.model.normalize,
            "trust_remote_code": self.model.trust_remote_code,
            "query_prefix": self.model.query_prefix,
            "document_prefix": self.model.document_prefix,
            "embed_with_metadata": self.model.embed_with_metadata,
            "embedding_contract_fingerprint": embedding_contract_fingerprint(self.model),
            "chunk_strategy": "article",
            "chunk_count": 2,
            "vector_count": 2,
            "dimension": 2,
            "dtype": "float32",
            "chunk_fingerprint": chunk_corpus_fingerprint(self.chunks),
        }
        (self.root / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_cache_health_accepts_matching_v2_contract(self) -> None:
        report = inspect_embedding_cache(
            self.root,
            chunks=self.chunks,
            model_config=self.model,
            expected_chunk_strategy="article",
        )

        self.assertTrue(report.valid)
        self.assertEqual(report.status, "healthy")

    def test_cache_health_detects_same_id_content_drift(self) -> None:
        changed = [make_chunk("一", "文本已经改变"), self.chunks[1]]
        report = inspect_embedding_cache(
            self.root,
            chunks=changed,
            model_config=self.model,
            expected_chunk_strategy="article",
        )

        self.assertFalse(report.valid)
        self.assertIn("chunk_fingerprint", {issue.code for issue in report.issues})

    def test_legacy_contract_can_be_audited_without_being_silently_reused(self) -> None:
        metadata_path = self.root / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.pop("schema_version")
        metadata.pop("chunk_fingerprint")
        metadata.pop("embedding_contract_fingerprint")
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        strict = inspect_embedding_cache(
            self.root,
            chunks=self.chunks,
            model_config=self.model,
        )
        advisory = inspect_embedding_cache(
            self.root,
            chunks=self.chunks,
            model_config=self.model,
            strict_contract=False,
        )

        self.assertFalse(strict.valid)
        self.assertTrue(advisory.valid)
        self.assertEqual(advisory.status, "warning")


class RerankerTest(unittest.TestCase):
    def test_cross_encoder_adapter_builds_metadata_aware_pairs_lazily(self) -> None:
        captured: dict = {}

        class FakeModel:
            def predict(self, pairs, **kwargs):
                captured["pairs"] = pairs
                captured["predict_kwargs"] = kwargs
                return [0.75]

        def factory(model_name: str, **kwargs):
            captured["model_name"] = model_name
            captured["model_kwargs"] = kwargs
            return FakeModel()

        config = RerankerConfig(
            key="fake_cross_encoder",
            provider="sentence_transformers_cross_encoder",
            model_name="test/reranker",
            batch_size=4,
            max_length=256,
        )
        reranker = CrossEncoderReranker(config, model_factory=factory)
        result = SearchResult(
            chunk=make_chunk("一", "条文正文"),
            score=1.0,
            rank=1,
            retriever="static",
        )

        self.assertEqual(reranker.score("问题", [result]), [0.75])
        self.assertEqual(captured["model_name"], "test/reranker")
        self.assertEqual(captured["model_kwargs"]["max_length"], 256)
        self.assertIn("中华人民共和国测试法", captured["pairs"][0][1])
        self.assertEqual(captured["predict_kwargs"]["batch_size"], 4)

    def test_reranking_preserves_base_trace_and_collects_usage(self) -> None:
        chunks = [make_chunk("一", "a"), make_chunk("二", "b"), make_chunk("三", "c")]

        class StaticRetriever:
            name = "static"

            def retrieve(self, query: str, top_k: int = 5):
                return [
                    SearchResult(
                        chunk=chunk,
                        score=1.0 / rank,
                        rank=rank,
                        retriever=self.name,
                        trace={"source": "base"},
                    )
                    for rank, chunk in enumerate(chunks[:top_k], start=1)
                ]

        class ReverseReranker:
            name = "fake"

            def score(self, query: str, results: list[SearchResult]) -> list[float]:
                return [float(result.chunk.chunk_id == "三") for result in results]

        retriever = RerankingRetriever(StaticRetriever(), ReverseReranker(), candidate_top_n=3)
        results = retriever.retrieve("query", top_k=2)

        self.assertEqual(results[0].chunk.chunk_id, "三")
        self.assertEqual(results[0].trace["base_rank"], 3)
        self.assertEqual(results[0].trace["source"], "base")
        self.assertEqual(retriever.stats.calls, 1)
        self.assertEqual(retriever.stats.documents, 3)

    def test_reranking_counts_failed_model_calls(self) -> None:
        chunk = make_chunk("一", "a")

        class StaticRetriever:
            name = "static"

            def retrieve(self, query: str, top_k: int = 5):
                return [SearchResult(chunk=chunk, score=1.0, rank=1, retriever="static")]

        class FailingReranker:
            name = "failing"

            def score(self, query: str, results: list[SearchResult]) -> list[float]:
                raise RuntimeError("model load failed")

        retriever = RerankingRetriever(StaticRetriever(), FailingReranker())
        with self.assertRaisesRegex(RuntimeError, "model load failed"):
            retriever.retrieve("query")

        self.assertEqual(retriever.stats.calls, 1)
        self.assertEqual(retriever.stats.failed_calls, 1)


class ExperimentHarnessTest(unittest.TestCase):
    def test_bm25_does_not_duplicate_cells_for_each_embedding(self) -> None:
        specs = build_experiment_specs(
            chunk_strategies=["article"],
            retrievers=["bm25", "dense"],
            embeddings=["e1", "e2"],
            adaptive_modes=["direct"],
            rerankers=["none"],
        )

        self.assertEqual(len(specs), 3)
        self.assertEqual(sum(spec.retriever == "bm25" for spec in specs), 1)

    def test_summary_includes_tail_latency_and_runtime_metrics(self) -> None:
        spec = ExperimentSpec("article", "bm25", "none", False, "none")
        records = [make_record("a", 10), make_record("b", 20), make_record("c", 100, hit=0)]
        row = summarize_experiment(
            spec,
            records,
            top_k=5,
            build_seconds=0.2,
            build_peak_mb=3.0,
            runtime_metrics={"rerank_calls": 2, "rerank_documents": 10, "rerank_total_ms": 7.5},
        )

        self.assertEqual(row["hit_at_5"], 0.6667)
        self.assertIsNotNone(row["hit_at_5_ci_low"])
        self.assertGreater(row["p95_latency_ms"], row["p50_latency_ms"])
        self.assertEqual(row["rerank_documents"], 10)

    def test_matrix_marks_partial_cells_and_excludes_runtime_errors_from_quality(self) -> None:
        spec = ExperimentSpec("article", "bm25", "none", False, "none")
        row = summarize_experiment(
            spec,
            [make_record("ok", 10), make_record("error", 1, hit=0, error="model load failed")],
            top_k=5,
            build_seconds=0.1,
            build_peak_mb=1.0,
        )

        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["successful_case_count"], 1)
        self.assertEqual(row["hit_at_5"], 1.0)
        self.assertEqual(row["eval_error_count"], 1)

    def test_matrix_writer_emits_three_machine_and_human_readable_formats(self) -> None:
        output_dir = Path(".tmp") / "tests" / uuid4().hex
        spec = ExperimentSpec("article", "bm25", "none", False, "none")
        row = summarize_experiment(
            spec,
            [make_record("a", 1)],
            top_k=5,
            build_seconds=0.01,
            build_peak_mb=1.0,
        )
        paths = write_experiment_matrix([row], output_dir, "matrix", metadata={"run_id": "test"})

        self.assertTrue(all(path.exists() for path in paths))
        self.assertIn("| 状态 |", paths[2].read_text(encoding="utf-8"))


class UsageAndCliTest(unittest.TestCase):
    def test_usage_extracts_ollama_and_openai_shapes(self) -> None:
        self.assertEqual(
            extract_token_counts({"prompt_eval_count": 3, "eval_count": 4})["output_tokens"],
            4,
        )
        usage = CompletionUsage()
        before = usage.snapshot()
        usage.calls += 1
        usage.record_tokens(input_tokens=3, output_tokens=4, total_tokens=7)
        delta = usage_delta(before, usage.snapshot())
        self.assertEqual(delta["total_tokens"], 7)

    def test_evaluation_attributes_usage_to_assistant_and_judge(self) -> None:
        result = SearchResult(
            chunk=make_chunk("一", "第一条文本"),
            score=1.0,
            rank=1,
            retriever="static",
        )

        class TrackingClient:
            def __init__(self, response: str):
                self.response = response
                self.usage = CompletionUsage()

            def complete(self, prompt: str) -> str:
                self.usage.calls += 1
                self.usage.record_tokens(input_tokens=3, output_tokens=2, total_tokens=5)
                return self.response

        class StaticRetriever:
            name = "static"

            def retrieve(self, query: str, top_k: int = 5):
                return [result]

        class TrackingAssistant:
            last_adaptive_result = None
            last_evidence_check = None
            last_verification = None

            def __init__(self):
                self.llm = TrackingClient("answer")

            def reset_memory(self):
                return None

            def answer(self, question: str, *, generate: bool = True):
                self.llm.complete(question)
                return "有依据的回答 [S1]。", [result]

        judge = TrackingClient(
            '{"faithfulness": 0.9, "relevance": 0.9, "completeness": 0.9, "passed": true}'
        )
        records = evaluate(
            cases=[
                EvalCase(
                    case_id="usage",
                    question="第一条是什么？",
                    case_type="article_lookup",
                    expected_law="中华人民共和国测试法",
                    expected_articles=["第一条"],
                    keywords=["回答"],
                )
            ],
            retriever=StaticRetriever(),
            chunk_strategy="article",
            model="fake",
            generate=True,
            assistant=TrackingAssistant(),
            judge_client=judge,
        )

        self.assertEqual(records[0].assistant_llm_calls, 1)
        self.assertEqual(records[0].judge_llm_calls, 1)
        self.assertEqual(records[0].total_tokens, 10)
        self.assertEqual(records[0].token_usage_calls, 2)

    def test_cli_exposes_phase4b_commands_and_flags(self) -> None:
        parser = build_parser()
        health = parser.parse_args(["cache-health", "--embedding", "bge_large_zh"])
        matrix = parser.parse_args(["experiment-matrix", "--adaptive-modes", "direct,adaptive"])
        evaluation = parser.parse_args(["evaluate", "--reranker", "none"])

        self.assertEqual(health.command, "cache-health")
        self.assertEqual(matrix.command, "experiment-matrix")
        self.assertEqual(evaluation.reranker, "none")


if __name__ == "__main__":
    unittest.main()
