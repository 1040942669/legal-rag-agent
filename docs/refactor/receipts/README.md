# 真实发布回执

此目录保存已经完成远端核验的里程碑回执。当前 `M0.json`、`M1.json`、`M2.json` 与 `M3.json` 均已完成软件发布、独立回执、回执 master CI 和对应 Issue/Milestone 收口。`M4.json` 已记录经核验的 `v0.5.0` 软件、Tag、Release 与 release-target CI；其独立回执 PR、merge 后 master CI、Issue #16 和 Milestone 5 收口仍在进行，因此状态是 `verified_release_receipt_pending_merge`，不是最终 `verified`。

每个阶段远程核验发布后，将模板 `../templates/RELEASE_RECEIPT.example.json` 填为 `Mx.json`。
必须记录实际 Tag 目标 SHA、PR、CI、Release URL、是否 draft 以及发布验证时间。

模板中的 null 不可当作完成数据。不能预先为全部阶段创建假回执。
发布回执是 release 之后的文档提交；不要修改原 Tag 以包含回执，也不要制造自引用 SHA。
