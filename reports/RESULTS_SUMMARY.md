# 实验结果总汇 (v3 评测集)

> 本文保存历史实验快照与当时的解释，不是当前法律覆盖或语义正确性证明。
> Judge 分数受模型和 self-preference 影响；规则 verifier 只做引用编号、免责声明、
> 宽泛拒答词和有限词面启发式检查。引用存在或 verifier pass 不等于证据语义支持结论。

> 生成时间: 2026-06-13。所有检索实验使用 `eval_cases/legal_eval_cases_v3.jsonl`（120 cases，108 条有 gold 标注 + 12 条拒答），
> 指标为"有目标样例"上的值（排除拒答类），附 bootstrap 95% 置信区间。
> 数据集: 203 部法律 / 19,050 条文级 chunk（含专利法、劳动争议调解仲裁法、个人信息保护法、工伤保险条例、物业管理条例补录；
> 民法通则/合同法/继承法已废止打标降权 penalty=0.5；同法同条号完全重复条文已去重）。
>
> 复现边界: 这是 2026-06 本地增强语料、当时 embedding cache 和 API 模型版本的历史快照。仓库保存评测逻辑、v3 cases 和复现命令，但尚未提供自动下载并校验同一语料快照的流程；未来 API 模型更新也可能改变生成分数。

> Phase 4B 是旧路线编号，不表示当前 M0-M7 主线中的 M4 或其他里程碑已经完成。

## 1. Chunk 策略对比（BM25, article 索引外其余同参）

| 策略 | chunks | 索引体积 | 构建峰值内存 | Hit@3 | Hit@5 | MRR | 平均延迟 |
|---|---|---|---|---|---|---|---|
| article | 19,050 | 14.18 MB | 365 MB | 0.639 | 0.704 [0.611, 0.787] | 0.608 | 229 ms |
| neighbor (w=3) | 6,419 | 10.28 MB | 344 MB | **0.685** | 0.713 [0.620, 0.796] | **0.630** | **91 ms** |
| long_split | 19,192 | 14.29 MB | 365 MB | 0.639 | 0.704 [0.611, 0.787] | 0.612 | 233 ms |
| fixed_chars | 5,743 | 10.49 MB | 409 MB | 0.685 | **0.722** [0.630, 0.806] | 0.608 | 92 ms |

**Memory-performance tradeoff 结论**：chunk 数直接决定 BM25 检索延迟（19k chunks ≈ 230ms vs 6k chunks ≈ 91ms）。
neighbor/fixed_chars 用更少的 chunk 拿到了相当甚至略高的 Hit@5，但置信区间高度重叠，差异不显著；
article 仍保留为 baseline，因为它的条文级 metadata 最精确（条号引用、来源展示都依赖它）。

## 2. 检索器对比（article chunk）

| 检索器 | Embedding | Hit@3 | Hit@5 | MRR | 平均延迟 |
|---|---|---|---|---|---|
| BM25（自研） | - | 0.639 | 0.704 [0.611, 0.787] | 0.608 | 229 ms |
| Dense | bge-large-zh-v1.5 | 0.667 | 0.676 [0.583, 0.759] | 0.538 | **25 ms** |
| Dense | ChatLaw-Text2Vec | 0.185 | 0.222 [0.148, 0.296] | 0.160 | 19 ms |
| Dense | **Qwen3-Embedding-4B (API)** | **0.954** | **0.981** [0.954, 1.000] | **0.884** | 352 ms |
| RRF (BM25+Dense) | bge-large-zh-v1.5 | 0.713 | 0.796 [0.713, 0.870] | 0.609 | 263 ms |
| RRF (BM25+Dense) | Qwen3-Embedding-4B | 0.917 | 0.944 [0.898, 0.982] | 0.783 | 537 ms |
| LlamaIndex BM25 | - | 0.093 | 0.111 [0.056, 0.176] | 0.090 | 0.2 ms |
| LlamaIndex Dense | bge-large-zh-v1.5 | 0.796 | 0.870 [0.806, 0.935] | 0.709 | 633 ms |

关键发现：

1. **Qwen3-Embedding-4B dense 是全场最优**（Hit@5 = 0.981），比 BM25 baseline 高 27.7 个百分点，置信区间完全不重叠，差异显著。
2. **RRF 不是免费的午餐**：当 dense 远强于 BM25 时，RRF 融合反而被 BM25 拖累（0.944 < 0.981）。RRF 只在两路质量接近时有增益（bge: 0.796 > max(0.704, 0.676)）。
3. **LlamaIndex 自带 BM25 几乎不可用**（0.111）：其默认分词器不处理中文。自研 BM25 用单字+bigram+书名号+条号分词、加法律名/条号 metadata boost，拿到 0.704。这是"为什么自研检索而不用框架默认组件"的直接证据。
4. **LlamaIndex Dense（同样的 bge 模型）0.870 显著高于自研 Dense 的 0.676**：LlamaIndex 默认把 metadata（法律名、条号）拼进 embedding 文本。该发现已回灌到自研管线（`embed_with_metadata` 消融，见第 5 节）。
5. **法律领域专用模型 ChatLaw-Text2Vec 表现最差**（0.222），"领域模型一定更好"不成立，旧的 SBERT 架构 + 小参数量打不过新一代通用模型。

## 3. Adaptive（受控查询理解）A/B 对比（同一 120 cases）

| 配置 | Hit@5 (scored) | MRR | 平均延迟 |
|---|---|---|---|
| BM25 direct | **0.704** | **0.608** | 229 ms |
| BM25 + adaptive | 0.667 | 0.577 | 415 ms |
| Dense qwen3 direct | **0.981** | **0.884** | 352 ms |
| Dense qwen3 + adaptive | 0.954 | 0.867 | 347 ms |

分题型看（Hit@5，BM25）：

- multi_article: 0.800 → 0.867（**adaptive 唯一正收益**：多意图拆分确实帮助多条文召回）
- semantic_scenario: 0.467 → 0.367（规则 normalizer 改写丢失语义信息）
- adaptive_contradictory: 0.667 → 0.333（改写后丢掉关键词）
- 其余题型不变

**结论：在当前实现下 adaptive lane 整体为负收益**，只对 multi_article 有效。规则式 query 改写在强检索器（qwen3 dense）面前纯粹有害。
这是一个诚实的受控实验结论：查询理解的价值取决于检索器强度，盲目加"agentic 查询改写"会损害效果。
后续方向：只对 multi_intent 触发 adaptive、或改用 LLM normalizer 重测。

## 4. 拒答与可控性

- 12 条拒答 case（案件策略/违法帮助/非法律/医疗金融）在当前规则 router 下均能命中至少一个高风险 flag（12/12）。这验证的是路由覆盖，不等于回答质量。
- 30 条生成子集只抽取了其中 3 条拒答 case；四个 backend 都在生成前走统一规则拒答，因此该子集的 Refusal correctness = 1.000。旧指标还会把非风险样例自动记为 true，因此这个值不证明各 backend 分别学会拒答，也不衡量过度拒答、危险建议是否仍被输出或语义适当性。
- 旧版 retrieval-only 报告曾把拼接的检索文本送入 verifier，得到的 `0.933` 不具备“11/12 正确拒答”的含义；当前实现已将 retrieval-only 的回答指标标为 `N/A`。
- 全部 120 case 校验过 gold：expected_law/expected_articles 均真实存在于索引中。

## 5. Metadata embedding 消融（自研 dense, bge-large-zh-v1.5, article chunk）

起因：第 2 节发现同一个 bge 模型，LlamaIndex dense（0.870）远高于自研 dense（0.676）。
逐项把 LlamaIndex 的默认行为搬回自研管线做消融：

| 配置 | Hit@5 (scored) | MRR |
|---|---|---|
| 自研 dense（纯 chunk 文本） | 0.676 [0.583, 0.759] | 0.538 |
| + metadata 拼接（法律名/条号前置） | 0.731 [0.639, 0.815] | 0.551 |
| + bge 官方 query instruction（"为这个句子生成表示…"） | 0.769 [0.685, 0.843] | 0.577 |
| LlamaIndex dense（参照） | 0.870 [0.806, 0.935] | 0.709 |

结论：

1. **metadata 拼接 +5.5pp，query instruction 再 +3.8pp**，合计解释了约一半差距。
   "embedding 喂什么文本"比换模型便宜得多，是 dense 检索第一性的工程杠杆。
2. 剩余 ~10pp 差距来自 LlamaIndex 节点文本的其他默认处理（完整 metadata 模板、文本格式化），
   未继续追平——因为自研管线的真正答案是直接换 Qwen3-Embedding-4B（0.981），而不是在 bge 上磨剩余 10pp。
3. 该发现已固化到配置：`bge_large_zh_meta`（embed_with_metadata + query_prefix）作为消融配置保留，
   `qwen3_embedding_4b_meta` 作为显式消融配置开启 metadata 拼接；项目默认 embedding key 仍是 `bge_large_zh`。

## 6. LLM backend 生成对比（30 case 子集 + LLM-as-judge DeepSeek-V3）

检索固定为 BM25 + article（Hit@5 = 0.667，四组完全一致，保证只比生成质量）。
报告: `reports/v3gen_models_bm25.md`。

| 模型 | 部署 | 关键词覆盖率 | Verifier pass | Judge faithfulness | Judge pass | 平均延迟 |
|---|---|---|---|---|---|---|
| qwen2.5:7b (q4_K_M) | 本地 Ollama | 0.433 | 0.467 | 0.988 | **1.000** | 13.8 s |
| Qwen2.5-7B-Instruct | SiliconFlow API | 0.233 | 0.267 | 0.950 | 0.933 | **5.6 s** |
| Qwen3.5-4B | SiliconFlow API | **0.500** | **0.633** | 0.940 | 0.933 | 141.3 s |
| DeepSeek-V3 | SiliconFlow API | 0.439 | 0.500 | **1.000** | **1.000** | 7.1 s |

四模型汇总（30 cases × 4 backends = 120 model-case records）：Faithfulness 0.970 / Relevance 0.978 / Completeness 0.975 / Judge pass 0.967，
Citation validity 0.925；Refusal correctness 1.000（30 条子集只含 3 条拒答 case，每个 backend 均由同一规则 router 在生成前拒答，且普通样例按旧口径自动记为 true，样本量和分母均有限）。

这里的 `Citation validity` 只检查引用 rank 是否存在，`Verifier pass` 只表示旧版规则门禁未产生失败原因；二者都不是 claim-support rate、语义忠实度或法律正确率。

关键发现：

1. **小模型 + 长思考可以换质量**：Qwen3.5-4B（推理型）verifier pass 0.633 全场最高，但平均延迟 141s，
   是 DeepSeek-V3 的 20 倍。质量-延迟 tradeoff 非常陡峭。
2. **本地 4bit 7B 在该子集上未观察到明显劣势**：qwen2.5:7b q4_K_M（verifier 0.467）高于 API 版
   Qwen2.5-7B-Instruct（0.267），但两者并非完全同一权重（本地为 base 系微调版、API 为 Instruct）。
   这只能作为部署可行性信号，不能替代同权重、同提示、同样例的严格量化 A/B。
3. **历史 judge faithfulness 普遍 ≥ 0.94**：这是当时 judge 与提示配置下的信号，
   不能据此断言不同 backend 的差距不在幻觉，也不能替代语义支持或人工法律审查。
4. **方法论注意**：judge 为 DeepSeek-V3，对 DeepSeek-V3 自身的打分存在 self-preference 偏置，
   该行结论需谨慎引用；verifier pass 也是有已知盲区的规则启发式，不是无偏横向真值。

## 7. Phase 4B 自动矩阵复跑（2026-09-18）

Run ID: `experiment_matrix_20260917T182859Z_aeb3d324`。同一份 120-case v3 数据、article chunk、自研 BM25、top-k=5；108 条有 gold 样例进入 Hit/MRR，12 条 refusal 不混入检索均值。

| 配置 | Hit@3 | Hit@5 | MRR | 目标覆盖率 | 平均延迟 | P50 | P95 | Build time | Build peak |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| direct | 0.639 | **0.704 [0.611, 0.787]** | **0.608** | **0.661** | 243.5 ms | 201.0 ms | 316.1 ms | 3.228 s | 364.9 MB |
| rules adaptive | 0.620 | 0.667 [0.574, 0.759] | 0.578 | 0.624 | 437.5 ms | 288.0 ms | 1189.3 ms | 3.719 s | 364.9 MB |

本次自动化结果的意义:

1. direct 的 Hit@5/MRR 与 2026-06 手工结果一致，证明 matrix harness 没有改变指标语义。
2. adaptive 的质量仍然更低，同时 P95 是 direct 的约 3.8 倍，继续默认关闭不仅是质量决策，也是尾延迟决策。
3. 2026-09 平均延迟与 2026-06 不完全一致，说明跨日期、跨环境不应把毫秒值当成稳定常数；同一次 run 内的相对差异才可比较。
4. runner 同时产出 CSV、JSON、Markdown，并保留每格状态、失败原因、内存、rerank/LLM 调用和 token 字段。生成文件默认不提交，复核后的结论进入本报告。

### Cache v2 迁移结果

真实旧 `bge_large_zh` cache 可读取且 chunk/vector 数一致，但缺少 `schema_version`、query/document prefix、`embed_with_metadata` 和 corpus/contract fingerprint。`cache-health` 将其判定为 invalid，避免 silent reuse。

尝试在当前机器重建 19,050 个 1024 维向量时，CPU 速度约 7 秒/批、1,191 批，预计超过 2 小时，因此主动停止且未覆盖旧 cache。后续应在 GPU 或可接受长任务的环境重建，然后先通过 `cache-health` 再运行 dense/RRF 矩阵。

### Reranker 证据边界

`BAAI/bge-reranker-v2-m3` adapter、top-N trace 和 runtime stats 已完成并通过 fake scorer 回归测试，但约 2.29 GB 的真实模型尚未跑完整 v3 A/B。本报告不提供虚构的 reranker 分数；默认仍为 `none`。

## 复现命令

```powershell
# chunk 策略对比
python -m legal_rag.cli evaluate --chunk-strategy <article|neighbor|long_split|fixed_chars> --retriever bm25 --cases eval_cases/legal_eval_cases_v3.jsonl --prefix v3_<s>_bm25
# 检索器对比
python -m legal_rag.cli evaluate --chunk-strategy article --retriever <bm25|dense|rrf|llamaindex_bm25|llamaindex_dense> --embedding <key> --cases eval_cases/legal_eval_cases_v3.jsonl
# adaptive A/B
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_v3.jsonl --adaptive
# Phase 4B 自动 direct/adaptive 矩阵
python -m legal_rag.cli experiment-matrix --chunk-strategies article --retrievers bm25 --adaptive-modes direct,adaptive --rerankers none --cases eval_cases/legal_eval_cases_v3.jsonl
# cache v2 契约检查
python -m legal_rag.cli cache-health --chunk-strategy article --embedding bge_large_zh
# 可选 reranker A/B（首次运行需下载/加载模型）
python -m legal_rag.cli experiment-matrix --chunk-strategies article --retrievers bm25 --adaptive-modes direct --rerankers none,bge_v2_m3 --rerank-top-n 20 --cases eval_cases/legal_eval_cases_v3.jsonl
# 多模型生成 + judge
python -m legal_rag.cli evaluate --chunk-strategy article --retriever bm25 --cases eval_cases/legal_eval_cases_v3_gen_subset.jsonl --generate --models <m1,m2,...> --judge
```
