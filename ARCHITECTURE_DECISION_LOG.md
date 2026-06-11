# Architecture Decision Log

## 这份文档在做什么

这份文档记录本项目在开发过程中做过的关键架构取舍、被数据推翻的想法、实验设计和面试讲法。以后每次新增优化模块、删除模块、修改默认策略或发现失败模式，都要同步更新这里。

每条记录尽量保持这个结构:

```text
问题 / 触发点
最初想法
后来发现
为什么原方案不够
最终决策
面试讲法
后续验证指标
```

## 01. 不提前固定 chunk 大小

### 问题 / 触发点

项目一开始容易套用常见 RAG 经验，比如 256 或 512 token chunk。但用户指出数据集 README 已经说明每条法律条文独立成行，所以不能在看数据前拍板。

### 最初想法

直接设置固定 token chunk，并用 overlap 保证上下文连续。

### 后来发现

数据画像显示:

- 177 个法律文本文件；
- 14597 条记录；
- 平均每条 131 字符；
- P95 为 285 字符，P99 为 428 字符；
- 解析率 0.9875，大多数记录可解析出法律名称和条文号。

### 为什么原方案不够

固定 token chunk 会破坏天然条文边界，也会让引用变得不稳定。法律问答需要的是准确条文和来源，而不是简单凑上下文窗口。

### 最终决策

先做数据画像，再把 chunk 作为实验变量:

```text
article: 一条法律条文一个 chunk，作为 baseline
neighbor: 相邻条文合并，用于定义、例外和上下文依赖问题
long_split: 只切异常长条文
fixed_chars: 固定字符切分，仅作为对照组
```

### 面试讲法

```text
我没有直接套用 512 token chunk，因为现行法律数据集本身是一行一条法条。法律 RAG 的关键是保留条文边界和引用可解释性，所以我先做数据画像，再把条文级、邻近条文、长条文切分和固定长度切分作为实验变量，用 Hit@5、MRR、引用命中率和延迟来决定最终策略。
```

### 后续验证指标

- Hit@3 / Hit@5；
- MRR；
- 引用命中率；
- 平均检索延迟；
- chunk 数量和索引大小。

## 02. Embedding 模型采用三组对照而不是只选最大模型

### 问题 / 触发点

项目需要比较 embedding 模型。用户提出可以调用 API 或租用 GPU，所以不能只按本机资源选择小模型；同时也要能回答面试官“为什么这样选”。

### 最初想法

只使用一个通用中文 embedding 模型作为默认 dense retriever。

### 后来发现

法律 RAG 同时需要两类能力:

- 精确匹配法律名称、条文号和法律术语；
- 理解用户场景问题并召回相关法条。

因此只选一个模型很难说明 trade-off。

### 为什么原方案不够

单模型方案无法回答“领域模型是否有价值”“大模型 embedding 是否值得成本”“通用中文模型作为 baseline 到底差多少”。如果只说大模型更强，也无法解释成本、延迟和向量构建开销。

### 最终决策

采用三组 embedding 对照:

```text
bge_large_zh: BAAI/bge-large-zh-v1.5，中文通用强 baseline
chatlaw_text2vec: chestnutlzj/ChatLaw-Text2Vec，法律领域模型
qwen3_embedding_4b: Qwen/Qwen3-Embedding-4B，大模型上限组
```

`Qwen3-Embedding-8B` 暂时不作为前三组，保留为可选扩展。原因是当前语料规模约 1.46 万条，4B 已能代表高成本上限组；如果 4B 明显优于其他模型，再追加 8B 才有成本收益依据。

### 面试讲法

```text
我不是按参数量盲选 embedding，而是设计三组对照: 通用中文 baseline、法律领域模型和大模型上限组。法律问答既有条文号、法律名称这种精确匹配，也有场景语义召回，所以最终用 Hit@5、MRR、引用命中率、向量构建时间和平均检索延迟来决定，而不是默认越大越好。
```

### 后续验证指标

- 向量构建时间；
- embedding 维度和缓存大小；
- dense Hit@5 / MRR；
- RRF 后的 Hit@5 / MRR；
- 单次 query 编码延迟；
- 是否需要 GPU/API 才能接受。

## 03. 先上 RRF，reranker 放到第二阶段

### 问题 / 触发点

用户问是否需要加入 RRF 和 reranker。两者都能改善检索质量，但如果同时加入，会很难判断收益来自哪里。

### 最初想法

把 BM25、dense、RRF、reranker 全部放进第一版检索链路。

### 后来发现

法律 RAG 中 BM25 和 dense 的分数尺度不同，直接加权分数不稳定。RRF 只看排名，适合把 BM25 精确匹配和 dense 语义召回融合起来。

### 为什么原方案不够

Reranker 是重排 top-N 候选的增强模块，会带来额外模型调用、延迟和成本。如果基础召回不稳定，直接上 reranker 会掩盖 BM25、embedding 和 chunk 策略本身的问题。

### 最终决策

第一阶段检索矩阵:

```text
BM25 only
Dense only
BM25 + Dense + RRF
```

第二阶段再做:

```text
RRF top-20 -> reranker -> top-5
```

候选 reranker:

```text
BAAI/bge-reranker-base: 轻量实验
BAAI/bge-reranker-large: 效果上限
```

### 面试讲法

```text
我先引入 RRF，而不是马上加 reranker。因为 BM25 适合法律名、条号、术语精确匹配，dense 适合场景化语义召回，两者分数尺度不同，所以用 RRF 按排名融合，避免归一化分数带来的不稳定。reranker 放在第二阶段，只对 RRF top-20 重排，这样可以清楚衡量它相对基础召回的增益和延迟成本。
```

### 后续验证指标

- BM25 vs dense vs RRF 的 Hit@5 / MRR；
- RRF top-20 中目标法条召回率；
- reranker 后 top-5 引用命中率；
- reranker 增加的平均延迟；
- 失败样例是否从“没召回”变成“召回但排序低”。

## 04. Qwen3-Embedding-4B 使用 SiliconFlow API 而不是本地加载

### 问题 / 触发点

`Qwen3-Embedding-4B` 被选为大模型 embedding 上限组，但本地加载 4B 模型会占用较多显存/内存，和课程项目的快速实验目标冲突。

### 最初想法

像 BGE 和 ChatLaw 一样，用 `sentence-transformers` 在本地或租用 GPU 服务器构建向量。

### 后来发现

用户可使用 SiliconFlow，并且该平台支持 OpenAI-compatible embeddings API。对 14597 条法条构建向量时，API 调用能避免本地环境和显存配置问题，也更接近真实工程中的“按需调用外部模型服务”。

### 为什么原方案不够

本地加载 4B embedding 模型会把实验重点从“检索效果比较”转移到“环境和显存调试”。如果为了跑一个上限组而阻塞整个实验矩阵，工程收益不高。

### 最终决策

保留三组 embedding 的实验含义，但调整部署方式:

```text
bge_large_zh: 本地 sentence-transformers
chatlaw_text2vec: 本地 sentence-transformers
qwen3_embedding_4b: SiliconFlow API
```

API Key 不写入配置文件，只从环境变量 `SILICONFLOW_API_KEY` 读取。

### 面试讲法

```text
我保留 Qwen3-Embedding-4B 作为大模型上限组，但没有强行本地部署。因为这个组的目的不是证明我能调显卡，而是比较大模型 embedding 在法律 RAG 检索中的边际收益。为了控制实验成本和环境风险，我用 SiliconFlow 的 OpenAI-compatible embeddings API 构建向量缓存，并把 API 结果和本地 BGE/ChatLaw 在同一套 Hit@5、MRR、延迟指标下比较。
```

### 后续验证指标

- API 向量构建总耗时；
- 每批次请求稳定性和失败重试次数；
- 向量维度和缓存大小；
- RRF 后 Hit@5 / MRR；
- 与本地 BGE/ChatLaw 的延迟和质量差异；
- API 成本是否值得。

## 05. Phase 1 先做失败归因，再继续加 Adaptive RAG

### 问题 / 触发点

Phase 0 已经能重建 baseline，但检索失败时只能看到 Hit@5 失败，无法解释失败来自解析、chunk 边界、query 表达、召回不足、排序不足还是 metadata 缺口。

### 最初想法

直接进入 LLM query understanding，让模型把用户问题改写成更适合检索的 query。

### 后来发现

如果没有失败归因和 trace，LLM normalizer 即使命中率提升，也很难判断收益来自 query 改写、候选法律提示、多 query 覆盖，还是偶然排序变化。法律 RAG 需要可复盘的证据链，而不是只看单次回答是否看起来正确。

### 为什么原方案不够

过早加入 Adaptive RAG 会扩大调试面:

```text
用户 query -> LLM normalizer -> planner -> retrieval -> merge -> answer
```

如果检索失败，无法快速定位是 normalizer 改坏了 query，还是原始 BM25/chunk/metadata 本来就有问题。

### 最终决策

Phase 1 先补齐确定性诊断能力:

```text
sliding neighbor chunk
chunk diagnostics
rule-based query analyzer
configurable BM25 boost
BM25/RRF ranking trace
failure labeler
retrieval trace JSONL
```

默认检索链路仍保持保守，`article + BM25` 不因为 Phase 1 自动变成 adaptive。复杂 query 的 LLM normalizer 放到 Phase 2。

### 面试讲法

```text
我没有在 baseline 后马上加 LLM query rewrite，而是先做失败归因。因为法律 RAG 的关键不是让链路更复杂，而是能解释为什么没召回正确法条。我给 BM25/RRF 加了 ranking trace，给 evaluation 加了 wrong_law、wrong_article、metadata_gap、low_rank 等 failure label，并让 build-index 输出 chunk diagnostics。这样后续再加 adaptive query understanding 时，可以用同一套 trace 判断它到底解决了哪个失败类型。
```

### 后续验证指标

- failure label 分布；
- `wrong_law` 和 `wrong_article` 的前 N 失败样例；
- RRF trace 中 BM25/dense 子排名是否互补；
- sliding neighbor 是否改善多条文和相邻条文 case；
- trace JSONL 是否足以复现单个失败 case。

## 06. Phase 2 采用受控 Adaptive RAG，而不是自由 Agent Loop

### 问题 / 触发点

真实用户不会总是输入“某法第几条规定了什么”这种清晰问题。很多输入会混合情绪、多个意图、候选法律、矛盾事实或很长的场景描述。如果直接把整段原文交给 BM25，关键词会被稀释，跨法律问题也容易只召回一部分证据。

### 最初想法

引入 LLM query understanding，让模型把用户问题改写成更适合检索的 query，并自动决定后续检索动作。

### 后来发现

法律场景下自由 agent loop 风险太高: 模型可能无限补检索、扩展到用户没有问的法律问题，或者把个案策略包装成检索结论。Phase 1 已经有 analyzer、failure label 和 retrieval trace，因此 Phase 2 更适合做 bounded planning，而不是开放工具调用。

### 为什么原方案不够

如果 normalizer 没有 JSON contract，评测结果会漂移；如果 planner 没有限制，multi-query retrieval 会引入噪声；如果 merge trace 不记录来源 query，后续 answer verifier 无法判断证据链是否可靠。

### 最终决策

Phase 2 加入受控 adaptive lane:

```text
Query Analyzer -> trigger check -> NormalizedQuery JSON -> RetrievalPlan -> multi-query retrieval -> dedupe merge -> trace
```

默认 direct retrieval 不变。只有 `--adaptive` 且 analyzer 判断为 `vague`、`contradictory`、`emotional`、`too_long`、`multi_intent`、`many_law_hints` 或 `low_confidence` 时，才触发 adaptive。`--adaptive` 默认使用 deterministic fallback normalizer；只有 `--adaptive-use-llm` 才调用 Ollama 严格 JSON normalizer，异常时回退到规则结果。

### 面试讲法

```text
我没有把法律 RAG 升级成自由 Agent，而是做了受控 Adaptive RAG。清晰短查询继续走单 query 检索；复杂输入才触发 normalizer。LLM 只允许输出固定 JSON contract，planner 最多生成有限个 RetrievalPlan，merge 按 chunk_id 去重并保留 source_query 和 plan_id。这样既能处理多意图和模糊问题，又能用 trace 复盘每条证据来自哪里。
```

### 后续验证指标

- adaptive cases 上 direct vs adaptive 的 Hit@5 / MRR；
- adaptive 触发率和误触发率；
- normalizer fallback 次数和错误类型；
- 每个 case 的 plan 数、去重前后 evidence 数；
- multi-query 是否改善 `wrong_law`、`wrong_article` 或 `miss`；
- Phase 3 sufficiency checker 是否能消费 merge trace。

## 07. Phase 3 先做规则型证据校验，而不是 LLM Verifier

### 问题 / 触发点

Phase 2 已经能把复杂 query 拆成 bounded retrieval plans，但生成前仍缺少一道明确的证据门槛。检索结果为空、缺少目标法律/条文、引用编号不存在或用户请求个案策略时，系统不能把风险全部交给生成模型处理。

### 最初想法

加入一个 LLM verifier，让模型判断答案是否被证据支持，并在不足时自动补检索。

### 后来发现

LLM verifier 本身会引入漂移和成本，也可能把“看起来合理”的答案误判为通过。当前更需要的是稳定、可测试、可写入 trace 的底线能力: 引用编号是否存在、免责声明是否保留、是否命中高风险请求、证据是否覆盖显式法律和条号提示。

### 为什么原方案不够

如果直接上 LLM verifier，失败时很难区分是检索证据不足、回答引用错误，还是 verifier 判断漂移。法律 RAG 的 Phase 3 目标是把资料不足和越界风险显式化，而不是追求复杂 agent loop。

### 最终决策

Phase 3 采用规则型链路:

```text
Query Analyzer -> retrieval/adaptive merge -> EvidenceCheck -> at most one follow-up retrieval -> answer -> VerificationResult
```

`EvidenceCheck` 检查无结果、显式法律/条文缺失、低覆盖和 normalizer 标出的缺失事实；证据不足时最多补检索一轮。`VerificationResult` 检查 `[Sx]` 引用有效性、免责声明、高风险拒答和基础证据支撑。失败时统一降级到资料不足或边界拒答模板。

### 面试讲法

```text
我没有直接做自由补检索或 LLM verifier，而是先把证据门槛做成规则型、可测、可追踪。系统会在生成前判断证据是否覆盖显式法律和条文提示，证据不足最多补检索一轮；生成后校验引用编号、免责声明和越界拒答。这样能稳定检测虚假引用、资料不足和个案策略请求，同时保留 trace 解释每次为什么降级。
```

### 后续验证指标

- Evidence sufficiency pass；
- Citation validity；
- Verifier pass；
- Refusal correctness；
- follow-up retrieval 触发率和 `stop_reason` 分布；
- 被降级样例中是否真的缺法律依据或引用无效。
