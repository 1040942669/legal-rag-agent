# 改造交接记录

## 当前真实状态

- M0 软件版本已经发布并远端核验；终态为 `released`。发布回执 [PR #3](https://github.com/1040942669/legal-rag-agent/pull/3) 已于 `2026-09-19T07:13:37Z` 合并，`master` merge commit `52714d5f84634f008a5860f6bf4f5aa199ae3261` 的 [CI 35428743968](https://github.com/1040942669/legal-rag-agent/actions/runs/35428743968) 成功，Milestone 1 已关闭。
- 目标仓库：`1040942669/legal-rag-agent`；远端默认分支为 `master`。
- M0 Release target 仍为 `cb7e01982ca6fb95cebde1e5d707527fd36a250c`；M1 工作基线为当前 `origin/master` 的 `52714d5f84634f008a5860f6bf4f5aa199ae3261`。
- 软件 PR：[PR #2](https://github.com/1040942669/legal-rag-agent/pull/2)，于 `2026-09-19T07:02:14Z` 以普通 merge commit 合并，保留任务开始前已有的 Phase 4B 提交 `66da8e3056f8287997088d9f503846fd88c9b038`。
- Tag / Release：`v0.1.1` / [GitHub Release](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.1.1)，发布时间 `2026-09-19T07:05:12Z`。
- 当前工作分支：`codex/m1-verification`，从上述最新 `origin/master` 创建；工作区在启动时干净。
- M1 已明确由用户授权连续推进；当前状态为 `ready_for_release`。[Issue #4](https://github.com/1040942669/legal-rag-agent/issues/4)、[Milestone 2](https://github.com/1040942669/legal-rag-agent/milestone/2) 与 [PR #5](https://github.com/1040942669/legal-rag-agent/pull/5) 已创建。Tag / Release 仍未创建，因此 M1 尚未发布。
- M1 冻结软件候选为 `647c0238674abe603fdf33e80191c19eb6de3dd9`。它已完成 `0.2.0` 包构建与隔离安装，并由显式检出同一 PR head SHA 的 [Actions run 35461753394](https://github.com/1040942669/legal-rag-agent/actions/runs/35461753394) 验证累计门禁。更早的 `b7c564a...`、`8a31a36...` 和 merge-ref 证据只保留历史追溯价值。
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
11. M1 verifier 已拆分 schema、证据目录、引用 ID/对齐、可选快照/权限范围、免责声明、回答模式与语义状态；词面启发式不产生 `supported`。
12. 只有可信调用方显式注入 `VerificationContext` 时，越界证据才会在进入生成提示和返回 sources 前被过滤并再次核验。当前 CLI 没有认证身份、租户隔离或默认 active snapshot；这些能力不能提前算作完成。伪造引用或无效模式的草稿不会作为最终回答交付，安全终态会再次验证。
13. M1 评测 schema v2 已实现显式行为分母、retrieval-only N/A、Judge 成功/失败/未执行三态、历史输出防覆盖，以及生成尝试/最终交付分离的 Trace。
14. 两组完全虚构、非法律的改前/改后 fixture 已覆盖免责声明误判拒答和真实 ID 不等于语义支持；它们只证明规则边界，不是法律质量 benchmark。
15. M1 累积门禁已覆盖 7 个 M0 必需检查和 `M1-T01` 至 `M1-T10`，共 17 个唯一检查；mandatory pytest 遇到零测试、skip、xfail、JUnit 缺失或解析失败都会失败关闭。
16. GitHub Actions 已改为运行 M1 累积门禁并始终上传机器可读 JSON；产物名含 commit SHA 与 run attempt，Action 固定完整 SHA，权限仍为只读。
17. 离线门禁子进程同时设置 `ALLOW_LIVE_MODEL_CALLS=false` 与 `LEGAL_RAG_DISABLE_DOTENV=1`；Ollama、SiliconFlow、SentenceTransformer 和远程 embedding 入口会在初始化或调用前失败关闭。本次未读取或暂存被忽略的本地 `.env`。
18. 以 `647c0238674abe603fdf33e80191c19eb6de3dd9` 软件代码和定稿发布 README 构建的 `0.2.0` sdist/wheel 已通过核验；全新 Python 3.12.13 venv 离线 no-deps 安装后，distribution/module/console entry point 和模块来源均核对正确，wheel/sdist 共 88 个归档条目，禁止路径为 0。
19. PR #5 的 [Actions run 35461753394](https://github.com/1040942669/legal-rag-agent/actions/runs/35461753394) 显式检出同一 head `647c023...` 并成功；下载 JSON 确认 17/17、186 passed、148 subtests、JUnit 334/0/0/0。产物名为 `m1-quality-gate-647c0238674abe603fdf33e80191c19eb6de3dd9-1`。
20. Normalizer、结构化回答和 Judge 的模型 JSON 入口已统一拒绝重复 key、非标准非有限数值、孤立 surrogate、超限或资源异常输入；引用/拒答检查覆盖 Unicode 括号与不可见格式字符并保守失败关闭。
21. 被拒绝草稿正文和内容型 verifier 诊断不进入普通 Trace；安全 serializer 使用显式 allowlist，生成的 schema、引用片段和 source ID 列表只保留计数。Trace 仍记录用户 query、派生查询和检索 metadata，评测记录仍保存最终回答与成功 Judge comment，不能宣称全链路脱敏。

## 真实验证结果

- 修改前基线：`70 passed in 0.43s`。
- M0 候选本地统一门禁：7/7 必需检查通过；全量 89 passed；合成 smoke 1 passed。
- 最终 PR CI：89 passed，24 个 Markdown、2 个 JSON、79 个 Git candidate 文本凭证形状扫描均通过。
- lock / 环境：`uv lock --check`、`uv sync --check --offline --frozen` 均以 0 退出。
- 发布包：`uv build --offline` 成功产生 `0.1.1` sdist/wheel；wheel 在全新临时 Python 3.12.13 venv 离线安装并导入，版本核对为 `0.1.1`。
- 验收报告：`reports/refactor/M0.md`；机器可读回执：`docs/refactor/receipts/M0.json`。
- M1 累积门禁：精确检出软件候选 `647c023...` 的 PR-head CI 为 17/17 通过，全量 `186 passed, 148 subtests passed`，JUnit 汇总 `334 tests, 0 failures, 0 errors, 0 skipped`；合成 smoke 与 `M1-T01` 至 `M1-T10` 均独立通过。
- M1 环境与工作流：workflow YAML 可解析，`uv lock --check` 与 `uv sync --check --offline --frozen` 均通过；未知 milestone/mode 生成配置错误报告并返回逻辑退出码 2。
- M1 最终发布内容候选包：wheel `legal_rag_assistant-0.2.0-py3-none-any.whl`，101,188 bytes，SHA-256 `c5fa5e291e17d2dc5218577af23a1880e0c27ea93abfbd2e097be19c9df4a6f7`；sdist `legal_rag_assistant-0.2.0.tar.gz`，138,515 bytes，SHA-256 `b9172b8a18a983c30dcd9893df4ec808ad31d003e6fa33bcbd943b9896708078`。
- M1 验收报告：`reports/refactor/M1.md`；当前包含 clean 候选与精确 PR-head CI 证据，实际 merge SHA、master CI、Tag、Release 与发布回执仍待对应步骤真实完成。
- M1 PR CI 产物：`m1-quality-gate-647c0238674abe603fdf33e80191c19eb6de3dd9-1`，GitHub digest `sha256:d87112cbdc14efc31425da30eff28dbf73d13cdd6a5b92519ba04591073b0232`；本地解包 JSON SHA-256 `fb17787d94e042bb58d6c32d1400c5b0c5415cd54b1dd0abfdad936b955b818e`。

## 没有做的事情

- 没有运行在线或本地生成模型、Embedding、reranker、LLM judge、真实法律语料实验或付费调用。
- 没有读取/上传 `.env` 值、完整语料、Embedding cache、私人课程/简历/面试材料或大型产物。
- 没有引入数据库、FastAPI、LangGraph、Redis/Celery，没有部署生产。
- M1 尚未合并、打 Tag 或发布；`0.2.0` 目前只是 PR 中已验证候选，最新已核验 Release 仍为 `v0.1.1`。
- 本地门禁 JSON、下载的 CI JSON 与包构建物只保存在被忽略的本地 `.tmp`；它们没有进入 Git 或 Release 附件。GitHub Actions 产物按平台保留策略远端保存。
- 没有把历史 203 部法律或模型质量数字冒充 M0 新实测。

## 已知限制

- M0 证明离线工程基线，不证明真实法律问答质量或生产就绪。
- 当前 verifier 已能严格检查结构、引用目录/对齐和可选范围，但默认语义层仍只有 `uncertain/not_checked`；它不证明 claim-source 语义支持或法律正确性。
- 旧 `Refusal correctness` 仅在兼容输出中保留；新 schema 使用拒答召回与过度拒答的独立分母，不能与旧总均值直接比较。
- 项目声明 Python `>=3.10`，M0 必需门禁只固定验证 Python 3.12 系列。
- 凭证形状扫描不是绝对无泄漏保证，候选另经人工文件清单与完整 diff 审查。
- GitHub CI 安装锁定依赖时可以联网；门禁子进程使用离线依赖模式、禁用 dotenv 并在项目 provider 边界拒绝真实模型调用，合成 smoke 另在 Python 进程内阻断 socket，但仍不是 runner 的 OS 级 air-gap。

## 回滚

- 代码：M1 发布前可关闭 PR 而不触碰 `master`；发布后通过新的 revert PR 回滚实际 M1 merge commit，不强推、不重写历史。
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
| 当前里程碑 | M1 / `ready_for_release`（M0 已 `released`） |
| 当前工作分支 | `codex/m1-verification` |
| M1 冻结软件候选 | `647c0238674abe603fdf33e80191c19eb6de3dd9`；包验证与精确 PR-head CI 已通过 |
| M1 PR / Tag / Release | [PR #5](https://github.com/1040942669/legal-rag-agent/pull/5) / not_created / not_created |
| 上一已发布版本 | `v0.1.1` / [Release](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.1.1) / target `cb7e01982ca6fb95cebde1e5d707527fd36a250c` |
| M1 精确候选测试 | PR-head `647c023...`：186 passed，148 subtests passed；JUnit 334/0 failures/0 errors/0 skipped；无真实模型或语料调用 |
| M1 累积门禁 | 17/17 passed（含 M0 7 项 + M1 10 项）；精确 PR-head CI 得到同一计数 |
| M1 package | `0.2.0` clean build + Python 3.12.13 isolated install/import passed；hash 见上文 |
| M1 PR CI | head/checkout `647c023...` / [run 35461753394](https://github.com/1040942669/legal-rag-agent/actions/runs/35461753394) / success / exact head verified |
| M1 Issue / Milestone | [#4](https://github.com/1040942669/legal-rag-agent/issues/4) / [Milestone 2](https://github.com/1040942669/legal-rag-agent/milestone/2) |
| 阻塞 | 无已知软件阻塞；仍须按发布流程让文档提交通过正常 PR CI，不得绕过 review/protection |
| 下一条可执行动作 | 提交并推送本次发布文档，等待最终 PR head CI；随后普通合并 PR #5，核对 merge SHA 的 `master` CI，再创建 `v0.2.0` annotated Tag 与 GitHub Release |

## 完成说明

`v0.1.1` 的 `release_target_sha` 始终保持 `cb7e01982ca6fb95cebde1e5d707527fd36a250c`，即使当前 `master` 因回执提交高于 Tag。M1 从回执合并后的 `master` 开始，不能移动或复用旧 Tag。
