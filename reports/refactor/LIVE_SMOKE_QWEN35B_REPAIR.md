# Qwen3.5-35B 首轮 Smoke 修复验证记录

## 范围与当前状态

用户在首轮失败说明后明确要求“你直接进行测试和修bug吧”。本次据此修复正文引用提示，并在原首轮授权剩余额度内安排一次受控复验；不是新额度、M7、M6 发布或在线 HTTP / worker 模型接入验收。

本报告初始准备时间为 `2026-10-02T16:53:07Z`，即北京时间 `2026-10-03T00:53:07+08:00`。此时间只标识准备报告时的时钟读取，不冒充测试开始或提供商计费时间。

当前为 `stopped_after_fifth_rag_response_mode_failure`：执行前累计 M2 离线门禁、独立书面审查及零调用预检通过后，真实修复轮已发生 5 次新增请求并按失败规则停止。前 4 个草稿通过工程结构及引用检查，原专利题正文引用问题本轮实测通过；第 5 题检索未命中、回答模式不符被拒绝。不是整个 Smoke 通过，也不是法律准确率提升证明。首轮 2 次加本轮 5 次累计 7 次，估算费用 0.0070528 元，无未知用量或自动重试。停止后新增合成回归及最终四文件专项通过，runtime 未再修改；首次执行后累计 M2 因文档链接失败，文档修正后的最终累计 M2 已 25/25 passed、1191 + 157 subtests / JUnit 1348/0/0/0，原失败记录完整保留。

| 边界 | 固定要求 |
| --- | --- |
| 原总授权 | `qwen35b-first-smoke-20261003`，最多 10 次尝试、2 元人民币政策预算 |
| 提供商与模型 | 中国站 `https://api.siliconflow.cn/v1`，`Qwen/Qwen3.5-35B-A3B` |
| 价格快照 | 用户确认的输入 0.40、输出 3.20 元 / 百万 token，输入低于 128k |
| 已用额度 | 首轮 2 次尝试、完整 token 用量估算 0.0009544 元 |
| 修复轮最大新增额度 | 最多 8 次尝试、1.9990456 元；与首轮合计不超过原 10 次、2 元政策预算 |
| 修复授权执行身份 | `qwen35b-repair-smoke-20261003`，只对应此次余量复验，不重新授予 10 次 / 2 元 |
| 固定真实输出 Run ID | `live_smoke_20261003_repair` |
| 探测处理 | 只读复用首轮已通过 probe 的冻结证据；修复轮不重复发送付费 probe |
| 检索与保护 | article + BM25 top-5，串行、无自动重试、无重定向，输入最多 24,000 UTF-8 字节，输出最多 1,536 token，关闭思考并检查返回证据 |
| 不运行 | Judge、额外模型、30 条扩样、adaptive、LLM 改写/压缩、reranker、embedding API、Langfuse 实发、生产部署 |

当前工作分支为 `codex/live-smoke-qwen35b`，HEAD 仍为基线 `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`，存在本地未提交修改。不得把基线 SHA 或旧发布 CI 当作本次修复候选的提交或门禁。本次没有 commit、push、PR、Tag 或 Release，已发布的 M5/M6 Tag 及 Release 不变。

## 首轮失败事实与本次修复

首轮 Run ID `live_smoke_20261003_first` 的 provider / JSON probe 通过。随后第一条 `v3_lookup_patent_term` 草稿虽在 `claims.source_ids` 中填写 `S1`，但 `answer_text` 没有 verifier 可识别的正文引用；草稿被拒绝，业务摘要为 `status=stopped`、`stop_reason=generated_verifier_failed`，共发生 2 次尝试后停止。

两条首轮 ledger row 的 `succeeded` 仅表示 provider、完整用量、非思考及 JSON 检查通过，不等于草稿通过业务 verifier。降级后的受限响应通过检查，也不能掩盖原始草稿失败。首轮 shell 工具 exit 0 与业务 stopped 并存，该历史保留；外层没有传递 Python child exit，不能将 shell 0 解释为成功，也没有重新请求来制造一个更好退出码。

本次只澄清既有提示合同：

- 正文引用必须在 `answer_text` 对应断言的同一句中，放在句末标点之前。
- `claims.text` 逐字复制该单句正文，不含引用标注或句末标点；不得跨句、跨行。
- `claims.source_ids` 使用 `S1` 形式，必须与该断言同句可见引用一致。
- 示例仅说明布局，使用本次可见 source 编号；无证据示例不编造来源。
- 更新 prompt version 为 `m1-structured-qa-citation-alignment-v2`，parser schema 保持 `m1-structured-answer-v1`。

没有放松 verifier、删除引用失败断言或在模型输出后自动补引文。坏布局，包括缺正文引用、末尾集中引用、引用在句号之后、claim 跨句以及引用来源不一致，仍通过合成负向测试验证为拒绝。

## 原记录不可变与余量验证

以下首轮证据只读保留，不编辑原 ledger、summary、manifest、prompt、response 或 Trace：

| 首轮证据 | 原 SHA-256 |
| --- | --- |
| `artifacts/experiments/.live_authorizations/qwen35b-first-smoke-20261003.json` | `3d841f013f26941bc1c772f972ab4e242b493e611129eaa446a8ef834f3fa1f8` |
| `artifacts/experiments/live_smoke_20261003_first/summary.json` | `99072a7152d2b29f77dccdfad0d1b25ffa3e77c431808c422821e98a8fa34cb6` |
| `artifacts/experiments/live_smoke_20261003_first/manifest.json` | `79fa56a4ffd4d6503567674e39fefa5fcbc8f1b5dc12793c39170def263f0644` |

`read_repair_allowance` 仅读取固定首轮 ledger 与 summary，严格核对哈希、原 policy / model / endpoint / 费率 / 授权身份、两条成功且完整用量的尝试、预占、摘要同证据、旧 probe passed 和旧业务 verifier 失败。金额使用 Decimal 从 token 重算，不信任摘要浮数。实际只读核验得到 2 次、0.0009544 元已用，因此剩余 8 次、1.9990456 元。Runner 另外绑定首轮 manifest 的固定哈希。

未知费用、未决预占、损坏、内容不匹配、活跃锁或其他首轮终态都拒绝进入此次修复路径，不能通过更换输出目录、删除旧账本、构造任意新 ID 或把未知用量视为零来重开额度。新固定修复 ledger 为 `artifacts/experiments/.live_authorizations/qwen35b-repair-smoke-20261003.json`，仍一次性创建，存在即禁止再次 dispatch。

费用只是在用户确认价格与完整 API usage 下的估算，不是账号实际账单。UTF-8 字节加开销的预占不是提供商 tokenizer 或账单硬保证；超时后远端仍可能计算，任何已发请求都不能被本地取消撤销。失败或不确定的预占不退款；修复后再次失败就停止，不自动重试或补齐。

## 验证记录

离线环境沿用本地 Windows / Python `.venv`。离线 tests 仅 fake 或 mock，不读取真实 `.env`、原始响应或发提供商请求。以下 `executed_at` 未捕获精确 UTC 的行明确写出该缺项，不使用报告准备时钟代替。

| test_id | command | environment | executed_at | exit_code / status | output_summary | artifact_path |
| --- | --- | --- | --- | --- | --- | --- |
| REPAIR-PROMPT-RED | `.venv\Scripts\python.exe -B -m pytest tests/test_prompt_citation_contract.py -q` | Windows / 本地 `.venv`，合成 fixture | 2026-10-03；主 agent 回报，精确 UTC 未单独回传 | 1 / failed | 5 failed, 5 passed；缺少新提示合同，旧坏布局负向断言保留 | 本次主 agent 工具回执，未声称持久 JUnit |
| REPAIR-PROMPT-GREEN | `.venv\Scripts\python.exe -B -m pytest tests/test_prompt_citation_contract.py -q` | 同上，零提供商请求 | 2026-10-03；主 agent 回报，精确 UTC 未单独回传 | 0 / passed | 10 passed；仅修提示及 prompt 身份，不改 verifier | 本次主 agent 工具回执，未声称持久 JUnit |
| REPAIR-ALLOWANCE-RED | `.venv\Scripts\python.exe -B -m pytest tests/test_live_budget.py -q -k repair_allowance_reuses_original_authorization` | Windows / 本地 `.venv`，合成 receipt | 2026-10-03；精确 UTC 未记录 | 1 / failed | 1 failed, 99 deselected in 0.21s，待实现 helper 不存在 | 本次预算 agent 工具回执 |
| REPAIR-ALLOWANCE-GREEN | `.venv\Scripts\python.exe -B -m pytest tests/test_live_budget.py -q` | 同上，纯 fake | 2026-10-03；精确 UTC 未记录 | 0 / passed | 100 passed in 1.18s；保留原 73 项，新增 27 项 | 本次预算 agent 工具回执 |
| REPAIR-PRIOR-READONLY | 调用 `read_repair_allowance` 读取固定首轮 ledger 与 summary | 本地、只读、零密钥加载及提供商请求 | 2026-10-03；精确 UTC 未记录 | 0 / passed | prior 2 / 0.0009544 CNY；remaining 8 / 1.9990456 CNY | 原冻结 ledger / summary 及本次工具回执，不生成新付费记录 |
| REPAIR-TARGETED-EARLY | `.venv\Scripts\python.exe -B -m pytest tests/test_prompt_citation_contract.py tests/test_phase3.py tests/test_m1_verification.py tests/test_m1_synthetic_examples.py tests/test_m2_chat_stages.py tests/test_m2_experiment_lifecycle.py tests/test_live_budget.py tests/test_live_model_smoke.py tests/test_siliconflow_live_controls.py -q` | 本地、纯 fake / mock | 2026-10-03；精确 UTC 未单独回传 | 0 / passed | 263 passed + 89 subtests in 27.48s；此后新增 guard 测试，不作为最终全集结果 | 主 agent 工具回执，无持久 JUnit |
| REPAIR-RUNNER-RED | `.venv/Scripts/python.exe -B -m pytest -q tests/test_live_model_smoke.py::test_repair_preflight_is_zero_call_and_binds_prior_successful_probe tests/test_live_model_smoke.py::test_repair_reuses_prior_probe_and_uses_remaining_global_allowance` | 离线 fake / mock | 精确 UTC unknown，原工具未记录，不重建日期 | 1 / failed | 2 failed in 0.42s，两项 TypeError 均为缺少待实现 repair 参数 | Runner 原工具 chunk `7acfab`，本次核实历史证据，未重跑 |
| REPAIR-RUNNER | `.venv\Scripts\python.exe -B -m pytest tests/test_live_model_smoke.py -q` | 离线 fake / mock | 2026-10-03；主 agent 回报，精确 UTC 未单独回传 | 0 / passed | 39 passed in 1.78s，早于停止后新增合成回归 | Runner agent 工具回执，经主 agent 核验回报 |
| REPAIR-TARGETED | `.venv\Scripts\python.exe -B -m pytest tests/test_live_budget.py tests/test_siliconflow_live_controls.py tests/test_live_model_smoke.py tests/test_prompt_citation_contract.py -q --junitxml=.tmp/live-smoke-repair-targeted-final.xml` | 最终四文件，纯 fake / mock | JUnit timestamp `2026-10-03T01:06:43.278854+08:00` | 0 / passed | 178 passed in 4.59s；budget 100 / provider 28 / runner 40 / prompt 10，JUnit 178/0/0/0，XML time 4.585s | .tmp/live-smoke-repair-targeted-final.xml |
| REPAIR-INDEPENDENT-FAKE | `.venv\Scripts\python.exe -B -m pytest tests/test_live_budget.py tests/test_siliconflow_live_controls.py tests/test_live_model_smoke.py tests/test_prompt_citation_contract.py -q` | 独立 fake / mock 复测 | 2026-10-03；主 agent 回报，精确 UTC 未单独回传 | exit 未单独回传 / passed | 177 passed in 3.99s；不等于书面审查已通过 | 独立 reviewer 工具回执，经主 agent 核验回报 |
| REPAIR-INDEPENDENT-REVIEW | 独立只读代码及文档书面审查 | 不发真实请求 | 2026-10-03；精确 UTC 未单独回传 | passed，非命令退出码 | 书面审查已通过；包括失败停止、余量扣除、原记录不可变及 prompt/version 合同 | 独立 reviewer 书面回执，经主 agent 核验回报 |
| REPAIR-M2-INVALIDATION | `.venv\Scripts\python.exe -B -m pytest tests/test_m2_experiment_lifecycle.py::test_stage_contract_invalidation_is_directional -q` | 离线 fixture | 2026-10-03；精确 UTC 未单独回传 | 0 / passed | 1 passed in 0.59s | 独立 reviewer 工具回执 |
| REPAIR-M2-GATE | `.venv\Scripts\python.exe -B scripts/quality_gate.py --milestone M2 --mode offline --output .tmp/live-smoke-repair-before-m2.json` | 禁真实模型的累计工程门禁 | 2026-10-02T16:56:32Z 至 16:59:35Z | 0 / passed | 25/25 mandatory，181635 ms；全量 1190 passed + 157 subtests in 131.65s，JUnit 1347/0/0/0 | .tmp/live-smoke-repair-before-m2.json |
| REPAIR-PREFLIGHT | `.venv\Scripts\python.exe -B scripts/live_model_smoke.py --repair --preflight --run-id live_smoke_20261003_repair_preflight` | 本地、零提供商调用 | manifest / summary 文件写入 `2026-10-02T16:53:43.9804277Z` / `16:53:43.9806437Z` | 0 / passed | provider_initialized=false，live 0，new probe 0；prior 2，remaining 8 / 1.9990456 CNY | artifacts/experiments/live_smoke_20261003_repair_preflight/summary.json |
| REPAIR-EXECUTE | `.venv\Scripts\python.exe -B scripts/live_model_smoke.py --execute --repair --run-id live_smoke_20261003_repair` | 明确 opt-in 后，一次真实本地复验 | 2026-10-02T17:00:32.2128276Z 至 17:00:56.6474699Z，manifest / summary 文件时间 | child 2，shell 2 / stopped | 新 5 次，probe 仅复用旧通过记录；4/5 草稿工程检查通过，第 5 个 response_mode_invalid 后停止 | artifacts/experiments/live_smoke_20261003_repair/summary.json |
| REPAIR-POST-FAILURE-FAKE | `.venv\Scripts\python.exe -B -m pytest tests/test_live_model_smoke.py -q -k repair_response_mode_failure_is_not_hidden_by_valid_fallback`；完整 runner 复测 | 仅合成 fixture，不读取真实 raw 或新增付费调用 | 2026-10-03；精确 UTC 未单独回传 | passed，exit 未单独回传 | 新模式回归首次即 1 passed in 0.30s；全 runner 40 passed in 1.91s，证明既有 stop/fallback，不是新增 runtime 修复 | 主 agent 工具回执 |
| REPAIR-POST-FAILURE-INDEPENDENT | 独立 reviewer 复测同一新合成回归，完整参数未单独回传 | 纯 fake | 2026-10-03；精确 UTC 未单独回传 | passed，exit 未单独回传 | 1 passed in 0.29s | 独立 reviewer 工具回执 |
| REPAIR-M2-AFTER-EXECUTE-FIRST | `.venv\Scripts\python.exe -B scripts/quality_gate.py --milestone M2 --mode offline --output .tmp/live-smoke-repair-after-m2.json` | 禁真实模型 | 2026-10-02T17:10:07Z 至 17:13:02Z | 1 / failed | 24/25 mandatory，173638 ms；全量 1191 passed + 157 subtests in 123.06s，JUnit 1348/0/0/0；唯一失败是历史报告不属于 Git candidate 的链接 | .tmp/live-smoke-repair-after-m2.json |
| REPAIR-M2-AFTER-EXECUTE-FINAL | `.venv\Scripts\python.exe -B scripts/quality_gate.py --milestone M2 --mode offline --output .tmp/live-smoke-repair-after-m2-final.json` | 禁真实模型 | 2026-10-02T17:16:12Z 至 17:19:06Z | 0 / passed | 25/25 mandatory，172669 ms；全量 1191 passed + 157 subtests in 122.66s，JUnit 1348/0/0/0；不覆盖首次失败 | .tmp/live-smoke-repair-after-m2-final.json |
| REPAIR-DOCFIX-STATIC | `quality_gate._candidate_static_records(repo_root, "M2")` | 禁真实模型的候选静态检查 | 2026-10-02T17:15:48Z 至 17:15:49Z | 0 / passed | 39 个 Markdown、2 个 JSON、261 个候选文本，三项全部通过；未上传 ignored 历史报告 | .tmp/live-smoke-repair-docfix-static.json |
| REPAIR-RESULTS-STATIC | `quality_gate._candidate_static_records(repo_root, "M2")` | 结果回写后的禁真实模型候选静态检查 | 2026-10-02T17:21:26Z | 0 / passed | 39 个 Markdown、2 个 JSON、261 个候选文本，三项全部通过；`git diff --check` 同为 exit 0 | .tmp/live-smoke-repair-final-static.json |

此次真实执行在外层 PowerShell 捕获 Python 调用后的 `$LASTEXITCODE`，在 `finally` 恢复原进程级 `ALLOW_LIVE_MODEL_CALLS` 后显式传递该 child exit。实际 child / shell 均为 2，与持久业务 stopped 一致；修复了首轮外层 shell 0 掩盖 child 停止退出码的问题。首轮历史仍不改写。

修复预检的 9 题包含 8 条可生成问题和 1 条检索前拒答题，后者预期 0 次生成。8 条 prompt 为 5,103 至 6,269 UTF-8 字节，低于输入保护限额。数据集按 LF 归一化后的 SHA-256 为 `b3bee7b6249dbe05c2118c076b73409491fba623b88bc9b31172c9e2202c7c6f`，与首轮一致；不能把本机 CRLF 文件的原始字节哈希当作这一 LF 身份。

## 修复轮真实结果与身份

修复轮 Run ID `live_smoke_20261003_repair` 的 manifest 与 summary 文件写入时间分别为 `2026-10-02T17:00:32.2128276Z` 和 `17:00:56.6474699Z`，即北京时间 2026-10-03 01:00:32 至 01:00:56。文件时间不是精确远端计费时间。`status=stopped`、`stop_reason=generated_verifier_failed`，所有已初始化 provider 客户端已关闭，无 SDK 重试、额外付费 probe 或未知用量。

| 新调用 | 用途 | 输入 token | 输出 token | 总 token | 估算费用 CNY | 草稿工程结果 / 语义支持状态 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 1 | v3_lookup_patent_term | 1213 | 141 | 1354 | 0.0009364 | schema / verifier true，evidence_answer；not_checked |
| 2 | v3_lookup_labor_arb_limit | 1113 | 318 | 1431 | 0.0014628 | schema / verifier true，evidence_answer；uncertain |
| 3 | v3_lookup_sensitive_info | 1094 | 315 | 1409 | 0.0014456 | schema / verifier true，evidence_answer；not_checked |
| 4 | v3_scene_weekend_work | 1217 | 297 | 1514 | 0.0014372 | schema / verifier true，evidence_answer；uncertain |
| 5 | v3_scene_online_return | 1057 | 123 | 1180 | 0.0008164 | schema true，verifier false；response_mode_invalid |
| 修复轮合计 | 5 次新增尝试，0 自动重试 | 5694 | 1194 | 6888 | 0.0060984 | 处理 5/9 个 RAG case，4/5 草稿工程检查通过后停止 |
| 两轮累计 | 首轮 2 + 修复轮 5 = 7 次尝试 | 6688 | 1368 | 8056 | 0.0070528 | 不把不同 prompt 版本合并为法律质量提升指标 |

第五题 Hit@5 / target coverage 均为 0。草稿 schema、引用、免责声明及 source catalog 检查通过，但实际模式为 `insufficient_evidence`、预期模式为 `evidence_answer`，唯一失败原因是 `response_mode_invalid`。草稿 verifier 为 false；降级后的最终受限响应 verifier true 不算作该草稿成功。

独立审查已复现该检索失败，早期同题历史已有 `wrong_article` 记录，可在本地 ignored 的 `reports/v3_article_bm25.md:69` 核查；该文件不属于 Git candidate，不作为公开仓库内的 Markdown 链接，也不上传其中原始路径或语料。因此不能归因或宣传成此次引用 prompt 的新回归，也不能声称已修复它。本次没有放松模式要求、更改检索策略、修正题标、扩样、切换模型或继续付费。剩余 4/9 个 case 未运行，包括尚未实际处理的 refusal 题，不能把预检拒答推断当作真实整轮已完成。

首次执行后离线门禁保留为 failed：全量测试成功，但 `M0-Q01-markdown-links` 拒绝指向 ignored 历史报告的链接。只移除不可发布的超链接，保留本地历史定位与失败记录，不改检查器、runtime 或真实产物。下一次完整门禁使用独立输出文件，不覆盖该失败。

工程 citation 修复在前四题、尤其原专利题获得了实测证据，但 `not_checked` 和 `uncertain` 都不是语义正确性通过。所有费用仍为完整 API usage 在获准价格下的估算，不是已核验扣费。

| 修复轮本地证据 | SHA-256 |
| --- | --- |
| `artifacts/experiments/live_smoke_20261003_repair/manifest.json` | `c12e10aae625545d3b6029f9cc9240206898b9b1be152ee4ea8bf73a5aa92ae1` |
| 同目录 `summary.json` | `b7ee004a24081e39ca4debd0da6cc2054aa2579a9698dcad7ba5c5bc68f454ef` |
| `artifacts/experiments/.live_authorizations/qwen35b-repair-smoke-20261003.json` | `e27fd7f67976d67a229dcaf9ee1f2c2afa3859391b860865708088938b4fc812` |

首轮三个 SHA-256 在修复后仍保持上表原值，旧记录没有编辑。修复轮 manifest 的代码基线 HEAD 仍为 `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`，tracked diff SHA-256 为 `7ed4dcbc51bb6775a5c746c4f14b028a18b836732db2835db769c87a0656f690`。以下实际关键文件哈希包含尚未跟踪的新 runtime 文件，不能只用基线 HEAD 或 tracked diff 替代身份：

| 关键代码 | 修复轮执行 SHA-256 |
| --- | --- |
| legal_rag/chat.py | c27562ff92ed638bac133b4ea4a2fc380b8930b7ddfc9b55783e957fe61bbfc0 |
| legal_rag/experiment_lifecycle.py | e9ad513dd1494b275abe5d58691203049ceca89cda228ad85997b202c3d7bd05 |
| legal_rag/live_budget.py | ad17d6103f9bad2ab7b8ac8a0fd9f3771a07c07b5da5cc02472fdcfa70144052 |
| legal_rag/llm.py | b551877b4ea15dd422317f5d20acd8278cb0395e4b3209b42eb30c0b28bf62dc |
| scripts/live_model_smoke.py | 30ae4fc7bbcf8f620093d21f3b45c68f31ca172264a58f9bfb800340ee6b0200 |
| legal_rag/config.py | aff9d3433f30023cc1013ef43c30ae91b5fb4cd2de02458eab47bbacc6c9b28d |
| configs/default.yaml | 93258f18c0439ab33ee40ea34fa4cf5d676d7a99c027f7493062a0788c7bb422 |
| legal_rag/evaluation_scoring.py | 3b5a1436d89df7d881ff4eedf03de6c612cd651aa91bcc79e3ffe243a142c0de |

原始输出仅保存在 ignored 本地目录，Git 中只保存本报告的脱敏结果。两轮 canonical ledger 均已使用，未消耗的额度不构成自动补跑授权；停止后只追加合成回归及离线门禁，不再调用真实模型。

## 法律质量边界与未运行项

这仍是原固定 legacy regression 的修复复验，不是新 holdout 或 30 条完整评测。即使结构、正文引用和 verifier 通过，也仅证明工程合同检查，不证明引用语义成立、当前法条有效或法律结论正确。未启用 Judge 或人工法律评审，不从小样本声明准确率提升、生产可用或默认模型应升级。

旧轮网购题检索未命中等失败历史保留。只改变引用提示不能被解释为已修复所有检索、推理或法律质量问题。本修复轮已在该失败后停止，保留失败和未运行项，不松校验、补引文、自动增预算、换模型或扩样。

30 条扩样、Qwen 122B 对照、DeepSeek Judge、人工法律评审、HTTP API / M6 queue live 接入、真实 broker / DB 故障复验、远端 CI、生产部署和 M7 全部不在本次范围。Ruff 为 `not_run`，本地 `.venv` 未安装且没有为此联网安装依赖；不以其他检查冒充 Ruff 通过。

本报告记录局部引用修复实测及有界停止，不代表整个 Smoke 完成或通过。停止后的合成回归、最终四文件专项及文档修正后的完整累计 offline 门禁已通过，runtime 未再次修改。完整门禁期间冻结 tracked 候选文件，之后仅回写真实结果；候选 Markdown、STATE/manifest JSON 与高置信凭证风险的三项静态复核全部 passed，不用旧 CI 冒充当前候选 CI。commit / push / PR / Tag / Release 均未发生，不因旧 M6 远端授权自动实施本次远端操作。当前到此停止，不再发真实请求；未解决的检索覆盖与语义证据判定工作需要独立范围和质量证据。

参考：[首轮验收记录](LIVE_SMOKE_QWEN35B.md)、[真实模型 Smoke 使用说明](../../docs/LIVE_MODEL_SMOKE.md)、[项目执行规则](../../docs/refactor/AGENT_EXECUTION.md)。
