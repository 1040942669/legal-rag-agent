# 有限工程可演示性改善验收

日期：2026-10-07。状态：in_progress。这是软件工程改善报告，不是个人经历或法律质量验收。对应 [E1-E6 清单](../../docs/refactor/ENGINEERING_READINESS_TODO.md)。

## 源码与范围

开始时为 clean `db64d328cb289f4b099c9fc523437fba524809cf`，`origin/master` 为 `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`；继续已有工作分支和 [Draft PR #31](https://github.com/1040942669/legal-rag-agent/pull/31)。开发期 dirty 结果不能称为 clean HEAD 验收，最终提交及其 CI 需独立记录。

本轮选择保持 `legacy-v1` 默认，不继续分词器搜索；现代 BM25S profile 仍显式 opt-in，SmartCN/IK 仍仅实验。四轮同一重复开发集的数字保留，不重新跑质量试验或更改 gold。无新模型请求、共享索引缓存、依赖、migration、版本号、Tag、Release 或 M7 UI。

## 问题、实现与边界

1. factory 原先在 exact router 前立即构建 BM25；明确法条 found/miss 都承担不使用的词汇索引成本。采用 run 内延迟初始化，仅 lexical 实际进入时构建，保留数据库冻结/授权加载和完整 provenance。跨 run 不共享，避免缓存生命周期和撤权问题。只证明被测 exact 分支不构建，不声称完整 API P95 提速百分比。
2. M2 typed observation 的模型角色名单漏了 semantic，而 runner 的 ledger 已有该角色。观测桥接复用 runner 的 closed role 映射并保留既有 rerank 合同；不遍历任意外部输入 keys。不改真实预算、业务结果、结算或重试语义，不将缓存源账本计为本次费用。
3. `PgVectorExactRetriever.known_law_hints` 错误引用仅 lexical wrapper 才拥有的 `_entries`。vector adapter 不加载整个 corpus，因此明确返回空 tuple；不伪造法名知识或新增隐式数据库/provider操作。此前 `getattr(..., default)` 可能吞掉该 AttributeError，不能夸称修复了已观测的 API 崩溃。
4. 固定工程演示只编排已有和新增的合成合同测试，输出真实结果与 source identity；不是新 Agent、运行时 UI 或独立法律 benchmark。

## 开发验证与失败记录

| 轮次 | 实际结果 | 本地证据 / 局限 |
| --- | --- | --- |
| vector 测试初稿 | 1 failed，测试缺少 required model 参数 | fixture 编写错误，不当产品 RED |
| vector 属性真实 RED | 1 failed，AttributeError `_entries` | `.tmp/interview-ready-20261008/red-vector-hints.xml`，直接属性读取；无模型 |
| vector 与 boundary 定向 GREEN | 31 passed / 0.35s，exit 0 | `.tmp/interview-ready-20261008/green-vector-hints.xml`，与以后全量重叠不相加 |
| semantic 真实 RED | 3 failed / 3 passed / 15 deselected，exit 1 | `.tmp/interview-semantic-observation-red.xml`，semantic-only、双角色和 mixed unknown 输入 |
| semantic 完整 owned GREEN | 26 passed / 21.36s，exit 0 | `.tmp/interview-semantic-observation-green.xml`，包括实际 M2 fixture attempt 与缓存 |
| 观测相邻定向 GREEN | 15 passed / 0.29s，exit 0 | `.tmp/interview-semantic-observation-adjacent-green.xml`，不是法律效果评测 |
| lazy 首轮 RED | 22 failed / 4 passed，包含三类 fixture 错误 | 原日志 `.tmp/interview-ready-20261008/red-lazy-lexical.txt` 保留，不将全部失败称为产品反例 |
| lazy 修正 fixture 后旧工厂 RED | 21 failed / 5 passed，exit 1 | `.tmp/interview-ready-20261008/red-lazy-lexical-corrected.txt` / XML，纯内存载入旧 HEAD 工厂，未改工作文件 |
| lazy 五文件完整局部 GREEN | 105 passed / 9.16s，exit 0，1 条上游警告 | `.tmp/interview-ready-20261008/green-lazy-lexical-final.txt` / XML；含新 26 项、七 profile 完整 outcome/ranking/trace/provenance 相等及跨 run 隔离 |
| 演示入口补强后单元检查 | 22 passed / 0.32s，exit 0 | `.tmp/interview-engineering-showcase-unit-reviewed-final.xml`；旧16/19项轮次不累加 |

两个独立 reviewer 已只读接受 lazy 和补强后的固定演示入口。源身份清单覆盖 runtime、指定新测试与已跟踪公开 tests/configs/eval_cases 输入，不扫描 ignored 私料；不声称完整依赖环境被冻结。实际六组入口、隔离 PG、冻结 M2 和最终 CI 尚待运行回填；本表不借历史 2255/2412 数字覆盖本轮最终源码。

## 未运行项和保证边界

本轮真实模型/付费请求、远端 Langfuse 实发、当前供应商实际账单、人工法律评审、独立 holdout、生产容量及新的 Linux Redis/Celery 实测未运行。此前 worker/故障 CI 仍属于各自精确提交。未检出测试错误不表示无过拟合；相同开发集持续比较仍有选择偏差。

个人贡献、招聘岗位和真人口述练习不能从仓库推断。个人材料不上传，源码审计或参考答题要点不是候选人通过压力面，也不形成软件 Release。

## 回退与交接

无数据迁移。需要回退时正常 review revert 本轮代码，不删除 PostgreSQL、Redis 或 artifact，保留原调用账本和历史实验。保留默认配置，不退回已知错误的多法条所有权/语义发布合同。最终真实 commit/push/CI 与未完成项记录于 STATE/HANDOFF，不移动 M5/M6 Tag。
