# 真实发布回执

此目录保存已经完成远端核验的里程碑回执。当前已有 `M0.json` 和 `M1.json`；其余里程碑只有在对应 Release 发布并核验后才新增回执。

每个阶段远程核验发布后，将模板 `../templates/RELEASE_RECEIPT.example.json` 填为 `Mx.json`。
必须记录实际 Tag 目标 SHA、PR、CI、Release URL、是否 draft 以及发布验证时间。

模板中的 null 不可当作完成数据。不能预先为全部阶段创建假回执。
发布回执是 release 之后的文档提交；不要修改原 Tag 以包含回执，也不要制造自引用 SHA。
