# Phase 4B 技术调研与决策

> 调研日期: 2026-09-18。本文记录 Phase 4B 为什么这样实现，以及哪些热门机制被有意推迟。结论服务于当前法律 RAG 的可复现性和质量成本比，不代表通用排行榜。

## 1. 先判断瓶颈，再选机制

Phase 4A 已经给出三个关键信号:

1. 当前 BM25 的主要失败是 `wrong_law` 和 `wrong_article`，不是所有目标都已进入 top-N 后单纯排错顺序。
2. 规则 adaptive 在同一 120 cases 上整体负收益，说明增加 Agent 步骤本身不等于增加质量。
3. embedding 文本中的法律名、条号和 query instruction 能显著影响 dense 效果，缓存身份不能只由模型名决定。

因此 Phase 4B 的优先级是先建立缓存契约、自动实验矩阵和成本观测，再提供可选 reranker。只有当目标条文已经进入候选集但排名偏低时，reranker 才是正确工具。

## 2. 最新机制调研

| 机制 | 官方资料中的能力 | 本项目判断 |
| --- | --- | --- |
| Cross-encoder reranking | Sentence Transformers 的 CrossEncoder 对 query/document 成对编码，通常比独立向量相似度更适合精排，但文档表示不能预计算，推理成本随候选数增长。[官方文档](https://www.sbert.net/docs/package_reference/cross_encoder/model.html) | 值得做成 top-N 到 top-K 的可插拔后处理器，默认关闭并记录调用量与延迟。 |
| BGE reranker v2-m3 | BAAI 官方模型卡将其描述为多语言 reranker，并提供 `AutoModelForSequenceClassification` 的标准 pair scoring 用法；模型文件约 2.29 GB。[官方模型卡](https://huggingface.co/BAAI/bge-reranker-v2-m3) | 适合作为第一个本地基线，许可证和接入方式清楚，能直接复用 sentence-transformers CrossEncoder。 |
| Qwen3 Reranker | Qwen 官方在 2025-06 发布 0.6B/4B/8B reranker，支持 instruction 和 32K context；官方实现使用 causal LM 的 yes/no token logits，而不是普通 sequence-classification 头。[官方博客](https://qwenlm.github.io/blog/qwen3-embedding/)，[官方仓库](https://github.com/QwenLM/Qwen3-Embedding) | 新、强，但适配协议与资源成本都不同。先不伪装成通用 CrossEncoder；BGE 基线证明有收益后，再单独实现 Qwen adapter。 |
| Contextual Retrieval | Anthropic 的方案在 embedding 和 BM25 前为每个 chunk 增加文档级上下文，并强调结合 recall@20、reranking 和延迟成本做评测。[官方工程文章](https://www.anthropic.com/engineering/contextual-retrieval) | 法条 chunk 已有法律名、条号 metadata，Phase 4A 也完成 metadata embedding 消融。进一步用 LLM 为 19,050 chunks 生成上下文成本高，暂缓到明确出现上下文缺失型失败时。 |
| GenAI observability | OpenTelemetry GenAI semantic conventions 覆盖 operation、model、token usage、retrieval documents、workflow 和 evaluation score，同时提醒输入输出可能包含敏感内容。[官方规范](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/) | 采用其指标思想，但暂不引入仍在演进的完整依赖。当前输出稳定的聚合字段，并默认不记录 prompt/answer 原文到遥测。 |

## 3. 从 L9 课程手册吸收的原则

`L9_Agent_完整讲课内容.md` 只作为本地课程参考，不进入公开仓库。Phase 4B 吸收了其中与现有证据一致的设计原则:

- Harness 负责状态、工具调用、限制、日志和 fallback，模型不应自由控制整个工作流。
- ReAct 必须有轮次、工具和 token 上限；当前法律检索不需要开放循环。
- 评测要同时覆盖 end-to-end、步骤级 trace、延迟、调用次数和 token，而不是只看最终回答。
- 在 source、chunk、query 尚未诊断清楚前，不应先用昂贵 reranker 掩盖问题。
- 新机制只有在质量收益能覆盖成本时才保留，没有收益就默认关闭或删除。

这也是项目继续采用 deterministic orchestration、最多一轮 follow-up 和显式失败状态，而不升级为自由 ReAct、Reflection 或 multi-agent 的原因。

## 4. 已实现的工程决策

### 4.1 Embedding cache v2 契约

新缓存写入并校验:

- schema version、向量行数、维度、dtype、有限值和归一化漂移；
- embedding key、provider、model name、normalize、query/document prefix、metadata embedding 开关；
- 有序 chunk IDs，以及包含 chunk 文本和关键 metadata 的 SHA-256 corpus fingerprint；
- embedding contract fingerprint，防止同名目录复用不同文本构造方式的向量。

运行时 dense retriever 使用 fail-closed 校验。旧缓存可用 `--allow-legacy` 做只读审计，但不能在缺少契约证据时被静默当成 v2 缓存。

### 4.2 可选 reranker

`RerankingRetriever` 可包装 BM25、dense、RRF 或框架 retriever:

```text
base retriever top-N -> pair scorer -> reranked top-K
```

每条结果保留 base retriever、原始 rank/score，并新增 rerank score、候选数量和耗时。默认 `reranker=none` 时不加载模型，也不改变 baseline。

### 4.3 Experiment matrix harness

矩阵维度为:

```text
chunk strategy x retriever x embedding x direct/adaptive x reranker
```

单个 cell 失败会写入 `status=failed` 和错误原因，不会丢失其他已完成结果。每次运行同时输出 CSV、JSON 和 Markdown，记录质量、P50/P95、构建时间、峰值内存、rerank 调用量、LLM 调用量、token 和可选成本估算。

### 4.4 成本与隐私边界

- Ollama 和 OpenAI-compatible client 尽力读取 provider 返回的 input/output/total tokens；provider 不返回时明确显示 N/A。
- 成本单价不硬编码，因为价格会变化；评测命令通过每百万 token 的 user-supplied blended rate 估算。混用不同价格的生成模型和 judge 时应拆成独立 run。
- 聚合器只保存数字，不额外保存完整 prompt、用户输入或模型输出，避免为了观测扩大法律问题的敏感数据面。

## 5. 自动矩阵验证

2026-09-18 使用 120 条 v3 cases 运行 `article + BM25` direct/adaptive 两格矩阵，108 条有 gold 样例进入检索均值:

| 配置 | Hit@5 | MRR | 平均延迟 | P50 | P95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| direct | 0.704 | 0.608 | 243.5 ms | 201.0 ms | 316.1 ms |
| adaptive | 0.667 | 0.578 | 437.5 ms | 288.0 ms | 1189.3 ms |

自动 runner 复现了 Phase 4A 的质量结论，同时证明 adaptive 的尾延迟明显更高。因此默认仍是 direct；adaptive 只保留为受控实验能力。

## 6. 当前未宣称完成的部分

- BGE reranker adapter 与 mock contract 已通过测试，但本机尚未完成 120-case 真实模型 A/B。首次加载约 2.29 GB，只有质量提升且 P95 可接受时才考虑默认启用。
- 本地旧 BGE embedding cache 缺少 v2 的 prefix、metadata embedding 与 corpus fingerprint，health check 已正确拒绝。CPU 重建预估超过 2 小时，已停止；应在 GPU 或可接受长任务的环境中重新构建。
- Qwen3 Reranker、LLM contextualization、自由 ReAct/Reflection 和 multi-agent 都不是当前默认 backlog。它们需要新的失败证据和独立质量成本实验才能进入实现。

## 7. 验收命令

```powershell
uv run pytest -q

uv run python -m legal_rag.cli cache-health `
  --chunk-strategy article `
  --embedding bge_large_zh

uv run python -m legal_rag.cli experiment-matrix `
  --chunk-strategies article `
  --retrievers bm25 `
  --adaptive-modes direct,adaptive `
  --rerankers none `
  --cases eval_cases/legal_eval_cases_v3.jsonl

# 可选，会在首次运行时下载/加载 BGE reranker
uv run python -m legal_rag.cli experiment-matrix `
  --chunk-strategies article `
  --retrievers bm25 `
  --adaptive-modes direct `
  --rerankers none,bge_v2_m3 `
  --rerank-top-n 20 `
  --cases eval_cases/legal_eval_cases_v3.jsonl
```
