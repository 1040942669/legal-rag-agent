# 真实发布回执

此目录保存远端核验的里程碑回执。`M0.json` 至 `M5.json` 已完成软件发布、独立回执、回执 merge-target master CI 和对应 Issue/Milestone 收口。`M5.json` 记录了经核验的 `v0.6.0` 软件、Tag、Release、exact PR-head CI、release-target master CI、独立 receipt PR #23 final-head CI、普通 merge、receipt merge-target master CI 以及治理关闭时间，状态为 `verified`。

`M6.json` 保留已远端核验的 `v0.7.0` 软件 PR #26、四路精确 PR-head 与 release-target master CI、annotated Tag 和公开 Release。独立回执 [PR #27](https://github.com/1040942669/legal-rag-agent/pull/27) 的首轮候选及制品证据仍保留；最终 head `5d20b425...` 的 [CI 36559972128](https://github.com/1040942669/legal-rag-agent/actions/runs/36559972128) 四路成功，已于 `2026-09-29T11:21:13Z` 普通合并为 `0dcafb87...`，该精确提交的 [master CI 36561210905](https://github.com/1040942669/legal-rag-agent/actions/runs/36561210905) 也为四路成功。最终 head/master 的远端 artifact digest 和内部文件 SHA-256 已补记，故 `v0.7.0` 的独立回执本身已经完成。

当前 M6 软件修补已正式发布为 [v0.7.1](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.7.1)，整体状态为 `released_receipt_pending`。[版本化新回执](M6-v0.7.1.json) 记录 [软件 PR #28](https://github.com/1040942669/legal-rag-agent/pull/28) 的最终 head `d6fc26882237ca149ab38c7e944b32a260e0430b`、普通 squash merge/release target `582eb8949c1150fc7a12761bd46fbda9c173ef62`、各自四路成功的 [PR CI 36998001012](https://github.com/1040942669/legal-rag-agent/actions/runs/36998001012) 与 [master CI 36999853797](https://github.com/1040942669/legal-rag-agent/actions/runs/36999853797)，以及 annotated Tag 和非 draft Release。该补丁完成原 §11.6 的实际观测接线与真实性修正，不是为了文档回执生成的软件版本。

新候选和 release-target 的 M6 门禁均为 `58/58`、exit 0；合并专项 JUnit 均为 `76/0/0/0`，包含新增真实 PostgreSQL graph 3 用例及真实 worker T07，不能把 76 个合并用例说成 76 个 broker 场景。全量为 `1013 passed + 157 subtests passed`。新回执保存八份 GitHub artifact 的 ID/digest、精确 head/run 绑定、内部 M6 文件 SHA-256 与独立计算的 wheel 哈希。master 的权威 M6 wheel 为 `0.7.1`、438489 bytes、SHA-256 `00a37901b690c8a2059f930c63310de431b2eef7c591c89a40c31580f6259aa3`，13 个 installed-runtime 模块与隔离 smoke 通过。真实/付费模型及远端 Langfuse 实发未运行，不声称生产或法律质量提升。

独立文档回执当前为 `awaiting_final_head_gates`，分支 `codex/m6-v071-release-receipt`，已实际创建 open Draft [PR #29](https://github.com/1040942669/legal-rag-agent/pull/29)。首个已推送候选 `359def8477a9121f2bbd62ccc97bf4c20df86e16` 的角色是 `first_receipt_candidate_not_final_head`，不是最终回执 head；其 CI 在本回执中尚未核验，最终文档 SHA、merge 和对应 master CI 仍为 null。软件发布已经核验，不回写成“发布失败”；独立回执及治理仍须按门禁完成。[Issue #25](https://github.com/1040942669/legal-rag-agent/issues/25) 与 [Milestone 7](https://github.com/1040942669/legal-rag-agent/milestone/7) 在 `2026-10-02T11:31:17Z` 核验为 open，Milestone 有 1 个 open issue。`M6.json` 的旧软件/回执事实保持不变，通过 current pointer 指向新回执；`v0.7.0` 与 `v0.6.0` Tag 不移动，M7 未开始。

每个阶段远程核验发布后，将模板 `../templates/RELEASE_RECEIPT.example.json` 填为 `Mx.json`。
必须记录实际 Tag 目标 SHA、PR、CI、Release URL、是否 draft 以及发布验证时间。

模板中的 null 不可当作完成数据。不能预先为全部阶段创建假回执。
发布回执是 release 之后的文档提交；不要修改原 Tag 以包含回执，也不要制造自引用 SHA。
