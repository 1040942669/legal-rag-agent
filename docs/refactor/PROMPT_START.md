# 首次执行指令

> 历史启动模板：本模板用于首次启动 M0。M0 已完成并发布；后续窗口必须先读取 `STATE.json` 与 `HANDOFF.md`，从其中记录的当前里程碑继续，不能重复执行 M0 或把模板中的旧状态当作实时事实。

把下面整段发到已打开该项目的代码 Agent 窗口。

```text
你现在负责渐进改造当前 legal-rag-agent 项目。不要重新输出宏观方案，按仓库里的执行文档开始落实。

先读取当前及父目录适用的 AGENTS.md，然后读取：
- docs/refactor/AGENT_EXECUTION.md
- docs/refactor/MASTER_PLAN.md
- docs/refactor/STATE.json
- docs/refactor/HANDOFF.md
再核对当前代码、测试、README、历史方案和架构决策。

本次只执行 M0。首先检查 origin 是否为我当前的 legal-rag-agent、默认分支、当前 HEAD、
未提交改动、已有版本/PR/Release。不要覆盖我的本地工作，不要强制回到文档里记录的历史 SHA。
现有逐 case reset、评测集和历史实验应复用；不要照抄上一轮过时的待办。

我授权你在此仓库内创建阶段工作分支、按 M0 范围修改文件、运行离线测试、提交和 push，
创建 PR；当必要测试/CI 通过、仓库规则允许且不需要额外人的批准时合并，
再为该阶段创建并发布正确目标的 Tag 和 GitHub Release。
每个完整可测试子任务完成就 push 工作分支，不要等全部项目重构完才 push。

不要绕过保护规则或 review，不要强推、修改仓库权限、重写远程历史，
不要上传密钥、私人对话、未授权语料、大型模型/Embedding 文件，
不要未经预算授权调用付费 API 或开通服务，不部署生产。
版本号以远程实际情况核实，不能覆盖现有 Tag。

完成 M0 后更新版本、README、CHANGELOG、验收报告、STATE、HANDOFF 和发布回执。
告诉我：完成内容、真实测试结果、未运行项、PR/Tag/Release 链接、对应 commit、已知限制。
然后停下，不自动开始 M1。
如果测试、网络、权限、review 或发布流程受阻，记录具体阻塞及已完成的部分，
保留安全的 commit/push 成果，不标记为 released，不通过编造数据或跳过门禁来凑完成。
```
