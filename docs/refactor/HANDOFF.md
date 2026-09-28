# M4 执行交接

## 当前结论

- 当前里程碑：M4，计划版本 `v0.5.0`。
- 当前状态：本地发布候选已通过，远端精确最终 PR-head CI 尚待候选文档提交后复验；不是已发布状态。
- 分支：`codex/m4-api-sessions`。
- 起始基线：`origin/master` `61f1065fc6d5678d0d57114e2e361294d04906b4`。
- 最新已推送的文档前 head：`2c4c2a024f226d4f7f91442a543246ba3962029d`。候选文档提交后 HEAD 会继续前进，最终发布只能采用新的精确 PR head。
- 跟踪：[Issue #16](https://github.com/1040942669/legal-rag-agent/issues/16)、[Milestone 5](https://github.com/1040942669/legal-rag-agent/milestone/5)、draft [PR #17](https://github.com/1040942669/legal-rag-agent/pull/17)。
- `v0.5.0` Tag、GitHub Release 与 `docs/refactor/receipts/M4.json` 当前都不存在，这是正确状态。
- M0-M3 已发布且回执完整，最后已核验版本仍是 [v0.4.0](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.4.0)。M4 不移动任何旧 Tag。

## 已推送实现提交

1. `862b7d0919a1e5c5a1aa684388bf4556da28e72b`：`feat(m4): add durable service state schema`
2. `081acacfc2ddf0d1ba05782a5092b5cc48820e90`：`feat(m4): add transactional API run service`
3. `43e6a506bd62bb0d02395cc3397801815a01bd16`：`test(m4): verify service release boundary`
4. `2c4c2a024f226d4f7f91442a543246ba3962029d`：`ci(m4): verify the installed service environment`

不得 amend 已共享提交，不得强推。候选文档、最终包证据和远端事实使用后续普通提交追加。

## M4 实际完成范围

### 持久业务状态

Alembic `0005_m4_api_sessions` 和 `legal_rag/storage/schema.py` 新增：

- `sessions`
- `messages`
- `runs`
- `run_results`
- `idempotency_keys`
- `run_events`

数据库约束保证消息顺序、每个 run 的 user/assistant 消息唯一、result 唯一、事件 sequence 单调，以及每个 session 只有一个 `queued / running / interrupted` active run。

### HTTP 与身份

`legal-rag-api` 提供：

- `GET /health/live`
- `GET /health/ready`
- `POST /api/v1/sessions`
- `GET /api/v1/sessions/{id}/messages`
- `POST /api/v1/sessions/{id}/runs`
- `GET /api/v1/runs/{id}`
- `GET /api/v1/runs/{id}/evidence`
- `GET /api/v1/runs/{id}/events`
- `POST /api/v1/runs/{id}/cancel`
- `POST /api/v1/runs/{id}/resume`

开发期 opaque Bearer token 由服务端静态 registry 映射为 `user_id / scope_id / profile_id`。客户端不能用 query、body 或 `X-User-ID` 覆盖身份。跨用户访问 session、run、events、evidence、cancel 和 resume 统一返回不泄露对象存在性的 404。URL 中常见 credential 参数会在路由前被拒绝。

### RunService 事务边界

创建 run 使用一个短事务完成：

1. 锁定并校验所属 session。
2. 优先检查 `user_id + session_id + Idempotency-Key` 绑定。
3. 对规范化请求体计算 SHA-256。
4. 冻结 active snapshot、snapshot revision、activation ID、profile、retrieval config 和 graph version。
5. 校验 snapshot/profile import 可服务。
6. 原子写入 run、用户消息、幂等记录和 `run.queued`。

同 key、同 body 返回原 `run_id`，包括原任务已经成功的情况；同 key、不同 body 返回 409。不同 key 也不能越过数据库 active-run 约束。外部检索和执行不持有数据库事务。

### Supervisor、lease 与恢复

单进程 `RunSupervisor` 使用 `FOR UPDATE SKIP LOCKED` 领取 queued run，并写有限 `lease_owner / lease_expires_at`。状态迁移使用 revision 与 event sequence CAS。lease 判定使用 PostgreSQL `clock_timestamp()`，避免事务在 row lock 上等待后仍用事务开始时间错误接受过期 worker。

执行器在 daemon thread 内运行。超时线程进入 bounded quarantine，callback active flag 和数据库 lease/revision 双重 fence 阻止晚到写入。达到 4 个仍在 draining 的线程时 readiness 失败关闭，不继续领取并错误超时更多任务。

启动和周期恢复会分批扫描 stale run，直到最后一批不足 100；只有全部恢复后才 ready。M4 的恢复语义只有：

```text
running + expired lease -> interrupted
```

`interrupted` 仍占 active 槽位。所有者可以取消，`resume` 明确返回 501；M4 不伪造 M5 checkpoint resume。

### 安全事件与最终发布

SSE 事件持久化在 PostgreSQL，以 `(run_id, sequence)` 唯一标识。客户端可用 `Last-Event-ID` 或受约束的 `after` 续读。断线不取消 run，也不创建新 run。

事件 payload 使用关闭字段集合，只包含状态、计数、布尔值、受限 stage/reason 等事实摘要。未验证草稿、raw provider response、prompt、hidden reasoning 或 credential 字段不能进入 event、result、message 或 HTTP 响应。

安全 result、最终 assistant message、run 终态和 `answer.final` 在同一事务中提交。SSE 终态检查会比较当前 cursor 与持久 `event_sequence`；如果终态事件刚提交但尚未被当前连接读到，会继续补读，不会漏掉 `answer.final`。

### 生产装配边界

当前服务入口真实连接：

```text
RunService
  -> RunSupervisor
  -> LegalChatRunExecutor(generate=False)
  -> PostgresAssistantFactory
  -> frozen PostgreSQL corpus
  -> boundary-bound BM25
  -> provider-free safe result
```

M3 的 exact pgvector、精确法条 catalog 和实验 HNSW 仍存在，但没有被冒充为 M4 默认 API 路由。当前默认不调用生成模型。`verification.passed=true` 在 retrieval-only 路径只表示该受控结果允许发布，不表示语义蕴含或法律结论正确。

## 完整在线链路

```text
Authorization: Bearer token
  -> 服务端 principal
  -> 创建 session
  -> 幂等创建 run 并冻结数据边界
  -> 202 + run/status/events URL
  -> supervisor 领取并加 lease
  -> 读取当前 session 中仅来自 succeeded run 的有界历史
  -> 从冻结 snapshot/profile 装配检索器
  -> 检索、证据检查、provider-free 执行
  -> safe verification/result
  -> assistant message + result + succeeded + answer.final 原子提交
  -> GET status/evidence/messages 或 SSE sequence 续读
```

历史上下文只读取同一 user/session、当前 user message 之前、已成功且拥有完整 user/assistant 对及 result 的记录。默认最多 16 条消息、12,000 字符；queued、failed、cancelled 和 interrupted 不是可送入执行器的完成历史。

## 当前真实验证

本地环境：Windows、Python 3.12.12、PostgreSQL 18、pgvector 0.8.1。CI canonical 环境为 Linux/Python 3.12.13、PostgreSQL 18、pgvector 0.8.6；最终数字只能来自文档提交后的精确最终 PR head。

- 完整离线套件：`801 passed, 1 warning, 157 subtests passed`。
- 累计门禁内 JUnit：`958 tests, 0 failures, 0 errors, 0 skipped`。
- 完整 PostgreSQL integration：`79 passed`。
- M4-T01 至 M4-T08：`8/8`。
- M0-M4 累计 mandatory checks：`41/41`。
- 实现验收 quality gate JSON SHA-256：`216d3d8a2f26cb9e721843ec681ddfa3b01d4ac6b4e6b2c734637f79e862c5fb`。
- 文档与 API 示例冻结后的本地复验仍为 `41/41`，JSON SHA-256：`598e37db8d05c6fd62e68bec190fbf3b26ebf1e2bb9c799c5554fd3657613958`。
- 真实 PostgreSQL service restart receipt SHA-256：`d1754488ebfc9fc4bdd8ba7f4198cfe0ab04b87cf6603c97ce4fa6ac762e82ad`。
- 独立应用进程 restart receipt SHA-256：`ee9c9ad1f65cea066c79cfdb4c8cf95d491577b160944fe70c3720f84bd3d105`。
- 文档输入冻结后的 0.5.0 wheel：`354228` bytes、`78` entries、SHA-256 `df96b07a844e3a888ac12fb350890e49945d6ba70009f8ba477eef66ac19e583`；仓库外 offline/no-deps 隔离安装、`legal-rag --help` 与 `legal-rag-api --help` 均通过。
- pre-documentation head `2c4c2a0...` 的 GitHub Actions run `36469045549`：两个 job 均 success；offline artifact digest 为 `sha256:c3da41c02dc55afb0fd6ee9b7e280c4c05f9762e3fa436f8b98948ab8b2b07c8`，service artifact digest 为 `sha256:8429b014c64a6731cdc63e90f5dfc329273a016385c83360508edb7869f4da4b`。该 run 不替代候选文档提交后的精确 final-head CI。
- `uv lock --check`、一致 dependency selection 的 offline `uv sync --check`、targeted Ruff、workflow YAML 和 `git diff --check`：通过。
- Starlette TestClient/httpx compatibility layer 有一个非阻塞 deprecation warning；真实 M4 HTTP 验收使用 loopback Uvicorn TCP。
- 真实或付费生成模型、远程 embedding、reranker 和 Judge 调用：0。

首轮累计 gate 曾失败，不能从历史中删除：supervisor stale recovery 把真实 `batch_limit` 参数错写为测试替身接受的 `limit`，导致真实服务无法 ready/claim；释放 quarantine 线程后 `stop()` 也只做一次瞬时 reaping。修复真实和 fake 接口、增加短有界 drain grace 后，独立进程、模型超时、全量单元、全量 integration 和 41/41 gate 均重新通过。

文档收口阶段还有一次独立的安全预检拒绝：重跑命令最初漏设 `LEGAL_RAG_INTEGRATION_TEST=1`，因此 M3/M4 数据库 selectors 没有执行，门禁按 fail-closed 返回失败。补齐 integration guard、显式测试 DSN、pgvector 版本、禁用 dotenv 与 live call 后，最终复验 41/41 通过。这是调用环境缺项，不是代码测试失败，也没有被隐藏。

## M4-T01 至 M4-T08

| ID | 通过的真实边界 |
| --- | --- |
| M4-T01 | 两个 Bearer principal 经真实 TCP/DB 验证非枚举 owner isolation，覆盖 messages/run/evidence/events/cancel/resume |
| M4-T02 | 并发相同 key 只有一个 run、用户消息、幂等记录和最终回答，成功后 replay 仍复用 |
| M4-T03 | 同 key 不同 body 稳定 409；不同 key 并发也只有一个 active run，无 orphan |
| M4-T04 | 两个不同应用 PID 共用数据库；完成历史保留，stale running 变 interrupted，取消后可新建 |
| M4-T05 | 真实 SSE 主动断开后按 sequence 补读，不取消、不重复 run，最终事件与结果原子可见 |
| M4-T06 | verifier 拒绝的唯一草稿标记不出现在 SSE、status、evidence、messages 或数据库 payload |
| M4-T07 | 数据库不可用、执行超时、输入过长可归因且脱敏；旧线程晚写和跨 lease row-lock 写入均被 fence |
| M4-T08 | 原 CLI import/help 与 optional service 隔离；production provider-free wiring 和候选 wheel 双入口可验证 |

## 发布前仍需完成

1. 提交并推送文档候选；更新 PR #17 body。
2. 等待精确最终 PR head 的 offline 与 M4 service 两个 job 都 success，并核对同 SHA artifacts。
3. 将 PR 从 draft 转 ready，使用普通 merge commit 合并，不使用 admin、force、squash 或 rebase。
4. 等待精确 release-target master push 的两个 job 都 success。
5. 仅此后创建 annotated `v0.5.0` 并普通 push；验证 tag object peeled target 精确等于 release target。
6. 创建并核验非 draft、非 prerelease GitHub Release，不上传私密或本地产物。
7. 从最新 master 创建独立 `codex/m4-release-receipt`，记录 PR/master CI artifacts、wheel、restart、Tag 和 Release 事实。回执 PR 与其 merge 后 master CI 都成功后，关闭 Issue #16，再确认并关闭 Milestone 5。
8. 用小型 documentation-only finalize PR 回填回执 merge/master CI 与关闭时间，把 M4 改为 `released`。不移动 `v0.5.0`，不开始 M5。

## 已知限制与提升优先级

### M5 优先

- 持久 LangGraph checkpointer 与节点级 JSON 状态。
- budget reservation、绝对 deadline、heartbeat 与恢复不重置预算。
- graph/schema/version compatibility 和两进程 resume fencing。
- 外部请求结果不明时显式 `outcome_unknown`，不声称 provider exactly-once。

### M4 后续运维优化

- Python thread 不能强制终止阻塞 SDK；优先支持 provider cooperative cancel，必要时迁移到可终止子进程或独立 worker。
- shutdown incomplete 时保守保留 engine；未来增加 deferred reaper/disposer，改善同进程热重载。
- readiness 当前验证数据库、migration、active pointer 和 validated import，不做全语料逐行审计；可增加低成本只读 smoke，但不能让每次 health probe 全表扫描。
- provider-free run 当前会重新装配 bound corpus/BM25；应按 immutable `(scope, snapshot, profile)` 缓存，并避免 lexical path 传输无用高维向量。
- SSE 当前基于 PostgreSQL polling；规模增大后可用 LISTEN/NOTIFY 或 fanout 降低轮询，不改变 durable event log。
- 静态 token registry 只适合本地/受控环境。生产前需要 OIDC/OAuth2、tenant、TLS、rate limit、secret manager、审计与保留/删除策略。
- 精确法条 catalog、exact dense 和 HNSW 尚未接入默认 HTTP 路由。
- 真实完整法律语料上的服务 P95、ANN recall、模型质量、成本与人工法律评审尚未获预算执行。

## 回滚边界

- 优先关闭可选 HTTP service 并继续使用原 `legal-rag` CLI；不要删除 PostgreSQL 卷来伪造回滚。
- 代码问题通过新的 revert/fix PR 处理，不重写历史、不强推、不移动已经发布的 Tag。
- `0005` downgrade 会删除 M4 session/run/message/result/idempotency/event 表。有任何 M4 数据时不得盲目 downgrade；应先备份、导出并获得明确迁移批准，优先前向修复。
- `interrupted` 在 M4 不能精确 resume，只能由 owner 取消后创建新的 run。
- 软件 Tag 发布后固定在软件 merge commit；receipt/finalize 提交只记录事实，不移动 Tag。

## 停止条件

M4 只有在软件 PR、release-target master CI、annotated Tag、GitHub Release、独立 receipt PR、receipt merge 后 master CI、Issue #16 与 Milestone 5 收口、最终文档状态全部远端核验后才标记 `released`。完成后停止。M5 必须等待新的明确授权。
