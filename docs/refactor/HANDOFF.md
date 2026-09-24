# 改造交接记录

## 当前真实状态

- M3 已按用户授权启动，当前状态是 `in_progress`，不是 `released`。工作分支为 `codex/m3-storage-snapshots`，基线为 `6cd07d982ffeecec3632ff47c8473118585a9693`，模块 A+B 最新实现提交为 `4bc912b09931803d53b4ca3ffdc81d302ba65813`。
- M3 跟踪对象为仍处于 open 状态的 [Issue #12](https://github.com/1040942669/legal-rag-agent/issues/12)、[Milestone 4](https://github.com/1040942669/legal-rag-agent/milestone/4) 和 draft [PR #13](https://github.com/1040942669/legal-rag-agent/pull/13)。模块 A 与 B 均已验收并推送，但 PR 尚未合并；`candidate_sha`、`release_target_sha`、Tag、GitHub Release 和发布回执均不存在。
- 模块 A 已建立版本化语料/法条/分块/embedding schema、事务导入、不可变导入回执、Alembic 迁移与数据库 CI。模块 B 已完成 exact pgvector repository、typed retrieval boundary/provenance、全检索路径 fail-closed 约束、不可变语料行迁移和 SearchResult artifact 向后兼容。
- 模块 A 的原始验收历史继续保留：实现 head `68f89ed0cd1393d8acda7570a83bb0714f57a868`、12 项单元测试、9 项 PostgreSQL 集成测试及 [CI 35697224230](https://github.com/1040942669/legal-rag-agent/actions/runs/35697224230) 均已通过；这些历史对象没有被模块 B 的累计结果改写。
- 模块 B 定向离线/合同测试为 `210 passed`；本地累计 PostgreSQL 集成测试为 `16 passed`，使用 PostgreSQL 18.1 + pgvector 0.8.1；完整回归为 `612 passed, 157 subtests passed`，真实模型调用为 0。
- 精确 head `4bc912b...` 的 [CI 36045040845](https://github.com/1040942669/legal-rag-agent/actions/runs/36045040845) 已成功：quality gate 与 storage integration 两个 job 均成功，数据库 job 使用 PostgreSQL 18 + pgvector 0.8.6。quality artifact digest 为 `sha256:77be3dd106aabf0999c1d84519451bdaff4b7a88c3a68c32421d682702aafdca`，storage artifact digest 为 `sha256:4f06ece14c2b178552b2ccacd285aa7120d202dcf527ae0369b219eb6e5fe113`。
- M2 软件与文档状态均为 `released`。最终 PR head 为 `170da868831e2730ea15d56ed88ad24b1159e67b`；[PR #9](https://github.com/1040942669/legal-rag-agent/pull/9) 于 `2026-09-22T04:32:15Z` 普通合并，release target 为 `da7023a659672121fd772364e475870a0167e1be`。
- M2 的 [Issue #8](https://github.com/1040942669/legal-rag-agent/issues/8) 于 `2026-09-22T05:04:04Z` 关闭，[Milestone 3](https://github.com/1040942669/legal-rag-agent/milestone/3) 于 `2026-09-22T05:04:05Z` 关闭。annotated `v0.3.0` 已发布并远端核验：[GitHub Release](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.3.0)，发布时间 `2026-09-22T04:36:35Z`，非 draft、非 prerelease、附件 0。
- tag object `191bbf7dd26bf4010731309aa7d8bdfc40d9a50b` peeled 到 `da7023a659672121fd772364e475870a0167e1be`，与远端 `master` 和 Release target 一致。软件 Tag 不会为后续回执移动。
- M0 软件版本已经发布并远端核验；终态为 `released`。发布回执 [PR #3](https://github.com/1040942669/legal-rag-agent/pull/3) 已于 `2026-09-19T07:13:37Z` 合并，`master` merge commit `52714d5f84634f008a5860f6bf4f5aa199ae3261` 的 [CI 35428743968](https://github.com/1040942669/legal-rag-agent/actions/runs/35428743968) 成功，Milestone 1 已关闭。
- 目标仓库：`1040942669/legal-rag-agent`；远端默认分支为 `master`。M1 工作基线为 `52714d5f84634f008a5860f6bf4f5aa199ae3261`。
- M1 软件 [PR #5](https://github.com/1040942669/legal-rag-agent/pull/5) 已于 `2026-09-19T18:56:38Z` 普通合并；最终 PR head 为 `d2fa34776cd188197954eff9a0092c207f309b97`，release target / merge commit 为 `d51ed481f986dde807163f4b5583b07bc9ef6750`。
- release target 的 [master CI 35462786212](https://github.com/1040942669/legal-rag-agent/actions/runs/35462786212) 成功。远端 annotated tag object `546b0207eaf7dd46f1303baec9b279c14e41592c` peeled 到同一 target。
- Tag / Release：`v0.2.0` / [GitHub Release](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.2.0)，发布时间 `2026-09-19T18:59:07Z`；非 draft、非 prerelease、无附件。
- M1 回执分支为 `codex/m1-release-receipt`，从已发布的 `origin/master` 创建；软件 Tag 不包含也不需要包含后续回执提交。
- M1 终态为 `released`；机器可读回执 [PR #6](https://github.com/1040942669/legal-rag-agent/pull/6) 已于 `2026-09-19T19:16:26Z` 普通合并，merge `fee92b4457dc68edc413608fb3a0d263af740922` 的 [master CI 35463832520](https://github.com/1040942669/legal-rag-agent/actions/runs/35463832520) 成功。Issue #4 和 Milestone 2 均已关闭。
- M2 独立回执 [PR #10](https://github.com/1040942669/legal-rag-agent/pull/10) 已普通合并：最终 head `4ba8282a7ed6eba61a31f2cf2fcd88c7a5811dfc`，merge `6decc0f2e675300f1a175f7ab49c85852d35b38d`；最终 PR CI 与 merge 后 [master CI 35689024928](https://github.com/1040942669/legal-rag-agent/actions/runs/35689024928) 均成功。M3 已启动，M4-M7 尚未开始。

## M3 启动边界

- 计划版本：`v0.4.0`；只实现 PostgreSQL/pgvector 持久化、版本化语料快照、精确检索等价、全路径范围过滤、法条版本查询、原子激活/回滚、受控 ANN 和 M3-T01 至 M3-T08。
- 原离线 CLI 路径继续可用；只有显式选择数据库 profile 才要求数据库。正确性顺序是：先导入同一批已缓存 float32 向量，再证明 exact 等价和过滤闭包，最后才把 ANN 作为可关闭优化。
- 模块 A+B 只提供 schema、迁移、导入、exact retrieval 和全路径边界基础，不能被解释为 M3 整体完成、ANN/激活链完成、生产数据库部署或 `v0.4.0` 候选冻结。
- 默认仍禁止真实模型和付费调用；测试使用合成数据与已有缓存向量契约，不读取或上传私密 `.env`、完整私人语料或本地产物。

## M3 当前进展与下一步

- 数据模型覆盖 corpus snapshot、law/version/article、chunk 与多对多 article 映射、embedding profile、chunk embedding、导入回执、index build 和按 scope 的 active snapshot pointer；跨表约束确保 active pointer 不能指向错误租户/集合范围。
- 导入 bundle 先做 canonical 校验：ID、顺序闭包、维度、float32、有限数、关系引用、content hash、profile-sensitive bundle hash 均显式验证。重复导入必须完全匹配才幂等；冲突数据拒绝覆盖。
- importer 在单一事务和 PostgreSQL advisory lock 内运行；任一后段写入失败会整体回滚。集成测试覆盖首次导入、重复导入、并发导入、实际向量回读、维度 trigger、冲突和失败回滚。
- exact pgvector repository 使用规范化 float32/query 合同，显式把 pgvector `<#>` negative-inner-product distance 转回越大越好的 inner-product score，并稳定处理同分排序；它与现有 dense 基线做等价验证，边界由 scope、snapshot、embedding profile、法律/版本/法条 selector 和生效日期共同定义。
- typed `RetrievalBoundary` 与 `RetrievalProvenance` 绑定完整 chunk payload hash 和 article provenance；exact retrieval、RRF、rerank、adaptive、chat 与 evaluation/artifact 恢复路径均重新校验，任何边界丢失、降级、混合或篡改均 fail closed。旧五字段、真正 unbound 的 SearchResult artifact 仍可读取；携带 M3 强边界标记却缺 provenance 的旧产物会拒绝恢复。
- 第二个不可变迁移强化 snapshot/chunk/article/embedding 行约束和数据库 trigger，并随 Alembic 资源进入 wheel。CI 从 fresh database 升级到 head并运行全部 integration tests；CI 规范环境为 PostgreSQL 18 + pgvector 0.8.6，本地证据为 PostgreSQL 18.1 + pgvector 0.8.1，两者没有混写。
- 模块 B 的定向离线/合同测试 `210 passed`，本地数据库累计 `16 passed`，全量 `612 passed, 157 subtests passed`。wheel、lock、改动目标文件 Ruff 和 diff check 均通过；全仓 Ruff 仍只有既存的 `legal_rag/indexing.py:83` F841，不是模块 B 引入，不能把全仓 Ruff 宣称为全绿。
- 下一步是 M3-C：实现法名 + 条号 + 版本精确查询，以及 active snapshot 的原子激活和回滚。之后才进入 M3-D；只有 M3-T01 至 M3-T08 与累计发布门禁全部通过，才能冻结候选、创建 `v0.4.0` Tag/GitHub Release，并另行落发布回执。

## M2 启动边界

- 只实现实验分层、精确复用、回放、逐 case 原子结果、断点续评、并发 fake 隔离、分层计时与 `M2-T01` 至 `M2-T08`。
- 复用 v3 120 条评测集与固定 30 条生成子集；它们标记为 `legacy_regression`，不冒充新的盲测集。
- 默认离线、并发 1、真实模型调用关闭；dense query embedding 或 adaptive normalizer 是否产生外部调用必须由 manifest 与调用账本显式记录，不能笼统写成 retrieval-only 永远零调用。
- M2 阶段本身没有引入 PostgreSQL/pgvector、FastAPI、LangGraph、Redis/Celery、UI 或生产部署；PostgreSQL/pgvector 基础现已在后续 M3-A 引入，其他能力仍属于后续里程碑。
- 发布前必须累计通过 M0、M1 和 M2 门禁；发布 `v0.3.0` 后另以文档回执记录真实远端状态，不移动软件 Tag。

## M2 当前进展与唯一下一步

- 子任务 A 已在 `ee972a1` 完成：严格 canonical JSON、实验 manifest 身份、分阶段精确 cache key，以及 fresh/cache/replay 的 fail-closed 契约。
- 子任务 B 已在 `7bb06bf` 完成：不可变逐 case attempt/complete artifact、原子 no-clobber 发布、校验和、损坏清单、兼容 resume、retry budget 和 pending commit 恢复。
- 子任务 C 已在 `efd5f53` 完成：单 case/session work unit、group 内顺序与 group 间并发、真实 attempt 历史校验、checkpoint hash chain、进程内单 owner、provider semaphore、分层 timing/call ledger 和 replay 防绕过基础。
- 子任务 D 已分模块完成：真实 evaluation/chat adapter 与 `ConversationMemory` 恢复、精确 stage observation、纯评分、provider timeout/error 分类、冻结 dataset registry、四种 evaluation mode、artifact-only 聚合和内容寻址发布。
- 生命周期 CLI 已在 `f5f6e40` 完成：`plan / run / resume / aggregate / replay` 不加载 `.env`；offline source/replay 各 2/2 case 成功，replay actual calls 为 0，真实执行 hash 记录在 `reports/refactor/M2.md`。
- 累计 M2 gate 已在 `1fe3ddd` 完成：本地 25/25，完整 `548 passed, 157 subtests passed`，JUnit 705/0 failures/0 errors/0 skipped；同一 head 的 [CI 35685720818](https://github.com/1040942669/legal-rag-agent/actions/runs/35685720818) 成功并上传报告。
- `0.3.0` wheel/sdist 已离线构建，45/81 个归档条目，禁止路径 0；全新 Python 3.12.13 venv 从仓库外以 `-I` 验证 distribution/module/entry point 均正确。具体 size/hash 见 M2 报告与 STATE。
- 候选文件定向 Ruff、compileall、diff check 和门禁高置信凭证扫描通过。额外的全仓 Ruff 仍有本分支未修改的 `legal_rag/indexing.py` F841；它不是 M2 门禁或本次改动引入的问题，不在候选冻结中扩大为无关重构。
- 已知边界仍包括：跨进程/分布式唯一所有权未提供；生命周期命令依赖 Git 源码工作区；直接执行只覆盖 provider-free BM25；真实 provider、完整法律语料和法律质量未运行。
- 最终 PR head 的 [CI 35686885130](https://github.com/1040942669/legal-rag-agent/actions/runs/35686885130) 与 release target 的 [master CI 35687258856](https://github.com/1040942669/legal-rag-agent/actions/runs/35687258856) 均为 25/25、`548 passed, 157 subtests passed`、JUnit 705/0/0/0。两份 artifact digest 与 JSON hash 已写入 STATE、M2 报告和机器可读回执。
- M2 已完全收口，不得修改 `v0.3.0`。当前执行焦点已经转到获授权的 M3；M2 的历史证据不随 M3 分支移动。

## M1 启动边界

- 计划版本：`v0.2.0`；范围只包含验证边界、结构化回答兼容层、指标语义与 M1 累积门禁。
- 必须覆盖 `M1-T01` 至 `M1-T10`，包括免责声明/拒答分离、伪造引用、真引用但无语义支持、资料不足模式、顺序隔离、retrieval-only N/A、judge 错误、过度拒答和跨快照/权限证据。
- 默认只运行离线 fixture/fake 测试；未授权任何付费模型调用，不引入数据库、FastAPI、LangGraph、Redis/Celery 或生产部署。
- 每个可测试模块完成后 commit/push 工作分支；M1 全部通过后才冻结候选并发布。

## 已完成并核实

1. 审计真实工作区、origin、默认分支、HEAD、未提交内容、远端版本和仓库规则，保留用户原有提交、未跟踪文档与被忽略本地资料。
2. 新增完全虚构且明确不是法律文本的合成 fixture；BM25 smoke 在该测试中硬阻断 Python socket。
3. 新增 `scripts/quality_gate.py`：M0/offline 统一执行全量 pytest、合成 smoke、版本、CLI、Markdown、STATE/manifest JSON 和候选凭证风险检查；未知 stage/mode 以 2 退出。
4. 新增 PR / `master` push CI：只读权限，不使用 `pull_request_target`，不注入模型密钥，Action 固定完整 SHA，Python 固定 3.12.13，uv 固定 0.10.10。
5. 版本统一为 `0.1.1`，`pyproject.toml` 为权威来源；lock 只更新本包元数据。CLI `--help` 不再预先读取 `.env`。
6. README、评测方案、历史执行计划、结果报告和 ADR 已区分旧 Phase 与当前 M0-M7，并明确 verifier、citation 与 refusal 指标限制。
7. PR #2 最终 head `e426424b892bedd3bc1c5c13a34e94e7f1496c2f` 通过 [CI 35428009054](https://github.com/1040942669/legal-rag-agent/actions/runs/35428009054)。
8. 实际 merge commit `cb7e01982ca6fb95cebde1e5d707527fd36a250c` 通过 [master CI 35428226839](https://github.com/1040942669/legal-rag-agent/actions/runs/35428226839)。
9. 远端 annotated tag object 为 `9072726ee99c3d13903069f78b747f02a85c8093`，peeled target 为上述 merge commit；Release API/CLI 确认非 draft、非 prerelease、无附件。
10. M1 结构化回答已区分 `evidence_answer / insufficient_evidence / needs_clarification / out_of_scope`，并保留旧纯文本适配器但降低其可验证性。
11. M1 verifier 已拆分 schema、证据目录、引用 ID/对齐、可选快照/权限范围、免责声明、回答模式与语义状态；词面启发式不产生 `supported`。
12. 只有可信调用方显式注入 `VerificationContext` 时，越界证据才会在进入生成提示和返回 sources 前被过滤并再次核验。当前 CLI 没有认证身份、租户隔离或默认 active snapshot；这些能力不能提前算作完成。伪造引用或无效模式的草稿不会作为最终回答交付，安全终态会再次验证。
13. M1 评测 schema v2 已实现显式行为分母、retrieval-only N/A、Judge 成功/失败/未执行三态、历史输出防覆盖，以及生成尝试/最终交付分离的 Trace。
14. 两组完全虚构、非法律的改前/改后 fixture 已覆盖免责声明误判拒答和真实 ID 不等于语义支持；它们只证明规则边界，不是法律质量 benchmark。
15. M1 累积门禁已覆盖 7 个 M0 必需检查和 `M1-T01` 至 `M1-T10`，共 17 个唯一检查；mandatory pytest 遇到零测试、skip、xfail、JUnit 缺失或解析失败都会失败关闭。
16. GitHub Actions 已改为运行 M1 累积门禁并始终上传机器可读 JSON；产物名含 commit SHA 与 run attempt，Action 固定完整 SHA，权限仍为只读。
17. 离线门禁子进程同时设置 `ALLOW_LIVE_MODEL_CALLS=false` 与 `LEGAL_RAG_DISABLE_DOTENV=1`；Ollama、SiliconFlow、SentenceTransformer 和远程 embedding 入口会在初始化或调用前失败关闭。本次未读取或暂存被忽略的本地 `.env`。
18. 以 `647c0238674abe603fdf33e80191c19eb6de3dd9` 软件代码和定稿发布 README 构建的 `0.2.0` sdist/wheel 已通过核验；全新 Python 3.12.13 venv 离线 no-deps 安装后，distribution/module/console entry point 和模块来源均核对正确，wheel/sdist 共 88 个归档条目，禁止路径为 0。
19. PR #5 的 [Actions run 35461753394](https://github.com/1040942669/legal-rag-agent/actions/runs/35461753394) 显式检出同一 head `647c023...` 并成功；下载 JSON 确认 17/17、186 passed、148 subtests、JUnit 334/0/0/0。产物名为 `m1-quality-gate-647c0238674abe603fdf33e80191c19eb6de3dd9-1`。
20. Normalizer、结构化回答和 Judge 的模型 JSON 入口已统一拒绝重复 key、非标准非有限数值、孤立 surrogate、超限或资源异常输入；引用/拒答检查覆盖 Unicode 括号与不可见格式字符并保守失败关闭。
21. 被拒绝草稿正文和内容型 verifier 诊断不进入普通 Trace；安全 serializer 使用显式 allowlist，生成的 schema、引用片段和 source ID 列表只保留计数。Trace 仍记录用户 query、派生查询和检索 metadata，评测记录仍保存最终回答与成功 Judge comment，不能宣称全链路脱敏。
22. 最终文档 head `d2fa34776cd188197954eff9a0092c207f309b97` 在干净工作区本地再次通过 17/17 门禁，并由精确 PR-head [CI 35462691374](https://github.com/1040942669/legal-rag-agent/actions/runs/35462691374) 复验。
23. PR #5 普通合并为 `d51ed481f986dde807163f4b5583b07bc9ef6750`；对应 master push CI 复验 17/17、186 passed、148 subtests、JUnit 334/0/0/0。
24. annotated `v0.2.0` 的远端 tag object / peeled target 已交叉核对；GitHub Release API/CLI 确认发布对象非 draft、非 prerelease且无附件。
25. 机器可读发布回执为 `docs/refactor/receipts/M1.json`；它在软件 Release 之后由独立文档 PR 落库，不移动 `v0.2.0`。
26. 回执 PR #6 最终 head `11b2165396d2dc8c55c5537e33307c15a67376e2` 的 [CI 35463637086](https://github.com/1040942669/legal-rag-agent/actions/runs/35463637086) 成功；diff 只有 6 个文档文件。
27. 回执 merge `fee92b4457dc68edc413608fb3a0d263af740922` 的 master CI 再次通过 17/17、186 passed、148 subtests、JUnit 334/0/0/0；远端 Tag 仍 peeled 到软件 release target `d51ed481...`，Milestone 2 已关闭。

## 真实验证结果

- M3-A+B 最新实现 head `4bc912b09931803d53b4ca3ffdc81d302ba65813`：M3-B 定向离线/合同 `210 passed`；本地 PostgreSQL 18.1 + pgvector 0.8.1 累计 integration `16 passed`；完整回归 `612 passed, 157 subtests passed`；真实模型调用 0。
- 精确 head [CI 36045040845](https://github.com/1040942669/legal-rag-agent/actions/runs/36045040845) 的 quality gate 与 PostgreSQL 18 + pgvector 0.8.6 storage integration 两个 job 均成功；quality/storage artifact digest 分别为 `sha256:77be3dd106aabf0999c1d84519451bdaff4b7a88c3a68c32421d682702aafdca` 和 `sha256:4f06ece14c2b178552b2ccacd285aa7120d202dcf527ae0369b219eb6e5fe113`。
- M3-A+B 的 wheel 内容、lock、改动目标文件 Ruff 和 diff check 均通过；额外全仓 Ruff 仍报告既存 `legal_rag/indexing.py:83` F841。
- 修改前基线：`70 passed in 0.43s`。
- M0 候选本地统一门禁：7/7 必需检查通过；全量 89 passed；合成 smoke 1 passed。
- 最终 PR CI：89 passed，24 个 Markdown、2 个 JSON、79 个 Git candidate 文本凭证形状扫描均通过。
- lock / 环境：`uv lock --check`、`uv sync --check --offline --frozen` 均以 0 退出。
- 发布包：`uv build --offline` 成功产生 `0.1.1` sdist/wheel；wheel 在全新临时 Python 3.12.13 venv 离线安装并导入，版本核对为 `0.1.1`。
- 验收报告：`reports/refactor/M0.md`；机器可读回执：`docs/refactor/receipts/M0.json`。
- M1 累积门禁：最终 PR head `d2fa347...` 与 release target `d51ed48...` 的精确 CI 均为 17/17 通过，全量 `186 passed, 148 subtests passed`，JUnit 汇总 `334 tests, 0 failures, 0 errors, 0 skipped`；合成 smoke 与 `M1-T01` 至 `M1-T10` 均独立通过。
- M1 环境与工作流：workflow YAML 可解析，`uv lock --check` 与 `uv sync --check --offline --frozen` 均通过；未知 milestone/mode 生成配置错误报告并返回逻辑退出码 2。
- M1 最终发布内容候选包：wheel `legal_rag_assistant-0.2.0-py3-none-any.whl`，101,188 bytes，SHA-256 `c5fa5e291e17d2dc5218577af23a1880e0c27ea93abfbd2e097be19c9df4a6f7`；sdist `legal_rag_assistant-0.2.0.tar.gz`，138,515 bytes，SHA-256 `b9172b8a18a983c30dcd9893df4ec808ad31d003e6fa33bcbd943b9896708078`。
- M1 验收报告：`reports/refactor/M1.md`；机器可读发布回执：`docs/refactor/receipts/M1.json`。
- M1 最终 PR CI 产物：`m1-quality-gate-d2fa34776cd188197954eff9a0092c207f309b97-1`，GitHub digest `sha256:92611875339d1d779978ba2d94596629ea0425e4cf001e728e64dd3e6c188c71`；本地解包 JSON SHA-256 `da6e64182a64c6190c7a3f110dc11e2e6abe04d035e6d54578c9e45664ec66b5`。
- M1 master CI 产物：`m1-quality-gate-d51ed481f986dde807163f4b5583b07bc9ef6750-1`，GitHub digest `sha256:145aa5e10e5ab03d013bdbfe425af245eb565ffc2306fa09af8a2294f44b4a7f`；本地解包 JSON SHA-256 `6c3f0097aceecefcec58a1a8e95a399b512995555f94b5af1929bf9c9b3044b9`。

## 没有做的事情

- 没有运行在线或本地生成模型、Embedding、reranker、LLM judge、真实法律语料实验或付费调用。
- 没有读取/上传 `.env` 值、完整语料、Embedding cache、私人课程/简历/面试材料或大型产物。
- 已在 M3 工作分支引入可选 PostgreSQL/pgvector 基础，但没有生产部署、完整语料迁移、FastAPI、LangGraph、Redis/Celery 或 M4 以后的能力。
- M1、M2 软件、发布回执和 Milestone 收尾均已完成；没有重复发版或移动既有 Tag。M3 只有模块 A+B 通过，尚未合并或发布，也没有 M3 发布回执。
- 本地门禁 JSON、下载的 CI JSON 与包构建物只保存在被忽略的本地 `.tmp`；它们没有进入 Git 或 Release 附件。GitHub Actions 产物按平台保留策略远端保存。
- 没有把历史 203 部法律或模型质量数字冒充 M0 新实测。

## 已知限制

- M0 证明离线工程基线，不证明真实法律问答质量或生产就绪。
- 当前 verifier 已能严格检查结构、引用目录/对齐和可选范围，但默认语义层仍只有 `uncertain/not_checked`；它不证明 claim-source 语义支持或法律正确性。
- 旧 `Refusal correctness` 仅在兼容输出中保留；新 schema 使用拒答召回与过度拒答的独立分母，不能与旧总均值直接比较。
- 项目声明 Python `>=3.10`，M0 必需门禁只固定验证 Python 3.12 系列。
- 凭证形状扫描不是绝对无泄漏保证，候选另经人工文件清单与完整 diff 审查。
- GitHub CI 安装锁定依赖时可以联网；门禁子进程使用离线依赖模式、禁用 dotenv 并在项目 provider 边界拒绝真实模型调用，合成 smoke 另在 Python 进程内阻断 socket，但仍不是 runner 的 OS 级 air-gap。
- 非阻塞 P2：snapshot 在 repository preflight 之后、encoder 调用之前被 archive 时，可再增加一次 encoder 前 preflight 以更早停止无效编码。当前数据读取仍由数据库边界 fail closed，且 bound online 路径只允许本地可验证的 SentenceTransformer 编码器，因此这不是越权读取缺口，也不阻塞模块 B 验收。
- 非阻塞 P2：多父 snapshot 的数据库 trigger 在未来可对父行锁查询使用稳定 `ORDER BY`，进一步降低并发事务以相反顺序取锁时的死锁概率。现有不可变约束、正确性测试和集成测试均已通过，该项属于后续并发稳健性加固。

## 回滚

- 代码：发布后如需撤销，通过新的 revert PR 回滚 M1 merge commit `d51ed481f986dde807163f4b5583b07bc9ef6750`，不强推、不重写历史。
- 数据：本版没有数据库或语料迁移，无数据回滚动作。
- 任务/服务：本版没有队列、后台任务或生产部署，无运行中任务需要恢复。
- 版本：已发布 `v0.1.1` 与 `v0.2.0` 均不移动、不复用；修复使用后续版本。

## M0 回执收尾核对

1. 回执 PR #3 已正常合并，最终 head 为 `be34c68b2bd9f5bcc1405ec1437b30d0444fe420`。
2. 回执 merge SHA 的 `master` CI 已成功，Milestone 1 已关闭。
3. M0 不再有远端收尾动作，不得重复发版或移动 `v0.1.1`。

## 执行事实表

| 项目 | 当前值 |
|---|---|
| 当前里程碑 | M3 / `in_progress`；模块 A+B 已验收并推送，未合并、未发布 |
| M3 branch / base | `codex/m3-storage-snapshots` / `6cd07d982ffeecec3632ff47c8473118585a9693` |
| M3 Issue / Milestone / PR | [#12](https://github.com/1040942669/legal-rag-agent/issues/12) / [Milestone 4](https://github.com/1040942669/legal-rag-agent/milestone/4) / draft [PR #13](https://github.com/1040942669/legal-rag-agent/pull/13) |
| M3-A+B implementation / CI | `4bc912b09931803d53b4ca3ffdc81d302ba65813` / exact-head [run 36045040845](https://github.com/1040942669/legal-rag-agent/actions/runs/36045040845)；2/2 jobs success |
| M3-B 测试 | 定向离线/合同 210 passed；本地 PostgreSQL 18.1 + pgvector 0.8.1 integration 累计 16 passed；完整回归 612 passed + 157 subtests；CI PostgreSQL 18 + pgvector 0.8.6 |
| M3 CI artifacts | quality `sha256:77be3dd106aabf0999c1d84519451bdaff4b7a88c3a68c32421d682702aafdca`；storage `sha256:4f06ece14c2b178552b2ccacd285aa7120d202dcf527ae0369b219eb6e5fe113` |
| M3 Tag / Release | 未创建；`v0.4.0` 仍是计划版本 |
| M2 branch / base | `codex/m2-experiment-lifecycle` / `00ec3ad5b193a7442e427486e8bfade2dbabd482` |
| M2 Issue / Milestone | [#8](https://github.com/1040942669/legal-rag-agent/issues/8) / [Milestone 3](https://github.com/1040942669/legal-rag-agent/milestone/3) |
| M2 PR / Tag / Release | [PR #9](https://github.com/1040942669/legal-rag-agent/pull/9) merged / `v0.3.0` / [published](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.3.0) |
| M2 最终 PR head / release target | `170da868831e2730ea15d56ed88ad24b1159e67b` / `da7023a659672121fd772364e475870a0167e1be` |
| M2 PR / master CI | [run 35686885130](https://github.com/1040942669/legal-rag-agent/actions/runs/35686885130) / [run 35687258856](https://github.com/1040942669/legal-rag-agent/actions/runs/35687258856) / success |
| M2 子任务测试 | A/B/C/D/G 均已推送或进入本候选；M2-T01 至 T08 共 19 passed；整仓 548 passed + 157 subtests；25/25 累计门禁 |
| M2 package | `0.3.0` wheel 217,926 bytes / `cada4f6c...`；sdist 309,954 bytes / `3d6a0579...`；隔离安装通过 |
| 软件分支 / 回执分支 | `codex/m1-verification` / `codex/m1-release-receipt` |
| 最终 PR head / release target | `d2fa34776cd188197954eff9a0092c207f309b97` / `d51ed481f986dde807163f4b5583b07bc9ef6750` |
| M1 PR / Tag / Release | [PR #5](https://github.com/1040942669/legal-rag-agent/pull/5) / `v0.2.0` / [published](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.2.0) |
| M1 回执 PR | [PR #6](https://github.com/1040942669/legal-rag-agent/pull/6) / merged；final head `11b2165...` / merge `fee92b4...` / PR 与 master CI success |
| M1 精确候选测试 | final PR head 与 release target 均为 186 passed，148 subtests passed；JUnit 334/0 failures/0 errors/0 skipped；无真实模型或语料调用 |
| M1 累积门禁 | PR/master 均 17/17 passed（含 M0 7 项 + M1 10 项） |
| M1 package | `0.2.0` verified build + Python 3.12.13 isolated install/import passed；hash 见上文 |
| M1 PR / master CI | [run 35462691374](https://github.com/1040942669/legal-rag-agent/actions/runs/35462691374) / [run 35462786212](https://github.com/1040942669/legal-rag-agent/actions/runs/35462786212) / success |
| M1 Issue / Milestone | [#4](https://github.com/1040942669/legal-rag-agent/issues/4) / [Milestone 2](https://github.com/1040942669/legal-rag-agent/milestone/2) |
| M2 receipt | [PR #10](https://github.com/1040942669/legal-rag-agent/pull/10) merged；final `4ba8282...` / merge `6decc0f...` / final PR 与 master CI success |
| 阻塞 | 无 |
| 下一条可执行动作 | 在 draft PR #13 继续 M3-C：法名 + 条号 + 版本精确查询及 active snapshot 原子激活/回滚；再做 M3-D，M3-T01 至 T08 与累计门禁全通过后才冻结/发布 |

## 完成说明

`v0.3.0` 的 `release_target_sha` 固定为 `da7023a659672121fd772364e475870a0167e1be`。即使 `master` 已因回执 merge 前进到 `6decc0f2e675300f1a175f7ab49c85852d35b38d`，annotated Tag 仍 peeled 到软件 target。M2 已完成；M3 正在 draft PR #13 中分模块推进，目前模块 A+B 已获得本地与精确 head CI 证据并推送，但 M3 整体仍为 `in_progress`，尚未合并、冻结候选、打 Tag、发布或创建发布回执。
