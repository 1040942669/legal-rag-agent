# 改造交接记录

## 当前真实状态

- M0 已开始；仓库与远端审计、修改前离线基线测试已经完成。
- 目标仓库：`1040942669/legal-rag-agent`。
- 远端默认分支：`master`，审计时为 `4ed96ec921593ef81de61ce545513e3f1211b5db`。
- M0 真实代码基线：`66da8e3056f8287997088d9f503846fd88c9b038`，包含已完成但此前未推送的 Phase 4B 提交。
- 当前分支：`codex/m0-baseline`；工作区因 M0 文档更新而 dirty。
- 最近完成并验证的改造里程碑：无。
- 本文档包产生的远程 commit/PR/Tag/Release：无。

## 已做的核实

已核实真实工作区、origin、默认分支、HEAD、dirty 状态、远端分支、Tags、Releases、PR、
Actions 与仓库规则。已确认逐 case reset 和 Phase 4B 能力存在，不重复开发。

修改前实际执行 `uv run --frozen pytest -q`，结果为 `70 passed in 0.43s`；
包导入、CLI `--help`、测试收集和 `uv lock --check` 也均以退出码 0 完成。
详细记录见 `docs/refactor/BASELINE_AUDIT.md`。

## 没有做的事情

没有运行模型实验、数据库迁移、API 服务、生产部署或付费调用。
没有读取或上传 `.env` 值、完整语料、Embedding cache、私人材料或大型产物。
尚未建立 GitHub Issue/Milestone、push、PR、Tag 或 Release；候选 CI 也尚未运行。

## 下一步：M0

实现明确标注为非真实法律的合成离线 smoke、`scripts/quality_gate.py` 和对应测试，
然后建立只读权限的 PR/master CI。完成一个可测试子任务后显式 stage、commit 并 push 工作分支。
不开始 M1，不安装 LangGraph、数据库或 Redis。

## 执行者每次必须更新的内容

| 项目 | 当前值 |
|---|---|
| 当前里程碑 | M0 / in_progress |
| 实际开发分支 | `codex/m0-baseline` |
| 实际代码基线 | `66da8e3056f8287997088d9f503846fd88c9b038` |
| 本次改动文件 | 改造文档包、`BASELINE_AUDIT.md`、`STATE.json`、本交接文件；尚无业务代码修改 |
| 本次测试命令及结果 | pytest 70 passed；lock/import/CLI help/collect 均通过 |
| 未提交改动/归属 | 用户提供的未跟踪改造文档 + M0 审计更新，均在工作分支保留 |
| 已 commit / 已 push | 否 / 否 |
| PR / Tag / Release | 无 |
| 阻塞 | 无；候选实现与 CI 尚未完成 |
| 下一条可执行动作 | 实现并测试 M0 合成 smoke 与质量门禁 |

## 阶段结束报告

完成时替换为真实记录：做了什么、实际 passed/failed/skipped、哪些未运行、远程 URL、目标 SHA、回滚方法。
不能仅把“下一步”改成下一阶段而不保存测试和发布证据。
