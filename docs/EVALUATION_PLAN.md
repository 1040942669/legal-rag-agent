# Legal RAG Evaluation Plan

## 当前问题

早期 `eval_cases/legal_eval_cases.jsonl` 只有 10 条手写样例，主要用于 smoke test。它能验证系统是否跑通，但不足以支持模型选择、chunk 策略选择和面试中的工程结论。

## 评估目标

评估分成两层:

1. Retrieval evaluation: 检索器能否把正确法律和条文排进 top-k。
2. Answer evaluation: LLM 是否基于检索证据回答、引用是否真实、是否拒绝越界请求。

当前优先做 retrieval evaluation，因为生成质量会被检索质量强烈影响。

## Case 类型

`eval_cases/legal_eval_cases_v2.jsonl` 按真实法律问答场景分层:

- `article_lookup`: 用户明确知道法律和条文，测试精确条号检索。
- `semantic_scenario`: 用户只描述生活/业务场景，测试语义召回。
- `multi_article`: 一个问题需要多个条文共同支持，测试多目标召回。
- `cross_law`: 问题跨多部法律，测试检索器是否只盯住单一法律。
- `hard_negative`: 问题含相似关键词但目标法律不同，测试误召回。
- `refusal`: 越界法律建议或非法律问题，测试拒答边界。

## 指标

Retrieval:

- Hit@3 / Hit@5: 目标法律和条文是否进入 top-k。
- MRR: 第一个正确结果的倒数排名。
- Citation hit: top-k 中是否有可引用的目标条文。
- Group metrics: 按 case type 分组统计。
- Latency: 单次检索平均耗时。
- Failure label: 对未命中样例标注 `wrong_law`、`wrong_article`、`metadata_gap`、`low_rank`、`miss` 或 `not_applicable`。
- Ranking trace: BM25 记录 metadata boost，RRF 记录 BM25/dense 子排名、子分数和 fused score。

Answer:

- Keyword coverage: 答案是否覆盖关键事实。
- Citation faithfulness: 引用编号是否来自检索结果。
- Refusal correctness: 越界问题是否拒答。
- Hallucination sample review: 人工抽查答案是否编造法律依据。

## 实验矩阵

Chunk:

```text
article
neighbor
long_split
fixed_chars as comparison only
```

Retriever:

```text
BM25
Dense
BM25 + Dense + RRF
```

Embedding:

```text
bge_large_zh
chatlaw_text2vec
qwen3_embedding_4b
```

## Phase 1 诊断产物

`build-index` 会为每个 chunk 策略输出:

```text
artifacts/indexes/<strategy>/diagnostics.json
artifacts/indexes/<strategy>/diagnostics.md
```

诊断内容包括 chunk 数、长度分布、每 chunk 条文数、article span、top laws 和异常样例。它用于判断失败来自条文解析、chunk 边界、metadata 缺失还是检索排序，而不是直接替代检索指标。

`evaluate` 可选输出检索 trace:

```powershell
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --trace-path reports/eval_article_bm25_trace.jsonl
```

每条 JSONL 记录包含 query analyzer、retrieval results、ranking trace、failure label 和 run metadata，可用于回放失败样例。

## Phase 1 验收命令

```powershell
python -B -m pytest
python -m legal_rag.cli build-index --chunk-strategy neighbor --neighbor-window 3 --neighbor-stride 1
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --trace-path reports/eval_article_bm25_trace.jsonl
```

## 解释原则

- 不只看总平均分，要看分组指标。
- 如果 `article_lookup` 很强但 `semantic_scenario` 弱，说明需要更好的 embedding 或 reranker。
- 如果 `multi_article` 弱，说明可能需要 `neighbor` chunk 或扩大 top-k。
- 如果 `cross_law` 弱，说明查询改写或 hybrid retrieval 需要改。
- 如果 Qwen3 提升不明显但成本显著增加，默认方案仍选 `bge_large_zh + BM25 + RRF`。
- 如果 failure label 集中在 `wrong_law`，优先检查法律名提示和 hybrid retrieval；如果集中在 `wrong_article`，优先检查条号解析、metadata boost 和 chunk 边界；如果集中在 `low_rank`，再考虑 reranker。
