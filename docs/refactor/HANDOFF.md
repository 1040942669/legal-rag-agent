# M6 执行交接

> 当前状态以最前面的“当前事实”及 `STATE.json` 为准。后续 `v0.7.0` 回执待办、发布前候选及 M5 内容均保留为历史快照，不能把其旧待办或绿灯当作当前 `v0.7.1` 补丁的状态。

## 当前事实：M6 运行时观测补全进行中

- 当前里程碑仍为 M6，整体状态 `in_progress`。`v0.7.0` 软件与其独立回执已经完成；此次 `v0.7.1` 补丁已连接 `MASTER_PLAN.md` §11.6 的实际运行观测机制并通过本地运行路径回归，补丁状态为 `ready_for_review / pending_exact_final_ci`。最终候选门禁、发布、独立回执及治理尚未完成，因此不关闭 M6，也不把旧门禁通过等同于完整 M6 完成。
- 当前工作分支 `codex/m6-observation-completion` 从已核验的 `origin/master` `0dcafb87dd8537d66f6486febf73fd0d1258b0ed` 创建。首个已冻结本地基线 `10004b4f7b992f6a192d9b6674108bcea3f7b06d` 已 commit、正常 push，并建立 open 的 [Draft PR #28](https://github.com/1040942669/legal-rag-agent/pull/28)。该 SHA 只标识首个本地验证基线；本次文档更新尚未提交，不自引用未来提交 SHA，最终候选 SHA 应在冻结后读取并在后续证据中记录。
- 软件补丁计划为 `v0.7.1`，用于完成 M6 运行时观测及真实性修正，不是只为文档生成软件版本。已经发布的 `v0.7.0` 和 M5 `v0.6.0` 保留原 Tag、目标提交和 Release，不移动或重复发布。
- 原确认缺口包括：typed Observation 的预算、重试、缓存、证据、模型用量与耗时字段主要只在人工构造测试中出现，M2 工件和 M5 在线 run 缺少统一关联，以及幂等 API 重放可能把既有其它状态的 job 误记成 queued。实际调用点和可信持久事实已经连接，并新增运行路径回归；这些机制在本地已验证，仍须通过最终精确候选四路 CI，不提前声称发布完成。
- 首个冻结基线包含 jobs dispatcher/handlers、API 装配与状态事件、M5 graph/node/runner 观测、M2/local Trace 关联、provider 用量可用性以及对应观测回归。本次只补记 `STATE.json` 与本文件的当前事实；主执行流程负责冻结最终代码及文档候选和收口最终证据。
- [Issue #25](https://github.com/1040942669/legal-rag-agent/issues/25) 与 GitHub [Milestone 7](https://github.com/1040942669/legal-rag-agent/milestone/7) 于 `2026-10-02` 只读复核仍 open，Milestone 有 1 个 open issue。两者在完整补丁和独立回执通过之前保持 open；M7 未开始，也不在本次授权范围。

## 已核验的 v0.7.0 软件与独立回执

- 原软件 [PR #26](https://github.com/1040942669/legal-rag-agent/pull/26) final head `b9400ab289618707a53ee6b14f6ac1cee4af2ee1` 和 release target `28517b6f323253baf638ba60c887d10630dd0bf1` 各自的四路 CI 均成功。annotated `v0.7.0` object `f40715c16e90b429a7c49a6347092114fefc787d` 固定 peeled 到该软件提交，正式 [Release](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.7.0) 已于 `2026-09-29T10:39:52Z` 发布，非 draft、非 prerelease。
- 独立回执 [PR #27](https://github.com/1040942669/legal-rag-agent/pull/27) 的 final head `5d20b4256582b36159333b9998844d148b104d01` 经 [CI 36559972128](https://github.com/1040942669/legal-rag-agent/actions/runs/36559972128) 四路 success，于 `2026-09-29T11:21:13Z` 普通 merge 为 `0dcafb87dd8537d66f6486febf73fd0d1258b0ed`。该精确 merge-target [master CI 36561210905](https://github.com/1040942669/legal-rag-agent/actions/runs/36561210905) 亦为 offline、M4 service、M5 fault、M6 worker 四路 success；上述 GitHub 元数据在 `2026-10-02` 再次只读核验。
- final receipt head/master artifact 均有 `58/58` mandatory passed、exit 0、worker JUnit `41/0/0/0`，以及 `0.7.0` wheel 隔离 smoke passed。master M6 artifact digest 为 `sha256:1d1cf3e74c428b8ac82bfee22fe80bb6404c19615993db3d4d9c8929ca70e898`，内部 gate JSON SHA-256 为 `b963528a81f26ee3b9d1253c071988c2e9e2132fed2f2214fd14485d4c3a7df7`；下载的 worker 回执精确绑定 `0dcafb87...`。完整 final-head/master 摘要及内部文件哈希补入 [M6 回执](receipts/M6.json)。这些是历史已发布机制的证据，不覆盖新的未提交补丁。
- 修正了历史回执中软件 master M5 artifact digest 少一个末尾字符的抄写错误，GitHub Actions API 核验正确值为 `sha256:1bfbc884675b77bcf9e62df295431e318658ca0cd75c1b893821d15348a94d4c`；没有改动软件、测试结果或任何远端发布对象。

## 当前验证边界与下一条可执行动作

新的 `v0.7.1` 定向 jobs/API/M2 回归 `40 passed in 33.57s`，隔离 PostgreSQL durable 回归 `27 passed in 8.23s`，旧位置参数与打包检查 `8 passed in 0.54s`。本地 commit 前真实 wheel 为 `0.7.1`、440990 bytes、SHA-256 `1526add72ce32c5ec8db92fb5aa6fc4e886198328241bdbff5bae4645fc66028`；M6 仓库外隔离 smoke passed，`source_checkout_isolated=true`，新增模块导入及 migration head `0007_m6_jobs_outbox` 均通过。该 wheel 是提交前本地临时证据，不是最终 CI wheel。

首个冻结本地基线 `10004b4f7b992f6a192d9b6674108bcea3f7b06d` 的 M0 离线基础门禁已真实通过：`2026-10-02T10:47:05Z` 至 `10:49:22Z`，status `passed`、exit 0、`7/7` mandatory、136510 ms；内部全量 `1013 passed, 157 subtests passed in 132.70s`，JUnit `1170/0/0/0`。结果保留在 ignored `.tmp/m6-observation-committed-m0-gate.json`，不是本次尚未提交文档的最终候选证据。

此前全量门禁的旧生成元数据、可编辑安装文件清单及运行期间 tracked `diff_hash` 漂移失败均保留在 [补丁报告](../../reports/refactor/M6-observability-patch.md)，没有弱化断言。测试期间不得修改任何 Git 候选文件，包括文档，因为 M2 身份绑定整个 tracked diff，而不只运行时代码。

[初始 PR CI 36997422129](https://github.com/1040942669/legal-rag-agent/actions/runs/36997422129) 启动在首个冻结基线 `10004b4...`，记录时仍在运行；即使该轮通过，也不能代替本次最终文档 head 的四路 CI。最终候选尚未冻结，精确 final-head CI、merge/master CI、Tag/Release、独立回执及治理均未完成，不得复制旧 `58/58`、`41/0/0/0` 或旧 wheel SHA 作为补丁通过证据。

下一步先冻结完整代码和本次文档候选，核验适用本地门禁及 PR #28 精确 final head 四路 CI，再按 §13 的 review/保护规则检查、普通 merge、精确 master CI、annotated `v0.7.1` 和 Release、独立回执及治理收口顺序推进。任一门禁失败记录真实状态和失败证据；不强推、不 admin merge、不绕过 review。Issue #25/Milestone 7 在完整链路通过前保持 open。真实或付费模型、远端 Langfuse 实发、私人资料和未授权语料均未获启用。完成 M6 后停止，不进入 M7。

---

## 历史：v0.7.0 软件已发布，独立回执待完成

- M6 软件状态为 `released_receipt_pending`，不是发布失败，也尚不能把独立回执和 Issue/Milestone 治理写作完成。M6 从已核验的 M5 finalization `origin/master` `76a038936ddfd900f98ad8709fedcc50c07063d3` 开始；M5 `v0.6.0` Tag/Release 保持不变。
- 软件分支 `codex/m6-async-jobs` 的 [PR #26](https://github.com/1040942669/legal-rag-agent/pull/26) 最终 head 为 `b9400ab289618707a53ee6b14f6ac1cee4af2ee1`；[精确 head CI 36553279892](https://github.com/1040942669/legal-rag-agent/actions/runs/36553279892) 的 offline、M4 service、M5 fault、M6 worker 四路全部成功。PR 于 `2026-09-29T10:19:15Z` 普通 squash 合并，实际 merge commit 为 `28517b6f323253baf638ba60c887d10630dd0bf1`。该精确 master commit 的 [CI 36554828645](https://github.com/1040942669/legal-rag-agent/actions/runs/36554828645) 同样四路全部成功。
- 远端 annotated `v0.7.0` Tag object 为 `f40715c16e90b429a7c49a6347092114fefc787d`，peeled target 为上述软件 merge commit `28517b6...`；[GitHub Release v0.7.0](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.7.0) 于 `2026-09-29T10:39:52Z` 发布，已核验非 draft、非 prerelease，附件 0。Tag 不随回执文档提交移动，也不重复创建 Release。
- 独立回执工作分支 `codex/m6-release-receipt` 从已发布的 `origin/master` `28517b6...` 创建，仅修改文档。首个回执候选 `09bc3713c901e9ea11f48cda0fdb6b3ec441ae2b` 已 commit、正常 push，建立 [Draft PR #27](https://github.com/1040942669/legal-rag-agent/pull/27)；本次补记会产生新的最终文档 head，因此不得把首轮 CI 代替该最终 head、回执 merge 或 master CI。
- 跟踪 [Issue #25](https://github.com/1040942669/legal-rag-agent/issues/25) 和 GitHub [Milestone 7](https://github.com/1040942669/legal-rag-agent/milestone/7) 仍 open；应在独立回执正常合并且精确 merge-target master CI 成功后再关闭。M7 尚未开始，也不在本次范围。
- 本次授权覆盖在门禁和 review/保护规则满足时的正常 commit、push、PR、合并及发布。没有强推、admin merge 或 review 绕过；默认禁用真实或付费模型调用，不上传凭证、私人资料或未授权语料。

## 当前验证与可证明边界

- 发布目标 master run `36554828645` 的 M6 artifact 记录累计门禁 `58/58` mandatory passed、0 failed、exit 0；真实 PostgreSQL/Redis/Celery worker JUnit `41/0/0/0`，M6 `0.7.0` wheel 隔离安装/smoke passed，`live_model_calls=false`。M6-T01 至 T05、T07 是真实服务场景；T06 是 provider-free 观测单元。PR head `b9400ab...` 的四路 CI 也通过。具体证据、复现、早期失败和不运行项见 [M6 验收报告](../../reports/refactor/M6.md)，回执中应绑定精确 master artifact digest/文件哈希。
- 本地已执行 M0 离线基础门禁 `7/7` mandatory passed，内部全量 `977 passed, 157 subtests`、JUnit `1134/0/0/0`；安全修正后的隔离 PostgreSQL suite 曾 `44 passed`，最后定向 claim 隔离测试加入后按 CI 顺序 M6 数据库 suite `24 passed`。更早 code-frozen head `ed0c980...` 的四路 CI 也是通过，但不能代替最终软件 PR head 或 master 证据。
- 独立文档回执候选本地 M0 基础门禁再次 `7/7` mandatory passed、exit 0、109620 ms；内部全量 `977 passed, 157 subtests`、JUnit `1134/0/0/0`，35 个候选 Markdown 链接、STATE/manifest 与 244 个 Git-candidate 文本文件秘密形态检查通过。这不代替回执 PR 最终精确 head CI；临时结果保留在 ignored `.tmp/m6-receipt-m0-gate.json`。
- [回执 PR #27 首轮 CI 36558052677](https://github.com/1040942669/legal-rag-agent/actions/runs/36558052677) 对精确 head `09bc3713c901e9ea11f48cda0fdb6b3ec441ae2b` 四路全部 success：offline、M4 service、M5 fault、M6 worker。四个远端 artifact digest 与内部 M6 文件哈希写入 `docs/refactor/receipts/M6.json`；它仍是首轮候选证据，当前补记后的最终文档 head 需要重新跑 CI。
- 未运行真实法律语料/在线模型质量、人工法律评审、付费模型实验、生产部署及生产容量/SLO；因此本版只声称工程机制的 provider-free 验证，不声称法律回答质量或生产效果。隔离本地 PostgreSQL 55436 已停止，既有 5432 未触及；工具策略两次拒绝删除精确 ignored `.tmp/m6-pg-b8853e7d36d6` 与 `.tmp/m6-restart-receipt-b8853e7d36d6.json`，本地临时产物仍在且未上传。
- 已知可靠性边界：激活、item 与 job terminal 在同事务并受 lease/epoch/cancel fence；默认每 job 最多自动生成 5 条 outbox 记录并退避，耗尽时状态 `queued/delivery_unconfirmed`。同一 pending row 的未确认 broker 发送仍可重试，这不是发送调用硬上限。若预算耗尽且 broker 消息全失，需授权运维人工对账；不提供普通用户无限重投，不承诺 exactly-once。

## 下一条可执行动作

核验本次回执证据补记后的最终 PR #27 文档 head 四路精确 CI，核查 review/保护规则后正常合并，并核验回执 merge-target master CI。随后关闭 Issue #25/Milestone 7，补写真实治理事实；非递归 finalization PR 仍必须正常通过自己的门禁，不生成软件版本或移动 `v0.7.0`。任何一步未完成都保留 `released_receipt_pending`，不得再发一次 Release。完成本次 M6 范围后停止，不进入 M7。

---

## 历史：M6 发布前候选交接

## 当前事实

- M6 `v0.7.0` 在 `codex/m6-async-jobs` 已形成代码冻结候选，状态 `ready_for_release`，不表示已合并或发布。基线是已核验 `origin/master` `76a038936ddfd900f98ad8709fedcc50c07063d3`。该提交合并了 M5 finalization PR #24，[master CI 36504995613](https://github.com/1040942669/legal-rag-agent/actions/runs/36504995613) 三项全部成功。M5 `v0.6.0` 仍已发布，Tag 不移动，也不为 M5 新增回执。
- M6 跟踪 [Issue #25](https://github.com/1040942669/legal-rag-agent/issues/25) 和 GitHub [Milestone 7](https://github.com/1040942669/legal-rag-agent/milestone/7)。[Draft PR #26](https://github.com/1040942669/legal-rag-agent/pull/26) 的代码冻结 head 为 `ed0c980c8f6a5bf126c10d8876e49342f4403444`，[四路精确 head CI 36550803746](https://github.com/1040942669/legal-rag-agent/actions/runs/36550803746) 全绿。先前 `a7f4bc5...` 的 [CI 36546785727](https://github.com/1040942669/legal-rag-agent/actions/runs/36546785727) 也为四路成功，但早于激活 fence 与有界 outbox 重投修正，不能替代新候选门禁。安全修正提交为 `fde7f455ac3b43036887ee6c547cdf30e46c6318` 和 `6baaffa9b5ade8f2a1a27b1228ba6624501406d6`。本次交接文档后续提交无法自引用自身 SHA，仍需在该文档最终 head 重跑精确 CI。M6 尚无 Tag 或 Release。
- 本次授权：在门禁和仓库 review/保护规则满足时可 commit、push、创建 PR、正常合并并发布；不能强推或绕过 review。默认禁用 live/paid model，不上传凭证、私人资料、未授权语料。

## 实施中的 M6 文件与边界

- `legal_rag/jobs/`：服务端注册引用、PostgreSQL job/item/outbox、dispatcher、Celery worker 与 M2/M3 业务 handler；新增 `legal_rag/storage/alembic/versions/0007_m6_jobs_outbox.py` 和对应中央 schema metadata。
- `legal_rag/api/app.py`、`schemas.py`、`command.py`：owner-scoped 202 创建、状态/取消，`LEGAL_RAG_JOB_REGISTRY_PATH` opt-in 装配与 M6 schema readiness；不把在线 M5 graph 节点入队。
- `legal_rag/observability/`：typed local observation、默认关闭且需明示授权的脱敏 Langfuse exporter；失败不能改变主任务。
- `scripts/quality_gate.py`、`.github/workflows/quality-gate.yml` 与 M6 专项测试：58 项累积 gate 和独立 Linux PostgreSQL/Redis/Celery job 已在代码冻结 head 通过，最终文档 head 仍须重新验证；M6 在合并及发布前仍不是 `released`。
- `README.md`、`CHANGELOG.md`、`docs/README.md`、`reports/refactor/M6.md`、`decisions/ADR-004`：任务流程、可证明范围、配置、回滚和真实证据。

## 当前验证与待办

- 本地全量离线 `uv run --offline --frozen --no-sync pytest -q tests`：首轮 `976 passed, 157 subtests passed in 99.36s`；M5 wheel 工作流版本修正后的工作区复测 `976 passed, 157 subtests passed in 95.27s`，exit 0。聚焦工作流/门禁单元 `102 passed in 0.78s`。这些是修正前的本地历史记录，不代替后述 `ed0c980...` 精确 CI。
- 修正后本地 M0 累计基础门禁 `7/7` mandatory passed，exit 0，耗时 97829 ms；其中再次执行全量离线测试，Markdown 链接、STATE/manifest 和 Git-candidate 秘密形态扫描全部通过。门禁 JSON 保存在 ignored `.tmp`，不上传。
- 隔离 PostgreSQL 18.1/pgvector 0.8.1 集成：M6 store 初轮 9/9；修复中央 Alembic metadata 后，M6 store + M5 schema + M3 migration `22 passed in 18.84s`；加测 rollback 与 downgrade guard 后 store 13/13。累计 DB suite 在真实独立服务重启前 `37 passed in 22.93s`、重启后 `37 passed in 20.15s`；`m3_restart_probe` prepare/实际 stop/start/new-process verify 全部 exit 0。旧 M4 current-head 断言更新为 0005→0006→0007 后，聚焦 unit 27/27。既有 5432 未触及。
- 保留失败：初次跨迁移 2 failed/10 passed，原因是 0007 表缺中央 metadata；旧 M4 schema current-head 断言初次 1 failed/26 passed。均已定向修复并复测。完整细节见 [M6 报告](../../reports/refactor/M6.md)。
- 首轮精确 head [CI run 36544268358](https://github.com/1040942669/legal-rag-agent/actions/runs/36544268358)：offline、M4、M6 三路 success；M6 真实 Redis/Celery JUnit 32/0/0/0 覆盖 T01-T05/T07，T06 观测单元为 6/0/0/0，58/58 累计 gate、真实 PostgreSQL service restart 和隔离 0.7.0 wheel probe 通过。T04 是不可达 loopback Redis 地址的真实连接失败/恢复，并非停机共享 Redis 容器。M5 job 在 wheel probe 因固定 `--expected-version 0.6.0` 与 0.7.0 候选冲突而失败；随后改为读取候选版本并在第二轮重跑通过。首轮整体 3/4，不是发布门禁通过。
- 第二轮精确 head `a7f4bc5...` 的 [CI run 36546785727](https://github.com/1040942669/legal-rag-agent/actions/runs/36546785727) 四路 success；M5 0.7.0 兼容 wheel probe、M6 32/0/0/0 真实 worker JUnit、58/58 累计 gate 均通过。但这轮发生在发布前复核发现的两处风险修正之前，不能作为后续新代码的门禁。
- 代码冻结精确 head `ed0c980c8f6a5bf126c10d8876e49342f4403444` 的 [CI run 36550803746](https://github.com/1040942669/legal-rag-agent/actions/runs/36550803746) 已核验四路 success：offline、M4、M5、M6。M6 artifact ID `11025178041` 中累计 gate `58/58`，真实 PostgreSQL/Redis/Celery worker JUnit `41/0/0/0`，M6 `0.7.0` wheel 隔离安装与 smoke 成功，`live_model_calls=false`。M6-T01 至 T05、T07 为真实服务场景，T06 为 provider-free 观测单元。此结果覆盖激活 fence、有界 outbox 及定向领取测试，但后续仅文档提交会产生新 PR head，必须再跑四路门禁。
- 已 push 的 `ed0c980...` 代码冻结 head 在本地再次运行 M0 离线基础门禁：7/7 mandatory passed，exit 0，103264 ms；内部全量 `977 passed, 157 subtests`，JUnit `1134/0/0/0`，链接、STATE 与 243 个 Git-candidate 文本文件秘密形态扫描通过。该本地结果同样不能代替最终文档 head 的远程 CI。
- 安全修正本地初验：`integration_tests/test_m6_outbox_bounded_recovery_db.py`、`test_m6_handlers_db.py`、`test_m6_job_store_db.py` 与 M3 catalog/M5 schema/M3 migration 在隔离 PostgreSQL 55436 随机数据库合计 `44 passed in 19.78s`。最终导入激活改为同事务 job lease/epoch/cancel fence、pointer/item/terminal 提交；每个 job 默认最多自动创建 5 条 outbox 记录并指数退避，耗尽时 `queued/delivery_unconfirmed` 可见，迟到 worker 领取会清 warning；同一 pending 记录的未确认发送仍可重试。若所有 broker 消息丢失，尚需授权运维人工恢复，不能声称无限自动续跑。
- 安全修正后的 M0 本地基础门禁两轮均 7/7 mandatory passed；第二轮内部全量 `977 passed, 157 subtests`、JUnit 1134/0/0/0、STATE/链接/243 个 Git-candidate 文件秘密形态检查通过。该全量轮次早于最后的定向 outbox 领取隔离测试与文档精度修正；之后聚焦 unit `110 passed`、按 CI 文件顺序 M6 store/handler/outbox 数据库 suite `24 passed in 5.94s`，最终候选仍需精确 head CI。新门禁测试曾因误用 `scripts` 包导入得到 `1 failed, 102 passed`，改为既有文件加载方式后 `103 passed`；失败未当作通过证据。
- 尚未完成：最终文档 head 的四路 exact-head CI、review/仓库规则核验、普通 merge、精确 merge-target master CI、`v0.7.0` Tag/Release 与独立回执。隔离本地 cluster 经 data_directory/端口/PID 三重核对后再次正常停止，`pg_isready` 为 no response；既有 5432 未触及。工具策略此前两次拒绝清理精确 `.tmp/m6-pg-b8853e7d36d6` 和 `.tmp/m6-restart-receipt-b8853e7d36d6.json`，忽略的本地临时产物仍在，未上传。

## 下一条可执行动作

核对 PR #26 仅文档候选新精确 head 的四路 CI。全部成功后，按 `MASTER_PLAN.md` §13 的 review、普通 merge、精确 master CI、Tag、Release、独立回执顺序推进。若 CI、review、schema 或测试任一门禁不满足，停在真实状态，不造发布声明。M7 不在本次范围。

---

## 历史：M5 执行交接

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
