# 有限工程可演示性改善清单

日期：2026-10-07。用户要求在次日形成可解释、可演示的项目版本。本清单仅约束工程实现及验证；个人经历、简历和口述练习材料留本地，不属于公开仓库交付。它不是 M7 启动、发布授权或新质量实验协议。

## 范围与决定

从已有 `codex/chinese-bm25-optimization` / Draft PR #31 的 clean `db64d328cb289f4b099c9fc523437fba524809cf` 继续，不重置前置改动。实时核验的 `origin/master` 为 `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`。保留此前四轮比较、失败证据、付费历史和已发布 Tag。

用户委托优化后，采用保守交付决定：**保持 `legacy-v1` 默认，成熟候选显式 opt-in，停止新增分析器试验。** char 的召回收益与 MRR 回退均保留；这不是修改历史自动推广门槛或声称现代内核劣于所有中文方案。未有独立法律审核、未曝光 holdout 或当前整链路模型质量验证。

不新增跨 run 索引缓存、生产 ES、额外框架、模型调用、法律词典、金标准或 UI。不承诺生产吞吐、API 延迟改善或用户业绩。无 migration、依赖、版本号或 Release 变更。

## 子任务与验收边界

| ID | 子任务 | 必须能够失败的验收 | 状态 |
| --- | --- | --- | --- |
| E1 | 精确路由仅在真正调用 lexical 时构建 BM25 | exact found/miss/clarification 的 builder 调用为 0；同 run lexical 构建一次；冻结 corpus/settings；排名、provenance 不变；跨 run 不共享；初始化失败不缓存半成品 | 完成，真实修正fixture RED保留；105项定向及冻结M2通过 |
| E2 | M2 观测纳入真实 semantic 调用角色 | semantic-only 与 generation+semantic 正确计数；cache/replay 的 source calls 不计当前；embedding/other/未知角色不冒充 model；未知 token 仍 null；observer 失败不改业务 | 完成，3 failed /3 passed RED及26项GREEN、冻结M2通过 |
| E3 | 修复 vector adapter 的可选法名提示属性 | 直接访问不读取不存在的 `_entries`；不加载语料或触发 encoder/provider；prepare_question 保持 frozen boundary | 已有真实 AttributeError RED，31 项定向 GREEN；不是在线接口故障率改善 |
| E4 | 固定零模型工程演示入口 | 明确离线合同模式；仅固定 selector；新输出目录不覆盖证据；环境不传 key；JUnit 缺失/空/失败/skip/timeout 非 passed；源身份前后核验 | 完成，22入口单元及实际6组98项通过；不是全量门禁替代品 |
| E5 | 重新验证适用链路与审查 | 定向与固定演示；隔离 PostgreSQL 精确/lexical/观测/恢复合同；最终 frozen source 累计 M2；精确 GitHub head CI 另读；未跑真实 worker 不伪称已跑 | 3c3b588本地PG46+实际restart、M2 25/25及独立审查完成，远端CI待结束 |
| E6 | 更新公开工程验收、状态及交接 | 测试轮次不累加；失败和未运行项保留；commit/push/PR 与 merge/tag/release 区分；私密材料不 stage | 结果回填完成，文档静态/推送/精确head CI状态独立核对 |

本清单记录本轮已选择范围，不声称所有行都在观察 RED 前预注册。已发生的开发证据保持各自身份，正式结果在 [工程验收](../../reports/refactor/ENGINEERING_READINESS_20261008.md) 中记录。

## 实际链路与演示权威

- 在线：FastAPI 身份和幂等 run → PostgreSQL 冻结边界/策略 → supervisor 唤醒 → 有界 LangGraph → exact/lexical → 证据与可选受控 generation/checker → 原子结果提交 → SSE 持久 sequence 续读。不是多 Agent，也不是在线聊天节点通过 Celery。
- 批任务：注册 artifact/experiment → PostgreSQL job+outbox → Redis/Celery 投递 → fenced worker/item → 业务结果及进度。Redis 和 Celery result backend 不承担业务真相。
- PostgreSQL、checkpoint、external-call journal 和观测分别负责业务权限/提交、恢复位置、外部结果窗口和最佳努力执行事实，不能互相替代。
- 测试驱动的真实进程/数据库演示与合成 provider 合同分开；`supported` fake、fallback publishable 和检索 gold hit 都不是法律真值。

复现入口、输出身份与失败窗口见验收报告。恢复演示复用已有 `scripts/m5_recovery_demo.py` / 隔离 PostgreSQL 脚本，不复制一套恢复实现。
