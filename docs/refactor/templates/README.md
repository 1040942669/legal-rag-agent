# 执行记录模板

这些文件是待填写模板，不是完成证明。复制到对应阶段的位置后填写真实执行结果；不要把占位符直接发布为验收报告。

| 模板 | 建议保存位置 | 填写时机 |
|---|---|---|
| MILESTONE_ACCEPTANCE.md | `reports/refactor/Mx.md` | 阶段验收；候选与已发布证据分开 |
| PULL_REQUEST.md | PR 正文；也可保存于本地发布目录 | 创建/更新 PR |
| ISSUE.md | GitHub Issue 正文 | 阶段开始，先查重 |
| RELEASE_NOTES.md | GitHub Release 正文 | 候选通过后，发布前审查 |
| ADR.md | `docs/refactor/decisions/ADR-xxx.md` | 发生实质技术取舍 |
| RUN_MANIFEST.example.json | 本地 `artifacts/experiments/<id>/manifest.json` | 真实实验执行前生成并冻结 |
| EVAL_CASE.example.json | 待实现数据适配的参考，不直接覆盖旧 eval_cases | 新增/规范化用例 |
| RELEASE_RECEIPT.example.json | `docs/refactor/receipts/Mx.json` | 远程发布验证成功后填写 |

真实 key、个人对话、完整法律语料、原始模型输出和敏感日志不写进公开模板或报告。实验原始产物默认保留在本地受控目录。
