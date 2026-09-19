# 改造交接记录

## 当前真实状态

- M0 软件版本已经发布并远端核验；终态为 `released`。发布回执 [PR #3](https://github.com/1040942669/legal-rag-agent/pull/3) 已于 `2026-09-19T07:13:37Z` 合并，`master` merge commit `52714d5f84634f008a5860f6bf4f5aa199ae3261` 的 [CI 35428743968](https://github.com/1040942669/legal-rag-agent/actions/runs/35428743968) 成功，Milestone 1 已关闭。
- 目标仓库：`1040942669/legal-rag-agent`；远端默认分支为 `master`。
- M0 Release target 仍为 `cb7e01982ca6fb95cebde1e5d707527fd36a250c`；M1 工作基线为当前 `origin/master` 的 `52714d5f84634f008a5860f6bf4f5aa199ae3261`。
- 软件 PR：[PR #2](https://github.com/1040942669/legal-rag-agent/pull/2)，于 `2026-09-19T07:02:14Z` 以普通 merge commit 合并，保留任务开始前已有的 Phase 4B 提交 `66da8e3056f8287997088d9f503846fd88c9b038`。
- Tag / Release：`v0.1.1` / [GitHub Release](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.1.1)，发布时间 `2026-09-19T07:05:12Z`。
- 当前工作分支：`codex/m1-verification`，从上述最新 `origin/master` 创建；工作区在启动时干净。
- M1 已明确由用户授权连续推进，状态为 `in_progress`；[Issue #4](https://github.com/1040942669/legal-rag-agent/issues/4) 与 [Milestone 2](https://github.com/1040942669/legal-rag-agent/milestone/2) 已创建。
- M1 已推送的实现 HEAD 为 `b46228b137db832ff454fd1d2cedd2d92a9c11ef`。结构化验证核心、范围过滤和评测 schema v2 主体已完成；当前文档、合成样例和 canonical metrics 映射修正尚未提交，尚未创建 PR、Tag 或 Release。
- M2-M7 尚未开始；每个阶段仍独立测试、PR、合并、Tag、Release 和回执。

## M1 启动边界

- 计划版本：`v0.2.0`；范围只包含验证边界、结构化回答兼容层、指标语义与 M1 累积门禁。
- 必须覆盖 `M1-T01` 至 `M1-T10`，包括免责声明/拒答分离、伪造引用、真引用但无语义支持、资料不足模式、顺序隔离、retrieval-only N/A、judge 错误、过度拒答和跨快照/权限证据。
- 默认只运行离线 fixture/fake 测试；未授权任何付费模型调用，不引入数据库、FastAPI、LangGraph、Redis/Celery 或生产部署。
- 每个可测试模块完成后 commit/push 工作分支；M1 全部通过后才冻结候选并发布。

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
10. M1 结构化回答已区分 `evidence_answer / insufficient_evidence / needs_clarification / out_of_scope`，并保留旧纯文本适配器但降低其可验证性。
11. M1 verifier 已拆分 schema、证据目录、引用 ID/对齐、快照/权限范围、免责声明、回答模式与语义状态；词面启发式不产生 `supported`。
12. 越权证据在进入生成提示和返回 sources 前被过滤；伪造引用或无效模式的草稿不会作为最终回答交付，安全终态会再次验证。
13. M1 评测 schema v2 已实现显式行为分母、retrieval-only N/A、Judge 成功/失败/未执行三态、历史输出防覆盖，以及生成尝试/最终交付分离的 Trace。
14. 两组完全虚构、非法律的改前/改后 fixture 已覆盖免责声明误判拒答和真实 ID 不等于语义支持；它们只证明规则边界，不是法律质量 benchmark。

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
- M1 尚未创建 PR、合并、打 Tag 或发布；`v0.2.0` 仍是计划版本，最新已核验 Release 仍为 `v0.1.1`。
- M1 尚未实现独立质量门禁入口或更新 CI workflow；当前只真实运行了全量 pytest、M1 专项和累计 M0 门禁。
- 没有把历史 203 部法律或模型质量数字冒充 M0 新实测。

## 已知限制

- M0 证明离线工程基线，不证明真实法律问答质量或生产就绪。
- 当前 verifier 已能严格检查结构、引用目录/对齐和可选范围，但默认语义层仍只有 `uncertain/not_checked`；它不证明 claim-source 语义支持或法律正确性。
- 旧 `Refusal correctness` 仅在兼容输出中保留；新 schema 使用拒答召回与过度拒答的独立分母，不能与旧总均值直接比较。
- 项目声明 Python `>=3.10`，M0 必需门禁只固定验证 Python 3.12 系列。
- 凭证形状扫描不是绝对无泄漏保证，候选另经人工文件清单与完整 diff 审查。
- GitHub CI 安装锁定依赖时可以联网；门禁子进程使用离线模式，合成 smoke 另在 Python 进程内阻断 socket，并非 runner 的 OS 级 air-gap。

## 回滚

- 代码：通过新的 revert PR 回滚 `cb7e01982ca6fb95cebde1e5d707527fd36a250c`，不强推、不重写历史。
- 数据：本版没有数据库或语料迁移，无数据回滚动作。
- 任务/服务：本版没有队列、后台任务或生产部署，无运行中任务需要恢复。
- 版本：已发布 `v0.1.1` 不移动、不复用；修复使用后续版本。

## M0 回执收尾核对

1. 回执 PR #3 已正常合并，最终 head 为 `be34c68b2bd9f5bcc1405ec1437b30d0444fe420`。
2. 回执 merge SHA 的 `master` CI 已成功，Milestone 1 已关闭。
3. M0 不再有远端收尾动作，不得重复发版或移动 `v0.1.1`。

## 执行事实表

| 项目 | 当前值 |
|---|---|
| 当前里程碑 | M1 / `in_progress`（M0 已 `released`） |
| 当前工作分支 | `codex/m1-verification` |
| M1 已推送实现 HEAD | `b46228b137db832ff454fd1d2cedd2d92a9c11ef` |
| M1 PR / Tag / Release | not_created / not_created / not_created |
| 上一已发布版本 | `v0.1.1` / [Release](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.1.1) / target `cb7e01982ca6fb95cebde1e5d707527fd36a250c` |
| M1 本地测试 | 145 passed，60 subtests passed；无真实模型或语料调用 |
| 累计 M0 门禁 | 7/7 passed；门禁内 145 passed、60 subtests passed |
| M1 Issue / Milestone | [#4](https://github.com/1040942669/legal-rag-agent/issues/4) / [Milestone 2](https://github.com/1040942669/legal-rag-agent/milestone/2) |
| 阻塞 | 无 |
| 下一条可执行动作 | 提交并推送 M1 文档、合成样例和 canonical metrics 映射模块，然后实现独立 M1 累积质量门禁与 CI |

## 完成说明

`v0.1.1` 的 `release_target_sha` 始终保持 `cb7e01982ca6fb95cebde1e5d707527fd36a250c`，即使当前 `master` 因回执提交高于 Tag。M1 从回执合并后的 `master` 开始，不能移动或复用旧 Tag。
