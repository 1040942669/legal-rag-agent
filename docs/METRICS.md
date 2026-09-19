# 评测指标字典

本文定义 M1 起使用的评测指标 schema v2。它描述指标的含义、分母和不可用状态，不重新解释旧报告中的历史数字。旧 CSV 字段继续保留用于兼容；新代码和新报告应优先使用 `EvalRecord.to_canonical_dict()`、`canonical_metrics` 和显式执行状态。

## Case 行为真值

每个新 case 可以显式携带以下字段：

| 字段 | 含义 |
| --- | --- |
| `expected_behavior` | 预期行为：`evidence_answer`、`insufficient_evidence`、`needs_clarification` 或 `out_of_scope` |
| `session_group` | 多轮会话组；`null` 表示独立单轮 |
| `turn_index` | 组内从 0 开始的连续顺序；独立单轮必须为 0 |
| `schema_version` | case schema 版本 |

读取旧 case 时，`type=refusal` 映射为 `out_of_scope`，其他类型映射为 `evidence_answer`。`expected_answer_mode` 是 `expected_behavior` 的兼容别名；两个字段同时出现但值冲突时拒绝加载。预期行为只用于评测对照和分母，不反馈给生成、路由或 verifier，避免 gold 泄漏。

## 执行状态

每条记录分别保存 `service`、`generation`、`verification` 和 `judge`。状态使用 `succeeded`、`error`、`not_run`；服务返回经过验证的受限响应但上游生成失败时可以记录为 `degraded`。每个非成功状态同时保存稳定的 `reason`，例如：

- `retrieval_only`
- `programmatic_terminal`
- `generation_error`
- `service_error`
- `judge_not_configured`
- `timeout`
- `transport_error`
- `invalid_json`
- `invalid_schema`

Judge 错误不等于回答质量为零，也不写入主服务错误。只有 `judge.status=succeeded` 的记录进入 Judge 质量均值。

## Canonical 指标表示

schema v2 的每个指标统一表示为：

```json
{
  "value": null,
  "unavailable_reason": "retrieval_only"
}
```

有值时 `unavailable_reason` 为 `null`。不可用时 `value` 必须为 `null` 并说明原因，不能用 0 冒充失败或未执行。旧 CSV 中的 `-1` / `-1.0` 仅是兼容哨兵，聚合时必须排除。

## 检索指标

| Canonical 名称 | 分母 | 含义 | 旧字段 |
| --- | --- | --- | --- |
| `hit_at_3` / `hit_at_5` | 有 retrieval gold 的 case | 目标法律和条文是否进入 top-k | 同名 |
| `mrr` | 有 retrieval gold 的 case | 第一个匹配目标的倒数排名 | 同名 |
| `target_coverage` | 有条文 gold 的 case | top-k 覆盖的目标条文比例 | 同名 |
| `retrieval_target_hit` | 有 retrieval gold 的 case | 检索结果中是否存在目标来源 | `citation_hit` |

兼容字段 `citation_hit` 只表示检索目标命中，不是回答中的 citation 正确率，也不是 claim-source 支持率。拒答或其他无 retrieval gold 的 case 不进入 Hit@k、MRR 和覆盖率均值。

## 回答与验证指标

| Canonical 名称 | 可用条件 | 含义 |
| --- | --- | --- |
| `answer_text` | 产生最终面向用户的响应 | 最终交付文本，不是被拒绝的生成草稿 |
| `keyword_coverage` | 有最终响应 | 最终响应对 case 关键词的字面覆盖 |
| `schema_valid` | 执行 verifier | 结构化回答或兼容适配结果是否满足 schema |
| `evidence_catalog_valid` | 执行 verifier | 本次结果目录是否全部使用正整数且唯一的 rank/source ID；不检查来源正文或其他元数据 |
| `source_ids_exist` | 执行 verifier | 证据目录有效，且正文/claim 引用的全部 ID 都存在于该目录 |
| `citation_ids_valid` | 执行 verifier | 兼容复合结构门禁：目录与 ID 存在性通过、claim 引用可见；`evidence_answer` 还必须同时有结果、正文引用和 claim 引用 |
| `citation_alignment_valid` | 执行 verifier | 正文可见引用与 claim 绑定引用是否满足当前对齐规则 |
| `evidence_scope_valid` | 配置快照或权限范围检查 | 引用证据是否属于允许快照/范围；未配置时为 `null + scope_check_not_configured` |
| `citation_valid` | 执行 verifier | 旧兼容聚合：`citation_ids_valid=true` 且 `evidence_scope_valid` 不为 false；不包含语义支持判断 |
| `disclaimer_present` | 执行 verifier | 配置要求的免责声明是否位于响应末尾 |
| `response_mode_valid` | 执行 verifier | 最终文本、引用和澄清字段是否符合运行时预期模式 |
| `verifier_pass` | 执行 verifier | 本次配置要求的结构和行为检查是否通过 |
| `semantic_support_status` | 执行 verifier | `supported`、`unsupported`、`uncertain` 或 `not_checked` |
| `response_mode_correct` | 有预期模式和最终验证结果 | 最终模式是否等于 case 预期且模式行为检查通过 |

`citation_ids_valid=true` 不表示证据支持 claim。仅运行词面启发式时，语义层只能给出 `uncertain` 或 `not_checked`，不能因为没有发现问题就标为 `supported`。`verifier_pass` 不表示法律正确性。

`generation_attempt` 与最终响应分开保存。若草稿含伪造来源或其他结构/行为错误，Trace 记录被拒绝草稿的验证结果；回答指标只统计重新验证后的最终交付响应。

## 行为指标及分母

新报告必须列出下列分母：

- `retrieval_gold`
- `should_answer`
- `should_refuse`
- `should_clarify`
- `service_failures`
- `judge_succeeded`
- `judge_failed`
- `judge_not_run`

行为率统一同时报告 numerator、denominator 和 value：

| 指标 | 分子 | 分母 |
| --- | --- | --- |
| `refusal_recall` | 应拒答 case 中检测到明确拒答的数量 | 可评测的 `should_refuse` case |
| `over_refusal_rate` | 应回答 case 中被明确拒答的数量 | 可评测的 `should_answer` case |
| `clarification_recall` | 应澄清 case 中最终模式为 `needs_clarification` 的数量 | 可评测的 `should_clarify` case |
| `answer_mode_accuracy` | 最终模式等于预期模式且 `response_mode_valid=true` 的数量 | 有最终模式且服务未失败的 case |

逐 case canonical 字段与汇总关系如下：

- `refusal_recall_hit` 只在预期为 `out_of_scope` 时有布尔值，并聚合为 `refusal_recall`。
- `over_refusal` 只在预期为 `evidence_answer` 或 `insufficient_evidence` 时有布尔值，并聚合为 `over_refusal_rate`。
- `refusal_correctness` 在 canonical 中是 `refusal_recall_hit` 的兼容别名；非应拒答 case 为 `null + not_expected_to_refuse`，retrieval-only 为 `null + retrieval_only`。
- `response_mode_correct` 聚合为 `answer_mode_accuracy`；应澄清集合的最终模式另聚合为 `clarification_recall`。

`should_answer` 包含 `evidence_answer` 和 `insufficient_evidence`；`should_refuse` 只包含 `out_of_scope`；`should_clarify` 只包含 `needs_clarification`。服务失败从行为质量率的可评测分母中排除，但必须单独报告 `service_failures`，不能静默消失。

旧 `refusal_correctness` 保留在兼容 CSV 和 canonical 兼容别名中。schema v2 不再把“无需拒答”的普通 case 自动记为正确拒答，也不把免责声明或普通法律陈述中的“不得”“不能”当作拒答。

## Retrieval-only

`generate=false` 时只计算检索、证据覆盖和失败归因：

- `answer` 为空字符串；
- 不调用 answer verifier；
- 不调用 Judge；
- 回答、拒答、语义和 Judge canonical 指标均为 `null + retrieval_only`；
- 旧回答字段保留 `-1` / `-1.0` 兼容哨兵；
- Trace 的 `generation_attempt`、`final_response` 以及 generation/verification/judge 阶段明确记录未执行；service 阶段仍记录检索执行状态，旧 `verifier` 键为空对象。

这条边界不表示 retrieval 模式绝不会调用任何外部模型。后续 dense query embedding 或显式启用的 normalizer 需要由运行清单另行记录；它只保证没有回答生成、回答验证和 Judge 冒充为已执行。

## Judge

Judge 成功结果包含 `judge_faithfulness`、`judge_relevance`、`judge_completeness` 和重新计算的 `judge_pass`。超时、传输错误、无效 JSON 或字段 contract 错误分别归因，并将四项质量值保存为 `null + error_code`。报告同时列出成功、失败和未执行数量；均值只使用成功集合。

## 输出与历史兼容

- 新报告标记 `metrics_schema_version=2`。
- dict/list 类型的 canonical 字段以 JSON 写入 CSV，不能写成 Python 单引号 repr。
- 若目标 CSV 或 Markdown 任一已经存在，评测输出拒绝写入，避免覆盖历史报告或产生半套文件。
- 口径变化前后的总通过率不能直接比较并声称质量提升或退化。
- M1 合成样例只证明规则边界被修正，不代表真实法律问答质量提高。
