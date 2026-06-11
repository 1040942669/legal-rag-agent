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

`eval_cases/legal_eval_cases_adaptive.jsonl` 用于 Phase 2 对照:

- `adaptive_vague`: 用户问题缺少明确法律名或条号。
- `adaptive_multi_intent`: 一个输入里混合多个法律意图。
- `adaptive_contradictory`: 描述中存在相互冲突的事实。
- `adaptive_many_law_hints`: 用户同时提到多部候选法律，需要 bounded planner 截断。

## 指标

Retrieval:

- Hit@3 / Hit@5: 目标法律和条文是否进入 top-k。
- MRR: 第一个正确结果的倒数排名。
- Citation hit: top-k 中是否有可引用的目标条文。
- Group metrics: 按 case type 分组统计。
- Latency: 单次检索平均耗时。
- Failure label: 对未命中样例标注 `wrong_law`、`wrong_article`、`metadata_gap`、`low_rank`、`miss` 或 `not_applicable`。
- Ranking trace: BM25 记录 metadata boost，RRF 记录 BM25/dense 子排名、子分数和 fused score。
- Adaptive trace: 记录是否触发 adaptive、normalizer 输出、retrieval plans、merge 去重数量和每条证据的来源 query。

Answer:

- Keyword coverage: 答案是否覆盖关键事实。
- Evidence sufficiency pass: 生成前证据是否覆盖必要法律/条文提示，资料不足时是否进入降级路径。
- Citation validity: `[Sx]` 引用编号是否真实存在于当前检索结果。
- Verifier pass: 规则 verifier 是否同时通过引用、免责声明、越界拒答和基础证据支撑检查。
- Refusal correctness: 越界问题是否拒答，包括个案策略、违法帮助、非法律问题、医疗/金融越界建议。
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

## Phase 2 Adaptive 评测

Adaptive lane 默认关闭。启用 `--adaptive` 后，系统仍先运行规则 Query Analyzer；只有出现 `vague`、`contradictory`、`emotional`、`too_long`、`multi_intent`、`many_law_hints` 或 `low_confidence` 时，才进入 normalizer 和 multi-query planner。

默认 `--adaptive` 使用 deterministic fallback normalizer，不调用 LLM。只有显式传入 `--adaptive-use-llm` 时，才调用 Ollama 生成严格 JSON；非 JSON、字段缺失或调用失败都会回退到规则 normalizer，并把错误写入 trace。

Direct vs adaptive 对照命令:

```powershell
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_adaptive.jsonl --prefix eval_adaptive_cases_direct
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_adaptive.jsonl --adaptive --trace-path reports/eval_adaptive_cases_trace.jsonl --prefix eval_adaptive_cases_adaptive
```

如果要测试 LLM normalizer:

```powershell
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_adaptive.jsonl --adaptive-use-llm --model qwen2.5:7b --trace-path reports/eval_adaptive_llm_trace.jsonl --prefix eval_adaptive_cases_llm
```

Phase 2 trace 中的 `adaptive` 字段用于回答三个问题:

- 为什么触发或没有触发 adaptive。
- normalizer 产出了哪些 legal questions、law hints、article hints 和 keywords。
- 最终 evidence 来自哪个 plan/query，是否发生 chunk 去重或 top-k 截断。

## Phase 3 Evidence 和 Verifier 评测

Phase 3 在检索和生成之间新增规则型证据检查:

```text
retrieval results -> EvidenceCheck -> optional one-round follow-up -> answer -> VerificationResult
```

`EvidenceCheck` 会记录 `sufficient`、`missing_facts`、`missing_law_support`、`low_coverage`、`followup_queries` 和 `stop_reason`。如果证据不足，系统最多补检索一轮，然后要么标记 `sufficient_after_followup`，要么以 `max_rounds_reached` 停止并使用低置信回答模板。

`VerificationResult` 会检查:

- 回答中的 `[Sx]` 是否都能对应当前 sources。
- 答案是否保留免责声明。
- 高风险 query 是否被拒答。
- 关键法律结论是否至少带有引用或能被当前证据文本支撑。

Phase 3 验收命令:

```powershell
python -B -m pytest
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_adaptive.jsonl --adaptive --trace-path reports/eval_article_bm25_phase3_trace.jsonl --prefix eval_article_bm25_phase3
```

Trace JSONL 现在除 `adaptive` 外，还包含 `evidence` 和 `verifier` 字段，评估报告会汇总 `Evidence sufficiency pass`、`Citation validity`、`Verifier pass` 和 `Refusal correctness`。

## Phase 1 验收命令

```powershell
python -B -m pytest
python -m legal_rag.cli build-index --chunk-strategy neighbor --neighbor-window 3 --neighbor-stride 1
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --trace-path reports/eval_article_bm25_trace.jsonl
```

## Phase 2 验收命令

```powershell
python -B -m pytest
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_adaptive.jsonl --adaptive --trace-path reports/eval_article_bm25_adaptive_trace.jsonl --prefix eval_article_bm25_adaptive
```

## Phase 3 验收命令

```powershell
python -B -m pytest
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_adaptive.jsonl --adaptive --trace-path reports/eval_article_bm25_phase3_trace.jsonl --prefix eval_article_bm25_phase3
```

## 解释原则

- 不只看总平均分，要看分组指标。
- 如果 `article_lookup` 很强但 `semantic_scenario` 弱，说明需要更好的 embedding 或 reranker。
- 如果 `multi_article` 弱，说明可能需要 `neighbor` chunk 或扩大 top-k。
- 如果 `cross_law` 弱，说明查询改写或 hybrid retrieval 需要改。
- 如果 Qwen3 提升不明显但成本显著增加，默认方案仍选 `bge_large_zh + BM25 + RRF`。
- 如果 failure label 集中在 `wrong_law`，优先检查法律名提示和 hybrid retrieval；如果集中在 `wrong_article`，优先检查条号解析、metadata boost 和 chunk 边界；如果集中在 `low_rank`，再考虑 reranker。
