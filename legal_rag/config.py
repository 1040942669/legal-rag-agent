from __future__ import annotations

import copy
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "data": {
        "dataset_dir": "Chinese-Laws/Chinese-Laws",
        "readme_path": "Chinese-Laws/README.md",
        # 已被民法典(2021-01-01 施行)废止但仍保留在数据集中的法律。
        # 保留原文用于对照，检索时默认降权，见 retrieval.deprecated_penalty。
        "deprecated_laws": [
            "中华人民共和国民法通则",
            "中华人民共和国合同法",
            "中华人民共和国继承法",
        ],
    },
    "artifacts": {
        "profile_dir": "artifacts/profile",
        "index_dir": "artifacts/indexes",
        "report_dir": "reports",
    },
    "models": {
        "ollama": ["qwen2.5:7b", "qwen3.5:4b", "gemma4:e2b"],
        "default": "qwen2.5:7b",
    },
    "embedding": {
        "default": "bge_large_zh",
        "cache_dir": "artifacts/embeddings",
        "batch_size": 16,
        "device": "auto",
        "model_name": "BAAI/bge-large-zh-v1.5",
        "models": {
            "bge_large_zh": {
                "provider": "sentence_transformers",
                "model_name": "BAAI/bge-large-zh-v1.5",
                "role": "chinese_general_baseline",
                "normalize": True,
                "trust_remote_code": False,
                "query_prefix": "",
                "document_prefix": "",
            },
            "bge_large_zh_meta": {
                "provider": "sentence_transformers",
                "model_name": "BAAI/bge-large-zh-v1.5",
                "role": "metadata_embedding_ablation",
                "normalize": True,
                "trust_remote_code": False,
                "query_prefix": "为这个句子生成表示以用于检索相关文章：",
                "document_prefix": "",
                "embed_with_metadata": True,
            },
            "qwen3_embedding_4b_meta": {
                "provider": "siliconflow",
                "model_name": "Qwen/Qwen3-Embedding-4B",
                "role": "metadata_embedding_ablation",
                "normalize": True,
                "trust_remote_code": False,
                "api_base_url": "https://api.siliconflow.cn/v1",
                "api_key_env": "SILICONFLOW_API_KEY",
                "dimensions": None,
                "max_retries": 3,
                "query_prefix": "Instruct: Given a legal question, retrieve relevant Chinese law provisions.\nQuery: ",
                "document_prefix": "",
                "embed_with_metadata": True,
            },
            "chatlaw_text2vec": {
                "provider": "sentence_transformers",
                "model_name": "chestnutlzj/ChatLaw-Text2Vec",
                "role": "legal_domain_model",
                "normalize": True,
                "trust_remote_code": False,
                "query_prefix": "",
                "document_prefix": "",
            },
            "qwen3_embedding_4b": {
                "provider": "siliconflow",
                "model_name": "Qwen/Qwen3-Embedding-4B",
                "role": "large_embedding_upper_bound",
                "normalize": True,
                "trust_remote_code": False,
                "api_base_url": "https://api.siliconflow.cn/v1",
                "api_key_env": "SILICONFLOW_API_KEY",
                "dimensions": None,
                "max_retries": 3,
                "query_prefix": "Instruct: Given a legal question, retrieve relevant Chinese law provisions.\nQuery: ",
                "document_prefix": "",
            },
        },
    },
    "retrieval": {
        "default": "bm25",
        "top_k": 5,
        "bm25_k1": 1.5,
        "bm25_b": 0.75,
        "bm25_law_boost": 40.0,
        "bm25_article_boost": 80.0,
        "rrf_k": 60,
        "rrf_bm25_weight": 1.0,
        "rrf_dense_weight": 1.0,
        # 已废止法律的得分乘数。1.0 表示不降权；查询明确提到该法律时不降权。
        "deprecated_penalty": 0.5,
    },
    "adaptive": {
        "enabled": False,
        "use_llm": False,
        "max_queries": 3,
        "per_plan_top_k": None,
        "normalizer_retries": 0,
    },
    "chat": {
        "memory_token_limit": 2000,
        "ollama_base_url": "http://localhost:11434",
        "request_timeout": 180,
    },
    "judge": {
        "provider": "siliconflow",
        "model": "deepseek-ai/DeepSeek-V3",
        "api_base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "SILICONFLOW_API_KEY",
        "request_timeout": 120,
        "temperature": 0.0,
    },
    "chunking": {
        "default_strategy": "article",
        "candidates": ["article", "neighbor", "long_split", "fixed_chars"],
        "neighbor_window": 3,
        "neighbor_stride": None,
        "long_split_max_chars": 450,
        "long_split_overlap_chars": 60,
        "fixed_chars_size": 500,
        "fixed_chars_overlap": 80,
    },
}


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    if not path:
        return config

    config_path = Path(path)
    if not config_path.exists():
        return config

    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except ModuleNotFoundError:
        loaded = _parse_simple_yaml(config_path.read_text(encoding="utf-8"))

    _deep_merge(config, loaded)
    return config


def resolve_path(path: str | Path, root: str | Path | None = None) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    base = Path(root) if root else Path.cwd()
    return (base / candidate).resolve()


def _deep_merge(base: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", ""}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value.strip("\"'")


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the small YAML subset used by configs/default.yaml.

    PyYAML is the normal path. This fallback keeps profile and tests runnable
    before dependencies are installed.
    """

    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    lines = text.splitlines()

    for index, raw_line in enumerate(lines):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if line.startswith("- "):
            item = _parse_scalar(line[2:])
            if not isinstance(parent, list):
                raise ValueError("List item found outside a list in fallback YAML parser.")
            parent.append(item)
            continue

        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        if value:
            parent[key] = _parse_scalar(value)
            continue

        next_container: dict[str, Any] | list[Any] = {}
        for next_raw in lines[index + 1 :]:
            if not next_raw.strip() or next_raw.lstrip().startswith("#"):
                continue
            next_indent = len(next_raw) - len(next_raw.lstrip(" "))
            next_line = next_raw.strip()
            if next_indent > indent and next_line.startswith("- "):
                next_container = []
            break
        parent[key] = next_container
        stack.append((indent, next_container))

    return root
