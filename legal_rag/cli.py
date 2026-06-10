from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .chat import LegalChatAssistant, render_sources
from .chunking import load_chunks
from .config import load_config, resolve_path
from .data import profile_dataset, write_profile_outputs
from .embeddings import build_embedding_cache, embedding_cache_dir, resolve_embedding_model
from .env import load_dotenv
from .evaluation import evaluate, load_eval_cases, write_eval_outputs
from .indexing import build_index, resolve_chunks_path
from .llamaindex_backend import LlamaIndexRetriever
from .manifest import new_run_id
from .query import analyze_query
from .retrieval import build_retriever
from .tracing import JsonlTraceWriter, build_retrieval_trace_record


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 1
    try:
        return args.handler(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legal-rag",
        description="Chinese current law conversational RAG assistant.",
    )
    parser.add_argument("--config", default="configs/default.yaml", help="Path to config YAML.")
    subparsers = parser.add_subparsers(dest="command")

    profile = subparsers.add_parser("profile-data", help="Profile the law dataset before chunking.")
    profile.add_argument("--data-dir", default=None)
    profile.add_argument("--readme", default=None)
    profile.add_argument("--profile-dir", default=None)
    profile.add_argument("--report-dir", default=None)
    profile.set_defaults(handler=handle_profile_data)

    build = subparsers.add_parser("build-index", help="Build chunk artifacts after profile-data.")
    build.add_argument("--data-dir", default=None)
    build.add_argument("--profile-path", default=None)
    build.add_argument("--index-root", default=None)
    build.add_argument(
        "--chunk-strategy",
        choices=["article", "neighbor", "long_split", "fixed_chars"],
        default=None,
    )
    build.add_argument("--neighbor-window", type=int, default=None)
    build.add_argument("--neighbor-stride", type=int, default=None)
    build.set_defaults(handler=handle_build_index)

    baseline = subparsers.add_parser(
        "baseline",
        help="Rebuild the article + BM25 + retrieval-only baseline and write a reproducible report.",
    )
    baseline.add_argument("--data-dir", default=None)
    baseline.add_argument("--readme", default=None)
    baseline.add_argument("--profile-dir", default=None)
    baseline.add_argument("--index-root", default=None)
    baseline.add_argument("--report-dir", default=None)
    baseline.add_argument("--cases", default="eval_cases/legal_eval_cases_v2.jsonl")
    baseline.add_argument("--top-k", type=int, default=None)
    baseline.add_argument("--prefix", default=None)
    baseline.set_defaults(handler=handle_baseline)

    embeddings = subparsers.add_parser(
        "build-embeddings",
        help="Build dense embedding cache for a chunk strategy and embedding model.",
    )
    embeddings.add_argument("--index-dir", default=None)
    embeddings.add_argument("--chunk-strategy", default=None)
    embeddings.add_argument("--embedding", default=None, help="Embedding key from config.")
    embeddings.add_argument("--embedding-cache-root", default=None)
    embeddings.add_argument("--batch-size", type=int, default=None)
    embeddings.add_argument("--device", default=None)
    embeddings.set_defaults(handler=handle_build_embeddings)

    chat = subparsers.add_parser("chat", help="Start an interactive legal chat session.")
    chat.add_argument("--index-dir", default=None)
    chat.add_argument("--chunk-strategy", default=None)
    chat.add_argument("--retriever", default=None)
    chat.add_argument("--embedding", default=None, help="Embedding key for dense/rrf retrieval.")
    chat.add_argument("--embedding-cache-dir", default=None)
    chat.add_argument("--model", default=None)
    chat.add_argument("--top-k", type=int, default=None)
    chat.add_argument("--trace-path", default=None)
    chat.add_argument("--no-generate", action="store_true", help="Return retrieval results only.")
    chat.set_defaults(handler=handle_chat)

    eval_parser = subparsers.add_parser("evaluate", help="Run retrieval or generation evaluation.")
    eval_parser.add_argument("--index-dir", default=None)
    eval_parser.add_argument("--chunk-strategy", default=None)
    eval_parser.add_argument("--retriever", default=None)
    eval_parser.add_argument("--embedding", default=None, help="Embedding key for dense/rrf retrieval.")
    eval_parser.add_argument("--embedding-cache-dir", default=None)
    eval_parser.add_argument("--cases", default="eval_cases/legal_eval_cases_v2.jsonl")
    eval_parser.add_argument("--model", default=None)
    eval_parser.add_argument("--models", default=None, help="Comma-separated models or `all`.")
    eval_parser.add_argument("--top-k", type=int, default=None)
    eval_parser.add_argument("--generate", action="store_true")
    eval_parser.add_argument("--prefix", default=None)
    eval_parser.add_argument("--trace-path", default=None)
    eval_parser.set_defaults(handler=handle_evaluate)

    return parser


def handle_profile_data(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    data_dir = resolve_path(args.data_dir or config["data"]["dataset_dir"])
    readme = resolve_path(args.readme or config["data"]["readme_path"])
    profile_dir = resolve_path(args.profile_dir or config["artifacts"]["profile_dir"])
    report_dir = resolve_path(args.report_dir or config["artifacts"]["report_dir"])

    profile = profile_dataset(data_dir, readme)
    profile_path, report_path = write_profile_outputs(profile, profile_dir, report_dir)
    print(f"Wrote profile JSON: {profile_path}")
    print(f"Wrote profile report: {report_path}")
    print(json.dumps(profile["chunk_candidates"], ensure_ascii=False, indent=2))
    return 0


def handle_build_index(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    data_dir = resolve_path(args.data_dir or config["data"]["dataset_dir"])
    profile_path = resolve_path(
        args.profile_path or Path(config["artifacts"]["profile_dir"]) / "data_profile.json"
    )
    index_root = resolve_path(args.index_root or config["artifacts"]["index_dir"])
    strategy = args.chunk_strategy or config["chunking"]["default_strategy"]
    chunking_config = chunking_config_from_args(config, args)

    metadata = build_index(
        dataset_dir=data_dir,
        profile_path=profile_path,
        output_root=index_root,
        strategy=strategy,
        chunking_config=chunking_config,
        run_id=new_run_id(f"index_{strategy}"),
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


def handle_baseline(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    run_id = new_run_id("baseline_article_bm25")
    data_dir = resolve_path(args.data_dir or config["data"]["dataset_dir"])
    readme = resolve_path(args.readme or config["data"]["readme_path"])
    profile_dir = resolve_path(args.profile_dir or config["artifacts"]["profile_dir"])
    index_root = resolve_path(args.index_root or config["artifacts"]["index_dir"])
    report_dir = resolve_path(args.report_dir or config["artifacts"]["report_dir"])
    case_path = resolve_path(args.cases)
    top_k = args.top_k or int(config["retrieval"]["top_k"])
    strategy = "article"
    retriever_kind = "bm25"

    profile = profile_dataset(data_dir, readme)
    profile_path, profile_report_path = write_profile_outputs(profile, profile_dir, report_dir)
    index_metadata = build_index(
        dataset_dir=data_dir,
        profile_path=profile_path,
        output_root=index_root,
        strategy=strategy,
        chunking_config=config["chunking"],
        run_id=run_id,
    )
    index_dir = index_root / strategy
    chunks_path = resolve_chunks_path(index_dir)
    chunks = load_chunks(chunks_path)
    retriever = create_retriever(
        retriever_kind,
        chunks,
        config,
        top_k=top_k,
        chunk_strategy=strategy,
    )
    cases = load_eval_cases(case_path)
    records = evaluate(
        cases=cases,
        retriever=retriever,
        chunk_strategy=strategy,
        model="retrieval-only",
        generate=False,
        top_k=top_k,
    )
    prefix = args.prefix or f"baseline_{strategy}_{retriever_kind}"
    csv_path, report_path = write_eval_outputs(
        records,
        report_dir,
        prefix,
        metadata={
            "run_id": run_id,
            "config_path": str(resolve_path(args.config)),
            "case_path": str(case_path),
            "top_k": top_k,
            "chunk_count": len(chunks),
            "index_manifest_path": index_metadata.get("manifest_path", ""),
            "diagnostics_path": index_metadata.get("diagnostics_path", ""),
            **retrieval_metadata(config),
        },
    )
    print(f"Run ID: {run_id}")
    print(f"Wrote profile JSON: {profile_path}")
    print(f"Wrote profile report: {profile_report_path}")
    print(f"Wrote index manifest: {index_metadata.get('manifest_path')}")
    print(f"Wrote evaluation CSV: {csv_path}")
    print(f"Wrote evaluation report: {report_path}")
    return 0


def handle_build_embeddings(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    strategy = args.chunk_strategy or config["chunking"]["default_strategy"]
    index_dir = resolve_index_dir(args.index_dir, config, strategy)
    chunks_path = resolve_chunks_path(index_dir)
    model_config = resolve_embedding_model(config, args.embedding)
    cache_root = resolve_path(args.embedding_cache_root or config["embedding"]["cache_dir"])
    batch_size = args.batch_size or int(config["embedding"]["batch_size"])
    device = args.device or config["embedding"].get("device", "auto")

    metadata = build_embedding_cache(
        chunks_path=chunks_path,
        output_root=cache_root,
        chunk_strategy=strategy,
        model_config=model_config,
        batch_size=batch_size,
        device=device,
        run_id=new_run_id(f"embeddings_{strategy}_{model_config.key}"),
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


def handle_chat(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    run_id = new_run_id("chat")
    strategy = args.chunk_strategy or config["chunking"]["default_strategy"]
    index_dir = resolve_index_dir(args.index_dir, config, strategy)
    chunks = load_chunks(resolve_chunks_path(index_dir))
    retriever_kind = args.retriever or config["retrieval"]["default"]
    top_k = args.top_k or int(config["retrieval"]["top_k"])
    retriever = create_retriever(
        retriever_kind,
        chunks,
        config,
        top_k=top_k,
        chunk_strategy=strategy,
        embedding_key=args.embedding,
        embedding_cache_dir_arg=args.embedding_cache_dir,
    )
    assistant = LegalChatAssistant(
        retriever,
        model=args.model or config["models"]["default"],
        top_k=top_k,
        memory_token_limit=int(config["chat"]["memory_token_limit"]),
        ollama_base_url=config["chat"]["ollama_base_url"],
        request_timeout=int(config["chat"]["request_timeout"]),
    )
    trace_writer = None
    if args.trace_path:
        trace_writer = JsonlTraceWriter(resolve_path(args.trace_path), run_id=run_id)

    print("Legal RAG chat started. Type `exit` to quit.")
    while True:
        try:
            question = input("\n你: ").strip()
        except EOFError:
            break
        if question.lower() in {"exit", "quit", "q"}:
            break
        if not question:
            continue
        analysis = analyze_query(question)
        started = time.perf_counter()
        answer, results = assistant.answer(question, generate=not args.no_generate)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if trace_writer:
            trace_writer.write(
                build_retrieval_trace_record(
                    query=question,
                    retriever=getattr(retriever, "name", retriever_kind),
                    top_k=top_k,
                    results=results,
                    latency_ms=latency_ms,
                    analyzer=analysis.to_dict(),
                    metadata={
                        "chunk_strategy": strategy,
                        "generate": not args.no_generate,
                        "model": args.model or config["models"]["default"],
                    },
                )
            )
        print("\n助手:")
        print(answer)
        print("\n来源:")
        print(render_sources(results))
    return 0


def handle_evaluate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    run_id = new_run_id("eval")
    strategy = args.chunk_strategy or config["chunking"]["default_strategy"]
    index_dir = resolve_index_dir(args.index_dir, config, strategy)
    chunks_path = resolve_chunks_path(index_dir)
    chunks = load_chunks(chunks_path)
    retriever_kind = args.retriever or config["retrieval"]["default"]
    top_k = args.top_k or int(config["retrieval"]["top_k"])
    case_path = resolve_path(args.cases)
    cases = load_eval_cases(case_path)
    report_dir = resolve_path(config["artifacts"]["report_dir"])
    trace_path = resolve_path(args.trace_path) if args.trace_path else None
    trace_writer = JsonlTraceWriter(trace_path, run_id=run_id) if trace_path else None
    metadata = build_eval_metadata(
        args=args,
        config=config,
        run_id=run_id,
        case_path=case_path,
        index_dir=index_dir,
        chunks_path=chunks_path,
        chunks=chunks,
        retriever_kind=retriever_kind,
        strategy=strategy,
        top_k=top_k,
        embedding_key=args.embedding,
        embedding_cache_dir=args.embedding_cache_dir,
        trace_path=trace_path,
    )

    models = resolve_models(args, config)
    all_records = []
    for model in models:
        retriever = create_retriever(
            retriever_kind,
            chunks,
            config,
            top_k=top_k,
            chunk_strategy=strategy,
            embedding_key=args.embedding,
            embedding_cache_dir_arg=args.embedding_cache_dir,
        )
        assistant = None
        if args.generate:
            assistant = LegalChatAssistant(
                retriever,
                model=model,
                top_k=top_k,
                memory_token_limit=int(config["chat"]["memory_token_limit"]),
                ollama_base_url=config["chat"]["ollama_base_url"],
                request_timeout=int(config["chat"]["request_timeout"]),
            )
        records = evaluate(
            cases=cases,
            retriever=retriever,
            chunk_strategy=strategy,
            model=model if args.generate else "retrieval-only",
            generate=args.generate,
            top_k=top_k,
            assistant=assistant,
            trace_writer=trace_writer,
            trace_metadata=metadata,
        )
        all_records.extend(records)

    prefix = args.prefix or f"eval_{strategy}_{retriever_kind}"
    csv_path, report_path = write_eval_outputs(
        all_records,
        report_dir,
        prefix,
        metadata=metadata,
    )
    print(f"Wrote evaluation CSV: {csv_path}")
    print(f"Wrote evaluation report: {report_path}")
    if trace_path:
        print(f"Wrote retrieval trace JSONL: {trace_path}")
    return 0


def create_retriever(
    kind: str,
    chunks,
    config: dict,
    *,
    top_k: int,
    chunk_strategy: str,
    embedding_key: str | None = None,
    embedding_cache_dir_arg: str | None = None,
):
    model_config = resolve_embedding_model(config, embedding_key)
    if kind in {"llamaindex_bm25", "llamaindex_dense"}:
        return LlamaIndexRetriever(
            chunks,
            kind=kind.replace("llamaindex_", ""),
            top_k=top_k,
            embedding_model=model_config.model_name,
        )
    cache_dir = None
    if kind in {"dense", "rrf", "hybrid"}:
        cache_dir = resolve_embedding_cache_dir(
            embedding_cache_dir_arg,
            config,
            chunk_strategy,
            model_config.key,
        )
    return build_retriever(
        kind,
        chunks,
        embedding_model=model_config.model_name,
        embedding_model_config=model_config,
        embedding_cache_dir=cache_dir,
        device=config["embedding"].get("device", "auto"),
        rrf_k=int(config["retrieval"].get("rrf_k", 60)),
        rrf_bm25_weight=float(config["retrieval"].get("rrf_bm25_weight", 1.0)),
        rrf_dense_weight=float(config["retrieval"].get("rrf_dense_weight", 1.0)),
        bm25_k1=float(config["retrieval"].get("bm25_k1", 1.5)),
        bm25_b=float(config["retrieval"].get("bm25_b", 0.75)),
        bm25_law_boost=float(config["retrieval"].get("bm25_law_boost", 40.0)),
        bm25_article_boost=float(config["retrieval"].get("bm25_article_boost", 80.0)),
    )


def resolve_models(args: argparse.Namespace, config: dict) -> list[str]:
    if not args.generate:
        return ["retrieval-only"]
    if args.models == "all":
        return list(config["models"]["ollama"])
    if args.models:
        return [item.strip() for item in args.models.split(",") if item.strip()]
    return [args.model or config["models"]["default"]]


def resolve_index_dir(index_dir: str | None, config: dict, strategy: str) -> Path:
    if index_dir:
        return resolve_path(index_dir)
    return resolve_path(Path(config["artifacts"]["index_dir"]) / strategy)


def build_eval_metadata(
    *,
    args: argparse.Namespace,
    config: dict,
    run_id: str,
    case_path: Path,
    index_dir: Path,
    chunks_path: Path,
    chunks,
    retriever_kind: str,
    strategy: str,
    top_k: int,
    embedding_key: str | None,
    embedding_cache_dir: str | None,
    trace_path: Path | None = None,
) -> dict:
    metadata = {
        "run_id": run_id,
        "config_path": str(resolve_path(args.config)),
        "case_path": str(case_path),
        "top_k": top_k,
        "chunk_count": len(chunks),
        "chunks_path": str(chunks_path),
        "retriever": retriever_kind,
        "chunk_strategy": strategy,
        **retrieval_metadata(config),
    }
    manifest_path = index_dir / "manifest.json"
    if manifest_path.exists():
        metadata["index_manifest_path"] = str(manifest_path.resolve())
    diagnostics_path = index_dir / "diagnostics.json"
    if diagnostics_path.exists():
        metadata["diagnostics_path"] = str(diagnostics_path.resolve())
    if trace_path:
        metadata["trace_path"] = str(trace_path)
    if retriever_kind in {"dense", "rrf", "hybrid"}:
        model_config = resolve_embedding_model(config, embedding_key)
        metadata["embedding_key"] = model_config.key
        try:
            metadata["embedding_cache_dir"] = str(
                resolve_embedding_cache_dir(embedding_cache_dir, config, strategy, model_config.key)
            )
        except FileNotFoundError:
            metadata["embedding_cache_dir"] = embedding_cache_dir or ""
    return metadata


def chunking_config_from_args(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    chunking_config = dict(config["chunking"])
    if getattr(args, "neighbor_window", None) is not None:
        chunking_config["neighbor_window"] = args.neighbor_window
    if getattr(args, "neighbor_stride", None) is not None:
        chunking_config["neighbor_stride"] = args.neighbor_stride
    return chunking_config


def retrieval_metadata(config: dict[str, Any]) -> dict[str, Any]:
    retrieval = config.get("retrieval", {})
    return {
        "bm25_k1": float(retrieval.get("bm25_k1", 1.5)),
        "bm25_b": float(retrieval.get("bm25_b", 0.75)),
        "bm25_law_boost": float(retrieval.get("bm25_law_boost", 40.0)),
        "bm25_article_boost": float(retrieval.get("bm25_article_boost", 80.0)),
    }


def resolve_embedding_cache_dir(
    cache_dir: str | None,
    config: dict,
    strategy: str,
    embedding_key: str,
) -> Path:
    if cache_dir:
        resolved = resolve_path(cache_dir)
    else:
        resolved = embedding_cache_dir(config["embedding"]["cache_dir"], strategy, embedding_key).resolve()
    if not resolved.exists():
        raise FileNotFoundError(
            f"Embedding cache not found: {resolved}. "
            f"Run `python -m legal_rag.cli build-embeddings --chunk-strategy {strategy} "
            f"--embedding {embedding_key}` first."
        )
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
