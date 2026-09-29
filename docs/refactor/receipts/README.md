# 真实发布回执

此目录保存远端核验的里程碑回执。当前 `M0.json` 至 `M5.json` 均已完成软件发布、独立回执、回执 merge-target master CI 和对应 Issue/Milestone 收口。`M5.json` 记录了经核验的 `v0.6.0` 软件、Tag、Release、exact PR-head CI、release-target master CI、独立 receipt PR #23 final-head CI、普通 merge、receipt merge-target master CI 以及治理关闭时间，状态为 `verified`。

每个阶段远程核验发布后，将模板 `../templates/RELEASE_RECEIPT.example.json` 填为 `Mx.json`。
必须记录实际 Tag 目标 SHA、PR、CI、Release URL、是否 draft 以及发布验证时间。

模板中的 null 不可当作完成数据。不能预先为全部阶段创建假回执。
发布回执是 release 之后的文档提交；不要修改原 Tag 以包含回执，也不要制造自引用 SHA。
