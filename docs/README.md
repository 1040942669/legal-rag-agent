# 文档索引

公开文档只保留能够解释代码、实验和后续开发状态的内容。课程资料、简历、面试逐字稿、临时分析和过期演示不属于项目交付物，不应与工程事实混用。

## 推荐阅读顺序

1. [项目 README](../README.md)：问题、架构、量化结果、踩坑、快速开始和当前状态。
2. [实验结果总汇](../reports/RESULTS_SUMMARY.md)：v3 历史矩阵的完整指标与复现边界。
3. [评测方案](EVALUATION_PLAN.md)：case 类型、指标语义、judge 使用方式和解释原则。
4. [架构决策记录](../ARCHITECTURE_DECISION_LOG.md)：关键取舍、失败假设、回滚条件。
5. [Phase 4B 技术调研](PHASE4B_RESEARCH_AND_DECISIONS.md)：最新 reranker/observability 机制、L9 可取原则和暂缓项。
6. [执行计划](LEGAL_RAG_EXECUTION_PLAN.md)：Phase 0-5 的任务拆分、依赖和验收状态。

## 文档职责

| 文档 | 负责回答 | 不负责回答 |
| --- | --- | --- |
| `README.md` | 项目是什么、有什么结果、如何运行 | 每个实验的全部明细 |
| `RESULTS_SUMMARY.md` | 历史实验用了什么配置、数字如何解释 | 对未来模型版本的效果承诺 |
| `EVALUATION_PLAN.md` | 指标和 case 为什么这样设计 | 产品路线和任务排期 |
| `ARCHITECTURE_DECISION_LOG.md` | 为什么采用或拒绝某个方案 | 当前任务完成状态 |
| `PHASE4B_RESEARCH_AND_DECISIONS.md` | 最新机制如何映射到本项目、哪些能力暂缓 | 通用 Agent 教程或模型排行榜 |
| `LEGAL_RAG_EXECUTION_PLAN.md` | 已完成什么、还差什么、如何验收 | 单次实验的详细结果 |

## 更新规则

- 新功能必须同时补测试和对应阶段状态。
- 改变默认策略时，必须记录对照指标、失败模式和回滚条件。
- Retrieval-only 报告不得展示 answer-only 指标。
- 外部 API 或模型版本变化造成的结果，只能记录为新的实验快照。
- 生成报告和 trace 留在本地，只有经过复核的汇总结论进入仓库。
