# 真实发布回执

此目录保存远端核验的里程碑回执。`M0.json` 至 `M5.json` 已完成软件发布、独立回执、回执 merge-target master CI 和对应 Issue/Milestone 收口。`M5.json` 记录了经核验的 `v0.6.0` 软件、Tag、Release、exact PR-head CI、release-target master CI、独立 receipt PR #23 final-head CI、普通 merge、receipt merge-target master CI 以及治理关闭时间，状态为 `verified`。

`M6.json` 保留已远端核验的 `v0.7.0` 软件 PR #26、四路精确 PR-head 与 release-target master CI、annotated Tag 和公开 Release。独立回执 [PR #27](https://github.com/1040942669/legal-rag-agent/pull/27) 的首轮候选及制品证据仍保留；最终 head `5d20b425...` 的 [CI 36559972128](https://github.com/1040942669/legal-rag-agent/actions/runs/36559972128) 四路成功，已于 `2026-09-29T11:21:13Z` 普通合并为 `0dcafb87...`，该精确提交的 [master CI 36561210905](https://github.com/1040942669/legal-rag-agent/actions/runs/36561210905) 也为四路成功。最终 head/master 的远端 artifact digest 和内部文件 SHA-256 已补记，故 `v0.7.0` 的独立回执本身已经完成。

当前 M6 软件修补已正式发布为 [v0.7.1](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.7.1)，整体状态为 `released`，独立回执为 `verified`。[版本化新回执](M6-v0.7.1.json) 记录 [软件 PR #28](https://github.com/1040942669/legal-rag-agent/pull/28) 的最终 head `d6fc26882237ca149ab38c7e944b32a260e0430b`、普通 squash merge/release target `582eb8949c1150fc7a12761bd46fbda9c173ef62`、各自四路成功的 [PR CI 36998001012](https://github.com/1040942669/legal-rag-agent/actions/runs/36998001012) 与 [master CI 36999853797](https://github.com/1040942669/legal-rag-agent/actions/runs/36999853797)，以及 annotated Tag 和非 draft Release。该补丁完成原 §11.6 的实际观测接线与真实性修正，不是为了文档回执生成的软件版本。

新候选和 release-target 的 M6 门禁均为 `58/58`、exit 0；合并专项 JUnit 均为 `76/0/0/0`，包含新增真实 PostgreSQL graph 3 用例及真实 worker T07，不能把 76 个合并用例说成 76 个 broker 场景。全量为 `1013 passed + 157 subtests passed`。新回执保存八份 GitHub artifact 的 ID/digest、精确 head/run 绑定、内部 M6 文件 SHA-256 与独立计算的 wheel 哈希。master 的权威 M6 wheel 为 `0.7.1`、438489 bytes、SHA-256 `00a37901b690c8a2059f930c63310de431b2eef7c591c89a40c31580f6259aa3`，13 个 installed-runtime 模块与隔离 smoke 通过。真实/付费模型及远端 Langfuse 实发未运行，不声称生产或法律质量提升。

独立文档回执 [PR #29](https://github.com/1040942669/legal-rag-agent/pull/29) 最终 head 为 `51cbe21a9e08b3df68a8eeed296adfb11d3a0ca7`，[精确 PR CI 37003455439](https://github.com/1040942669/legal-rag-agent/actions/runs/37003455439) 四路成功。已于 `2026-10-02T12:10:19Z` 普通 merge 为 `7ec13709d90fad1a01b85b9558a3bb8924a0846d`；最终 head 与 merge 的文档 tree 相同，该精确提交的 [master CI 37005116491](https://github.com/1040942669/legal-rag-agent/actions/runs/37005116491) 也为四路成功。首个候选 `359def8477a9121f2bbd62ccc97bf4c20df86e16` 及其 cancelled/superseded CI 保留为历史，不作为最终通过证据。回执 final/master 的另八份 artifact ID/digest、内部 M6 哈希和真实 wheel 也已核验：均为 438731 bytes，M6 wheel SHA-256 分别为 `ca465b9a3ee9847fe599044834602f61fbac51d3a8dd1a9da1117cf4458748df` 与 `4974cf563c3fdac18b8bd13ce5d18052e6e11405f31a387eba08e97f0c011708`，不覆盖上述软件发布轮的权威 wheel。两轮 M6 均为 `58/58`、JUnit `76/0/0/0`、累计 M5 JUnit `81/0/0/0`，T06 42/T07 4、13 installed-runtime 模块与实际服务重启通过，无 live model calls。

验收通过后，[Issue #25](https://github.com/1040942669/legal-rag-agent/issues/25) 于 `2026-10-02T12:27:11Z` 以 completed 关闭，[Milestone 7](https://github.com/1040942669/legal-rag-agent/milestone/7) 于 `2026-10-02T12:27:23Z` 关闭，open issues 为 0、closed issues 为 3；实际状态于 `2026-10-02T12:27:32Z` 再核验。`M6.json` 中旧的 open/in_progress 字段和原审计说明是保留的历史快照，current pointer/milestone completion 与版本化回执才描述当前完成状态。这一次非递归文档收口只记录已完成的 PR #29、master 验收和治理关闭，不编造自身未来 SHA/PR，不产生另一软件版本、Tag 或 Release；已发布 Tag 不移动，M7 未开始。

每个阶段远程核验发布后，将模板 `../templates/RELEASE_RECEIPT.example.json` 填为 `Mx.json`。
必须记录实际 Tag 目标 SHA、PR、CI、Release URL、是否 draft 以及发布验证时间。

模板中的 null 不可当作完成数据。不能预先为全部阶段创建假回执。
发布回执是 release 之后的文档提交；不要修改原 Tag 以包含回执，也不要制造自引用 SHA。
