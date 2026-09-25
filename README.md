# Legal RAG Agent | 中国法律文本快照检索增强生成系统

一个面向指定中国法律文本快照的可复现 RAG 工程项目。它不是把大模型接到向量库后的演示，而是围绕法律场景中的三个核心问题展开：**如何稳定召回正确法条、如何证明一次优化真的有效、如何在证据不足时安全停止生成**。

仓库已经实现旧 Phase 路线中的规则型工程链路，包括数据画像、分块实验、混合检索、受控查询理解、证据覆盖启发式、引用编号检查、缓存契约和自动实验矩阵。默认链路保持保守：清晰问题直接检索，复杂问题才进入有边界的 adaptive lane；reranker 默认关闭，任何检索或生成增强都必须通过固定评测集、trace、质量与成本指标证明价值。M1 已把结构检查、行为检查和未知语义状态拆开；M2 进一步把 fresh/cache/replay、逐 case artifact、恢复、聚合和外部调用账本固化为显式实验生命周期。M3 的 PostgreSQL/pgvector 版本化语料、精确检索、原子快照切换、可复现导入和受控实验性 HNSW 已完成实现候选验收，但仍在等待包含候选文档与版本元数据的最终提交 CI；当前已发布并远端核验的最新版本仍是 [v0.3.0](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.3.0)。

> 本项目仅用于检索与工程研究，不提供个案法律意见。仓库不随附完整法律语料，历史快照的内容截止日期为 2025-01-01；因此本文不声称覆盖全部当前有效法律。数据来源及复现边界见下文。

## 项目产出

| 维度 | 已完成产出 |
| --- | --- |
| 数据与索引 | 203 部法律、19,050 个条文级 chunk 的历史实验快照；4 种 chunk 策略；废止法律标记与精确重复条文去重 |
| 检索能力 | 自研中文 BM25、dense、RRF、LlamaIndex 对照；可选 BGE cross-encoder reranker；法律名和条号 metadata boost；完整 ranking trace |
| 受控 Agent 能力 | 规则 Query Analyzer、严格 JSON normalizer、有限 multi-query planner、证据合并、最多一轮补检索 |
| 生成边界 | 高风险请求预拒答、证据充分性检查、结构化回答兼容层、引用/范围/行为检查、资料不足或澄清模板、最终交付前复核 |
| 评测体系 | 120 条分层评测集、30 条固定生成子集、bootstrap 95% CI、显式行为分母、answer/retrieval/Judge N/A、自动五维实验矩阵 |
| 工程质量 | M2 `v0.3.0` 为 548 个离线测试、157 个子测试与 25 项累计门禁；M3 实现候选在 Linux/Python 3.12.13 上为 671 个离线测试、157 个子测试、61 项 PostgreSQL integration 与 33/33 累计门禁；精确 cache/replay、原子 case artifact、断点续评、版本化存储和真实服务重启验证均有机器证据 |

历史实验中，`Qwen3-Embedding-4B` dense 的 Hit@5 达到 **0.981 [0.954, 1.000]**，无外部 API 的自研 BM25 baseline 为 **0.704 [0.611, 0.787]**。这些数字来自 2026-06 的固定本地语料快照和当时模型版本，不是跨语料、跨时间的效果承诺。完整实验条件见 [结果摘要](reports/RESULTS_SUMMARY.md)。

## 为什么值得做

法律 RAG 的难点不只是“回答像不像”，而是每个环节都可能制造一个看似合理但无法核验的结果：

- 法律文本有天然条文边界，盲目按固定 token 切分会破坏引用粒度。
- 用户常用场景化、情绪化或多意图表达，关键词可能被噪声稀释。
- BM25、dense 和框架默认组件的适用边界不同，融合不一定比单路更强。
- 命中相关法律不等于命中正确条号，生成正确文字也不等于引用真实存在。
- 法律场景不能依赖无限 Agent loop，在证据不足或请求越界时必须可预测地停止。

因此，本项目把 **评测、失败归因和证据边界** 作为主线，而不是把组件数量当作完成度。

## 系统架构

```mermaid
flowchart LR
    A[用户问题] --> B[Query Analyzer]
    B --> C{风险请求?}
    C -- 是 --> R[规则拒答]
    C -- 否 --> D{需要 Adaptive?}
    D -- 否 --> E[Direct Retrieval]
    D -- 是 --> F[JSON Normalizer]
    F --> G[Bounded Planner]
    G --> H[Multi-query Retrieval]
    E --> Q{Optional Reranker}
    H --> Q
    Q --> I[Evidence Merge + Trace]
    I --> J[Evidence Sufficiency]
    J -- 不足且未补检索 --> K[最多一轮 Follow-up]
    K --> I
    J -- 足够或停止 --> L[Answer Generator]
    L --> M[Answer Verifier]
    M -- 规则通过 --> N[带来源回答]
    M -- 部分规则失败 --> O[降级或拒答]
    M -- 其余失败 --> P[记录状态并执行既有处理]
```

这是在线问答调用链的简化图。M1 `v0.2.0` 将生成结果适配为结构化回答，分开检查 schema、引用 ID、可见证据范围、回答模式、免责声明和语义状态。M2 不改变这条业务链的安全含义，而是在其外层增加可恢复实验执行器：按阶段记录输入身份、缓存来源、checkpoint、计时和调用账本，再从不可变 case artifact 聚合报告。词面启发式最多给出 `uncertain` 或 `not_checked`，仍不证明引用语义支持或法律结论正确。

### 1. 数据驱动的分块

数据集本身是一行一条法律条文，因此 `article` 被选作可解释 baseline，而不是直接套用 256/512 token。项目同时保留 `neighbor`、`long_split` 和 `fixed_chars` 作为受控实验变量，并输出 chunk 长度、条号跨度、异常样例和索引规模诊断。

### 2. 可解释的多路检索

- 自研 BM25 使用中文单字、bigram、法律名和条号特征，弥补默认英文式 tokenizer 对中文法律文本的不适配。
- Dense 检索支持本地 sentence-transformers 和 OpenAI-compatible embedding API，并将 query instruction、metadata 拼接作为显式配置。
- RRF 只基于排名融合不同分值空间，同时保存 BM25/dense 子排名，便于解释每个结果从哪里来。
- 可选 cross-encoder 对 base retriever 的 top-N 候选做精排，保留原 rank/score，并单独记录候选数和重排耗时。
- 废止法律默认降权；用户明确查询旧法时不降权，保留历史法律研究能力。

### 3. 受控 Adaptive RAG

Adaptive lane 不是自由 Agent loop。只有规则分析器识别到模糊、多意图、矛盾、情绪化、过长、候选法律过多或低置信输入时才触发。LLM normalizer 必须返回严格 JSON，失败时回退到确定性规则；planner 限制 query 数量，补检索最多执行一轮。

### 4. 生成前后双重校验

生成前检查法律名、条号和问题覆盖是否充分。只有可信调用方显式注入 `VerificationContext` 时，证据才会在进入生成提示和返回 sources 前按 snapshot/scope 过滤，verifier 也会检查该范围；当前 CLI 没有认证身份、租户隔离或默认 active snapshot。生成后分别检查结构、引用 ID、claim 与可见引用对齐、可选范围、回答模式和免责声明。伪造引用或无效模式会进入有界的受限响应，最终交付响应再次验证；被拒绝草稿的正文与内容型诊断不会写入普通 Trace，只保留稳定状态和计数。

当前 verifier 仍是规则与启发式防线，不是语义支持或法律正确性证明。`source_ids_exist` 只说明编号属于本次结果目录；`citation_ids_valid` 还组合了当前可见引用/claim 对齐要求，快照与权限范围则由独立的 `evidence_scope_valid` 表示。没有启用经过校准的语义评审时，支持状态保持 `uncertain` 或 `not_checked`。免责声明不再等同拒答，普通法律文本中的“不得”“不能”也不会单独触发拒答。详细口径见[评测指标字典](docs/METRICS.md)。

### 5. 评测优先

评测报告同时记录 Hit@k、MRR、bootstrap 置信区间、平均/P50/P95 延迟、引用有效性、verifier、judge 和失败标签。Experiment matrix 可展开 chunk、retriever、embedding、adaptive、reranker 五个维度；LLM 和 reranker 的调用次数、token、耗时及可选成本估算统一聚合。Retrieval-only 与生成指标严格分离，外部 judge 的超时、格式错误和服务异常不会被伪装成模型质量失败。

## 关键实验结果

以下是 v3 固定评测集上的历史结果节选。检索指标只统计 108 条有 gold 的样例，12 条拒答样例单独处理。

| 配置 | Hit@5 | MRR | 结论 |
| --- | ---: | ---: | --- |
| 自研 BM25 | 0.704 | 0.608 | 无模型下载、无 API 的可复现 baseline |
| BGE dense | 0.676 | 0.538 | 纯 chunk 文本时没有超过 BM25 |
| BGE dense + metadata + query instruction | 0.769 | 0.577 | 文本工程带来 9.3 个百分点增益 |
| Qwen3-Embedding-4B dense | **0.981** | **0.884** | 当前快照上的质量优先配置 |
| BM25 + BGE RRF | 0.796 | 0.609 | 两路质量接近时融合有效 |
| BM25 + Qwen3 RRF | 0.944 | 0.783 | 弱检索器会拖累强 dense |
| LlamaIndex 默认 BM25 | 0.111 | 0.090 | 默认 tokenizer 不适合该中文语料 |
| LlamaIndex BGE dense | 0.870 | 0.709 | 暴露 metadata 文本拼接这一隐藏变量 |

Phase 4B 自动矩阵在同一 v3 集合上复跑了 BM25 direct/adaptive。质量与历史结论一致，但新增尾延迟揭示了更清楚的成本差异：

| 配置 | Hit@5 | MRR | 平均延迟 | P50 | P95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| BM25 direct | **0.704** | **0.608** | 243.5 ms | 201.0 ms | 316.1 ms |
| BM25 + rules adaptive | 0.667 | 0.578 | 437.5 ms | 288.0 ms | 1189.3 ms |

这次复跑同时验证了 matrix runner；它不是新的跨机器性能基准。延迟只应在同一次运行、同一机器内横向比较。

生成实验固定使用 30 条子集和同一 BM25 检索结果，对 4 个 backend 形成 120 条 model-case 记录。历史报告中的 judge faithfulness 为 0.970，citation validity 为 0.925。由于 judge 使用 DeepSeek-V3，对同模型存在 self-preference 风险，规则 verifier 又只有启发式边界，这些数字只能作为当时配置下的信号，不能证明语义支持或法律正确性。

## 真正有价值的踩坑与修复

| 问题 | 根因 | 修复与价值 |
| --- | --- | --- |
| Adaptive 开启后整体效果下降 | 规则改写丢失语义词，强 retriever 已不需要额外改写 | 用同一 120 cases 做 A/B，BM25 Hit@5 从 0.704 降至 0.667；因此默认关闭，只保留可测的受控入口 |
| 同一 BGE 模型在 LlamaIndex 中明显更强 | 框架默认把法律名、条号等 metadata 拼入 embedding 文本 | 做 metadata 与 query instruction 消融，自研 dense 从 0.676 提升到 0.769，并把文本构造变成显式配置 |
| 框架默认 BM25 几乎失效 | 默认 tokenizer 没有针对中文法律名、条号和连续汉字设计 | 实现中文单字 + bigram + metadata boost，Hit@5 从对照的 0.111 提升到 0.704 |
| Retrieval-only 报告出现“拒答正确率” | 旧逻辑把拼接的检索文本送入回答 verifier，指标语义错误 | 无生成时回答类指标统一标为 `N/A`，避免用无意义数字包装效果 |
| 多 case 生成评测可能相互污染 | 聊天后端保留短期记忆，后一条 case 会看到前一条上下文 | 每个 case 开始前重置 memory，保证多模型横向比较公平 |
| Judge 服务异常被混入模型质量 | API/JSON 错误和低分没有分开统计，且模型返回的 `passed` 可与分数矛盾 | 严格解析单个 JSON 对象、校验分数范围、按阈值重算 pass；judge error 单独计数并排除质量均值 |
| 新旧法律和重复法条争夺排名 | 原始语料包含已废止法律及同法同条号完全重复文本 | 增加废止标记与可配置 penalty，只做保守精确去重，避免误删修订内容 |
| 旧 embedding cache 能加载但身份不完整 | 旧 metadata 没记录 query/document prefix、metadata embedding 开关和语料内容指纹，同目录可能静默复用错误向量 | 引入 cache schema v2、模型契约与 corpus SHA-256；dense 运行时 fail-closed，并提供 `cache-health` 迁移诊断 |
| 手工实验难复跑且只看平均延迟 | 多条 CLI 命令容易漏参数，单格失败会中断结果，平均值掩盖尾延迟 | 增加 experiment matrix harness，逐格记录状态并输出 CSV/JSON/Markdown、P50/P95、内存、调用/token/成本字段 |

这组结论里最重要的不是“组件越多越好”，而是保留负结果：RRF、Adaptive 和领域 embedding 都曾在合理假设下失败，项目据此调整默认策略，而不是隐藏实验。

## 快速开始

### 环境

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- 可选：Ollama，用于本地生成
- 可选：SiliconFlow API Key，用于 Qwen3 embedding、API 模型和 LLM-as-judge

```powershell
git clone https://github.com/1040942669/legal-rag-agent.git
Set-Location legal-rag-agent
uv sync --locked
```

### 准备语料

基础语料来自 Hugging Face 的 [Kuugo/Chinese_Law](https://huggingface.co/datasets/Kuugo/Chinese_Law)，其数据卡标注为 Apache-2.0，内容截止到 2025-01-01。下载后目录应满足：

```text
Chinese-Laws/
├── README.md
└── Chinese-Laws/
    ├── 中华人民共和国民法典.txt
    └── ...
```

仓库不提交完整法律文本、embedding cache 或生成报告。历史结果使用 203 部法律的本地增强快照，精确复跑历史数字需要相同语料；使用公开数据的当前版本时，应把新结果视为一次新的实验。

### 10 分钟本地语料链路

以下命令不调用 LLM，也不需要 API Key，但需要先准备上节说明的本地法律语料：

```powershell
uv run python -m legal_rag.cli profile-data
uv run python -m legal_rag.cli build-index --chunk-strategy article
uv run python -m legal_rag.cli evaluate `
  --chunk-strategy article `
  --retriever bm25 `
  --cases eval_cases/legal_eval_cases_v3.jsonl `
  --prefix v3_article_bm25
```

启动只检索、不生成的交互模式：

```powershell
uv run python -m legal_rag.cli chat --chunk-strategy article --retriever bm25 --no-generate
```

### Dense 与生成实验

复制环境变量模板并填写需要的 Key：

```powershell
Copy-Item .env.example .env
```

```text
SILICONFLOW_API_KEY=your_key
```

构建 Qwen3 embedding 并运行 dense 评测：

```powershell
uv run python -m legal_rag.cli build-embeddings `
  --chunk-strategy article `
  --embedding qwen3_embedding_4b `
  --batch-size 8

uv run python -m legal_rag.cli evaluate `
  --chunk-strategy article `
  --retriever dense `
  --embedding qwen3_embedding_4b `
  --cases eval_cases/legal_eval_cases_v3.jsonl
```

固定检索条件，对比多个生成 backend 并启用 judge：

```powershell
uv run python -m legal_rag.cli evaluate `
  --chunk-strategy article `
  --retriever bm25 `
  --cases eval_cases/legal_eval_cases_v3_gen_subset.jsonl `
  --generate `
  --models qwen2.5:7b,siliconflow:deepseek-ai/DeepSeek-V3 `
  --judge `
  --prefix v3gen_models_bm25
```

### Cache、矩阵与 Reranker

构建 embedding 后先验证缓存和当前 chunk/config 是否完全匹配：

```powershell
uv run python -m legal_rag.cli cache-health `
  --chunk-strategy article `
  --embedding bge_large_zh
```

一条命令复跑 direct/adaptive 矩阵：

```powershell
uv run python -m legal_rag.cli experiment-matrix `
  --chunk-strategies article `
  --retrievers bm25 `
  --adaptive-modes direct,adaptive `
  --rerankers none `
  --cases eval_cases/legal_eval_cases_v3.jsonl
```

可选 BGE reranker 默认不加载；显式启用时才下载模型并将 top-20 重排为 top-5：

```powershell
uv run python -m legal_rag.cli evaluate `
  --chunk-strategy article `
  --retriever bm25 `
  --reranker bge_v2_m3 `
  --rerank-top-n 20 `
  --cases eval_cases/legal_eval_cases_v3.jsonl
```

首次运行 `bge_v2_m3` 需要下载/加载约 2.29 GB 模型。它目前是实验能力，不是默认链路；只有完整 A/B 同时提升质量且 P95 可接受时才会晋升为默认。

### M2 实验生命周期（v0.3.0）

[已合并 PR #9](https://github.com/1040942669/legal-rag-agent/pull/9) 提供 `plan / run / resume / aggregate / replay` 五个显式入口。当前可执行后端是 provider-free BM25；`offline` 使用 2 条完全虚构的合成 case，`retrieval` 必须显式给出本地语料路径。`smoke-generation` 与 `full-regression` 可以生成计划，但在没有预算闸门和显式 provider 配置时会失败关闭，不会读取 `.env` 或尝试模型请求。

这些命令要求在 Git 源码工作区运行，因为 manifest 会记录真实 HEAD、dirty 状态、未跟踪文件摘要和分阶段实现指纹。默认原始工件、精确阶段缓存和聚合报告均写入被 Git 忽略的 `artifacts/experiments/`。

```powershell
# 只校验并打印 manifest，不创建实验目录
uv run python -m legal_rag.cli experiment plan `
  --experiment-id offline-demo `
  --mode offline

# 新建并执行；已有同名实验会被拒绝，必须显式 resume
uv run python -m legal_rag.cli experiment run `
  --experiment-id offline-demo `
  --mode offline

uv run python -m legal_rag.cli experiment resume `
  --experiment-id offline-demo

# 只读取持久工件，发布内容寻址的 JSON、JSONL、CSV 和 Markdown
uv run python -m legal_rag.cli experiment aggregate `
  --experiment-id offline-demo

# 创建新的 experiment_id，只允许精确缓存命中，任何 miss 都失败关闭
uv run python -m legal_rag.cli experiment replay `
  --source-experiment-id offline-demo `
  --experiment-id offline-demo-replay
```

回放不会退化为 fresh，也不会复用源实验目录。新实验的 attempt 会保留 `replay` 来源、原始 source call provenance、实际外部调用 0 和独立计时；聚合器不会调用 retriever、assistant 或 provider，也不会把损坏 case 补成成功。

2026-09-22 的真实无模型执行记录以提交 `f5f6e40c1d1219188069b625f6fa693b78dce588` 为代码身份：source `m2-offline-f5f6e40` 与 replay `m2-offline-f5f6e40-replay` 均完成 2/2 个 case；replay 的实际外部调用为 0，两个 case 的 cache mode 均为 `replay`。聚合键为 `befeb112749971f15329921e84603b4fb48c220469a0136c204d5a30fe4c4c0d`，bundle hash 为 `c848654fecf3ad6333c482ada39befd5e976ceb198cdc0934cbb4f23d64c3432`。原始产物位于本地忽略目录，没有提交到仓库；这些结果只证明生命周期语义，不是法律质量实验。

### M3 版本化存储与精确检索（发布候选，等待最终候选提交 CI）

draft/open [PR #13](https://github.com/1040942669/legal-rag-agent/pull/13) 已完成模块 A-D 的实现候选。PostgreSQL/pgvector 路径把 `scope / snapshot / embedding profile / law / version / article / effective date` 固化为 typed boundary，并在排序和 `LIMIT` 前应用 hard filter。exact inner-product 仍是默认且权威的数据库向量检索；BM25、dense、RRF、adaptive、rerank、chat 和 artifact 恢复路径都会复核同一 provenance、正文 hash、向量 hash 和 hydrated payload hash。

结构化法条目录按精确法名、规范化条号以及可选版本/生效日期返回互斥的 `found / not_found / needs_disambiguation`，不做模糊标题、相邻条文或跨版本兜底。active snapshot 以 `revision + activation_id`、不可变 activation ledger、事务 advisory lock、确定顺序 row lock 和完整 CAS 原子激活、替换与回滚；失败事务保留旧 active，运行中的请求继续固定原快照。

M3-D 新增四个关键边界：

- `plan`（别名 `dry-run`）与 `validate` 只读取本地产物并生成可重算的机器 JSON，不连接数据库；`apply` 会先重新验证全部本地输入，再选择性显式迁移、事务导入、数据库回读和发布 receipt，并且不会自动激活快照。
- exact 仍是默认路径。HNSW 只有显式构造实验 retriever 时才启用，按 snapshot/profile/dimension/generation 隔离物理索引，并把 build receipt 绑定到不可变 generation；维度超过 pgvector HNSW 2000 上限时提前拒绝，但 exact 仍可使用。
- 过滤后的 ANN underfill 不是成功假象，而是 typed outcome。策略可选择保留显式 underfill，或在相同 typed boundary 下执行有超时上限的 exact fallback；超时、候选不足和填满 top-k 分别记录，任何 fallback 都不会移除过滤条件。
- Alembic `0004_m3_ann_build_guards`、downgrade/re-upgrade 回归和真实 PostgreSQL 服务容器重启验证，证明 schema、导入数据、active pointer、exact、catalog、HNSW build receipt 与检索结果在新进程中仍可复核。

可复现导入的最小命令形态如下。请求 manifest 只允许相对 `source-root` 的本地产物路径；输出文件采用 no-clobber 语义：

```powershell
# 无数据库：生成确定性计划；dry-run 是 plan 的等价别名
uv run python -m legal_rag.storage.import_cli plan `
  --manifest artifacts/m3-import/request.json `
  --source-root artifacts/m3-import/source `
  --output artifacts/m3-import/plan.json

# 无数据库：重新构建输入身份并与计划精确比较
uv run python -m legal_rag.storage.import_cli validate `
  --manifest artifacts/m3-import/request.json `
  --source-root artifacts/m3-import/source `
  --plan artifacts/m3-import/plan.json

# 显式数据库操作；--migrate 不是默认行为，apply 也不会激活快照
$env:LEGAL_RAG_DATABASE_URL = "postgresql+psycopg://USER@127.0.0.1:5432/DB"
uv run python -m legal_rag.storage.import_cli apply `
  --manifest artifacts/m3-import/request.json `
  --source-root artifacts/m3-import/source `
  --plan artifacts/m3-import/plan.json `
  --migrate `
  --receipt artifacts/m3-import/receipt.json
```

精确实现 head `cf2c7a82195b63786d77bfb0d5cea9799b18013b` 的 [CI run 36073478416](https://github.com/1040942669/legal-rag-agent/actions/runs/36073478416) 两个 job 均成功。Linux/Python 3.12.13 离线结果为 `671 passed, 157 subtests passed`，JUnit 为 `828/0/0/0`；PostgreSQL 18 + pgvector 0.8.6 integration 为 `61 passed`；M0-M3 累计门禁为 `33/33`。本地对应结果为 `671 passed, 157 subtests passed`、PostgreSQL 18.1 + pgvector 0.8.1 integration `61 passed` 和累计门禁 `33/33`。当前七文件候选快照还完成了独立 `0.4.0` 包审计：wheel/sdist 分别为 66/109 个归档条目，禁止路径 0、migration 4/4，全新 Python 3.12.13 环境 no-index/no-deps 安装后 distribution、模块版本和 `legal-rag --help` 均通过。CI 同时通过真实服务重启与 wheel 资源检查；真实模型、远程 embedding、reranker、Judge 和付费调用均为 0。

这组证据覆盖实现 head，不覆盖本节候选文档及随后需要同步的版本元数据。因此当前状态只能是 `ready_for_review / awaiting final candidate CI`：PR #13 仍是 draft/open，`v0.4.0` Tag、GitHub Release 和发布回执均不存在；最终候选提交通过同等门禁前，不得写成已合并或已发布。

## 测试与复现边界

```powershell
uv run --offline --frozen --no-sync python scripts/quality_gate.py --milestone M2 --mode offline
```

该 M2 门禁不需要完整语料或模型 Key，会累积运行 M0 的 7 项工程检查、`M1-T01` 至 `M1-T10` 和 `M2-T01` 至 `M2-T08`，共 25 个必检 ID：全量测试、明确禁止 socket 访问的合成 BM25 smoke、包版本导入、CLI help、Markdown 相对链接、STATE/manifest JSON、候选文件凭证风险检查，以及结构/行为/指标、精确回放、缓存失效、恢复、损坏、并发、调用账本和分母边界。离线子进程会禁用 dotenv 加载并在 provider 边界拒绝真实模型调用；mandatory pytest 出现零测试、skip、xfail 或无效 JUnit 也不会假绿。最终 PR head `170da868831e2730ea15d56ed88ad24b1159e67b` 的 [CI](https://github.com/1040942669/legal-rag-agent/actions/runs/35686885130) 与 release target `da7023a659672121fd772364e475870a0167e1be` 的 [master CI](https://github.com/1040942669/legal-rag-agent/actions/runs/35687258856) 均为 `548 passed, 157 subtests passed`、JUnit 705/0/0/0、25/25。这仍不是 OS 级 air-gap，也不代表真实法律质量已经验证。

M3 的 33 项累计门禁必须连接一次性 PostgreSQL/pgvector 测试库，并接收真实服务重启前生成的 receipt。可直接复跑普通集成测试；完整门禁还必须由操作者在 prepare 与 verify 之间真正重启数据库服务，而不是只重建 SQLAlchemy Engine：

```powershell
uv run --frozen --no-sync pytest -q integration_tests

uv run --frozen --no-sync python scripts/m3_restart_probe.py prepare `
  --receipt artifacts/m3-restart-receipt.json

# 在这里真实重启 PostgreSQL 服务，等待 pg_isready 成功，再由新进程复核
uv run --frozen --no-sync python scripts/m3_restart_probe.py verify `
  --receipt artifacts/m3-restart-receipt.json

uv run --offline --frozen --no-sync python scripts/quality_gate.py `
  --milestone M3 `
  --mode integration `
  --restart-receipt artifacts/m3-restart-receipt.json `
  --output artifacts/m3-quality-gate.json
```

门禁要求 `ALLOW_LIVE_MODEL_CALLS=false`、禁用 dotenv，并把数据库限制在显式测试环境。M3-T01 至 M3-T08 覆盖确定性导入、exact 等价、精确法条、全路径过滤、ANN underfill/fallback、维度与 profile 防护、原子激活/回滚、迁移和真实服务重启；它证明工程与存储合同，不证明法律内容、时效性或模型回答正确。

需要明确区分三类可复现性：

1. 代码与离线逻辑：M0 发布时由 89 个测试提供基线；M1 `v0.2.0` 由 186 个测试、148 个子测试与 17 项累计门禁提供证据；M2 `v0.3.0` 由 548 个测试、157 个子测试和 25 项累计门禁覆盖；M3 实现候选由 671 个离线测试、157 个子测试、61 项数据库集成测试和 33 项累计门禁覆盖，但仍等待最终候选提交 CI。这不等于法律正确性保证。
2. 历史检索数字：依赖 2026-06 的 203 部法律快照及对应 embedding cache。
3. API 生成分数：还依赖外部模型版本、服务状态和 judge 偏差，不能视为永久固定值。

## 仓库结构

```text
legal_rag/                         核心实现
├── query.py                       规则查询分析与风险标记
├── query_understanding.py         JSON normalizer 与 fallback
├── planning.py                    有界 multi-query 计划
├── retrieval.py                   BM25、dense、RRF 与 trace
├── rerank.py                      Cross-encoder adapter 与 top-N 精排包装器
├── embeddings.py                  Encoder、cache schema 与 health check
├── experiments.py                 自动实验矩阵与统一汇总
├── experiment_runtime.py          实验身份、阶段缓存、case store 与通用 runner
├── experiment_adapter.py          真实评测/chat 阶段适配、恢复与纯评分
├── experiment_aggregation.py      只读 artifact 聚合与内容寻址发布
├── experiment_lifecycle.py        plan/run/resume/aggregate/replay CLI 编排
├── storage/                        M3 PostgreSQL/pgvector 存储边界
│   ├── import_cli.py               可复现 plan/dry-run/validate/apply 入口
│   ├── import_workflow.py          本地产物校验、导入计划与验证回执
│   ├── repository.py              事务导入与不可变语料 repository
│   ├── retrieval.py               filter-before-limit 的 exact pgvector 检索
│   ├── catalog.py                 精确法名/条号/版本目录与原子快照切换
│   ├── ann.py                     显式实验 HNSW、typed underfill 与 exact fallback
│   └── alembic/versions/           0001-0004 可打包数据库迁移
├── evidence.py                    证据充分性检查
├── verifier.py                    引用与回答校验
├── judge.py                       严格 LLM-as-judge 适配器
└── evaluation.py                  指标、置信区间与报告
configs/default.yaml               可审计的默认参数
eval_cases/                        固定评测集
tests/                             离线回归测试
integration_tests/                 真实 PostgreSQL/pgvector 隔离集成测试
scripts/m3_restart_probe.py        数据库服务重启前后跨进程验证
scripts/quality_gate.py            M0-M3 机器可读累计门禁
reports/RESULTS_SUMMARY.md         历史实验总表
docs/                              评测方法与开发计划
ARCHITECTURE_DECISION_LOG.md       关键架构决策和反例
```

生成的索引、embedding cache、CSV/JSON 报告和 trace 默认位于 `artifacts/`、`reports/`，均不进入 Git。

## 历史 Phase 实现快照与当前改造状态

截至 2026-09-18，旧 Phase 0-4B 路线实现了可复现 baseline、检索诊断与 trace、受控查询理解、最多一轮补检索、规则 verifier、v3 评测集、reranker adapter、embedding cache v2 和自动实验矩阵。旧 Phase 编号与当前 M0-M7 里程碑不一一对应；旧路线的 reranker/cache A/B 仍是未完成的实验项，不代表当前发布主线的下一步。

当前 M0-M7 主线以 [MASTER_PLAN](docs/refactor/MASTER_PLAN.md)、[STATE](docs/refactor/STATE.json) 和 [HANDOFF](docs/refactor/HANDOFF.md) 为权威来源。M0 已于 2026-09-19 作为 [v0.1.1](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.1.1) 发布并远端核验；M1 已于 2026-09-20 作为 [v0.2.0](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.2.0) 发布并远端核验；M2 软件 [PR #9](https://github.com/1040942669/legal-rag-agent/pull/9) 已正常合并，并于 2026-09-22 作为 [v0.3.0](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.3.0) 发布、远端核验。M2 的独立文档回执已通过 [PR #10](https://github.com/1040942669/legal-rag-agent/pull/10) 合并并通过 master CI，Issue #8 与 Milestone 3 已关闭；软件 Tag 没有因回执移动。M3 的 A-D 实现已在 draft/open [PR #13](https://github.com/1040942669/legal-rag-agent/pull/13) 完成实现 head 验收，当前为 `ready_for_review / awaiting final candidate CI`；包含候选文档和版本元数据的最终提交尚未通过 CI，`v0.4.0` Tag、GitHub Release 和发布回执也尚不存在。M4-M7 尚未开始。

## 文档导航

- [文档索引](docs/README.md)：公开文档的职责和阅读顺序。
- [历史实验结果](reports/RESULTS_SUMMARY.md)：完整数字、环境和解释边界。
- [评测方案](docs/EVALUATION_PLAN.md)：case 设计、指标定义和报告原则。
- [评测指标字典](docs/METRICS.md)：M1 schema v2、显式分母、N/A 与旧字段映射。
- [当前改造主计划](docs/refactor/MASTER_PLAN.md)：M0-M7 的范围、依赖和验收标准。
- [机器可读状态](docs/refactor/STATE.json) 与 [执行交接](docs/refactor/HANDOFF.md)：当前事实、下一步和阻塞项。
- [历史 Phase 0-5 执行记录](docs/LEGAL_RAG_EXECUTION_PLAN.md)：旧路线的状态、依赖和验收记录，仅供追溯。
- [Phase 4B 技术调研](docs/PHASE4B_RESEARCH_AND_DECISIONS.md)：最新机制、L9 可取原则、实现范围和暂缓项。
- [架构决策记录](ARCHITECTURE_DECISION_LOG.md)：为什么这样设计、哪些假设被实验推翻。

## 数据与责任说明

外部法律数据遵循其来源页面声明的许可证，本仓库不重新分发完整语料。法律法规会更新，任何实验结果都受语料截止日期影响。系统输出只适合作为信息检索线索，不能替代执业律师意见或官方法律数据库。
