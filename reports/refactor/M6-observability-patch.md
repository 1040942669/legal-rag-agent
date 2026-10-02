# M6 v0.7.1 运行时观测修补验收报告

## 当前状态与范围

软件已正式发布，完整 M6 为 `released_receipt_pending`。补丁分支 `codex/m6-observation-completion` 从实时核验的 `origin/master` `0dcafb87dd8537d66f6486febf73fd0d1258b0ed` 创建；不使用过时本地 master。该基线是独立回执 PR #27 的正常 merge，精确 [master CI 36561210905](https://github.com/1040942669/legal-rag-agent/actions/runs/36561210905) 四路通过。[PR #28](https://github.com/1040942669/legal-rag-agent/pull/28) 的首个冻结代码提交为 `10004b4f7b992f6a192d9b6674108bcea3f7b06d`，最终文档 head 为 `d6fc26882237ca149ab38c7e944b32a260e0430b`。最终 head 重新通过自身本地门禁及四路 CI，未沿用修改前的绿灯。PR 于 `2026-10-02T11:13:49Z` 正常 squash 合并为 `582eb8949c1150fc7a12761bd46fbda9c173ef62`，与候选树相同；该实际目标的 master CI 亦四路 success。

此补丁仅完成 MASTER_PLAN §11.6 的原 M6 观测要求，不进入 M7。已发布 `v0.7.0`/`v0.6.0` 均保持原 Tag 目标，完整历史任务可靠性与发布证据保留在 [M6 原验收报告](M6.md) 和 [M6 回执](../../docs/refactor/receipts/M6.json)。annotated `v0.7.1` object `a4d7c84087ba32ad183efd20275778f5f473bd9c` 固定 peeled 到软件目标 `582eb894...`；[Release](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.7.1) id `401759927` 于 `2026-10-02T11:30:49Z` 正式发布，非 draft、非 prerelease，0 assets，targetCommitish 为完整目标 SHA。新 [发布回执](../../docs/refactor/receipts/M6-v0.7.1.json) 单独走文档门禁，不为了回执移动 Tag 或再造软件版本。

## 触发问题与修补

- 丰富字段原先主要由单元测试手工构造，未证明生产链路记录实际耗时、预算、缓存、证据和用量。
- 幂等 POST 返回既有终态 job 时，旧代码无条件发出 queued 观测。
- 旧 dispatcher retry_count 默认零，成功投递可能再次误报 business enqueue。

修补在实际 worker/item/stage、M2 已验证 attempt、M5 节点和外部动作边界记录事实。job/experiment/run/session 关联来自可信冻结状态；独立 case 不虚构 session。当前读取与历史计算分开，模型调用、预留预算与 provider token coverage 分开，未知 token 和费用保留 null。Exporter 白名单和默认关闭不变。ADR 的 T06 表述已与 provider-free 脱敏/失败隔离的实际验收边界对齐。

## 已执行验证

| 记录 | 实际命令或范围 | 结果 |
|---|---|---|
| Harness 首次红灯 | 新 `test_m6_harness_observation.py` 在实现前运行 | collection error：新观测模块尚不存在，未当作 passed |
| job/API 首轮行为红灯 | 幂等终态、耗时、重试、复用等新增断言 | `10 failed, 4 passed`；成功投递断言另有 `1 failed, 9 passed` |
| Harness/既有 M5/provider 回归 | `pytest -q tests/test_m6_harness_observation.py tests/test_m5_graph_runtime.py tests/test_m5_retry_policy.py tests/test_m2_provider_clients.py` | `104 passed in 2.04s`，exit 0；此轮早于随后加强的 dispatch/coverage 断言 |
| jobs/API/M2 bridge/观测组合 | provider-free 七组定向 suite | `45 passed in 23.48s`，exit 0；随后真实 bridge 调用点 suite `10 passed in 6.06s` |
| 新 Harness PostgreSQL 实测 | `pytest -q integration_tests/test_m6_harness_observation_db.py`，独立 PostgreSQL 18 loopback cluster、guarded 随机测试库 | `3 passed in 3.48s`，exit 0；实际 graph 节点/证据/预算、超时重试、完整 fake usage、sink failure |
| 新旧 M6 durable 回归 | 上述新 Harness DB suite，加 handlers/store/outbox 三文件 | `27 passed in 8.23s`，exit 0；JUnit `.tmp/m6-observation-durable-junit.xml` |
| 新 Harness/gate/API 定向 | `pytest -q tests/test_m6_harness_observation.py tests/test_m6_quality_gate.py tests/test_m6_api_jobs.py` | `26 passed in 0.86s`，exit 0 |
| 最终 jobs/API/M2 定向 | API、jobs、worker、M2 bridge 四组 | `40 passed in 33.57s`，exit 0；增加了完整历史、完成指针、output/usage 一致性验证 |
| 旧用量构造兼容性 | 既有七个位置参数和不变 snapshot | 新断言先 `1 failed`，修正新增字段顺序后与打包断言共 `8 passed in 0.54s`，exit 0 |
| 本地 wheel 隔离 smoke | `release_wheel_probe.py`，M6，实际安装、CLI、模块与 migration | passed，version `0.7.1`，101 entries，440990 bytes，SHA-256 `1526add72ce32c5ec8db92fb5aa6fc4e886198328241bdbff5bae4645fc66028`；仅本地 commit 前证据，不替代最终 CI wheel |
| 冻结代码提交完整离线门禁 | `quality_gate.py --milestone M0 --mode offline`，commit `10004b4`，Windows/Python 3.12.13，UTC `2026-10-02T10:47:05Z` 至 `10:49:22Z` | `7/7 passed`，exit 0，136510 ms；`1013 passed, 157 subtests passed in 132.70s`，JUnit `1170/0/0/0`；版本、CLI、smoke、Markdown links、STATE 与 secret scan passed |
| 最终文档 head 完整离线门禁 | 同命令，commit `d6fc268`，UTC `10:53:47Z` 至 `10:55:51Z` | `7/7 passed`，exit 0，123567 ms；`1013 passed, 157 subtests passed in 119.85s`，JUnit `1170/0/0/0` |
| 精确最终 PR head CI | [run 36998001012](https://github.com/1040942669/legal-rag-agent/actions/runs/36998001012)，Linux/Python 3.12.13，exact `d6fc268` | 四路 success；累计 `25/25`、`41/41`、`51/51`、`58/58`，均 exit 0；M6 JUnit `76/0/0/0`，T06 `42 passed`、T07 `4 passed` |
| 实际 merge-target master CI | [run 36999853797](https://github.com/1040942669/legal-rag-agent/actions/runs/36999853797)，push exact `582eb894` | 四路 success；相同四级累计门禁 passed；M4 JUnit `79/0/0/0`、M5 `81/0/0/0`、M6 `76/0/0/0`，含真实 PG graph 3 cases；M6 gate 464392 ms，T06 `42 passed`、T07 `4 passed`；closed worker/fault validators errors `[]`、live calls false |
| 最终 master M6 wheel | CI 实际 wheel 与其 probe 独立比对 | `0.7.1`，438489 bytes，101 entries，SHA-256 `00a37901b690c8a2059f930c63310de431b2eef7c591c89a40c31580f6259aa3`；13 installed-runtime modules、API/jobs CLI、head `0007_m6_jobs_outbox`、source isolation 全 passed |

本地 DB 首次运行得到 `3 errors`，因为复用的旧临时 cluster 没有假定的测试角色；已停止该 cluster，改为本补丁显式 provision 的独立 cluster 后重新运行。不是产品通过证据，未触及既有 5432 service，也未上传 DSN 或测试数据库。

全量离线门禁保留了三轮失败：前两轮分别 `1011 passed`、`1012 passed`，均另有 `157 subtests passed`，唯一门禁失败为根目录被忽略的旧 `0.7.0` egg-info 遮蔽版本；旧生成物已可恢复地移到本地临时备份，未删除。第三轮版本通过，但 `1 failed, 1011 passed, 157 subtests passed`，可编辑安装文件清单未包含迁移。没有弱化断言；已改为构建和安装真实 wheel，打包定向断言通过。随后重建当前版本的开发元数据并确认测试使用 workspace 源码。

一次开发中组合回归曾 `1 failed, 30 passed`，M2 `evaluation_case_failed`；失败原因未确定，不将推测当结论。冻结实现后的上述 `40 passed` 未改变工件一致性门禁。所有失败均不计入 passed 证据。

第四轮 `1 failed, 1012 passed, 157 subtests passed`，M2 resume 拒绝 `$.code.diff_hash` 改变。此次全量测试期间主执行更新了 tracked 文档，身份覆盖整个仓库而非仅 runtime，因此不能在测试期间修改文档。停止全部写入并提交后，上表冻结门禁通过。打包与 Harness `8 passed in 0.54s`；此项失败未绕过严格恢复校验。

上述软件、CI、wheel、Tag 与 Release 已远端核验；两个独立代理交叉检查前三路 artifact 与摘要，主核验检查 M6 closed receipt、JUnit、实际 wheel 和 provenance。精确 PR/master 四路 artifact digest、关键内部文件 SHA-256 保存在新回执；不同 CI job 的 wheel byte SHA 不同，不声称 byte-for-byte reproducible。

本独立回执首个候选 `359def8477a9121f2bbd62ccc97bf4c20df86e16` 已 commit、正常 push，并建立 [Draft PR #29](https://github.com/1040942669/legal-rag-agent/pull/29)；该 SHA 不替代最终文档候选。本次更新不自引用未来 SHA，最终 receipt head/master 门禁与 Issue #25/Milestone 7 关闭仍未执行，不预写为完成。`2026-10-02T11:31:17Z` 的历史核验快照中两个治理对象仍 open，milestone open_issues 为 1；关联文档 PR 后当前 open 计数会包含该 PR。回执通过后再关闭，并作非递归文档收口；M7 不开始。

## 未运行项与回滚

真实或付费模型、远端 Langfuse 实发、生产容量、真实法律质量实验均未运行。fake usage 只证明计量机制，不证明线上成本或问答质量。

无新 migration、依赖或 M5 checkpoint schema。可关闭观测 feature，或按正常 review revert 补丁；业务任务继续保留数据库与 immutable artifact，不删除 broker/数据库卷。完成原 M6 并核验治理文件后停止，不自动开始 M7。
