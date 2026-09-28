# M5 执行交接

## 当前状态

- 当前里程碑：M5，计划版本 `v0.6.0`。
- 当前状态：`ready_for_review`。实现与本地专项验收已完成，但尚未发布；精确 final PR head、merge target、Tag、GitHub Release 和独立发布回执必须在真实发生后补录。
- 工作分支：`codex/m5-harness-recovery`。
- 软件 PR：[PR #22](https://github.com/1040942669/legal-rag-agent/pull/22)，当前仍为 Draft。
- 跟踪项：[Issue #21](https://github.com/1040942669/legal-rag-agent/issues/21)；[Milestone 6](https://github.com/1040942669/legal-rag-agent/milestone/6)。两者保持 open，直到软件发布、回执 PR 和 receipt merge 后 master CI 全部完成。
- 最新已推送 pre-documentation head：`979c12fab74ec0911bfa93fd9de07f7437c1eb5d`。它不是最终 release target。
- 已验证的上一版本仍是 M4 / [`v0.5.0`](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.5.0)。远端尚无 `v0.6.0` Tag 或 Release。
- 本里程碑没有真实、付费模型调用，没有生产部署，没有强推或保护规则绕过。
- M6 未开始；M5 收口后必须停止。

## 已推送提交

1. `4a7dc54e15fd0f7a48d0acea78dd91945890e8db`：`feat(m5): add durable harness foundations`
2. `d6f50c8e719b257bd5a3fea36e867268b3cb8243`：`feat(m5): execute bounded recoverable graph`
3. `ae65907e5e34abaa2461c28d420e7ee23e2630f3`：`fix(m5): preserve legacy executor rollback path`
4. `27c0179b176f8474365db1c805787324482343e5`：`test(m5): prove cross-process fault recovery`
5. `a0dd830c879abd6b17288abb1034857c4c5b3696`：`fix(m5): close recovery and follow-up gaps`
6. `7ba6ae831ba6f3cf0fa5739f87033fffe59bc763`：`ci(m5): enforce release fault receipts`
7. `aaa56131743da94d25b38beb670a4b6f8c4d7f53`：`test(m5): preserve legacy restart fixture readiness`
8. `979c12fab74ec0911bfa93fd9de07f7437c1eb5d`：`style(m5): format restart fixture`

候选文档提交尚未形成，因此上面最后一个 SHA 只能作为 pre-documentation head。

## M5 实际完成范围

### 1. 单一受控图执行链

`legal_rag/harness/graph.py` 将现有能力编排为有界节点：

```text
analyze -> route -> retrieve -> merge -> check_evidence
                                      | enough
                                      v
                                    generate -> verify -> persist_result
                                      ^
                                      | bounded follow-up
                                  plan_followup
```

图只负责控制和持久恢复，不重复实现检索、证据、生成和验证业务。`GraphRunExecutor` 从 M4 冻结的 user、session、scope、snapshot、profile、config 和 graph identity 构造执行状态。状态是严格 JSON envelope，不存放数据库连接、模型客户端、函数、凭证或未验证 draft。

### 2. PostgreSQL 双层持久状态

- LangGraph 使用锁定的 PostgreSQL checkpointer 保存框架 checkpoint。
- Alembic `0006_m5_harness_recovery` 增加应用拥有的 budget ledger、external attempt journal、verified artifact、checkpoint projection 和恢复字段。
- 应用投影用于鉴权、预算、审计、对账和安全发布，不把框架私有 checkpoint schema 当业务 API。
- `InMemorySaver` 明确不能通过持久恢复验收；M5 production readiness 在 checkpointer 未 setup 时 fail-closed。
- graph/schema version 同时进入 checkpoint namespace 和应用记录。旧 checkpoint 不兼容时明确失败，不静默丢历史。

### 3. 绝对 deadline 与持久预算

- `execution_deadline_at` 在 run 创建时写入，resume 不重置。
- retrieval round、query、tool、model、embedding 和单操作 retry 都有服务端上限。
- 预算 reservation 与 external attempt journal 在外部动作前持久化；进程退出不会把额度恢复成初值。
- `429`、暂时网络错误等只按策略有限重试；`400`、`401`、schema/权限错误不盲重试。
- 到达预算或 deadline 后进入 `completed_with_limits`，并保存机器可读 `stop_reason`。

### 4. side-effect journal 与未知结果

Checkpoint 不能证明外部 provider exactly-once。M5 因此记录 `reserved -> dispatched -> succeeded / failed / outcome_unknown`：

- 在 provider 调用中被 kill，恢复时将已 dispatched 且无可信结果的 attempt 标为 `outcome_unknown`。
- provider 或 planner 已返回但 artifact/checkpoint 尚未持久化时，恢复会根据同一 lease epoch 和最新 trusted checkpoint 的数据库时间保守对账。
- 只有 attempt 完成时间严格早于可信 checkpoint，才认为结果已被后续 checkpoint 覆盖。时间相等、没有 checkpoint 或结果无法证明持久化时都按 unknown 处理。
- unknown 不退款，记录 `possible_duplicate_cost=true`，默认恢复不再次调用 provider/planner，终态为 `completed_with_limits / external_outcome_unknown`。
- 这降低重复费用风险，但不声称撤回已经发送的外部请求，也不声称 provider exactly-once。

### 5. lease heartbeat 与 epoch fencing

- 每次有效 claim/resume 递增 `lease_epoch`。
- heartbeat、budget、attempt、checkpoint、artifact、result 和 terminal transition 都校验 worker identity 与 epoch。
- 两个进程同时 resume 时至多一个取得执行权。
- takeover 后旧 owner 即使仍存活，也不能写 checkpoint 或 terminal result。
- PostgreSQL 数据库时间是 lease 判断依据，避免等待 row lock 时使用过期事务时间。

### 6. 显式 resume 与 follow-up 新 run

- `POST /api/v1/runs/{id}/resume` 先做 owner isolation，再检查状态、版本、deadline、lease 和未知外部结果。
- M5 默认不无限自动恢复。stale work 先被归类为 interrupted，由 owner 显式 resume 或受控恢复命令继续。
- `needs_clarification` 是 answer-bearing 终态。用户补充信息时在同一 session 创建新 run，通过 `parent_run_id` 连接旧 run。
- parent 必须属于同一用户、同一 session，且为允许的终态。子 run 有自己的 deadline、预算、事件和结果，父 run 预算保持不变。
- parent 进入幂等 hash；同 key 换 parent 稳定冲突。无 parent 的 M4 request hash 公式保持兼容。

### 7. 工具与发布安全边界

- 首批工具是服务端注册的只读 allowlist；参数经过 schema 校验。
- user、scope、snapshot、profile 和权限边界来自服务器冻结状态，证据文本或模型输出不能覆盖。
- prompt injection 测试真实经过 `GraphRunExecutor`，验证未知工具、未授权参数和越权 scope 被拒绝。
- 未验证 draft 不进入 HTTP、SSE、messages、result 或安全事件。
- error、receipt 和日志只保存脱敏分类、计数、hash 和 PID 等工程事实，不保存凭证、问题正文、证据正文或数据库 URL。

## M5-T01 至 M5-T10

| ID | 已验证机制 |
|---|---|
| M5-T01 | 检索与 checkpoint 已持久化、生成前 hard kill；新进程从保存节点恢复且不重复整段检索 |
| M5-T02 | provider 内、generator 返回后 artifact 前、planner 返回后 checkpoint 前三类 hard kill；全部保守对账 unknown、不退款、不重复调用 |
| M5-T03 | 安全最终结果已落库、最终节点结束前 hard kill；恢复对账已有 result，不插入第二个回答 |
| M5-T04 | 补检索循环受 round/tool/model budget 限制；澄清补充创建 parent-linked 新 run，父预算不变 |
| M5-T05 | `429` 和暂时错误有限 retry；timeout 进入 unknown；`400`/`401` 不 retry |
| M5-T06 | 两个独立进程竞争 resume 只有一个赢家；旧 owner 的 checkpoint 与 terminal 写入均被 epoch fence 拒绝 |
| M5-T07 | graph/schema version 不兼容明确失败，不静默续跑或丢历史 |
| M5-T08 | 含 prompt injection 的不可信证据无法越过工具白名单、冻结 scope 或泄露 sentinel |
| M5-T09 | deadline 过期后 resume 不延长时间，直接得到受限终态 |
| M5-T10 | 只配置 in-memory checkpointer 时验收失败；必须使用真实 PostgreSQL 持久 checkpointer 和不同 PID |

## 当前本地证据

- 全量 provider-free 测试：`921 passed, 157 subtests passed`。
- M5 工作流精确套件：`81 passed`，无 failure、error、skip 或 xfail。
- M5 fault receipt：10 个场景齐全，closed-schema/exact-SHA validator 为 0 错误。
- 强化后的 T02：三个不同 crash window 全部真实跨进程通过。
- fresh PostgreSQL 环境暴露的 M4 legacy restart readiness 兼容问题已修复；精确失败测试复跑 `1 passed in 10.82s`。
- 质量门禁单元测试：`91 passed`；相关 focused 回归曾达到 `208 passed` 和独立审计 `216 passed`。
- Ruff、Black、YAML、lock、diff 和 selector AST preflight 已通过当前对应文件检查。
- 冻结实现树的 M5/M4 wheel smoke 已通过；M5 wheel 包含 9 个 runtime 文件、9 个 optional service dependencies，并在 checkout 外隔离导入 8 个模块。
- 真实或付费模型调用：`0`。

以上是 pre-documentation/修复阶段的本地证据，不替代最终候选 exact-HEAD gate、GitHub Actions 和 release-target master CI。最终权威数量与 artifact digest 必须从对应远端 run 下载核对后再写入回执。

## 累计门禁与回执链

M5 gate 累计 M0-M5 共 51 个唯一 mandatory ID。M5 的 10 个 ID 映射到 17 个精确 pytest selector；运行前检查文件存在、UTF-8、Python AST 和顶层测试节点唯一性。门禁要求：

1. 真实 PostgreSQL integration guard 与测试 DSN。
2. `ALLOW_LIVE_MODEL_CALLS=false`。
3. exact HEAD 的 M5 fault receipt。
4. receipt closed schema、重复 JSON key 拒绝、大小和嵌套限制、敏感字段和值扫描。
5. PostgreSQL 18 + pgvector、migration head `0006_m5_harness_recovery`、持久 checkpointer、不同进程 PID 和竞争 PID 证据。
6. JUnit 非零测试，且 failure/error/skip/xfail 全为 0。
7. 一次命令化恢复 demo、数据库 service restart、wheel build 与仓库外 isolated smoke。

回执生成采用跨进程锁和临时文件加 `os.replace` 原子发布。场景未齐时保持 `in_progress`；十个场景全部满足并通过自校验后才写 `passed`。

## 当前发布阻塞与下一步

当前没有实现层已知 P0/P1，但发布链尚未完成，因此 M5 不能标为 `released`。必须按顺序：

1. 完成 README、CHANGELOG、ADR、验收报告、STATE 和本 HANDOFF 的候选文档。
2. 在最终 docs commit 的精确 SHA 上重跑 full suite、81 项 M5 suite、one-command demo、PostgreSQL restart probe、M5 wheel probe 和 51/51 gate。
3. push 最终候选，更新 PR #22 body，转为 ready for review。
4. 等待精确 final PR head 的全部必需 CI 成功。仓库当前没有 branch protection，不能因此提前合并，也不得使用 `--admin`。
5. 普通 merge PR #22，等待精确 master merge target CI 成功。
6. 只在上述 CI 成功后创建 annotated `v0.6.0`，核对 peeled target，再创建非 draft、非 prerelease GitHub Release。
7. 从已验证 master 新建 `codex/m5-release-receipt`，写 `docs/refactor/receipts/M5.json` 及真实远端事实，创建独立 receipt PR。
8. 等 receipt exact final-head CI 和 receipt merge 后 master CI 成功，再关闭 Issue #21 和 Milestone 6。
9. 如需 documentation-only 非递归收尾，只记录已经完成的外部事实，不移动 `v0.6.0`，不为收尾 PR 再生成无限回执链。
10. M5 完成后停止，不启动 M6。

## 已知限制与后续提升点

- provider exactly-once 不可由 checkpoint 保证；unknown 只能保守停止、计费不退款并依赖 provider idempotency key 或人工对账。
- 当前 supervisor 仍是受控本地调度，不是 Redis/Celery 多 worker 系统；M6 未实现。
- 静态 Bearer registry、loopback 演示和 provider-free fake 不是生产 IdP、TLS、rate limit 或容量验证。
- 真实法律语料质量、在线模型效果、成本、延迟和人工法律正确性没有在 M5 验证，不能用工程故障测试外推。
- `0006` downgrade 会删除 M5 checkpoint projection、budget、attempt 和 artifact 数据。对有价值的运行历史应先备份并优先采用前向修复。
- 运行中的旧 graph 必须由兼容 runner 继续、明确迁移或受控结束，不能用新 graph 强行解释旧 checkpoint。
- 后续优先改进包括：provider 端 idempotency/对账适配、checkpoint retention 与归档、租约/预算运维指标、恢复演练自动化、真实语料质量评测，以及在明确授权后评估 M6 队列与观测层。

## 停止条件

只有 PR、精确 master CI、annotated Tag、GitHub Release、独立 release receipt、receipt merge 后 master CI、Issue 和 Milestone都已真实核验，才可把 M5 标成 `released`。任一步失败都要记录真实阻塞并保留已验证提交，不得移动 Tag、编造回执或把未发布状态写成完成。M5 完成后停止。
