# 项目管理流程

> 生效日期：2026-10-22，适用于 v1.1.1 之后的全部开发与维护。本文件只规定"怎么做"；"做什么、不做什么、什么时候开新版本"以 [项目边界](PROJECT_SCOPE.md) 为准。两者冲突时，先改项目边界，再动代码。

## 1. 原则

1. **轻量**：一个工作项对应一个 Issue、一个分支、一个 PR。不再写 STATE、回执和交接文档（项目边界 D7）。
2. **仓库事实优先**：代码、测试和 CI 结果优先于任何文档和聊天记录。文档与代码不一致，按缺陷处理。
3. **文档随代码走**：改了行为、默认值、数字或命令，就在同一个 PR 里更新对应文档（见第 6 节），不留"之后再补"。
4. **一个事实只写在一个地方**：其他地方只写摘要并链接过去。
5. **负结果照样记录**；不在开发集上调参或挑配置（项目边界第 4 节）。

## 2. 事实来源

| 问题 | 唯一来源 | 说明 |
| --- | --- | --- |
| 做什么、不做什么、什么时候开新版本 | [PROJECT_SCOPE.md](PROJECT_SCOPE.md) | 改范围时追加新决定（D9、D10……），不改写旧决定 |
| 为什么这样设计 | [ARCHITECTURE_DECISION_LOG.md](../ARCHITECTURE_DECISION_LOG.md) | 新的设计取舍追加编号条目 |
| 每个版本改了什么 | [CHANGELOG.md](../CHANGELOG.md) | 未发布的改动记在 `[Unreleased]` 下 |
| 评测数字 | `reports/retrieval/*.md` 等报告 | README 只放摘要和链接 |
| 指标定义 | [METRICS.md](METRICS.md) | |
| 待办、缺陷、想法 | GitHub Issues 与 Milestone | 文档里不维护任务清单 |
| 怎么做 | 本文件 | |
| 历史记录（只读） | `docs/refactor/`、`docs/retrieval-rework/`、`docs/LEGAL_RAG_EXECUTION_PLAN.md`、`reports/refactor/`、`START_HERE_REFACTOR.md` | 不再更新，也不作为当前指令 |

## 3. 工作项分级

每个 Issue 开出来时先定级，级别决定走哪条路、要不要发版。

| 级别 | 定义 | 例子 | 版本 | 是否先改项目边界 |
| --- | --- | --- | --- | --- |
| P0 正确性 | 会给出错误法律依据、错误数字、损坏数据，或存在安全漏洞 | 条号解析错、引用指向错误条文、鉴权可被绕过 | 尽快发 `v1.1.x` | 否 |
| P1 工程缺陷 | 不加功能、不改对外行为的质量问题 | 测试依赖缺失、异常被吞、CI 覆盖缺口、生产代码用 `assert` | 累积后发 `v1.1.x` | 否 |
| P2 文档 | 文档过期、与事实不符或缺失 | README 的版本说法落后于 CHANGELOG | 纠正已发布的数字或结论时发 `v1.1.x`；纯措辞不发版 | 否 |
| P3 范围变更 | 新功能、新语料、改默认策略、新增依赖或服务、改变安装方式 | API 限流、拆分可选依赖、换默认检索器 | `v1.x.0` | **是**：先在项目边界追加决定并写明理由 |
| 想法 | 没有失败样例或数据支撑 | "试试另一个向量库" | 不发版 | 只建 Issue，标 `idea`，不写代码 |

处理顺序：P0，阻塞测试或 CI 的 P1，P2，其余 P1，P3。

P1 不加功能、不改默认行为，因此不违反项目边界 D1"只修正确性 bug，不加功能"。拿不准是 P1 还是 P3 时，按 P3 处理。

## 4. 一个工作项的完整流程

### 4.1 开工前（每次）

```powershell
git fetch --prune origin
git switch master
git pull --ff-only
git status            # 必须干净；不干净先处理，不要带着无关改动开工
uv sync --frozen --all-groups --extra service
```

必须带 `--extra service`：只运行 `uv sync` 时，`tests/test_m4_api_http_unit.py` 会因为缺少 `fastapi` 在收集阶段失败。

### 4.2 建 Issue

用 `gh issue create` 或网页建 Issue，正文按第 9 节的模板填写，并打上级别标签。准备放进某个版本的 Issue，挂到对应的 Milestone（例如 `v1.1.2`）。

没有 Issue 的改动只限两种：错别字、断链。

### 4.3 建分支

| 前缀 | 用途 |
| --- | --- |
| `fix/` | P0、P1 |
| `docs/` | P2 |
| `feat/` | P3 |
| `chore/` | 依赖、CI、发版、清理 |
| `exp/` | 实验，通常只改报告 |

```powershell
git switch -c fix/evaluation-swallowed-errors
```

用 Orca 新建 worktree 时，分支会被自动命名为 `1040942669/<名字>`。这时把类型写在名字开头，例如 `1040942669/fix-evaluation-errors`、`1040942669/feat-web-ui`。

### 4.4 测试先行

- P0 和 P1：先写一个能复现问题的回归测试，确认它在修复前失败。
- 只依赖本地文件的测试放 `tests/`；需要 PostgreSQL、Redis 的放 `integration_tests/`，运行时要设置 `LEGAL_RAG_INTEGRATION_TEST=1`。
- 测试不得调用真实模型。CI 里 `ALLOW_LIVE_MODEL_CALLS=false`，provider 边界会拒绝调用。

### 4.5 实现

- 只做 Issue 范围内的最小改动，不顺手重构无关代码。需要重构就另开 Issue。
- 跟随周围代码的命名、注释密度和写法。
- 数据库 schema 只能新增 Alembic migration，不修改已发布的 migration。

### 4.6 本地检查

```powershell
uv run --frozen pytest -q
uv run --offline --frozen --no-sync python scripts/quality_gate.py --milestone M2 --mode offline
```

改到 `legal_rag/storage`、`legal_rag/api`、`legal_rag/harness`、`legal_rag/jobs` 时，集成测试由 CI 的 PostgreSQL、Redis 服务容器负责；本地有数据库时也可以先跑对应的 `integration_tests/test_m*_*.py`。

### 4.7 同步文档

按第 6 节的矩阵检查，并在 CHANGELOG 的 `[Unreleased]` 下记一条（纯措辞修改除外）。

### 4.8 提交

沿用仓库已有的 Conventional Commits 写法：

```text
<type>(<scope>): <一句话说明做了什么>

<为什么这样改；有取舍时写取舍>

Fixes #<issue>
```

`type` 取 `fix`、`feat`、`docs`、`test`、`refactor`、`chore`、`ci`。一个提交只做一件事。

### 4.9 PR 与合并

1. `git push -u origin <branch>`，再用 `gh pr create` 开 PR，正文按第 9 节的 PR 模板填写。
2. CI 的四个 job（offline、m4 service、m5 fault injection、m6 worker）必须全绿。CI 失败先修，不重跑碰运气；确属不稳定的测试，另开 P1 Issue 记录。
3. 自查 diff 一遍：没有调试代码、没有无关文件、没有密钥。
4. 用普通 merge commit 合并（与 #33、#34 一致）。不 force push 到 `master`。

### 4.10 合并后

```powershell
git switch master
git pull --ff-only
git branch -d <branch>
git push origin --delete <branch>
```

确认 Issue 已被 `Fixes #N` 自动关闭。需要发版时转第 7 节。

## 5. 完成的定义

一个 PR 只有在下面每一项都满足时才能合并：

- [ ] Issue 写了验收标准，并且已经满足
- [ ] P0、P1 有回归测试，修复前失败，修复后通过
- [ ] 本地 `pytest` 和 M2 离线门禁通过
- [ ] CI 四个 job 全绿
- [ ] 第 6 节要求的文档已经更新
- [ ] CHANGELOG `[Unreleased]` 已记录（纯措辞修改除外）
- [ ] 没有提交 `.env`、法律文本、`artifacts/` 下的产物或任何密钥
- [ ] 有付费模型调用时，PR 描述里写了大致花费

## 6. 文档同步矩阵

| 改动 | 必须同步更新 |
| --- | --- |
| 修复导致基线数字变化 | 对应报告、README 结果段、CHANGELOG（写明旧值和新值） |
| 改默认配置（`configs/default.yaml`） | README 快速开始、项目边界第 3 节、ADL 新条目（写对照指标和回滚条件）、CHANGELOG |
| 新增或修改 CLI 命令、参数 | README 对应段落、CHANGELOG |
| 新增或修改环境变量 | `.env.example`、README、CHANGELOG |
| 数据库 schema | 新 Alembic migration、CI 里 wheel 资源检查的 migration 清单、README 服务段、CHANGELOG |
| 依赖 | `pyproject.toml` 与 `uv.lock`（运行 `uv lock`）、CHANGELOG |
| 新的评测数字 | 新报告，或在原报告中新增一节；README 只放摘要 |
| 新增文档 | [docs/README.md](README.md) 的阅读顺序和职责表 |
| 范围变化 | 项目边界新决定、ADL 条目 |

## 7. 版本与发布

### 7.1 版本号

| 版本 | 什么时候发 |
| --- | --- |
| `v1.1.x` | P0 修复后立即发；P1 和纠正已发布结论的 P2 累积后一起发 |
| `v1.x.0` | P3，且满足项目边界第 6 节的条件 |
| `v2.0.0` | 暂无计划 |

不是每次合并都发版。未发布的改动留在 CHANGELOG 的 `[Unreleased]` 下。

### 7.2 发版步骤

1. 从最新 `master` 建分支 `chore/release-v1.1.2`。
2. 修改 `pyproject.toml` 的 `version`，然后运行 `uv lock`（`uv.lock` 记录了项目自身的版本）。
3. CHANGELOG：把 `[Unreleased]` 改成 `[1.1.2] - YYYY-MM-DD`，上方新建空的 `[Unreleased]`。
4. README 或项目边界里写到"当前版本"的地方一并更新。
5. 开 PR，CI 全绿后合并。
6. 在合并提交上打 annotated tag，并推送：

   ```powershell
   git switch master
   git pull --ff-only
   git tag -a v1.1.2 -m "v1.1.2" HEAD
   git push origin v1.1.2
   ```

7. 用 CHANGELOG 中这一版的内容创建 GitHub Release：`gh release create v1.1.2 --title v1.1.2 --notes-file <摘录文件>`。

已发布的 tag 不移动、不删除；发错了就发下一个版本修正。发版不再写回执、STATE 或交接文档。

## 8. 实验与付费调用

- `ALLOW_LIVE_MODEL_CALLS` 默认为 `false`，只在当前终端临时打开，不写进 `.env`。
- 跑之前估算调用次数和费用；跑完按 SiliconFlow 账单记录实际花费，写进 PR 描述。
- 原始产物放 `artifacts/`（已被 Git 忽略）；只有复核过的汇总进入 `reports/`。
- 每份报告必须写明：语料清单与 revision、题集 ID、模型提供方与名称、运行日期、代码 commit、metrics schema、已知局限。
- 实验中途失败（例如服务端 502、本机内存不足）要在报告里写清楚哪些结果完整、哪些不完整，不能把不完整的结果当作完整结果。
- SiliconFlow 不提供模型版本号。同一实验在不同时间重跑得到的数字，记为新的快照，不覆盖旧数字。
- 不在开发集上挑配置。有了人工撰写、冻结后才使用的留出集，才讨论效果优化（项目边界第 6 节）。

## 9. Issue、标签与模板

### 9.1 标签

| 标签 | 含义 |
| --- | --- |
| `P0` `P1` `P2` `P3` `idea` | 第 3 节的级别，每个 Issue 必须有且只有一个 |
| `bug` `documentation` `enhancement` | 沿用 GitHub 默认标签，表示类型 |
| `experiment` | 需要调用付费模型或产出新数字 |
| `ci` | 只涉及 CI 或门禁 |

每个计划发布的版本建一个 Milestone，发版后关闭。

### 9.2 Issue 模板

```markdown
## 现象
（看到了什么；附命令、输出或文件位置）

## 复现步骤
1.

## 影响
（错在哪里、影响哪些命令或数字）

## 验收标准
- [ ]

## 级别
P0 / P1 / P2 / P3 / idea，理由：
```

### 9.3 PR 模板

```markdown
## 关联
Fixes #

## 改了什么

## 为什么这样改
（有取舍时写清楚放弃了什么）

## 怎么验证的
- [ ] 回归测试：
- [ ] 本地 pytest 与 M2 离线门禁
- [ ] CI 四个 job

## 文档同步
（按第 6 节列出更新了哪些文档；不需要时写"无"并说明原因）

## 发版
不发版 / 进入 v1.1.x / 进入 v1.x.0
```

## 10. 定期维护

每月第一周做一次。有改动时建分支 `chore/maintenance-YYYY-MM`，没有改动就只在这里核对，不留记录。

1. **分支**：`git fetch --prune`，用 `git branch -r --merged origin/master` 找出已合并的远程分支并删除。发布 tag 已经保留了历史，不需要保留分支。
2. **依赖**：在分支上运行 `uv lock --upgrade`，看 `uv.lock` 的 diff 并跑 CI。安全更新优先合并；有破坏性变化的依赖单独开 P1 Issue。
3. **模型**：核对默认模型（`BAAI/bge-m3`、`BAAI/bge-reranker-v2-m3`、`deepseek-ai/DeepSeek-V4.1-Flash`、`zai-org/GLM-5.3-Flash`）是否仍在 SiliconFlow 上提供，价格是否变化。下线或涨价就开 P3 Issue。
4. **语料**：查看 `Kuugo/Chinese_Law` 是否有新 revision。有就开 P3 Issue，不自动切换。
5. **文档**：运行 M2 离线门禁（含 Markdown 相对链接检查）；搜索 README 和项目边界里的版本号，确认与 `pyproject.toml` 一致。
6. **Issue**：关闭已解决或不再相关的 Issue；`idea` 超过三个月没有新证据的，关闭并注明原因。

## 11. 与 Claude Code 等 Agent 协作

- 开工先读本文件和[项目边界](PROJECT_SCOPE.md)，再读相关代码与测试。`docs/refactor/` 下的执行规则和提示词是历史记录，不是当前指令。
- Agent 不直接向 `master` 提交，所有改动按第 4 节走分支和 PR。
- 推送分支、开 PR、合并、打 tag、发 Release、调用付费模型，都要先得到所有者确认，除非当次对话已经明确授权。
- 付费调用的每个批次结束后，报告大致花费。
- 探索多个文件可以交给只读子 Agent；写文件和 Git 操作由主会话完成。
- Agent 的结论以仓库事实为准：说"已修复"必须有测试通过，说"CI 通过"必须看过 CI 结果。

## 12. 文档健康规则

- 历史文档的文件头加一行说明：从哪天起不再更新，当前流程见本文件。
- README 面向读者，回答项目是什么、有什么结果、怎么运行。发布证据（commit SHA、CI run 编号、tag 对象）放在 CHANGELOG 或报告里，不放在 README。
- 日期一律写绝对日期（2026-10-22），不写"上周""最近"。
- 中英混排的文档统一 UTF-8 编码，不使用全角破折号等容易出编码问题的符号。
- 文档里的数字必须能追溯到一份报告；没有报告支撑的数字不写。
