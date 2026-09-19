# 从这里开始：Legal RAG 改造交接包

这是开发文档包，不是已经改造好的代码。文件准备于 2026-09-19，参考仓库 `1040942669/legal-rag-agent` 的 `4ed96ec...` 快照。实施时必须重新查看真实工作区。

## 放到哪里

将本包的 `docs/refactor/` 和本文件放到项目根目录。它不会主动执行代码、安装依赖或向 GitHub 写入。

不要覆盖已有 `AGENTS.md`、README、业务代码或当前交接状态。相同路径已存在时先比较内容；已有执行记录优先保留，再合并新方案。

## 文档入口

| 文件 | 用途 |
|---|---|
| [开发与发布总方案](docs/refactor/MASTER_PLAN.md) | 详细架构、8 个里程碑、测试、版本、回滚和发布要求 |
| [给 Agent 的执行规则](docs/refactor/AGENT_EXECUTION.md) | 每次进入项目必须遵循的工作顺序 |
| [当前状态](docs/refactor/STATE.json) | 机器可读阶段状态；初始为全部未开始 |
| [交接记录](docs/refactor/HANDOFF.md) | 人类可读的接续点、已执行测试、阻塞和下一步 |
| [启动指令](docs/refactor/PROMPT_START.md) | 第一次交给项目窗口的完整指令 |
| [继续指令](docs/refactor/PROMPT_RESUME.md) | 后续窗口核实状态后继续 |
| [模板说明](docs/refactor/templates/README.md) | 验收、Issue、PR、Release、ADR、实验与发布回执模板 |

## 第一次直接发送这段话

```text
请读取当前目录及父目录适用的 AGENTS.md，以及：
1. docs/refactor/AGENT_EXECUTION.md
2. docs/refactor/MASTER_PLAN.md
3. docs/refactor/STATE.json
4. docs/refactor/HANDOFF.md

现在在当前 legal-rag-agent 仓库开始执行 M0，而不是重新给我一份方案。
先核对 origin、默认分支、当前 HEAD、未提交改动和远程版本；保留我的现有工作。
仅完成 M0：基线审计、离线测试、开发门禁、文档和版本发布流程。

我授权在此仓库范围内创建工作分支、修改相应文件、运行离线测试、commit、push、创建 PR，
并在必需测试通过且仓库规则允许时合并，然后创建该里程碑对应的 Tag 和 GitHub Release。
每个可测试子任务完成后 push 工作分支；整个 M0 通过后再发版本。
不要绕过 review/保护规则，不要强推、改仓库权限、覆盖我的未提交修改或上传私密资料。
不要未经额度授权调用付费模型服务。

完成后给出真实 PR、Tag、Release、commit 和测试结果，更新 STATE/HANDOFF/发布回执，然后停下。
若测试、权限或发布受阻，保留已经完成的安全工作，明确记录阻塞步骤，不要标记为已发布。
```

完整版本见 `PROMPT_START.md`。下一窗口用 `PROMPT_RESUME.md`，不要在一份新聊天里重新描述整个项目。

## 计划版本

M0 v0.1.1 → M1 v0.2.0 → M2 v0.3.0 → M3 v0.4.0 → M4 v0.5.0 → M5 v0.6.0 → M6 v0.7.0（增强）→ M7 v0.8.0。

这些是计划号；如仓库已有冲突版本，执行者需先核实并顺延，不能覆盖已有 Tag。

## 重要校正

本方案已按较新的源码核对：逐 case reset 已实现，不能再当作待修的新缺陷；现有 120 条 v3 评测资料应复用并核验。现有验证器仍需拆分免责声明、拒答和语义支持判断。依据与限定详见主文档第 1 节。
