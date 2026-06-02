from __future__ import annotations

from .models import Chunk, SearchResult


def is_llamaindex_available() -> bool:
    try:
        import llama_index.core  # type: ignore  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


class LlamaIndexRetriever:
    def __init__(
        self,
        chunks: list[Chunk],
        *,
        kind: str,
        top_k: int = 5,
        embedding_model: str = "BAAI/bge-small-zh-v1.5",
    ) -> None:
        if not is_llamaindex_available():
            raise RuntimeError("LlamaIndex is not installed. Run `uv sync` first.")
        self.name = f"llamaindex_{kind}"
        self.chunks = chunks
        self.chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        self.kind = kind
        self.top_k = top_k
        self.embedding_model = embedding_model
        self._retriever = self._build_retriever()

    def _build_retriever(self):
        from llama_index.core import Settings, VectorStoreIndex
        from llama_index.core.schema import TextNode
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding

        nodes = [
            TextNode(
                text=chunk.text,
                id_=chunk.chunk_id,
                metadata={
                    "law_names": "、".join(chunk.law_names),
                    "article_numbers": "、".join(chunk.article_numbers),
                    "source_files": " | ".join(chunk.source_files),
                    "strategy": chunk.strategy,
                },
            )
            for chunk in self.chunks
        ]

        if self.kind == "bm25":
            from llama_index.retrievers.bm25 import BM25Retriever

            return BM25Retriever.from_defaults(nodes=nodes, similarity_top_k=self.top_k)

        if self.kind == "dense":
            Settings.embed_model = HuggingFaceEmbedding(model_name=self.embedding_model)
            index = VectorStoreIndex(nodes)
            return index.as_retriever(similarity_top_k=self.top_k)

        raise ValueError(f"Unsupported LlamaIndex retriever kind: {self.kind}")

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        previous_top_k = getattr(self._retriever, "similarity_top_k", None)
        if previous_top_k is not None:
            self._retriever.similarity_top_k = top_k
        nodes = self._retriever.retrieve(query)
        results: list[SearchResult] = []
        for rank, node_with_score in enumerate(nodes[:top_k], start=1):
            node = node_with_score.node
            chunk = self.chunk_by_id.get(node.node_id)
            if not chunk:
                continue
            results.append(
                SearchResult(
                    chunk=chunk,
                    score=float(node_with_score.score or 0.0),
                    rank=rank,
                    retriever=self.name,
                )
            )
        return results

