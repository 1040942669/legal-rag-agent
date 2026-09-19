# 改造交接记录

## 当前真实状态

- M0 软件版本已经发布并远端核验；当前状态为 `released_receipt_pending`，仅剩回执文档 PR/CI/合并。不得重复创建 Release 或移动 Tag。
- 目标仓库：`1040942669/legal-rag-agent`；远端默认分支为 `master`。
- 发布目标 / 当前 `origin/master` 起点：`cb7e01982ca6fb95cebde1e5d707527fd36a250c`。
- 软件 PR：[PR #2](https://github.com/1040942669/legal-rag-agent/pull/2)，于 `2026-09-19T07:02:14Z` 以普通 merge commit 合并，保留任务开始前已有的 Phase 4B 提交 `66da8e3056f8287997088d9f503846fd88c9b038`。
- Tag / Release：`v0.1.1` / [GitHub Release](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.1.1)，发布时间 `2026-09-19T07:05:12Z`。
- 当前回执分支：`codex/m0-release-receipt`，从已发布的 `origin/master` 创建；软件 Tag 不包含也不需要包含后续回执提交。
- Issue #1 已关闭；Milestone 1 在回执合入后关闭。
- M1-M7 均未开始；active milestone 仍为 M0。

## 已完成并核实

1. 审计真实工作区、origin、默认分支、HEAD、未提交内容、远端版本和仓库规则，保留用户原有提交、未跟踪文档与被忽略本地资料。
2. 新增完全虚构且明确不是法律文本的合成 fixture；BM25 smoke 在该测试中硬阻断 Python socket。
3. 新增 `scripts/quality_gate.py`：M0/offline 统一执行全量 pytest、合成 smoke、版本、CLI、Markdown、STATE/manifest JSON 和候选凭证风险检查；未知 stage/mode 以 2 退出。
4. 新增 PR / `master` push CI：只读权限，不使用 `pull_request_target`，不注入模型密钥，Action 固定完整 SHA，Python 固定 3.12.13，uv 固定 0.10.10。
5. 版本统一为 `0.1.1`，`pyproject.toml` 为权威来源；lock 只更新本包元数据。CLI `--help` 不再预先读取 `.env`。
6. README、评测方案、历史执行计划、结果报告和 ADR 已区分旧 Phase 与当前 M0-M7，并明确 verifier、citation 与 refusal 指标限制。
7. PR #2 最终 head `e426424b892bedd3bc1c5c13a34e94e7f1496c2f` 通过 [CI 35428009054](https://github.com/1040942669/legal-rag-agent/actions/runs/35428009054)。
8. 实际 merge commit `cb7e01982ca6fb95cebde1e5d707527fd36a250c` 通过 [master CI 35428226839](https://github.com/1040942669/legal-rag-agent/actions/runs/35428226839)。
9. 远端 annotated tag object 为 `9072726ee99c3d13903069f78b747f02a85c8093`，peeled target 为上述 merge commit；Release API/CLI 确认非 draft、非 prerelease、无附件。

## 真实验证结果

- 修改前基线：`70 passed in 0.43s`。
- M0 候选本地统一门禁：7/7 必需检查通过；全量 89 passed；合成 smoke 1 passed。
- 最终 PR CI：89 passed，24 个 Markdown、2 个 JSON、79 个 Git candidate 文本凭证形状扫描均通过。
- lock / 环境：`uv lock --check`、`uv sync --check --offline --frozen` 均以 0 退出。
- 发布包：`uv build --offline` 成功产生 `0.1.1` sdist/wheel；wheel 在全新临时 Python 3.12.13 venv 离线安装并导入，版本核对为 `0.1.1`。
- 验收报告：`reports/refactor/M0.md`；机器可读回执：`docs/refactor/receipts/M0.json`。

## 没有做的事情

- 没有运行在线或本地生成模型、Embedding、reranker、LLM judge、真实法律语料实验或付费调用。
- 没有读取/上传 `.env` 值、完整语料、Embedding cache、私人课程/简历/面试材料或大型产物。
- 没有引入数据库、FastAPI、LangGraph、Redis/Celery，没有部署生产。
- 没有开始 M1；verifier/refusal 的代码级语义修复仍属于未来 M1。
- 没有把历史 203 部法律或模型质量数字冒充 M0 新实测。

## 已知限制

- M0 证明离线工程基线，不证明真实法律问答质量或生产就绪。
- 当前 verifier 只检查引用 rank、免责声明字符串、宽泛拒答词和有限词面启发式，不证明 claim-source 语义支持或法律正确性。
- 旧 `Refusal correctness` 会把非风险样例自动记为 true，且不测过度拒答。
- 项目声明 Python `>=3.10`，M0 必需门禁只固定验证 Python 3.12 系列。
- 凭证形状扫描不是绝对无泄漏保证，候选另经人工文件清单与完整 diff 审查。
- GitHub CI 安装锁定依赖时可以联网；门禁子进程使用离线模式，合成 smoke 另在 Python 进程内阻断 socket，并非 runner 的 OS 级 air-gap。

## 回滚

- 代码：通过新的 revert PR 回滚 `cb7e01982ca6fb95cebde1e5d707527fd36a250c`，不强推、不重写历史。
- 数据：本版没有数据库或语料迁移，无数据回滚动作。
- 任务/服务：本版没有队列、后台任务或生产部署，无运行中任务需要恢复。
- 版本：已发布 `v0.1.1` 不移动、不复用；修复使用后续版本。

## 回执收尾步骤

1. 提交并 push 当前回执分支。
2. 创建独立文档 PR，记录其 URL，将 STATE/HANDOFF 终态更新为 `released`。
3. 对回执 PR 最新 head 运行同一离线 CI；复核仅文档 diff 后正常合并。
4. 等待回执 merge SHA 的 `master` CI 成功，关闭 Milestone 1。
5. 报告 M0 的真实 PR、Tag、Release、commit、测试和限制，然后停止，不开始 M1。

## 执行事实表

| 项目 | 当前值 |
|---|---|
| 当前里程碑 | M0 / `released_receipt_pending` |
| 软件分支 / 回执分支 | `codex/m0-baseline` / `codex/m0-release-receipt` |
| 最终 PR head | `e426424b892bedd3bc1c5c13a34e94e7f1496c2f` |
| Release target | `cb7e01982ca6fb95cebde1e5d707527fd36a250c` |
| 软件 PR | [#2](https://github.com/1040942669/legal-rag-agent/pull/2) / merged |
| Tag / Release | `v0.1.1` / [published](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.1.1) |
| CI | PR head success / merge SHA `master` success |
| 测试 | 89 passed；门禁 7/7；合成 smoke 1 passed；wheel 构建/安装 success |
| 阻塞 | 无软件阻塞；仅回执尚未经 PR 合入 `master` |
| 下一条可执行动作 | commit/push 回执分支并创建文档 PR |

## 完成条件

回执 PR 合入后，把 STATE/HANDOFF 状态改为 `released`，记录回执 PR URL；`release_target_sha` 始终保持 `cb7e01982ca6fb95cebde1e5d707527fd36a250c`，即使最终 `master` 因回执提交高于 Tag。随后关闭 Milestone 1 并停止。
