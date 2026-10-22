# 文档索引

公开文档只保留能够解释代码、实验和后续开发状态的内容。课程资料、简历、面试逐字稿、临时分析和过期演示不属于项目交付物，不应与工程事实混用。

## 推荐阅读顺序

1. [项目 README](../README.md)：问题、架构、量化结果、踩坑、快速开始和当前状态。
2. [项目边界与版本决定](PROJECT_SCOPE.md)：范围、语料、评测与流程的决定；2026-10-22 起解除冻结，v1.2 的计划在第 8 节。
   [核心架构说明](ARCHITECTURE_CORE.md)：一次问答请求经过的每个机制做什么、为什么、怎么验证，以及已知问题。
3. [项目复盘](../reports/CASE_STUDY.md)、[v1.0.0 基线报告](../reports/retrieval/V1_0_BASELINE.md) 与 [v1.1.0 设计消融](../reports/retrieval/V1_1_SILICONFLOW.md)：问题如何被发现、冻结语料上 BM25、向量检索、多路召回与重排的数字。
4. [实验结果总汇](../reports/RESULTS_SUMMARY.md)：2026-06 历史矩阵的完整指标与复现边界（旧语料，条件不同）。
5. [评测方案](EVALUATION_PLAN.md)：case 类型、实验流程、judge 使用方式和解释原则。
6. [评测指标字典](METRICS.md)：M1 schema v2、显式分母、N/A 和旧字段映射的权威定义。
7. [架构决策记录](../ARCHITECTURE_DECISION_LOG.md)：关键取舍、失败假设、回滚条件。
8. [Phase 4B 技术调研](PHASE4B_RESEARCH_AND_DECISIONS.md)：reranker/observability 机制、L9 可取原则和暂缓项。
9. [历史 Phase 执行计划](LEGAL_RAG_EXECUTION_PLAN.md)：截至 2026-09-18 的 Phase 0-5 记录，不是当前 M0-M7 路线。
10. [M0-M7 改造主线](refactor/MASTER_PLAN.md)、[状态](refactor/STATE.json) 与 [交接记录](refactor/HANDOFF.md)：M0-M6 的发布历史；M7 已取消，v1.0.0 起不再更新。
11. [M6 验收报告](../reports/refactor/M6.md)、[观测修补报告](../reports/refactor/M6-observability-patch.md)、[v0.7.1 回执](refactor/receipts/M6-v0.7.1.json) 与 [ADR-004](refactor/decisions/ADR-004-m6-batch-jobs-observability.md)：批任务、真实 broker/worker、运行时观测、两版发布、完成的独立回执及治理关闭的真实证据。

## 文档职责

| 文档 | 负责回答 | 不负责回答 |
| --- | --- | --- |
| `README.md` | 项目是什么、有什么结果、如何运行 | 每个实验的全部明细 |
| `PROJECT_SCOPE.md` | 做什么、不做什么、什么情况才开新版本 | 实验明细 |
| `reports/CASE_STUDY.md`、`reports/retrieval/*.md` | 检索问题的发现过程、冻结基线与历史对比 | 对新问题的泛化承诺 |
| `RESULTS_SUMMARY.md` | 历史实验用了什么配置、数字如何解释 | 对未来模型版本的效果承诺 |
| `EVALUATION_PLAN.md` | case、实验流程和历史评测为什么这样设计 | M1 指标字段的逐项 contract |
| `METRICS.md` | M1 schema v2 指标、分母、N/A 与兼容映射 | 产品路线、历史实验结果 |
| `ARCHITECTURE_DECISION_LOG.md` | 为什么采用或拒绝某个方案 | 当前任务完成状态 |
| `PHASE4B_RESEARCH_AND_DECISIONS.md` | 最新机制如何映射到本项目、哪些能力暂缓 | 通用 Agent 教程或模型排行榜 |
| `refactor/MASTER_PLAN.md`、`STATE.json`、`HANDOFF.md` | M0-M6 的路线与发布记录（历史） | 当前范围，见 `PROJECT_SCOPE.md` |
| `reports/refactor/M6.md`、`refactor/decisions/ADR-004-*.md` | M6 任务机制、真实门禁与可恢复边界 | 真实法律质量或生产吞吐证明 |
| `LEGAL_RAG_EXECUTION_PLAN.md` | 历史 Phase 0-5 做过什么、当时还差什么 | 当前 M0-M7 发布状态 |

## 更新规则

- v1.0.0 起范围冻结：只修正确性 bug，开新版本的条件见 `PROJECT_SCOPE.md` 第 6 节。
- 修复必须同时补回归测试。
- 改变默认策略时，必须记录对照指标、失败模式和回滚条件。
- Retrieval-only 报告不得展示 answer-only 指标。
- 新评测报告必须记录 metrics schema；不可用值使用 `null + reason`，旧 `-1` 哨兵不得进入均值。
- 外部 API 或模型版本变化造成的结果，只能记录为新的实验快照。
- 生成报告和 trace 留在本地，只有经过复核的汇总结论进入仓库。
