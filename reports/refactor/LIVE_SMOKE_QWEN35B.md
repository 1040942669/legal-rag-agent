# 首轮 Qwen3.5-35B 真实接入验收记录

## 范围与当前状态

本次仅实现和执行用户明确授权的首轮本地 smoke：`Qwen/Qwen3.5-35B-A3B`、article + BM25 top-5、1 次 JSON 探测及固定 9 条已有回归题，最多 10 次尝试、2 元费用政策预算。没有实施 M7，没有重新发布 M6，也没有启用 Judge、adaptive、改写、压缩、reranker、云 embedding 或 Langfuse 实发。

当前状态为 `stopped_after_first_rag_verifier_failure`。用户已确认中国站输入低于 128k 时 0.40 / 3.20 元每百万 token、实名、余额和模型权限。完整离线门禁和独立安全审查通过后，真实执行仅发生 2 次尝试；probe 通过，但首个 RAG 草稿引用对齐失败，程序停止并保留失败，不重试或补跑。

## 基线与变更

- 已核验真实工作目录、origin `1040942669/legal-rag-agent`、远端默认分支 master，并 fetch 最新 origin/master。
- 工作分支 `codex/live-smoke-qwen35b` 从 `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc` 创建。该 HEAD 是已存在的 M6 finalization PR #30 merge，不是本次尚未提交代码的候选 SHA。原有本地 master 未用作基线，原分支和代码均保留。
- 本次修改 SiliconFlow 客户端的 keyword-only 可选限制，保留默认请求参数；新增单次授权持久账本、固定 smoke CLI 及三份离线回归测试。README、使用说明、STATE 与 HANDOFF 同步记录局部实验，不改包版本或 milestone 状态。
- 账本绑定 `qwen35b-first-smoke-20261003`，和输出目录无关。新输出目录不能重开本轮额度；已有账本禁止 dispatch。
- M5 `v0.6.0`、M6 `v0.7.0` / `v0.7.1` 和 Release 保持不变。不创建新的回执、PR、Tag 或 Release。

## 已执行验证

环境为 Windows 本地 `.venv`，Python 3.12 系列、openai 2.40.0、httpx 0.28.1。以下属于本地未提交实现，不冒充精确 PR / master CI。

| 验证 | 实际结果 |
| --- | --- |
| Provider 控制测试初始红灯 | 20 failed, 1 passed，缺少待实现的 keyword-only 参数和 metadata |
| Provider 控制及已有 provider 回归 | 101 passed in 4.58s，纯 fake / mock HTTP |
| 增强 provider 控制 + 已有 provider + 初版预算 | 180 passed in 2.29s，纯 fake / mock HTTP |
| 预算初始红灯 | 待实现模块不存在，测试收集失败 |
| 思考标志类型回归红灯 | 12 failed, 60 deselected，复现类型绕过，修复后严格 bool |
| 错模型费用回归红灯 | 1 failed, 72 deselected，复现错误费率被标为已知，已修复为 unknown |
| 最终预算专项 | 73 passed in 0.92s，纯 fake |
| Runner 首轮 | 1 failed, 22 passed，测试读取错误 Trace 字段；按现有结构合同修正 |
| Runner 最终专项 | 30 passed in 1.22s，纯 fake |
| 首轮失败格式的新增合成回归 | 单项 1 passed in 0.21s；完整 runner 31 passed in 1.21s；不读取真实 raw、不改变 runtime |
| 独立安全审查首次复测 | 115 passed, 1 failed in 3.14s，Trace 顶层字段测试错误，失败保留 |
| 独立安全审查最终复测 | 131 passed in 3.50s，纯 fake；代码和文档书面审查已通过 |
| 零调用真实本地预检 | exit 0，`preflight_passed`，模型次数 0、provider 未初始化 |
| 执行前完整累计 M2 离线门禁 | 25/25 mandatory passed，exit 0，177275 ms；全量 1144 passed + 157 subtests in 126.58s，JUnit 1301/0/0/0 |
| 真实提供商调用 | 2 次，无失败/重试的提供商响应；RAG verifier 拒绝草稿并停止整轮 |
| 最终三个新专项 | 132 passed in 3.37s，JUnit 132/0/0/0，仅 fake / mock HTTP |
| 执行后完整累计 M2 离线复验 | 25/25 mandatory passed，exit 0，176154 ms；全量 1145 passed + 157 subtests in 124.34s，JUnit 1302/0/0/0 |
| Ruff | not_run，当前 `.venv` 未安装 ruff；未为此联网安装依赖 |

预检命令为 `.venv\Scripts\python.exe -B scripts/live_model_smoke.py --preflight --run-id live_smoke_preflight_20261003`，artifact 在 ignored `artifacts/experiments/live_smoke_preflight_20261003`。实际索引为 19,050 chunks、205 个索引来源文件；索引 SHA-256 `0b705f64dcc4235517516def05bbd7ae46377cb6704181fad85afba14f2b8ec2`，来源集合 hash `f7319964ea679fca22206b6f99d8daa42ec19f7b0cf0b36040d78ec342be9640`。全部文章与固定本地公开目录解析内容匹配，上传时只保留来源 basename。

固定 8 条可生成问题的预检 prompt 为 4,063 至 5,229 UTF-8 字节，均低于 24,000 字节限制。拒答题在检索前停止，预期 0 次生成。预检还保留网购退货题 `hit_at_5=0` / `target_coverage=0` 的真实检索结果，不把这一失败改成成功。

## 首轮真实结果

Run ID 为 `live_smoke_20261003_first`。manifest 于 `2026-10-02T16:25:46.5449686Z` 写入、summary 于 `16:25:51.1910748Z` 写入（北京时间 2026-10-03 00:25:46 至 00:25:51，文件时间不是精确远端计费时间）。摘要 `status=stopped`、`stop_reason=generated_verifier_failed`、客户端关闭 `closed`。处理 1/9 个 RAG case，草稿通过 0/1，剩余 8 个不运行；预检拒答判断不冒充整轮真实执行完成。

| 调用 | 用途 | 输入 token | 输出 token | 总 token | 估算费用 CNY | 结果 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 1 | JSON 兼容 probe | 27 | 5 | 32 | 0.0000268 | JSON、完整用量、模型一致、非思考观察检查通过 |
| 2 | v3_lookup_patent_term | 967 | 169 | 1136 | 0.0009276 | Provider 检查及 schema 通过，草稿 verifier 拒绝 |
| 合计 | 2 次尝试，0 自动重试 | 994 | 174 | 1168 | 0.0009544 | 未继续消耗额度 |

第一题命中目标 rank 1，Hit@5 / target coverage 均为 1。生成草稿 `schema_valid=true`，claims 中来源 `S1` 存在，但正文没有可识别的 `[S1]` 引用，因此 `citation_alignment_valid=false`、`citation_ids_valid=false`，错误为 `citation_ids_invalid`，不是 API 连接失败或目标法条不存在。既有安全逻辑拒绝该草稿并生成 `insufficient_evidence` 受限响应。

最终受限响应的 `verifier_pass=true` **不能当作模型草稿成功**；原始草稿 `generation_attempt.status=rejected`、`verification.passed=false` 已保留，`response_mode_correct=false`、`keyword_coverage=0` 也不改写。摘要优先检查拒绝前 verifier，未被降级后绿灯掩盖。语义支持保持 `not_checked`，没有 Judge 或人工法律质量评审，不宣称法律结论正确。

两条账本 row 的 `succeeded` 仅表示 provider / token / 非思考 / JSON 检查通过，不表示业务 RAG 通过。账本已存在，任意新输出目录都不能重新获得本轮额度。禁止删除账本或以剩余预算为理由补跑。下一轮若要改提示、扩大样本或再发真实请求，需要另行明确授权；本轮没有修改既有引用要求来迎合模型输出。

本地 ignored 证据：

- `artifacts/experiments/live_smoke_20261003_first/manifest.json`，SHA-256 `79fa56a4ffd4d6503567674e39fefa5fcbc8f1b5dc12793c39170def263f0644`。
- 同目录 `summary.json`，SHA-256 `99072a7152d2b29f77dccdfad0d1b25ffa3e77c431808c422821e98a8fa34cb6`。
- `artifacts/experiments/.live_authorizations/qwen35b-first-smoke-20261003.json`，SHA-256 `3d841f013f26941bc1c772f972ab4e242b493e611129eaa446a8ef834f3fa1f8`。
- 原始 prompt / response / case trace 仅在同一 ignored 输出目录，不复制进 Git。manifest 的 tracked diff SHA-256 为 `63c5eb3317f3fa886804cdf3c47ccdb767e8fd980146b435d191ec653d55743d`，另外保存 untracked 关键代码 hash。

真实执行关键代码 SHA-256（独立审查、执行及后续记录期间 runtime 不修改）：

| 文件 | SHA-256 |
| --- | --- |
| legal_rag/llm.py | b551877b4ea15dd422317f5d20acd8278cb0395e4b3209b42eb30c0b28bf62dc |
| legal_rag/live_budget.py | 368e576d6ce821c54e4c53f67dfb85f2c8a315692ec1184b9342500f87180bc0 |
| scripts/live_model_smoke.py | 268bdc85db4015ec91072ea037cc3ae4906b8d98791b41ba7d45f3882c246634 |

## 可复现验证记录

以下离线命令使用 Windows / Python 3.12.13、`ALLOW_LIVE_MODEL_CALLS=false`、`LEGAL_RAG_DISABLE_DOTENV=true`；fake 测试中的局部 opt-in 与 mock HTTP 不触发真实网络。独立审查环境 Key 仅为 `fake-offline-key`，不加载用户 `.env`。

| test_id | command | executed_at | exit_code / status | output_summary | artifact_path |
| --- | --- | --- | --- | --- | --- |
| LIVE-PREFLIGHT | `.venv\Scripts\python.exe -B scripts/live_model_smoke.py --preflight --run-id live_smoke_preflight_20261003` | 2026-10-03 本地预检，文件时间见 artifact | 0 / passed | 0 calls，provider_initialized=false | artifacts/experiments/live_smoke_preflight_20261003/summary.json |
| LIVE-INDEPENDENT-FAKE | `.venv\Scripts\python.exe -B -m pytest tests/test_live_budget.py tests/test_siliconflow_live_controls.py tests/test_live_model_smoke.py -q` | 真实执行前；由独立审查代理回报 | 0 / passed | 131 passed in 3.50s | 本次审查消息，未声称持久 JUnit |
| LIVE-M2-BEFORE-EXECUTE | `.venv\Scripts\python.exe -B scripts/quality_gate.py --milestone M2 --mode offline --output .tmp/live-smoke-before-execute-m2.json` | 2026-10-02T16:22:04Z 至 16:25:02Z | 0 / passed | 25/25，1144 + 157，JUnit 1301/0/0/0 | .tmp/live-smoke-before-execute-m2.json |
| LIVE-EXECUTE | `.venv\Scripts\python.exe -B scripts/live_model_smoke.py --execute --run-id live_smoke_20261003_first` | 2026-10-02T16:25:46Z 至 16:25:51Z，文件时间 | 0 / stopped | 2 calls，API probe passed，RAG verifier rejected | artifacts/experiments/live_smoke_20261003_first/summary.json |
| LIVE-FINAL-FAKE | `.venv\Scripts\python.exe -B -m pytest tests/test_live_budget.py tests/test_siliconflow_live_controls.py tests/test_live_model_smoke.py -q --junitxml=.tmp/live-smoke-targeted-final.xml` | 2026-10-03T00:32:11.874014+08:00 | 0 / passed | 132 passed in 3.37s，JUnit 132/0/0/0 | .tmp/live-smoke-targeted-final.xml |
| LIVE-M2-AFTER-EXECUTE | `.venv\Scripts\python.exe -B scripts/quality_gate.py --milestone M2 --mode offline --output .tmp/live-smoke-after-execute-m2.json` | 2026-10-02T16:33:38Z 至 16:36:35Z | 0 / passed | 25/25，1145 + 157，JUnit 1302/0/0/0 | .tmp/live-smoke-after-execute-m2.json |

`LIVE-EXECUTE` 的 CLI 按当前源码对 stopped 状态应返回 2；本次工具却报告 shell 进程 exit 0。另用不调用模型的 `python -c "import sys; sys.exit(2)"` 加相同 try/finally 环境恢复做离线诊断，确认 child exit 2 可以与 shell/tool exit 0 并存，最外层未传递应用退出码。不能用工具 exit 0 当业务成功；上述 `0 / stopped` 保留真实工具退出状态，以持久 summary 的 stopped 判定本轮结果。未重新发请求来制造退出码一致。

两次完整门禁运行期间均冻结 tracked 文件。执行后的新 fake 测试只使用合成 fixture，runtime 与真实运行代码 hash 一致。最后只补写本地门禁结果和停止交接，再核验文档链接、JSON 和凭证风险；没有创建最终提交或声称精确远端 CI 成功。

结果回写后的独立静态复核三项全部 passed：38 个候选 Markdown 的相对链接、STATE 与 manifest JSON，以及 259 个候选文本文件的高置信凭证风险扫描。生成证据为 ignored `.tmp/live-smoke-final-static.json`，记录真实命令标识、环境、执行时间和 exit 0；`git diff --check` 亦为 exit 0。未安装 Ruff，因此其未运行项保持明确，不用其他检查冒充 Ruff 通过。

## 证据边界与未运行项

- 费用为用户确认价格与 API 完整 usage 的估算，不是账号实际账单。UTF-8 预占不是提供商 tokenizer 或账单硬保证；未知、异常及失败的预占不释放，错模型费率记 unknown。
- 当前规则 verifier 不证明引用语义或法律结论正确。旧题回归不是新 holdout；不能据此改默认模型、声明准确率提升或证明所有当前法律有效。
- 30 条完整生成评测、Qwen 122B 对照、DeepSeek 独立 Judge、人工法律质量评审、API / M6 queue 的 live 接入、真实 broker/DB 故障复验、远端 CI、生产部署与 M7 全部 not_run。
- 本次尚未 commit / push，无本次 PR / Tag / Release。HEAD 为基线 `4d9546e...`，不将旧发布门禁算成本次实现的门禁。

使用及停止规则见 [真实模型 Smoke 说明](../../docs/LIVE_MODEL_SMOKE.md)。
