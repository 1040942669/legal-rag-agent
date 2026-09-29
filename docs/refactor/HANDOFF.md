# M5 执行交接

## 当前状态

- 当前里程碑：M5，版本 `v0.6.0`。
- 当前状态：`released`。软件发布、独立发布回执、回执 merge-target master CI、Issue 与 Milestone 治理关闭均已完成并远端核验。
- 软件分支：`codex/m5-harness-recovery`。
- 软件 PR：[PR #22](https://github.com/1040942669/legal-rag-agent/pull/22)，final head `aa737e8d1f77214277c0544ce069d36c2b2161ff`，普通 merge commit `832acaafaf5633e76daed7a62a73755187fca51e`。
- 精确 PR-head CI：[run 36497021956](https://github.com/1040942669/legal-rag-agent/actions/runs/36497021956)，3/3 jobs success。
- 精确 release-target master CI：[run 36498443123](https://github.com/1040942669/legal-rag-agent/actions/runs/36498443123)，3/3 jobs success。
- annotated Tag：`v0.6.0`，Tag object `c0ef0721ab49da0d7840b76e741a52e35b8941d2`，peeled target 精确为 `832acaafaf5633e76daed7a62a73755187fca51e`。
- GitHub Release：[v0.6.0 - Durable Harness Recovery](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.6.0)，于 `2026-09-28T23:41:41Z` 发布，非 draft、非 prerelease，附件 0。
- 当前 finalization 分支：`codex/m5-release-finalize`，基线为回执普通 merge commit `3436e9ad41c7455aa5f31117ab7ece3f4ea847c1`。该分支只记录已经发生的治理事实，不新增产品能力，也不生成递归回执。
- 独立回执 PR：[PR #23](https://github.com/1040942669/legal-rag-agent/pull/23)。first candidate `3eead1deb511325c91d47b31f9c39f2868607b07` 的 [CI run 36500603258](https://github.com/1040942669/legal-rag-agent/actions/runs/36500603258) 与 final head `9dd6ec3867f05dd207ec15657861028f138167fc` 的 [CI run 36501403167](https://github.com/1040942669/legal-rag-agent/actions/runs/36501403167) 均为 3/3 jobs success。PR 于 `2026-09-29T00:13:59Z` 普通合并为 `3436e9ad41c7455aa5f31117ab7ece3f4ea847c1`，该精确 commit 的 [master run 36502063863](https://github.com/1040942669/legal-rag-agent/actions/runs/36502063863) 亦为 3/3 success。
- 跟踪项：[Issue #21](https://github.com/1040942669/legal-rag-agent/issues/21) 于 `2026-09-29T00:23:07Z` 关闭；确认 `open_issues=0` 后，跟踪 M5 的 GitHub [Milestone 6](https://github.com/1040942669/legal-rag-agent/milestone/6) 于 `2026-09-29T00:23:22Z` 关闭。这里的 GitHub Milestone 编号不表示路线图 M6 已开始。
- 本里程碑没有真实或付费模型调用，没有生产部署，没有强推、admin merge、auto-merge、review 绕过或保护规则绕过。
- M6 未开始；M5 完成后必须停止。

## 软件提交与发布对象

1. `4a7dc54e15fd0f7a48d0acea78dd91945890e8db`：`feat(m5): add durable harness foundations`
2. `d6f50c8e719b257bd5a3fea36e867268b3cb8243`：`feat(m5): execute bounded recoverable graph`
3. `ae65907e5e34abaa2461c28d420e7ee23e2630f3`：`fix(m5): preserve legacy executor rollback path`
4. `27c0179b176f8474365db1c805787324482343e5`：`test(m5): prove cross-process fault recovery`
5. `a0dd830c879abd6b17288abb1034857c4c5b3696`：`fix(m5): close recovery and follow-up gaps`
6. `7ba6ae831ba6f3cf0fa5739f87033fffe59bc763`：`ci(m5): enforce release fault receipts`
7. `aaa56131743da94d25b38beb670a4b6f8c4d7f53`：`test(m5): preserve legacy restart fixture readiness`
8. `979c12fab74ec0911bfa93fd9de07f7437c1eb5d`：`style(m5): format restart fixture`
9. `aa737e8d1f77214277c0544ce069d36c2b2161ff`：`docs(m5): prepare durable recovery candidate`
10. `3eead1deb511325c91d47b31f9c39f2868607b07`：M5 独立回执 first candidate
11. `9dd6ec3867f05dd207ec15657861028f138167fc`：`docs(m5): verify receipt candidate`

`aa737e8...` 是软件 PR 的最终 head，`832acaaf...` 是通过 master CI 的软件发布目标，`9dd6ec3...` 是回执 PR 的最终 head，`3436e9a...` 是通过 master CI 的回执合并目标。Tag 永久留在软件发布目标，不跟随后续 receipt 或 finalization 文档提交移动。

## 完整运行链路

### 1. HTTP、持久服务与有界图

M4 的 Bearer owner isolation、session、message、run、idempotency、SSE 与 immutable result 继续作为外层服务边界。M5 的 `GraphRunExecutor` 只在冻结的 graph version 为 `m5-bounded-v1` 时接管执行：

```text
POST /api/v1/sessions/{session_id}/runs
  -> authenticate owner
  -> freeze scope / snapshot / embedding profile / graph identity / config
  -> create idempotent durable run and absolute deadline
  -> supervisor claims lease epoch
  -> GraphRunExecutor loads PostgreSQL checkpoint and application projection
  -> analyze_query
  -> route
  -> retrieve
  -> merge_evidence
  -> check_evidence
       -> enough: generate -> verify -> persist_result
       -> insufficient and budget remains: plan_followup -> retrieve
       -> exhausted/deadline/unknown: persist limited terminal result
  -> atomically publish result, assistant message and final event
  -> SSE clients replay the durable event suffix
```

旧 graph version 继续使用稳定的 `LegalChatRunExecutor`，没有被 M5 强制迁移。图是唯一自适应控制循环，旧 adaptive/planner loop 不与它叠加。

### 2. PostgreSQL 双层持久状态

- LangGraph `PostgresSaver` 保存框架 channel/checkpoint。
- Alembic `0006_m5_harness_recovery` 增加应用拥有的 `run_checkpoints`、`run_budget_ledgers`、`run_external_attempts`、verified artifact 和恢复字段。
- 框架 checkpoint 决定图从哪里继续；应用投影决定 owner、graph/schema/hash 兼容性、可信 checkpoint pointer、预算、审计和是否允许发布。
- 只信任当前 run row 指向的、同一 lease epoch 的应用 checkpoint。旧 namespace 中的孤儿 framework checkpoint 不能推进业务状态。
- `InMemorySaver` 无法通过 readiness 或 M5-T10；普通启动不会静默建表或退回内存 saver，`legal-rag-api --migrate` 才显式运行 Alembic 与 saver setup。

### 3. deadline、预算、retry 与 side-effect journal

- run 创建时写入绝对 `execution_deadline_at`，resume 不重置。
- 默认上限为 2 个检索轮次、每轮 3 个 query、8 次工具、4 次模型、4 次 embedding、单操作 1 次重试、90 秒 deadline 和 `top_k=5`。
- 外部动作前先在 durable ledger 中预留额度，再记录 `reserved -> dispatched -> succeeded / failed / outcome_unknown / abandoned_before_dispatch`。
- `429` 和暂时网络错误只在预算内有限重试；`400`、`401`、schema 或权限错误不盲重试。
- 达到预算或 deadline 后保存 `completed_with_limits` 与机器可读 `stop_reason`，不会为了得到答案无限循环。

### 4. 三类崩溃窗口

1. 检索 checkpoint 已持久化、生成尚未开始：新进程从保存节点继续，不重复整段检索。
2. provider 或 planner 已 dispatch，甚至已返回，但结果尚未进入可信 artifact/checkpoint：保守记为 `outcome_unknown`，已预留预算不退款，默认不重发。
3. immutable result、assistant message 和 final event 已在同一事务落库，但图末 checkpoint 尚未完成：恢复时对账已有唯一结果，不生成第二个答案。

Checkpoint 无法撤回外部请求，也无法证明 provider exactly-once。M5 只做到保守记账、默认避免重复调用、披露 `possible_duplicate_cost=true`，不承诺零重复计费。

### 5. lease epoch 与 fencing

- 每次有效 claim/resume 递增 `lease_epoch`。
- heartbeat、budget、attempt、checkpoint、artifact、result、event 和 terminal transition 都验证 worker identity、epoch、revision 与数据库 wall clock。
- 两个进程同时 resume，只有一个取得执行权。
- takeover 后旧 owner 的 checkpoint 写和 terminal result 写均被拒绝，数据库行数保持不变。

### 6. 显式 resume 与 clarification follow-up

- `POST /api/v1/runs/{id}/resume` 先执行非枚举 owner isolation，再校验状态、graph/schema/hash、deadline、lease 与未知外部结果。
- M5 不无限自动恢复。stale work 先进入 `interrupted`，由 owner 显式 resume 或受控恢复命令继续。
- `needs_clarification` 是 answer-bearing 终态。用户补充信息会在同一 session 创建带 `parent_run_id` 的新 run，而不是复活旧 run。
- child run 有独立 deadline、预算、事件和结果，父 run 的已消耗额度保持不变；parent identity 进入幂等 hash，M4 无 parent 的 request hash 保持兼容。

### 7. 工具与数据安全边界

- 模型可见工具固定为 `search_laws`、`get_article`、`get_neighbors`、`inspect_evidence_metadata`。
- user、scope、snapshot、profile、数据库 DSN 和权限来自服务端冻结状态，不接受证据文本或模型输出覆盖。
- checkpoint state 是关闭字段的严格 JSON envelope，拒绝未知字段、非有限数值、连接对象、客户端、凭证、prompt、raw draft 和未验证答案正文。
- fault receipt、日志和发布回执只保存分类、计数、hash、PID 与公开 GitHub 元数据，不保存问题正文、证据正文、数据库 URL 或私密资料。

## M5-T01 至 M5-T10

| ID | 已验证机制 | 发布证据 |
|---|---|---|
| M5-T01 | hard kill 后跨 PID 从检索 checkpoint 恢复，retrieval invocation 保持 1 | passed |
| M5-T02 | provider 内、provider 返回后 artifact 前、planner 返回后 checkpoint 前三个窗口均保守 unknown，不退款且默认不重复调用 | passed |
| M5-T03 | result 落库后图结束前 kill，恢复只对账已有结果，不产生第二答案 | passed |
| M5-T04 | follow-up loop 受 durable budget 限制；clarification 创建 parent-linked 新 run，父预算不变 | passed |
| M5-T05 | `429`/timeout 有限 retry，`400`/`401` 不 retry，attempt 次数可审计 | passed |
| M5-T06 | 两进程竞争 resume 只有一个有效 owner；旧 owner checkpoint/terminal 写均被 fence | passed |
| M5-T07 | graph/schema/checkpoint 不兼容时显式失败且 pointer 不移动 | passed |
| M5-T08 | prompt injection 不能选择未知工具、覆盖冻结 scope 或泄露 sentinel | passed |
| M5-T09 | resume 保留原 absolute deadline，过期后新增 dispatch 为 0 | passed |
| M5-T10 | `InMemorySaver` 验收失败，必须使用真实 PostgreSQL saver 与不同 PID | passed |

## 验证与制品证据

### 本地精确软件候选 `aa737e8...`

- 全量 provider-free：`921 passed, 157 subtests passed in 76.08s`。
- 精确 M5 suite：`81 passed in 44.34s`，JUnit `81/0/0/0`。
- 10 个 fault scenario 全部 passed，closed-schema validator errors `[]`。
- one-command recovery demo：passed，scenario `M5-T01`，候选 SHA 精确匹配。
- 真实 PostgreSQL 18 service stop/start 后新进程复核：passed；本地 pgvector `0.8.1`，migration `0006_m5_harness_recovery`。
- M0-M5 累计 gate：`51/51`，全量测试仍为 `921 + 157 subtests`。
- 本地 wheel：393143 bytes、87 entries，SHA-256 `52e1910fb3890992f2c4aa7d2a3f39b65bcaf529c219b7a1769e806a61b4c9fa`；9 个 M5 runtime 文件、9 个 optional service dependencies、8 个仓库外隔离模块导入通过。

### 精确 PR head 与 release target

- PR #22 exact-head run `36497021956`：offline、M4 service、M5 fault 三个 jobs 全部 success。
- release-target master run `36498443123`：同样 3/3 success。
- master M5 JUnit：81 tests、0 failures、0 errors、0 skipped，time 75.248s。
- master M5 gate：51/51，duration 326375ms。
- master PostgreSQL：18；pgvector：0.8.6；migration head：`0006_m5_harness_recovery`；真实 service restart passed。
- master M5 wheel：391093 bytes、87 entries、SHA-256 `05e78eaf8430a92244a570f7f97e418b6a413fec899477509be029b6f234afeb`。
- 三个 master artifact digest 与内部文件 SHA-256 已写入 `docs/refactor/receipts/M5.json`。
- 真实或付费模型调用：0。

### 独立回执 final head 与 merge target

- PR #23 final head `9dd6ec3867f05dd207ec15657861028f138167fc` 的 [run 36501403167](https://github.com/1040942669/legal-rag-agent/actions/runs/36501403167)：offline、M4 service、M5 fault 三个 jobs 全部 success。
- final-head artifact digest：offline `sha256:8af81ba947dcf0f2ea7dc98eb4611c6acc97e84aabd10b8830a3b902e368e845`；M4 `sha256:ff9aeadef3be9a8b8f4aca993b4058f26126d171a80c06394983deb93f5e2202`；M5 `sha256:89f164949bd30288b12f93469de2035bcf93033ebd67a1c8414990528ddeff37`。
- PR #23 于 `2026-09-29T00:13:59Z` 以普通 merge commit `3436e9ad41c7455aa5f31117ab7ece3f4ea847c1` 合并；没有 admin、auto、squash、rebase 或强推。
- 精确 receipt merge-target [master run 36502063863](https://github.com/1040942669/legal-rag-agent/actions/runs/36502063863)：3/3 jobs success。artifact digest：offline `sha256:c55adfdad0d6cc47ea8921d9a7676990dbac23b89c823e16b1383435cfe3a129`；M4 `sha256:3d70c2fe5ea2b919f8e818475af45e5e2d3b5daafd55b41f21d316287b3d743d`；M5 `sha256:cdeceb76af01fcb6453a3fc9b6f702ad2670d9bffef3ec5ac6b0074b85e9fcd6`。
- final-head 与 merge-target 的全部内部 gate、JUnit、restart、demo、wheel probe 和 wheel SHA-256 已写入 `docs/refactor/receipts/M5.json`；Issue #21 与 Milestone 6 只在 merge-target CI 成功后关闭。

### 保留的失败证据

- 较早 `7ba6ae8...` 的 PR run `36495569685` 在 M4 service job 得到 `78 passed, 1 failed`。旧 M4 process fixture 没有建立 M5 persistent checkpointer，导致 readiness 未就绪。修复把遗留 fixture 明确固定为 `m4-linear-v1`，没有弱化生产 M5 的 fail-closed readiness；最终 PR 与 master 三路 CI 均通过。
- 第一次本地 restart prepare 使用复用数据库，遇到 snapshot activation conflict；该次没有进入重启。
- 第二次新数据库完成重启但名称不满足 integration fixture guard；第一次累计 gate 因相同 guard 失败关闭。随后使用 guard-compliant fresh database 完整重跑并通过。
- 这些失败保留在报告中，没有被冒充为发布成功证据。

## 已完成的治理链与最终化边界

1. 软件 PR #22 exact-head CI、普通 merge、release-target master CI、annotated Tag 与 GitHub Release 已完成。
2. 独立 receipt PR #23 first candidate 和 final head 的精确三路 CI 已完成。
3. receipt PR 已普通合并，精确 receipt merge-target master 三路 CI 已完成。
4. Issue #21 与 Milestone 6 已按依赖顺序关闭，M5 机器状态现在是 `released`。
5. 当前 `codex/m5-release-finalize` 只补写上述外部事实；该 PR 仍要求 exact-head 三路 CI、普通 merge 和精确 merge-target master CI。
6. finalization 不递归生成新 receipt，不移动 `v0.6.0`，不修改产品代码；完成后停止，不启动 M6。

## 已知限制与优先提升点

- provider exactly-once 仍不可证明。优先引入 provider 原生 idempotency key、请求账单对账和 unknown outcome 运维队列。
- supervisor 仍是受控本地调度，不是分布式 worker 系统。队列、公平调度、背压、指标、trace 和告警属于尚未开始的 M6 范围。
- PostgreSQL saver 与应用 checkpoint projection 不在一个跨表原子事务中。当前依靠可信 pointer、epoch fencing 和失败关闭；后续可评估 outbox/commit protocol。
- wheel 在不同 CI job 中内容等价但字节 SHA 不同，说明构建尚未 byte-for-byte reproducible。后续应固定归档时间戳与构建环境。
- checkpoint retention、归档、租约清理、attempt 对账和长时间运行容量尚无生产级 SLO。
- 静态 Bearer registry、loopback fixture 和 provider-free fake 不是生产 IdP、TLS、rate limit、容量或灾备验证。
- 真实法律语料覆盖、时效性、在线模型质量、引用蕴含、成本、P95/P99 延迟和人工法律评审未在 M5 验证。
- `0006` downgrade 会删除 M5 checkpoint projection、budget、attempt 和 artifact 数据。对有价值历史必须先备份并优先前向修复。
- GitHub Actions 对 pinned `actions/upload-artifact` 给出 Node.js 20 deprecated、强制 Node.js 24 的非阻塞警告，后续应升级 action pin。

## 停止条件

M5 软件、独立 receipt、receipt merge-target CI、Issue 与 Milestone 已真实核验，因此里程碑状态是 `released`。操作性停止条件只剩非递归 finalization PR 的 exact-head CI、普通 merge 和精确 merge-target master CI；任一步失败都要记录真实阻塞，不得移动 Tag、编造递归回执或启动 M6。finalization 完成后停止。
