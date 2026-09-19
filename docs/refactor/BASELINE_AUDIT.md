# M0 基线审计

> 审计时间：2026-09-19T14:26:14+08:00
>
> 审计范围：M0-01 仓库审计与 M0-02 基线清单
>
> 本文记录实际工作区事实，不把历史实验结果当成本次复跑结果。

## 1. 仓库身份与工作区

| 项目 | 实际值 |
|---|---|
| 工作目录 | `D:\Study\Python_project\LLM-application` |
| Git 根目录 | `D:/Study/Python_project/LLM-application` |
| origin | `https://github.com/1040942669/legal-rag-agent.git` |
| GitHub 仓库 | `1040942669/legal-rag-agent`，public |
| 远端默认分支 | `master` |
| 审计时远端 master | `4ed96ec921593ef81de61ce545513e3f1211b5db` |
| 审计前本地分支 | `codex/phase4b-evaluation-platform`，无 upstream |
| 审计前本地 HEAD | `66da8e3056f8287997088d9f503846fd88c9b038` |
| M0 工作分支 | `codex/m0-baseline`，从上述本地 HEAD 创建 |
| 本地相对远端 | 0 behind，1 ahead；领先提交为既有 Phase 4B 实现 |

审计前没有已跟踪文件的未提交修改。存在 `START_HERE_REFACTOR.md` 和
`docs/refactor/` 文档包等 18 个未跟踪文件，它们是本次执行输入，已原样保留。
本地 HEAD 中已有的 Phase 4B 代码也被保留；M0 没有把工作区退回文档记录的旧 SHA。

工作区还存在被 `.gitignore` 或 `.git/info/exclude` 排除的本地语料、模型产物、
实验输出、`.env` 以及个人学习材料。审计没有 stage、上传或公开这些内容。

## 2. 远端版本与仓库规则

2026-09-19 通过 `git ls-remote`、`git fetch --prune --tags` 和 GitHub CLI 交叉核实：

- 远端仅有 `master` 分支；当前 M0 分支及原 Phase 4B 本地分支当时均未推送。
- Git tags：0。
- GitHub Releases：0。
- GitHub PR：0。
- Issues 与 milestones：0。
- 现有提交式 Actions workflow：无；仅有 GitHub 自动的 Dependency Graph workflow。
- `master` 没有 branch protection、ruleset、required checks、required review 或 CODEOWNERS。
- merge commit、squash 和 rebase merge 均被仓库允许。

因此计划版本 `v0.1.1` 没有远端命名冲突。没有保护规则不等于允许绕过流程；
M0 仍必须通过 PR、精确候选 SHA 的 CI 和人工 diff 审查后才能合并及发布。

## 3. 运行环境与依赖

| 项目 | 实际值 |
|---|---|
| OS | Microsoft Windows NT 10.0.26100.0 |
| PowerShell | 7.6.5 |
| Git | 2.52.0.windows.1 |
| host Python | 3.12.13 |
| 项目 `.venv` Python | 3.12.12 |
| uv | 0.10.10 |
| 项目 Python 约束 | `>=3.10` |
| 包版本（审计时） | `0.1.0` |
| lockfile | `uv.lock` 存在，131 个解析包；`uv lock --check` 通过 |
| uv.lock SHA-256 | `0E4D32803D861979B819D489A9E5AC46C72876DA97F5AF9C1281EE8B55D03D51` |

`pyproject.toml` 和 `legal_rag/__init__.py` 各自保存一份 `0.1.0`，存在版本漂移风险。
M0-06 将以 `pyproject.toml` 为版本源，并同步更新确实受影响的 lock 元数据。

依赖包含可选使用的大型本地模型栈。测试中的相关 adapter 均为 lazy load 或 fake，
本次没有运行 dense、reranker、生成、judge 或模型下载。全新 Linux CI 安装依赖可能较大，
这是安装成本风险，不是本次测试失败。

## 4. 数据与模型配置边界

- 仓库跟踪 5 份评测文件：v1 10 条、v2 31 条、adaptive 4 条、v3 120 条、
  v3 生成子集 30 条。
- 本地存在被忽略的完整语料目录、索引和 embedding cache；M0 不读取或上传这些产物。
- 测试目录已有小型 fixture，但其内容使用真实法律名称，不能单独满足 M0-T03 的
  “明确合成且非真实法律条文”要求。
- 配置中存在本地 Ollama、sentence-transformers 与 SiliconFlow 模型入口；BM25 是默认检索器，
  adaptive 与 reranker 默认关闭。
- 本地忽略的 `.env` 存在，模型 API key 状态记为“已配置”；值未写入日志或报告。
- `ALLOW_LIVE_MODEL_CALLS` 未启用；本里程碑没有费用预算，所有 live model 调用禁止。

## 5. 审计时真实测试

| Test ID | 实际命令 | 结果 | 退出码 | 说明 |
|---|---|---:|---:|---|
| baseline-lock | `uv lock --check` | passed | 0 | lockfile 可解析，无更新 |
| baseline-tests | `uv run --frozen pytest -q` | 70 passed | 0 | 0 failed，0 skipped，0.43s |
| baseline-import | `uv run --frozen python -c "import legal_rag ..."` | passed | 0 | module/distribution 均为 0.1.0 |
| baseline-cli | `uv run --frozen python -m legal_rag.cli --help` | passed | 0 | 8 个子命令，未调用模型 |
| baseline-collect | `uv run --frozen pytest --collect-only -q` | 70 collected | 0 | 测试可完整收集 |

上述是修改前基线，不是最终 M0 候选验收。历史 README 中的检索和生成指标没有在 M0 复跑，
也没有被当成本次质量提升证据。

## 6. 已存在能力与文档差异

真实 HEAD 已包含 Phase 0-3、Phase 4A 和 Phase 4B 的现有实现，包括逐 case memory reset、
120 条 v3 评测、cache v2、reranker adapter、实验矩阵和成本/延迟聚合。M0 复用这些成果，
不重复实现逐 case reset，也不把 adaptive 或 reranker 改为默认。

与初始改造文档相比，工作区比 `4ed96ec...` 多一个尚未发布的 Phase 4B 提交；
因此 M0 以 `66da8e3...` 为真实代码基线。README 声称的 `70 passed` 已在本机复核；
历史真实语料和模型指标未复核。

## 7. M0 开始时的缺口

1. 没有 `scripts/quality_gate.py`，也没有统一 JSON 门禁结果。
2. 没有 `.github/workflows` 中的 PR/master 离线 CI。
3. 没有明确标注为合成、非真实法律的离线 smoke fixture。
4. `--help` 在解析参数前加载本地 `.env`，虽未联网，但不符合最小暴露原则。
5. 包版本有两份手写来源，尚未升级到计划的 `0.1.1`。
6. 尚无 `CHANGELOG.md`、M0 验收报告、PR、Tag、Release 或发布回执。

下一步只处理上述 M0 缺口，不开始 M1 的验证语义改造。
