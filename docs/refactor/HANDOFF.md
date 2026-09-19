# 改造交接记录

## 当前真实状态

- M0 实现和发布前验收记录已完成，状态为 `ready_for_release`；包含验收文档的冻结候选已通过精确 SHA CI。本次仅记录该事实的元数据提交仍必须通过最新 head CI，尚未合并、打 Tag 或发布，因此不是 `released`。
- 目标仓库：`1040942669/legal-rag-agent`；远端默认分支为 `master`。
- 远端阅读基线：`4ed96ec921593ef81de61ce545513e3f1211b5db`。
- 保留的既有代码提交：`66da8e3056f8287997088d9f503846fd88c9b038`，包含用户已完成的 Phase 4B。
- 实际开发分支：`codex/m0-baseline`。
- M0 审计提交：`3c5cd1f397791e26465ccd4f27840c434ea18db3`。
- 已验证实现候选：`b3a2b4605a66c66f76ac6f199552c613e623f44d`。
- 已验证冻结候选：`98db5d72f146dce163314050a9968764de2d14e0`。
- GitHub 对象：[Milestone 1](https://github.com/1040942669/legal-rag-agent/milestone/1)、[Issue 1](https://github.com/1040942669/legal-rag-agent/issues/1)、[PR 2](https://github.com/1040942669/legal-rag-agent/pull/2)。

## 已完成并核实

1. 核实真实工作区、origin、默认分支、HEAD、dirty 状态、远端 branches/tags/releases/PR、Actions 和仓库规则；原有提交、未跟踪改造文档和被忽略本地资料均未覆盖。
2. 创建 `BASELINE_AUDIT.md`，记录环境、锁文件、基线 70 个离线测试和不能在 M0 重跑的真实实验。
3. 新增完全虚构且明确不是法律文本的 fixture；BM25 smoke 在 monkeypatch 硬阻断 socket 的情况下通过。
4. 新增 `scripts/quality_gate.py` 和测试；M0/offline 统一执行全量 pytest、合成 smoke、版本、CLI、Markdown、STATE/manifest JSON 和候选凭证风险检查。未知 stage/mode 以 2 退出，缺失、跳过或失败的必需检查不会变绿。
5. 新增 PR / `master` push CI：只读权限，不使用 `pull_request_target`，不注入模型密钥，Action 固定完整 SHA，Python 固定 3.12.13，uv 固定 0.10.10。
6. 版本统一为 `0.1.1`，`pyproject.toml` 为权威来源；lock 只更新本包元数据。CLI `--help` 不再预先读取 `.env`。
7. README、评测方案、历史执行计划、结果报告和 ADR 已区分旧 Phase 与当前 M0-M7，并明确 verifier、citation 与 refusal 指标限制。

## 真实验证结果

- 修改前：`uv run --frozen pytest -q` -> `70 passed in 0.43s`。
- 实现候选本地统一门禁：7/7 必需检查通过；全量 `89 passed in 0.76s`；合成 smoke `1 passed in 0.04s`。
- lock / 环境：`uv lock --check`、`uv sync --check --offline --frozen` 均以 0 退出。
- 发布包：`uv build --offline` 成功产生 `0.1.1` sdist/wheel；wheel 在全新临时 Python 3.12.13 venv 离线安装并导入，版本核对为 `0.1.1`。
- GitHub CI：实现候选 `b3a2b4605a66c66f76ac6f199552c613e623f44d` 的 [run 35427519552](https://github.com/1040942669/legal-rag-agent/actions/runs/35427519552) 成功。
- 冻结候选 CI：包含验收报告的 `98db5d72f146dce163314050a9968764de2d14e0` 的 [run 35427914463](https://github.com/1040942669/legal-rag-agent/actions/runs/35427914463) 成功。
- 安全检查：门禁扫描 78 个 Git candidate 文本，无高置信凭证形状；人工审查文件清单和 diff。扫描是纵深防线，不是无泄漏保证。
- 详细矩阵见 `reports/refactor/M0.md`。

## 没有做的事情

- 没有运行在线或本地生成模型、Embedding、reranker、LLM judge、真实法律语料实验或付费调用。
- 没有读取/上传 `.env` 值、完整语料、Embedding cache、私人课程/简历/面试材料或大型产物。
- 没有引入数据库、FastAPI、LangGraph、Redis/Celery，没有部署生产。
- 没有开始 M1；verifier/refusal 的代码级语义修复仍属于 M1。
- 没有声称复现历史 203 部法律或模型质量数字。

## 发布前下一步

1. 提交并 push 当前 `ready_for_release` 元数据收口。
2. 等待 PR #2 对这个最新 head 重新运行 CI；不得只复用 `98db5d7...` 的绿灯。
3. 复核 base/head、完整 diff、最新 CI 和仓库规则，随后将 draft PR 标为 ready 并正常合并，不使用 admin bypass。
4. 获取真实 merge SHA，等待 `master` push CI 对该 SHA 成功。
5. 确认远端不存在冲突 Tag 后，使 annotated `v0.1.1` 精确指向已验证 merge SHA；创建 draft Release、检查后发布并远端核验。
6. 从更新后的 `origin/master` 创建独立文档分支，写 `docs/refactor/receipts/M0.json`，更新本文件、STATE 和验收报告，经第二个 PR/CI 合并；不移动软件 Tag。
7. 完成 M0 后报告并停止，不开始 M1。

## 执行事实表

| 项目 | 当前值 |
|---|---|
| 当前里程碑 | M0 / `ready_for_release` |
| 实际开发分支 | `codex/m0-baseline` |
| 实际代码基线 | `66da8e3056f8287997088d9f503846fd88c9b038` |
| 远端阅读基线 | `4ed96ec921593ef81de61ce545513e3f1211b5db` |
| 已验证实现候选 | `b3a2b4605a66c66f76ac6f199552c613e623f44d` |
| 已验证冻结候选 | `98db5d72f146dce163314050a9968764de2d14e0` |
| 已 commit / 已 push | 是 / 是（`3c5cd1f...`、`b3a2b46...`） |
| PR / planned Tag / Release | PR #2 / `v0.1.1` / 尚未发布 |
| 测试 | 本地 89 passed；门禁 7/7；候选 GitHub CI success；wheel 构建/安装 success |
| 阻塞 | 无；合并前仍必须等待本次元数据提交的最新 head CI |
| 下一条可执行动作 | 提交/push 本状态收口，等待最新 head CI 后复核并合并 |

## 发布后必须替换的内容

发布完成后，记录实际 PR、merge SHA、`v0.1.1` remote peeled target、Release URL/时间、`master` CI、回执 PR/commit，并把状态更新为 `released`。若软件 Release 已发布但回执尚未合并，使用 `released_receipt_pending`，不能重复发版或移动 Tag。
