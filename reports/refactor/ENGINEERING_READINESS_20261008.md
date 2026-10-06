# 有限工程可演示性改善验收

日期：2026-10-07。状态：local_verified，远端精确源码 CI 待结束。这是软件工程改善报告，不是个人经历或法律质量验收。对应 [E1-E6 清单](../../docs/refactor/ENGINEERING_READINESS_TODO.md)。

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

两个独立 reviewer 已只读接受 lazy 和补强后的固定演示入口。源身份清单覆盖 runtime、指定新测试与已跟踪公开 tests/configs/eval_cases 输入，不扫描 ignored 私料；不声称完整依赖环境被冻结。上述表格是开发期，实际六组入口、隔离PG与冻结M2已在下节独立回填，远端CI另核验；不借历史2255/2412数字覆盖本轮最终源码。

## 冻结源码的独立运行

源码提交 `3c3b58822e4446a7d6f2412f3bc10eea86ea1b50` 已 commit/push；远端同名分支读回相同 SHA。下列运行开始和结束均为该 clean HEAD，期间无 tracked 写入。其后的结果文档提交不伪称自己已执行该全量门禁。

| 真实执行 | 结果 | 身份与边界 |
| --- | --- | --- |
| 固定工程入口 | 6/6 组通过，JUnit合计98/0/0/0，exit0 | `uv run --offline python scripts/engineering_showcase.py --output-dir .tmp/interview-ready-20261008/showcase-3c3b588`；lazy26、route22、semantic16、trace26、harness7、vector1。约35秒，UTC18:07:39至18:08:14；记录文件身份稳定，clean_commit，不是API或性能benchmark |
| 隔离真实 PostgreSQL | 46 passed /61.22s，JUnit46/0/0/0，exit0 | `scripts/isolated_pg_tests.ps1 -ReceiptName readiness-3c3b588-pg -RestartProbe`，选择 M4实际接线、general service、M6graph观测、catalog标题、M3exact、M5 T01/T02/T03及并发恢复。完整selector在下节；合成语料、fake provider，含实际进程kill与fencing |
| 数据库物理重启 | 实际stop/start后新进程核验通过 | 自建cluster `.tmp/isolated-pg-71967d7d70fe4a7786b93897af2c229c/`，`restart-prepared.json` 与 prepare/stop/start/verify logs 保留。PostgreSQL18、pgvector0.8.1、migration0008；最后自建cluster已停止，共享服务未修改 |
| 累计 M2离线门禁 | 25/25必检passed，exit0，244652ms | `uv run --offline python scripts/quality_gate.py --milestone M2 --mode offline --output .tmp/readiness-3c3b588-m2.json`；UTC2026-10-06T18:07:39Z至18:11:45Z |
| 上一行内全量pytest | 2315 passed +157 subtests /192.12s；JUnit2472/0/0/0，1条jieba上游warning | 不与98、46、105或其它重叠专项相加，不是法律题正确数量 |
| source-head GitHub CI | [37508896508](https://github.com/1040942669/legal-rag-agent/actions/runs/37508896508)，观察时四路in_progress | 精确head为3c3b588；旧db64四路成功与dde6成功不替代本轮。本地未新跑Linux真实broker，待该head自己的远端worker证据 |

本地证据 SHA256：M2报告 `46c6b957ea496d31e086c26f3f1010971beb05d98401b31a49c3c017503af5cc`；PG JUnit `9ef0144183b6500ddebe8f81c8bda08519efc0fee963483b872b845b36d10b75`；showcase manifest `2eaaff44c83a1b0e1532e4ed8ede24aecd1340a9c5ecc8aaa0908b1043466ea9`。hash不是法律正确性、完整环境或供应链安全认证。

### 复现与证据钩子

```powershell
uv run --offline python scripts/engineering_showcase.py --list
uv run --offline python scripts/engineering_showcase.py
$env:ALLOW_LIVE_MODEL_CALLS = 'false'
$env:LEGAL_RAG_DISABLE_DOTENV = '1'
./scripts/isolated_pg_tests.ps1 -ReceiptName readiness-new-run -RestartProbe -TestPaths @(
  'integration_tests/test_m4_service_wiring.py',
  'integration_tests/test_general_service_execution.py',
  'integration_tests/test_m6_harness_observation_db.py',
  'integration_tests/test_catalog_title_availability_db.py',
  'integration_tests/test_m3_exact_retrieval.py',
  'integration_tests/test_m5_fault_recovery.py::test_m5_t01_kill_after_retrieval_checkpoint_resumes_without_retrieval',
  'integration_tests/test_m5_fault_recovery.py::test_m5_t02_kill_after_model_dispatch_records_unknown_outcome_and_keeps_reserved_budget',
  'integration_tests/test_m5_fault_recovery.py::test_m5_t03_kill_after_result_commit_reconciles_without_duplicate_answer',
  'integration_tests/test_m5_concurrent_resume.py'
)
```

上述PG入口需要已安装本地 PostgreSQL/pgvector和项目测试依赖。`ReceiptName`每次采用新名称，勿覆盖旧证据。入口会启动并停止自己的随机loopback cluster，不复用共享5432；实际测试用随机隔离库。没有PostgreSQL时只跑离线入口并记录PG not_run，不补造输出。恢复单场景复用 `scripts/m5_recovery_demo.py`，须提供明确disposable数据库，不能当作完整M5/M6门禁。

运行时观测钩子来自真实 `HarnessObservationAdapter` / M2 bridge：`trace_id/run_id/session_id/job_id/experiment_id`、node/tool、duration、retry/cache、evidence IDs、权威预算和已知usage coverage。PG测试 `test_m6_real_graph_records_nodes_evidence_cache_and_authoritative_budget` 写实际LocalJsonlObserver；它用合成资料，测试的本地trace不等于生产Langfuse已导出。观察schema不含query/prompt/答案全文；未知tokens/cost保持null，sink失败不决定业务结果。无新增虚构业务埋点。

## 未运行项和保证边界

本轮真实模型/付费请求、远端 Langfuse 实发、当前供应商实际账单、人工法律评审、独立 holdout、生产容量及新的 Linux Redis/Celery 实测未运行。此前 worker/故障 CI 仍属于各自精确提交。未检出测试错误不表示无过拟合；相同开发集持续比较仍有选择偏差。

个人贡献、招聘岗位和真人口述练习不能从仓库推断。个人材料不上传，源码审计或参考答题要点不是候选人通过压力面，也不形成软件 Release。

## 回退与交接

无数据迁移。需要回退时正常 review revert 本轮代码，不删除 PostgreSQL、Redis 或 artifact，保留原调用账本和历史实验。保留默认配置，不退回已知错误的多法条所有权/语义发布合同。最终真实 commit/push/CI 与未完成项记录于 STATE/HANDOFF，不移动 M5/M6 Tag。
