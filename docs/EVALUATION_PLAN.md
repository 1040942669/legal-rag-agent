# Legal RAG Evaluation Plan

> 本文中的 Phase 编号描述评测能力的历史来源；当前 M0-M7 改造状态以 `docs/refactor/MASTER_PLAN.md`、`STATE.json` 和 `HANDOFF.md` 为准。
>
> M1 起，指标字段、分母、不可用原因和旧字段映射以 [评测指标字典](METRICS.md) 为唯一权威定义。本文保留 case、实验流程和历史 Phase 口径，避免把历史结果改写成新 schema 实测。

## 当前问题

早期 `eval_cases/legal_eval_cases.jsonl` 只有 10 条手写样例，主要用于 smoke test。Phase 4A 已新增 `legal_eval_cases_v3.jsonl`（120 条，其中 108 条有检索目标、12 条拒答）和 30 条生成子集。v1/v2 继续保留用于兼容和快速回归，但实验结论应明确写出使用的 case 版本。

## 评估目标

评估分成两层:

1. Retrieval evaluation: 检索器能否把正确法律和条文排进 top-k。
2. Answer evaluation: LLM 是否基于检索证据回答、引用编号是否属于本次检索结果、是否按旧规则拒绝越界请求。

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

`eval_cases/legal_eval_cases_v3.jsonl` 用于 Phase 4A 的分层实验。它合并 article lookup、semantic scenario、multi-article、cross-law、hard negative、四类 adaptive 输入和 refusal；`legal_eval_cases_v3_gen_subset.jsonl` 是其中固定的 30 条生成评测子集。测试会校验总数、ID 唯一性和子集关系，避免数据文件被无意改坏。

## 指标

Retrieval:

- Hit@3 / Hit@5: 目标法律和条文是否进入 top-k。
- MRR: 第一个正确结果的倒数排名。
- Citation hit: top-k 中是否有可引用的目标条文。
- Group metrics: 按 case type 分组统计。
- Bootstrap 95% CI: 对有检索目标的 Hit@3、Hit@5 和 MRR 做固定随机种子的 percentile bootstrap；拒答样例不混入检索均值。
- Latency: 同时报告平均值、P50 和 P95；跨机器只比较质量，延迟应在同次运行内横向比较。
- Runtime/Cost: retriever build time/peak memory、rerank calls/documents/time、assistant/normalizer/judge calls、provider token usage，以及显式给定单价时的估算成本。
- Failure label: 对未命中样例标注 `wrong_law`、`wrong_article`、`metadata_gap`、`low_rank`、`miss` 或 `not_applicable`。
- Ranking trace: BM25 记录 metadata boost，RRF 记录 BM25/dense 子排名、子分数和 fused score。
- Adaptive trace: 记录是否触发 adaptive、normalizer 输出、retrieval plans、merge 去重数量和每条证据的来源 query。

Answer 和 verification:

- 结构层分别记录 `schema_valid`、`evidence_catalog_valid`、纯 ID 存在性的 `source_ids_exist`、复合引用门禁 `citation_ids_valid`、`citation_alignment_valid` 和可选的 `evidence_scope_valid`。
- 行为层分别记录 `disclaimer_present`、`response_mode_valid`、`response_mode_correct`、`refusal_recall`、`over_refusal_rate` 和 `clarification_recall`。
- 语义层使用 `supported / unsupported / uncertain / not_checked`。当前确定性词面启发式只允许产生 `uncertain` 或 `not_checked`，不会把“没有发现问题”自动写成 `supported`。
- `verifier_pass` 只表示配置要求的结构/行为检查通过，不表示法律正确性或 claim-source 语义蕴含。
- LLM judge 仍为可选项；成功、失败和未执行有独立计数，超时、传输或 JSON/schema 错误以 `null + reason` 保存并排除质量均值。
- Hallucination sample review 仍需人工抽查；M1 合成 fixture 只证明规则边界，不代表真实法律回答质量提高。

新报告显式列出 retrieval gold、应答、应拒答、应澄清、服务失败和 Judge 三态分母。拒答召回只统计 `out_of_scope` 真值集合；过度拒答只统计应回答集合。预期行为来自评测 case，不来自 router 是否恰好识别风险，避免把 analyzer 漏检从分母中删除。

Retrieval-only 不构造 answer、不调用 answer verifier 或 Judge。回答、拒答、语义和 Judge 指标统一为 `null + retrieval_only`；旧 CSV 中保留 `-1` / `-1.0` 兼容哨兵，但这些值不得进入均值。Trace 同时明确 generation、verification 和 judge 未执行，旧 `verifier` 键保持空对象。

旧报告中的 `Citation validity` 主要表示引用 rank/ID 存在，`Refusal correctness` 则是旧风险 flag + 宽泛词语匹配；两者不得按 schema v2 重新解释。当前纯 ID 存在性由 `source_ids_exist` 表示，`citation_ids_valid` 还组合可见引用/claim 对齐要求；权限/快照由 `evidence_scope_valid` 独立表示，语义支持状态另行记录。

成本不硬编码平台价格。`--input-cost-per-million` 和 `--output-cost-per-million` 接受用户在运行时提供的美元 blended rate；当一次评测混用不同价格的生成模型和 judge 时，应拆成独立 run，不能把一个 blended estimate 当作真实账单。

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
LlamaIndex BM25 / Dense (framework comparison only)
```

Embedding:

```text
bge_large_zh
chatlaw_text2vec
qwen3_embedding_4b
bge_large_zh_meta / qwen3_embedding_4b_meta (metadata-text ablation)
```

Adaptive:

```text
direct
adaptive (deterministic rules by default)
```

Reranker:

```text
none
bge_v2_m3 (optional cross-encoder)
```

自动矩阵命令会展开上述维度并输出 CSV、JSON 和 Markdown。BM25 cell 不会因为传入多个 embedding key 被无意义地重复执行；单个 cell 缺 cache、缺模型或执行失败时写入 `status=failed`，其他 cell 继续运行。

```powershell
uv run python -m legal_rag.cli experiment-matrix `
  --chunk-strategies article,neighbor `
  --retrievers bm25,dense,rrf `
  --embeddings bge_large_zh,qwen3_embedding_4b `
  --adaptive-modes direct,adaptive `
  --rerankers none,bge_v2_m3 `
  --cases eval_cases/legal_eval_cases_v3.jsonl
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

旧 Phase 3 的 `VerificationResult` 当时检查:

- 回答中的 `[Sx]` 是否都能对应当前 sources。
- 答案是否保留免责声明。
- 高风险 query 是否被拒答。
- 部分未引用法律句是否能通过有限词面启发式；它不是语义蕴含判断。

Phase 3 验收命令:

```powershell
python -B -m pytest
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_adaptive.jsonl --adaptive --trace-path reports/eval_article_bm25_phase3_trace.jsonl --prefix eval_article_bm25_phase3
```

M1 Trace 在历史 `adaptive`、`evidence` 和 `verifier` 键之外，新增 `execution`、`generation_attempt` 和 `final_response`。这使被 verifier 拒绝的草稿不会冒充最终交付回答；retrieval-only 也不会冒充已经生成和验证。

## Phase 4A 评测硬化与实验结论

Phase 4A 已完成以下能力:

- v3 固定评测集和 30 条生成子集。
- 有检索目标样例的 bootstrap 95% CI。
- 每个生成 case 前清空对话记忆，避免跨 case 泄漏。
- 可选 LLM-as-judge；其输出使用严格数值/布尔 contract，`passed` 由分数阈值重新计算。
- judge API 或格式错误与模型质量失败分开统计。
- BM25、dense、RRF、LlamaIndex adapter、metadata embedding 和 adaptive lane 的固定命令对照。

检索基线复跑命令:

```powershell
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_v3.jsonl --prefix v3_article_bm25
```

生成与 judge 命令（会调用本地 Ollama 和/或 SiliconFlow）:

```powershell
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_v3_gen_subset.jsonl --generate --models qwen2.5:7b,siliconflow:deepseek-ai/DeepSeek-V3 --judge --prefix v3gen_models_bm25
```

历史结果见 `reports/RESULTS_SUMMARY.md`。该报告来自 2026-06 的本地增强语料快照；精确复现需要相同的 203 部法律语料、embedding cache 和当时的 API 模型版本。仓库当前可以复现评测逻辑与命令，但还没有自动下载并校验该语料快照。

## Phase 4B 自动实验平台

Phase 4B 的工程能力已完成:

- `Reranker` protocol、lazy CrossEncoder adapter 和 `RerankingRetriever`，默认 `none` 不加载模型。
- Embedding cache schema v2，校验模型、prefix、metadata embedding、维度、dtype、归一化、chunk 顺序和 corpus fingerprint。
- `cache-health` 命令；旧 cache 可以 advisory 模式审计，但 runtime 不静默复用缺契约缓存。
- 五维 experiment matrix runner，统一输出 CSV、JSON 和 Markdown，单格失败显式保留。
- 平均/P50/P95、build time/peak memory、rerank 调用、LLM 调用、token 和可选成本估算。

自动矩阵已在 120 条 v3 cases 上复跑 `article + BM25`:

| 配置 | Hit@5 (n=108) | MRR | 平均延迟 | P50 | P95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| direct | 0.704 | 0.608 | 243.5 ms | 201.0 ms | 316.1 ms |
| adaptive | 0.667 | 0.578 | 437.5 ms | 288.0 ms | 1189.3 ms |

该结果复现了 adaptive 整体负收益，并补充了尾延迟证据。BGE reranker 的接口和 mock 回归已完成，但尚未把约 2.29 GB 的真实模型跑完 120-case A/B，因此不得宣称 reranker 已提升质量，也不把它设为默认。

旧 BGE cache 缺少 schema v2 所需的 prefix、metadata embedding 和 corpus fingerprint，health check 会要求重建。CPU 重建在当前机器预估超过 2 小时，已停止；应在 GPU 或可接受长任务的环境运行:

```powershell
uv run python -m legal_rag.cli build-embeddings --chunk-strategy article --embedding bge_large_zh
uv run python -m legal_rag.cli cache-health --chunk-strategy article --embedding bge_large_zh
```

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
- 默认策略必须同时报告质量、延迟和外部 API 依赖；历史实验中 Qwen3 dense 质量最高，但不能只凭 Hit@5 忽略成本、隐私和离线可用性。
- 如果 failure label 集中在 `wrong_law`，优先检查法律名提示和 hybrid retrieval；如果集中在 `wrong_article`，优先检查条号解析、metadata boost 和 chunk 边界；如果集中在 `low_rank`，再考虑 reranker。
