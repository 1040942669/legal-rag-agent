from __future__ import annotations

import argparse
import os
import sys

from legal_rag.env import load_dotenv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local Legal RAG web demo.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", default="configs/default.yaml")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65_535:
        raise SystemExit("--port must be between 1 and 65535")

    # Dense retrieval, reranking and generation call SiliconFlow.  The offline
    # guard is lifted for this process only; the shell and every other command
    # keep their own ALLOW_LIVE_MODEL_CALLS setting.
    os.environ["ALLOW_LIVE_MODEL_CALLS"] = "true"
    load_dotenv()

    # Service dependencies stay lazy so ``--help`` works without FastAPI.
    import uvicorn

    from .app import create_app
    from .runtime import build_runtime

    # Warnings are Chinese; never let a narrow console encoding stop startup.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    runtime = build_runtime(args.config)
    for slot in runtime.retrievers.values():
        state = "ready" if slot.available else "unavailable"
        print(f"retriever {slot.key}: {state}")
    print(f"startup ms: {runtime.startup_ms}")
    for warning in runtime.warnings:
        print(f"warning: {warning}")
    print(f"Legal RAG web demo: http://{args.host}:{args.port}/", flush=True)
    uvicorn.run(
        create_app(runtime),
        host=args.host,
        port=args.port,
        access_log=False,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
