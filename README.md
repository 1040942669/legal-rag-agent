# 现行中国法律聊天助手 RAG

这是一个基于 LlamaIndex 设计的中国现行法律文本聊天助手项目。项目重点不是先拍脑袋决定 chunk 大小，而是先观察数据结构，再把 chunk 策略作为实验变量进行比较。

数据来源为 `Chinese-Laws`，README 标注数据截止到 2025-01-01，文本格式为一行一条法律条文。

## 功能

- `profile-data`: 读取 README 和全量法律文本，生成数据画像报告。
- `build-index`: 在已有数据画像的前提下，按指定 chunk 策略生成索引材料，并输出 chunk 诊断报告。
- `chat`: 启动命令行法律聊天助手，支持短期记忆、追问改写、引用来源。
- `evaluate`: 运行固定测试集，输出 retrieval、answer、latency、失败归因和可选 trace 等指标。
- `baseline`: 一键重建 `article + BM25 + retrieval-only` 可复现基线。
- controlled adaptive retrieval: 对复杂输入可选启用 query normalizer、law router、multi-query planning 和 evidence merge。

Phase 1 已补齐检索可靠性和失败归因能力:

- `neighbor` chunk 支持 `neighbor_stride`，可做滑动相邻条文窗口。
- `build-index` 会在 `artifacts/indexes/<strategy>/` 下写出 `diagnostics.json` 和 `diagnostics.md`。
- BM25 的 `k1`、`b`、法律名 boost、条号 boost 已配置化。
- BM25/RRF 检索结果会记录 ranking trace，RRF 可解释 BM25 与 dense 的子排名。
- `evaluate` 报告会统计 `wrong_law`、`wrong_article`、`metadata_gap`、`miss` 等 failure label。
- `evaluate` 和 `chat` 支持 `--trace-path` 输出 JSONL 检索 trace。

Phase 2 已加入受控 query understanding 和 multi-query planning:

- 默认链路仍是 direct retrieval，baseline 不会自动调用 LLM。
- `--adaptive` 只在模糊、多意图、矛盾、情绪化、过长、候选法律过多或低置信查询上触发 adaptive lane。
- `--adaptive-use-llm` 才会调用 Ollama normalizer，并要求严格 JSON；失败时回退到规则 normalizer。
- adaptive trace 会记录 normalizer、retrieval plans、merge 去重和每条证据的来源 query。

## 模型

默认使用本地 Ollama 模型:

- `qwen2.5:7b`
- `qwen3.5:4b`
- `gemma4:e2b`

如果未安装 LlamaIndex，`profile-data`、`build-index`、BM25 检索和 retrieval-only 评估仍可运行。安装依赖后，dense、hybrid 和 LlamaIndex adapter 可用于更完整实验。

## 快速开始

```powershell
uv sync
```

如果使用 SiliconFlow API，在项目根目录创建 `.env`:

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`:

```text
SILICONFLOW_API_KEY=你的 SiliconFlow API Key
```

先生成数据画像:

```powershell
python -m legal_rag.cli profile-data
```

再构建条文级 baseline 索引:

```powershell
python -m legal_rag.cli build-index --chunk-strategy article
```

运行 retrieval-only 评估:

```powershell
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25
```

运行 adaptive retrieval-only smoke:

```powershell
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_adaptive.jsonl --adaptive --trace-path reports/eval_article_bm25_adaptive_trace.jsonl --prefix eval_article_bm25_adaptive
```

一键重建 baseline:

```powershell
python -m legal_rag.cli baseline
```

构建 embedding cache:

```powershell
python -m legal_rag.cli build-embeddings --chunk-strategy article --embedding bge_large_zh
python -m legal_rag.cli build-embeddings --chunk-strategy article --embedding chatlaw_text2vec
```

`qwen3_embedding_4b` 默认通过 SiliconFlow API 构建向量，API Key 会自动从项目根目录 `.env` 读取:

```powershell
python -m legal_rag.cli build-embeddings --chunk-strategy article --embedding qwen3_embedding_4b --batch-size 8
```

运行 RRF 融合检索:

```powershell
python -m legal_rag.cli evaluate --chunk-strategy article --retriever rrf --embedding bge_large_zh --prefix eval_article_rrf_bge_large
```

启动聊天:

```powershell
python -m legal_rag.cli chat --chunk-strategy article --retriever bm25 --model qwen2.5:7b
```

启用受控 adaptive 聊天:

```powershell
python -m legal_rag.cli chat --chunk-strategy article --retriever bm25 --adaptive --trace-path reports/chat_adaptive_trace.jsonl
```

如果要让复杂输入调用 Ollama 做严格 JSON normalizer:

```powershell
python -m legal_rag.cli chat --chunk-strategy article --retriever bm25 --adaptive-use-llm --model qwen2.5:7b
```

如果只想看检索结果，不调用 Ollama:

```powershell
python -m legal_rag.cli chat --chunk-strategy article --retriever bm25 --no-generate
```

## 实验建议

1. 先看 `reports/data_profile.md`，确认条文长度、解析率和异常样例。
2. 分别构建候选 chunk:

```powershell
python -m legal_rag.cli build-index --chunk-strategy article
python -m legal_rag.cli build-index --chunk-strategy neighbor
python -m legal_rag.cli build-index --chunk-strategy long_split
python -m legal_rag.cli build-index --chunk-strategy fixed_chars
```

如果要测试滑动相邻条文窗口:

```powershell
python -m legal_rag.cli build-index --chunk-strategy neighbor --neighbor-window 3 --neighbor-stride 1
```

3. 跑检索实验:

```powershell
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --prefix eval_article_bm25
python -m legal_rag.cli evaluate --chunk-strategy neighbor --retriever bm25 --prefix eval_neighbor_bm25
```

需要复盘单个 case 的检索链路时，打开 JSONL trace:

```powershell
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --trace-path reports/eval_article_bm25_trace.jsonl
```

比较 direct retrieval 和 adaptive retrieval 时，先固定同一组 cases 和 retriever，再分别运行:

```powershell
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_adaptive.jsonl --prefix eval_adaptive_cases_direct
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_adaptive.jsonl --adaptive --trace-path reports/eval_adaptive_cases_trace.jsonl --prefix eval_adaptive_cases_adaptive
```

4. 依赖装好后跑 dense 和 RRF:

```powershell
python -m legal_rag.cli build-embeddings --chunk-strategy article --embedding bge_large_zh
python -m legal_rag.cli build-embeddings --chunk-strategy article --embedding chatlaw_text2vec
python -m legal_rag.cli evaluate --chunk-strategy article --retriever dense --embedding bge_large_zh --prefix eval_article_dense_bge_large
python -m legal_rag.cli evaluate --chunk-strategy article --retriever rrf --embedding bge_large_zh --prefix eval_article_rrf_bge_large
```

5. Ollama 服务正常后比较三个模型:

```powershell
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --generate --models all --prefix eval_article_bm25_models
```

## 设计取舍

- 不删除原始数据，所有筛选和标准化都发生在读取阶段。
- 条文级 chunk 是 baseline，不是无条件最终方案。
- 固定大小 chunk 只作为课程要求和对照实验，避免覆盖数据本身的条文边界。
- 具体案件策略和个性化法律意见默认拒答。
- 回答必须包含来源编号，并带有免责声明。
- Adaptive RAG 只做受控查询理解和有限检索计划，不做自由 agent loop 或无限补检索。

## 输出位置

- 数据画像 JSON: `artifacts/profile/data_profile.json`
- 数据画像报告: `reports/data_profile.md`
- 索引 chunk: `artifacts/indexes/<strategy>/chunks.jsonl`
- Chunk 诊断: `artifacts/indexes/<strategy>/diagnostics.json` 和 `diagnostics.md`
- 评估 CSV 和报告: `reports/`
- 检索 trace JSONL: 由 `--trace-path` 指定
