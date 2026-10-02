# 真实发布回执

此目录保存远端核验的里程碑回执。`M0.json` 至 `M5.json` 已完成软件发布、独立回执、回执 merge-target master CI 和对应 Issue/Milestone 收口。`M5.json` 记录了经核验的 `v0.6.0` 软件、Tag、Release、exact PR-head CI、release-target master CI、独立 receipt PR #23 final-head CI、普通 merge、receipt merge-target master CI 以及治理关闭时间，状态为 `verified`。

`M6.json` 保留已远端核验的 `v0.7.0` 软件 PR #26、四路精确 PR-head 与 release-target master CI、annotated Tag 和公开 Release。独立回执 [PR #27](https://github.com/1040942669/legal-rag-agent/pull/27) 的首轮候选及制品证据仍保留；最终 head `5d20b425...` 的 [CI 36559972128](https://github.com/1040942669/legal-rag-agent/actions/runs/36559972128) 四路成功，已于 `2026-09-29T11:21:13Z` 普通合并为 `0dcafb87...`，该精确提交的 [master CI 36561210905](https://github.com/1040942669/legal-rag-agent/actions/runs/36561210905) 也为四路成功。最终 head/master 的远端 artifact digest 和内部文件 SHA-256 已补记，故 `v0.7.0` 的独立回执本身已经完成。

完整 M6 仍为 `in_progress`：全计划审计发现 §11.6 的预算、重试、缓存、证据、模型用量、耗时与 M2/M5 运行关联缺少实际调用点记录，幂等请求还可能产生虚假的 queued 事件。当前 `codex/m6-observation-completion` 从上述已核验 master 开始补全真实产品机制，计划软件补丁为 `v0.7.1`；这不是为了文档回执而生成软件版本。现有 58/58 门禁属于旧版本证据，不能证明新补丁已经通过。Issue #25/Milestone 7 保持 open，待补丁完成自己的候选 CI、正常合并、精确 master CI、Tag/Release 和独立回执后再收口。已发布 `v0.7.0` 与 `v0.6.0` Tag 不移动，M7 未开始。

每个阶段远程核验发布后，将模板 `../templates/RELEASE_RECEIPT.example.json` 填为 `Mx.json`。
必须记录实际 Tag 目标 SHA、PR、CI、Release URL、是否 draft 以及发布验证时间。

模板中的 null 不可当作完成数据。不能预先为全部阶段创建假回执。
发布回执是 release 之后的文档提交；不要修改原 Tag 以包含回执，也不要制造自引用 SHA。
