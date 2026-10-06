# Changelog

本文件记录候选与已发布版本的用户可见变更。历史实验数字仍以对应报告中的语料、模型和时间条件为准。

## [Unreleased]

当前Elasticsearch＋IK实验源码 `dde6fc3c568b461aa84cfa38351478b239eb20db` 已提交推送到 [Draft PR #31](https://github.com/1040942669/legal-rag-agent/pull/31)。本地精确源码M2通过，最新读取其 [CI 37502817897](https://github.com/1040942669/legal-rag-agent/actions/runs/37502817897) 已四路completed/success；不预写后续文档head通过。前轮SmartCN源码 `8f042fb46b83c7d2e7eb630c1197546f0d98c3cd` 的 [CI 37403867767](https://github.com/1040942669/legal-rag-agent/actions/runs/37403867767) 及随后文档提交 `31a6e63` 的 [CI 37405297646](https://github.com/1040942669/legal-rag-agent/actions/runs/37405297646) 均已四路成功，属于独立历史事实。运行时提交 `04fd389945125c81767745bbdce48137921d26fd` 与文档提交 `e3d32a96d3a37f5ab7c0e559bb000448fb0e603d` 的 [CI 37354368069](https://github.com/1040942669/legal-rag-agent/actions/runs/37354368069) / [37356943303](https://github.com/1040942669/legal-rag-agent/actions/runs/37356943303) 也保留原四路成功与真实broker/worker证据，不改绑到追加实验。PR仍为draft，未合并或发布，后续文档head另行核验；默认、生产配置和版本未变，M6 `v0.7.1` 与既有Tag/回执不变，M7未开始。

- 增加仅实验使用的ES/IK9.1.4自有loopback节点、原默认词典与预冻结v4三臂协议，不接生产CLI/API/M2 selector或共享索引缓存。最终 `chinese_bm25_20261007_ik_fixed_fourth_verified` 的Hit@5/MRR@5为legacy 76/108、0.610184，char 83/108、0.597065，IK 71/108、0.550307；720条执行完整、两遍一致、身份及清理通过，新增模型调用与费用0。char selected但promotion eligible为空，默认仍为 `legacy-v1`，不推广IK或SmartCN，不按题补词、调参或改门槛。完整结果见 [IK验收记录](reports/refactor/ELASTICSEARCH_IK_EXPERIMENT.md)。
- 最终dde6源码135项离线合同和8项显式真实本地ES checks通过，后者不冒充默认CI已运行ES。clean源码本地M2为25/25 passed、exit0、267732ms；其中2255 passed＋157 subtests、206.58s、JUnit2412/0/0/0、4条jieba上游警告。首轮中断、恢复轮失真的OOV诊断和被取代而取消的c241门禁均保留，不作为最终通过或winner证据。同轮legacy/char/IK第二遍client p95为612.04/30.11/59.41ms，采样进程树RSS为468.02/449.29/1656.56MiB，只是带采样开销的直接排名测量，不证明生产API延迟、容量或安全部署；120题仍为重复开发集、108有gold/12 NA。
- 增加成熟BM25S及显式jieba/sklearn字符分析器，记录完整依赖/参数身份，不新增场景词典或手写评分公式。前两组固定开发集对照保留负结果与取舍；用户随后选择继续实测SmartCN，而非接受字符方案默认推广。
- 前轮增加仅实验使用的Lucene 9.12.3/Java17本地桥、独立v3协议与四臂benchmark，不修改旧协议/gold，不接生产API、SmartCN服务selector或共享缓存。第三轮 `chinese_bm25_20261006_smartcn_fixed_third` 的Hit@5/MRR@5依次为legacy 76/108、0.610184，char 83/108、0.597065，BM25S＋SmartCN 73/108、0.506632，原生Lucene＋SmartCN 73/108、0.503237；四臂两pass稳定，错误及模型调用均0。没有候选同时不回退，当轮默认不变、不推广SmartCN；这些是第三轮独立历史结果，随后用户追加IK实测，不是接受char的MRR取舍。12条无gold题保持NA，不当作拒答通过。
- 前轮SmartCN实验桥合同139项通过（102单元＋37显式实际JVM），不是远端Java CI。clean `8f042fb...` 本地累计M2为25/25 passed、316818ms；其中2120 passed＋157 subtests、245.43s、JUnit2277/0/0/0，4条jieba上游警告。生产代码未改，两次追加实验未重跑本地PG/wheel，旧证据仍绑定04fd；重复开发集不是独立法律质量或生产容量验收。
- CLI、API和M2统一冻结实际检索配置；现代 `general-reference-v3` / `reference-evidence-v2` 合同移除统一0.01原始分数阈值，历史 `general-reference-v2` / `reference-evidence-v1` 工件保留原判定。现代风险词只作提示信号，不能证明违法意图；原文检索不据此预拒答，自由生成遇到用途不明信号先澄清。
- 明确引用的选择与法条所有权分离，复杂意图不伪造硬要求；目录保留完整标题，有界查询语法不支持不等于存储身份无效。运行时提交04fd的本地M2累计25/25，其中2018 passed与157 subtests（180.49秒，4条jieba警告）；17文件PostgreSQL回归93 passed（94.32秒），真实重启后的新进程核验schema0008成功。这些旧证据保留原身份，详见 [验收记录](reports/refactor/CHINESE_BM25_OPTIMIZATION.md)，不等于法律质量、生产容量或真实模型验收。

## [0.7.1] - 2026-10-02

软件 [PR #28](https://github.com/1040942669/legal-rag-agent/pull/28) 正常 squash 合并为 `582eb8949c1150fc7a12761bd46fbda9c173ef62`。最终 head `d6fc26882237ca149ab38c7e944b32a260e0430b` 的 [CI](https://github.com/1040942669/legal-rag-agent/actions/runs/36998001012) 和实际目标的 [master CI](https://github.com/1040942669/legal-rag-agent/actions/runs/36999853797) 均 4/4 success。annotated Tag object `a4d7c84087ba32ad183efd20275778f5f473bd9c` 固定 peeled 到该软件提交；[Release](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.7.1) 于 `2026-10-02T11:30:49Z` 发布，非 draft、非 prerelease。独立 [回执 PR #29](https://github.com/1040942669/legal-rag-agent/pull/29) 的最终 head/master [CI 37003455439](https://github.com/1040942669/legal-rag-agent/actions/runs/37003455439)/[37005116491](https://github.com/1040942669/legal-rag-agent/actions/runs/37005116491) 均 4/4 success，普通 merge 为 `7ec13709d90fad1a01b85b9558a3bb8924a0846d`。Issue #25/Milestone 7 随后于 `2026-10-02T12:27:11Z`/`12:27:23Z` 关闭，M6 为 `released`；旧 v0.7.0/v0.6.0 Tag 不移动。

### Fixed

- 幂等 job 提交记录返回的持久状态，不再把已运行或已结束任务误报为新 queued；dispatcher 的成功事件描述真实投递，而不是业务重新排队。
- 当前 worker/item/stage 使用单调时钟记录耗时，重试来源区分 outbox delivery attempts、item attempts 和 worker claims。复用仅记录当前读取，不重放历史计算与调用。
- 已有 M2 评测 Trace 关联 job/experiment/case-attempt；M5 实际节点、外部动作、持久预算、证据与 artifact cache lookup 接入统一观测。提供方未报告或报告不完整的 token 分量保持 null，费用不估算。
- 累计 M6 门禁增加实际调用点与真实 PostgreSQL 图执行断言；精确候选与 master 均 58/58，专项 JUnit 76/0/0/0，完整离线 1013 tests + 157 subtests，M4/M5 JUnit 79/0/0/0、81/0/0/0。0.7.1 wheel 通过隔离 installed-runtime smoke。详见 [观测修补验收报告](reports/refactor/M6-observability-patch.md)。
- 无新增依赖、migration 或 checkpoint schema；旧 CompletionUsage 位置参数和 snapshot 保持兼容。模型计数是受控客户端调用次数，不证明 HTTP 已发出或已收费。缺失 token 和费用保持未知。

## [0.7.0] - 2026-09-29

> 软件及独立回执已发布：[PR #26](https://github.com/1040942669/legal-rag-agent/pull/26) final head `b9400ab289618707a53ee6b14f6ac1cee4af2ee1` 的 [四路 CI run 36553279892](https://github.com/1040942669/legal-rag-agent/actions/runs/36553279892) 全部成功；普通 squash merge 和 release target 为 `28517b6f323253baf638ba60c887d10630dd0bf1`，其 [master CI run 36554828645](https://github.com/1040942669/legal-rag-agent/actions/runs/36554828645) 亦为 4/4 success。annotated `v0.7.0` Tag object `f40715c16e90b429a7c49a6347092114fefc787d` 精确 peeled 到该提交，[GitHub Release](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.7.0) 于 `2026-09-29T10:39:52Z` 发布，非 draft、非 prerelease。独立回执 [PR #27](https://github.com/1040942669/legal-rag-agent/pull/27) 和其 [master CI](https://github.com/1040942669/legal-rag-agent/actions/runs/36561210905) 均完成，四路 success；运行时观测修补已作为上面的 v0.7.1 发布，完整 M6 已在 v0.7.1 独立回执及治理门禁完成后收口。

### Added

- 两个明确的批任务入口：预注册 M2 provider-free 离线评测和 M3 prebuilt 语料/索引导入；202 只确认 PostgreSQL 接受，任务状态和进度可由 owner 查询。
- Alembic `0007_m6_jobs_outbox` 的 job、item ledger、transactional outbox、lease epoch/有限重领、取消和重复投递对账；Redis 仅作为 Celery broker。
- 导入最终激活与 job 租约核验、item 完成和成功终态同事务提交；默认最多自动创建 5 条 outbox 记录并指数退避，耗尽后显示 `delivery_unconfirmed`；同一未确认 pending 记录仍可重试发送。
- Linux/WSL2 Celery prefork worker 与 outbox dispatcher；消息只有 job ID 与 schema version，worker 不接收 HTTP 提供的路径或私人文本。
- 本地 typed observation，以及默认关闭、需显式数据流确认且严格脱敏的可选 Langfuse OTLP exporter。
- M6-T01 至 M6-T07 验收入口和 M0-M6 累计门禁；最终软件 PR head、release-target master 及独立回执的 head/master 四路 CI 均成功，真实 PostgreSQL/Redis/Celery worker JUnit 41/0/0/0、累计门禁 58/58、0.7.0 wheel 隔离 smoke 成功且无 live model 调用。此处数字只对应历史 0.7.0，不替代观测补丁验收。

### Security and limits

- job 注册引用由受控主机配置，绑定 authenticated scope/profile；GET/cancel 对其他 owner 返回非枚举 404。Redis outage 不删除 PostgreSQL job/结果。
- M2 评测不调用 live model；M3 导入只消费已验证的预构建 artifact，`embedded` 阶段不声称现场生成 embedding。索引读回验证后才激活快照。
- Celery late ACK、worker-loss reject 与数据库 fencing 仍是 at-least-once，不承诺任意外部调用 exactly-once。取消在阶段边界生效，不能撤销已提交操作。
- 自动发布预算耗尽且 broker 消息全部丢失时，不保证无人值守的继续恢复；需授权运维对账。当前无普通用户重投 API。
- 本版验证工程机制，不证明真实法律问答质量、生产容量、外部 Langfuse 实发或任意 provider exactly-once。软件 Release 和回执已存在；Issue/Milestone 因观测补丁尚未闭环而保持 open。真实测试、未运行项和回滚边界见 [M6 验收报告](reports/refactor/M6.md)。

## [0.6.0] - 2026-09-29

> 已发布并完成独立回执：[PR #22](https://github.com/1040942669/legal-rag-agent/pull/22) final head `aa737e8d1f77214277c0544ce069d36c2b2161ff` 的三路 CI 与 merge/release target `832acaafaf5633e76daed7a62a73755187fca51e` 的三路 master CI 均成功；M5 专项 JUnit 为 81/0/0/0，M0-M5 累计门禁 51/51，恢复 demo、真实 PostgreSQL service restart 与隔离 wheel probe 通过。annotated `v0.6.0` Tag object `c0ef0721ab49da0d7840b76e741a52e35b8941d2` 精确 peeled 到 release target，GitHub Release 非 draft、非 prerelease。独立 [receipt PR #23](https://github.com/1040942669/legal-rag-agent/pull/23) final head `9dd6ec3867f05dd207ec15657861028f138167fc` 的 [三路 CI run 36501403167](https://github.com/1040942669/legal-rag-agent/actions/runs/36501403167) 为 3/3 success；PR 于 `2026-09-29T00:13:59Z` 以普通 merge commit `3436e9ad41c7455aa5f31117ab7ece3f4ea847c1` 合并，其 [master run 36502063863](https://github.com/1040942669/legal-rag-agent/actions/runs/36502063863) 亦为 3/3 success。Issue #21 于 `2026-09-29T00:23:07Z` 关闭，跟踪 M5 的 GitHub Milestone 6 于 `2026-09-29T00:23:22Z` 关闭，因此 M5 状态为 `released`；在 M5 完成时，路线图 M6 尚为 `not_started`。

### Added

- `m5-bounded-v1` 单一有界 LangGraph 控制图，覆盖查询分析、路由、检索、证据合并/检查、有限 follow-up、生成、验证和结果持久化，不与旧 adaptive loop 叠加。
- 严格版本化 JSON graph state，拒绝未知字段、非有限数、prompt、secret、credential、raw draft、client 和 connection 对象，并在节点边界冻结 run identity、scope、snapshot、profile、budget 和 absolute deadline。
- PostgreSQL `PostgresSaver` 持久 checkpoint，以及应用侧可信 checkpoint projection；每个 lease epoch 使用隔离 thread identity，并显式拒绝 `InMemorySaver` 作为持久恢复后端。
- Alembic `0006_m5_harness_recovery`，新增 durable budget ledger、external attempt journal、node artifact、application checkpoint、parent lineage、deadline、stop reason、lease epoch 和 resume 状态。
- 显式 owner-scoped resume、lease heartbeat 与 epoch fencing；两个进程竞争同一 run 时至多一个有效 owner，失效 owner 不能推进 checkpoint pointer 或发布终态。
- side-effect-aware recovery：检索 checkpoint 可复用；dispatch 后未持久化的 provider/planner 结果记为 `outcome_unknown` 且预算不退款；已提交最终结果在图尾崩溃后通过唯一结果对账，避免第二条回答。
- parent-bound clarification follow-up：`needs_clarification` 保持终态，用户补充创建同 session 的新 child run，child 使用独立 deadline/ledger，父 run 预算不变。
- 只读工具白名单 `search_laws`、`get_article`、`get_neighbors`、`inspect_evidence_metadata`，并将 user/scope/snapshot/profile/DSN/SQL 固定在可信服务端边界。
- M5-T01 至 M5-T10 跨进程 PostgreSQL fault-injection suite、关闭 schema 的 fault receipt、一命令恢复 demo、M5 wheel probe 和 M0-M5 累计门禁契约。

### Changed

- 包版本升级到 `0.6.0`；`langgraph` 与 `langgraph-checkpoint-postgres` 保持在可选 `service` extra，旧 `legal-rag` CLI 和非 M5 `LegalChatRunExecutor` 路径继续可用。
- `/api/v1/runs/{id}/resume` 只对兼容 graph version 的 owned `interrupted` run 启用；M4 run 保持明确 unsupported，不静默迁移。
- 外部操作在 dispatch 前先事务性预留预算。429 和 timeout 只做有界重试，400/401 不盲重试；resume 不重置预算或绝对 deadline。
- `legal-rag-api --migrate` 在 Alembic 后初始化 PostgreSQL saver schema；普通启动只验证 checkpointer readiness，不隐式迁移或回退内存存储。

### Security

- prompt injection 内容不能注册额外工具、覆盖 authenticated/frozen retrieval boundary、提交 SQL/DSN，或把 prompt、secret、credential 和未验证 draft 写入 checkpoint、event、result 或普通日志。
- checkpoint serializer 禁止 pickle fallback；业务恢复只信任应用 checkpoint pointer，不把 stale lease 留下的 framework checkpoint 当成当前状态。
- checkpoint、event、attempt reconciliation 和 terminal publication 同时受 lease owner、lease epoch、database wall clock、revision 和 event sequence 约束。
- 发布验证禁用 dotenv 和 live provider；真实/付费 generation model、remote embedding、reranker 与 LLM Judge 调用均为 0，也未上传私密语料、凭证或本地数据库内容。

### Known limitations

- 软件 exact-head、release-target master CI、Tag、Release、独立 receipt PR、receipt merge 后 master CI 与 Issue/Milestone 治理闭环均已完成；后续非递归 finalization PR #24 也已合并并通过精确 master CI，未移动 `v0.6.0`，也未新增产品能力。
- LangGraph checkpoint 与应用事务不是跨表全局原子提交；M5 通过可信业务 pointer 和保守 outcome reconciliation 缩小风险，但不承诺任意 provider exactly-once、远端撤销或零重复计费。
- unknown external outcome 默认不静默重试，run 可能以 `completed_with_limits` 结束并给出 stop reason。
- `v0.6.0` 的 supervisor 本身不是通用分布式队列；批量评测、导入任务及 worker 运营当时未包含，后由独立的 `v0.7.0` M6 能力实现，不改变 M5 在线 runner 的边界。
- `0006` downgrade 对 M5-only 终态/事件会先拒绝；即使可以执行，也会删除 M5 应用表/列，不能视为 valued data 的无损回滚。
- 本阶段证明恢复、预算、fencing 和安全工具边界，不证明法律正确性、完整现行法覆盖、live-model 质量或生产容量。
- 不同 CI job 构建的 wheel 大小和内容合同相同但字节 SHA 不同，当前构建尚未 byte-for-byte reproducible。

## [0.5.0] - 2026-09-29

> 已发布并完成独立回执：PR #17 完成 M4 候选实现并通过 exact-head CI；首次 merge 后 master service CI 以 `78 passed, 1 failed` 暴露了把三个真实 PostgreSQL callback 压入 150ms 的测试时序假设，因此发布被阻断。PR #18 只修正该测试预算，随后 exact-head 与最终 release-target master CI 全部通过。annotated `v0.5.0` 和 non-draft、non-prerelease GitHub Release 已发布；独立 receipt PR #19 的 final-head 与 merge-target master CI 均成功，Issue #16 与 Milestone 5 已按顺序关闭。

### Added

- 可选 FastAPI v1 服务与 `legal-rag-api` 入口，提供 liveness/readiness、持久 session/message/run、evidence、cancel、明确 unsupported resume 和经鉴权的 SSE 事件续读。
- Alembic `0005_m4_api_sessions`，新增 session、message、run、result、idempotency key 与有序 event 表；数据库 partial unique index 保证同一 session 只有一个 active run。
- 服务端 Bearer token registry，将 opaque token 映射为不可由客户端覆盖的 `user_id / scope_id / profile_id`，并对跨用户 session/run/events/evidence/cancel/resume 使用非枚举式 404。
- 短事务 `RunService`：run 创建时冻结 snapshot revision、activation、profile、retrieval config 与 graph version；run、用户消息、幂等绑定和首事件原子写入。
- 单进程 `RunSupervisor`，使用 `FOR UPDATE SKIP LOCKED`、有限 lease、revision/event sequence CAS、数据库 wall clock 与 stale `running -> interrupted` 恢复；执行超时线程被隔离且晚到写入被 fence。
- PostgreSQL 持久 SSE 事件日志与 `Last-Event-ID` 补读。只发布安全阶段摘要；result、最终 assistant message、run 终态与 `answer.final` 同事务提交。
- 真实 loopback HTTP/SSE、并发幂等、两个独立应用 PID 重启、数据库故障、模型超时、输入上限、草稿隔离和 production wiring 集成测试，以及 M0-M4 `41/41` 累计门禁。
- 候选 wheel 审计脚本，检查 0.5.0 metadata、`0001` 至 `0005` migration、service runtime 模块、optional extra、禁止路径，以及仓库外 `legal-rag` / `legal-rag-api` 双入口 smoke。

### Changed

- 包版本升级到 `0.5.0`。FastAPI、SQLAlchemy、psycopg、pgvector 和 Uvicorn 保持在 `service` optional extra；原 `legal-rag` CLI 仍可在没有服务依赖、数据库配置或 API 进程时运行。
- 默认 HTTP service 使用冻结 PostgreSQL 语料上的 bound BM25 与 `LegalChatRunExecutor(generate=False)`，保持 provider-free；M3 exact pgvector、catalog 和实验 HNSW 没有被误写成默认 API 路由。
- PostgreSQL 客户端新增有界 connect、pool、statement 与 lock timeout；HTTP 数据库异常只返回脱敏且可归因的 503。

### Security

- 身份凭证只接受 Authorization Bearer header；常见 URL credential 参数在进入路由前被拒绝，任意 `X-User-ID` 不能覆盖 principal。
- Stage/failure event 使用关闭字段集合和递归敏感字段拒绝；被 verifier 拒绝的草稿、raw provider output、prompt、hidden reasoning 与凭证字段不能进入 SSE、result 或 message。
- lease 检查使用 PostgreSQL `clock_timestamp()`，避免事务开始时间在 row-lock 等待后错误放行过期 worker；run/result/message 使用单条一致快照读取，避免 READ COMMITTED 撕裂。
- 取消、超时或进程退出都不伪称已撤回外部 provider 请求；旧线程只能继续消耗其自身资源，不能提交最终结果。
- M4 候选验证禁用 dotenv 和 live provider，没有调用真实/付费生成模型、远程 embedding、reranker 或 Judge，也没有上传私密语料、凭证或本地数据库数据。

### Known limitations

- M4 是单进程、串行 supervisor，不是分布式任务平台；精确 checkpoint/resume、节点复用、预算恢复与外部调用 outcome reconciliation 属于 M5。
- Python 无法强制终止已阻塞在 SDK 中的线程；quarantine 和 fencing 保护数据正确性，但不保证停止提供商计算或计费。
- shutdown 中若仍有隔离线程存活会保守地保留 engine；同一 Python 进程反复热重载时可能暂时保留旧连接池，未来可增加 deferred disposer。
- 开发用静态 token registry 不是生产 IdP；本版没有 OIDC、TLS、rate limit、secret manager、完整审计/观测或生产部署。
- 默认 API 每个 run 仍从 PostgreSQL 重新装配 bound corpus/BM25；完整语料下的缓存、内存和 P95 尚未做发布级优化。
- 本版证明服务、持久化、并发与安全事件合同，不重新证明语义蕴含、法律正确性、完整当前法律覆盖或真实模型质量。

## [0.4.0] - 2026-09-25

> 已发布：M3 软件 PR #13、release-target master CI、annotated `v0.4.0`、GitHub Release、独立回执 PR #14、回执 merge 后 master CI、Issue #12 与 Milestone 4 均已完成并远端核验。

### Added

- PostgreSQL/pgvector 版本化语料 schema 与 Alembic `0001` 至 `0004` 迁移，覆盖不可变 corpus 行、active snapshot activation ledger、HNSW build generation guard 和可打包 migration 资源。
- 可复现存储导入入口 `python -m legal_rag.storage.import_cli`：`plan`/`dry-run`、`validate` 与 `apply` 分离；计划与验证默认不连接数据库，apply 会重新验证本地产物、事务导入、回读并生成机器 receipt，且不会隐式激活快照。
- 绑定 scope、snapshot、embedding profile、law、version、article 与 effective date 的 typed retrieval boundary/provenance；数据库 exact pgvector、bounded BM25、RRF、adaptive、rerank、chat 和 artifact 恢复路径共享同一边界闭包。
- Profile-free 的精确法条目录，按精确法名、规范化条号以及可选 law/version/date 返回 `found`、`not_found` 或 `needs_disambiguation`，并携带可重算的 snapshot membership proof。
- Revisioned active snapshot pointer、不可变 activation event、完整 CAS、scope 级事务锁，以及原子 activate/replace/rollback。
- 显式实验 HNSW index manager 与 retriever，物理索引和 build receipt 绑定 snapshot/profile/dimension/generation；typed ANN underfill 可保持显式失败，或在同一 hard boundary 下执行有时限的 exact fallback。
- M3-T01 至 M3-T08 累计质量门禁，共 `33/33` 个机器可读必检 ID；另有真实 PostgreSQL 服务重启、新进程复核、downgrade/re-upgrade 和 wheel migration 资源检查。

### Changed

- 数据库向量检索继续以 exact inner product 为默认和权威路径；HNSW 不会自动替代 exact，只有调用方显式选择实验策略且验证对应 generation-bound build receipt 后才可使用。
- Hard filter 现在在数据库排序和 `LIMIT` 之前执行；多 article chunk、ANN underfill 和 exact fallback 均保留完整组合 selector，不允许先全局召回后在应用层补过滤。
- Active snapshot 从单一指针升级为 `(snapshot_id, revision, activation_id)`；运行中的 `REPEATABLE READ` 请求固定其起始快照，后续请求才观察新 activation。
- 版本候选升级到 `0.4.0`；数据库依赖仍是可选安装项，既有 provider-free 离线/M2 artifact 路径保持兼容。

### Security

- Snapshot/profile/import receipt、向量维度/归一化、模型 revision、chunk/vector/payload hash 和 HNSW generation 在执行前或读取时失败关闭；trace fingerprint 不替代完整 typed authorization boundary。
- 导入 manifest 只接受 source-root 内的安全相对路径并拒绝敏感字段；输出 plan/receipt 采用 no-clobber，数据库 URL 的公开表示会脱敏，apply 先完成本地复核才解析数据库配置。
- ANN fallback 只能在原边界内运行，无法通过 underfill 移除 scope、snapshot、profile、law/version/article 或日期过滤；超时和候选不足是显式状态。
- M3 验收只使用 loopback 测试数据库，禁用 dotenv 和 live provider；真实模型、远程 embedding、reranker、Judge 与付费调用均为 0。

### Known limitations

- M3 已发布并完成独立回执；后续文档与 M4 工作不会移动或复用 `v0.4.0`。
- HNSW 是显式实验能力，pgvector HNSW 维度上限为 2000；不支持的维度仍可使用 exact。ANN recall 和性能没有在真实完整法律语料上做发布级基准，本版不把它设为默认。
- 日期过滤只证明版本元数据覆盖 `[valid_from, valid_to)`，不判断具体案件应适用哪一版法律；本里程碑也不证明法律内容、模型回答或引用语义正确。
- 当前 CLI 尚未实现终端用户认证、多租户授权或默认 active snapshot 注入；typed scope 是可信上层必须提供的边界，不应被误解为完整生产权限系统。
- Deferred activation consistency trigger 仍会扫描全历史，导入 repository 的全局事务锁牺牲并发；后续可按 scope/父 snapshot 优化，但不影响当前正确性结论。

## [0.3.0] - 2026-09-22

### Added

- 严格实验 manifest 与分阶段精确缓存契约，区分 fresh、cache 和零外调 replay；缓存键按阶段依赖定向失效。
- 不可变逐 case attempt/complete artifact、校验和、损坏清单、兼容性检查和 pending commit 恢复。
- 通用 work-unit runner，支持 session group 内顺序执行、不同 work unit 并发、重试预算、断点续跑、checkpoint hash chain、分层计时和外部调用账本。
- 真实 evaluation/chat adapter 与 `ConversationMemory` 导出恢复，逐阶段保存 query、retrieval、generation、verification、judge 和最终结果。
- provider timeout、并发上限、可重试/不可重试错误分类，以及默认关闭的真实模型调用闸门。
- 冻结数据集 registry、四种 evaluation mode、artifact-only 聚合，以及 `experiment plan/run/resume/aggregate/replay` 生命周期 CLI。
- 累计 M2 离线门禁，将 M0 的 7 项、M1 的 10 项和 M2-T01 至 M2-T08 合并为 25 项机器可读检查。

### Changed

- 包版本升级为 `0.3.0`；实验命令不加载 `.env`，必须显式选择模式和输入，生成模式在未授权时失败关闭。
- replay 必须使用新的 experiment ID 和精确缓存命中，不能静默回退 fresh；聚合只读已持久化 artifact，发布内容寻址且不可覆盖。
- fresh/cache/replay 的真实调用数、来源调用 provenance 与耗时分开记录；失败、损坏、终止、耗尽重试和未运行均保留显式状态。

### Security

- 真实模型调用默认关闭；本里程碑的验收、示例运行和发布门禁均未读取本地 `.env`，未调用在线或付费模型。
- 实验身份记录真实 Git HEAD、dirty/untracked 摘要、配置、数据、语料、索引和阶段实现指纹，resume/replay 对不兼容事实失败关闭。

### Known limitations

- 当前可直接执行的生命周期后端是 provider-free BM25；`smoke-generation` 和 `full-regression` 只可规划，未提供预算与 provider 时不会运行。
- M2 证明实验身份、恢复、回放、聚合和指标口径的工程语义，不证明真实法律回答质量提高；本次未复跑完整法律语料、dense embedding、reranker 或模型 Judge。
- 生命周期命令当前要求 Git 源码工作区，以便读取真实 commit 与实现指纹；尚未引入跨进程任务队列、数据库或生产服务。

## [0.2.0] - 2026-09-20

### Added

- M1 结构化回答兼容层，显式区分 `evidence_answer`、`insufficient_evidence`、`needs_clarification` 与 `out_of_scope`。
- schema、证据目录、引用 ID/对齐、可注入快照/权限范围、免责声明、回答模式和未知语义状态的分层验证结果。
- 评测指标 schema v2，记录应答/应拒答/应澄清、服务失败和 Judge 三态分母，并单独计算拒答召回与过度拒答。
- 明确标注为虚构且非法律内容的 M1 改前/改后回归 fixture。

### Changed

- 免责声明和普通法律陈述中的“不得”“不能”不再被当作拒答；受限模式只接受有界模板。
- 伪造、未对齐，或在显式 `VerificationContext` 下越权的来源会使生成草稿失败；最终降级响应会再次验证后才交付。
- Retrieval-only 不再拼接检索文本冒充回答，也不运行 answer verifier 或 Judge；不可用指标使用 `null + reason`，旧 CSV 仍保留 `-1` 兼容哨兵。
- Judge 超时、传输与格式错误独立归因并排除质量均值；评测输出拒绝覆盖已有历史文件。
- Normalizer、生成回答和 Judge 的模型 JSON 使用精确字段集，拒绝重复 key、非标准数值、孤立 surrogate、超限或资源异常输入；生成回答与 Judge 失败关闭，Normalizer 使用稳定错误码并确定性回退。

### Security

- 当可信调用方显式注入 `VerificationContext` 时，不在允许快照或权限范围内的证据会在进入生成提示和返回 sources 之前被移除；当前 CLI 尚无认证身份、租户隔离或默认快照约束。
- Trace 分开保存被拒绝的生成尝试与最终交付结果；被拒绝草稿正文、schema 详情、畸形引用片段和生成 source ID 列表不进入普通 Trace，只保留安全字段与计数。
- 引用和拒答边界对全角/Unicode 括号、格式控制符、组合标记、filler 和不可见字符做保守失败关闭；正确引用只接受精确 ASCII `[S正整数]`。
- M1 累积离线门禁显式禁用 dotenv 与真实模型调用，CI 上传按 commit 和重跑编号区分的机器可读报告。

### Known limitations

- 当前语义支持仍是确定性词面启发式，只能可靠表达 `uncertain` 或 `not_checked`；没有声称已验证法律正确性。
- `VerificationContext` 是供可信上层注入的集成边界，不等于当前 CLI 已实现用户鉴权或多租户隔离。
- 本候选只运行离线 fake/fixture 测试，没有调用真实生成模型、Embedding、reranker 或 LLM Judge，也没有复跑历史法律质量实验。

## [0.1.1] - 2026-09-19

### Added

- M0 工作区与远端基线审计，明确区分真实代码、历史结果和未运行项目。
- 明确标注为完全虚构、非真实法律条文的 BM25 离线 smoke fixture，并在测试中硬性阻断网络。
- 统一 JSON 质量门禁，覆盖离线测试、包导入、CLI help、文档链接、JSON 状态和变更文件凭证风险。
- 对 pull request 和 `master` push 生效的最小权限 GitHub Actions 离线检查。
- 起始工作区中已经完成的 Phase 4B 实验平台，包括 cache v2、可选 reranker adapter、实验矩阵和成本/尾延迟聚合。

### Changed

- 包版本升级为 `0.1.1`，`pyproject.toml` 成为版本权威来源，运行时版本从 distribution metadata 读取。
- CLI 只在解析出实际业务命令后加载 `.env`；执行 `--help` 不再接触本地凭证文件。
- README 与历史执行文档区分旧 Phase 0-4B 演进和新的 M0-M7 增量主线。

### Security

- CI 不注入模型密钥，不执行真实模型、完整语料、Embedding 或 reranker 任务。
- 质量门禁只扫描 Git 候选文件，不读取被忽略的 `.env`、私人材料、完整语料或本地模型产物。

### Known limitations

- 本版验证离线工程基线，不复跑历史真实语料或模型质量实验。
- 当前 verifier 仍是启发式结构与行为检查；引用编号存在不等于语义支持已经被证明。
- Python `>=3.10` 是声明兼容范围，本阶段本地与 CI 必需门禁固定验证 Python 3.12 系列。
