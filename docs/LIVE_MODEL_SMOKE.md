# 首轮与修复真实模型接入 Smoke

本入口是用户单独授权的、本地单次真实请求实验，不是新的 M6 发布、HTTP 服务接入验收或 M7。默认命令只做本地预检，零模型请求；仓库默认 `ALLOW_LIVE_MODEL_CALLS=false` 的规则不变。

首轮已真实执行并停止。用户随后明确要求继续测试和修 bug，本次修复沿用原总限额，不新增一个完整的 10 次 / 2 元额度。修复真实执行新增 5 次尝试，前 4 条草稿通过，遇到第 5 条回答模式不匹配后立即停止；完整 9 题未通过，不能把局部引用修复或降级后绿灯当成整轮成功。

## 固定边界与累计授权

| 项目 | 固定值 |
| --- | --- |
| 提供商 | 硅基流动中国站 `https://api.siliconflow.cn/v1` |
| 模型 | `Qwen/Qwen3.5-35B-A3B` |
| 首轮授权身份 | `qwen35b-first-smoke-20261003`，已使用，不能重开 |
| 修复授权身份 | `qwen35b-repair-smoke-20261003`，一次性固定身份，不按输出目录重新计额 |
| 累计最大尝试数 | 首轮与修复合计 10，包含探测与失败的 dispatch；不是保证发生 10 个已收费 HTTP 请求 |
| 累计费用政策预算 | 首轮与修复合计 2 元人民币，逐次持久预占；不是提供商账单硬上限 |
| 输入保护 | 每次最多 24,000 UTF-8 字节；按字节数加 1,024 token 包装余量估算预占 |
| 输出保护 | 每次 `max_tokens=1536`；显式 `enable_thinking=false`，并检查返回的非思考证据 |
| 执行方式 | 串行，SDK `max_retries=0`，禁 HTTP 重定向，不自动补跑、切模型或换目录续费 |
| 检索 | 已有本地 article 索引，自研 BM25，top-5 |
| 数据 | 原 30 条生成回归子集中的固定 9 条单轮问题；首轮另含 1 条 JSON 兼容探测，修复不重复该探测 |
| 不启用 | Judge、adaptive、LLM 改写、对话压缩、reranker、embedding API、Langfuse 实发 |

固定问题覆盖 article lookup、semantic scenario、hard negative、multi-article 和 refusal。规则拒答或证据不足时不生成，因此实际模型调用可能少于 10。它是 legacy regression，不是新 holdout，不能用来证明泛化准确率。

首轮已有 2 次尝试、估算消耗 `0.0009544` 元必须计入累计额度。本次修复启动时最多允许再尝试 **8 次**，费用政策预算为 **1.9990456 元**。固定 9 题中最多 8 题需要生成，拒答题预期 0 次生成；若规则判断资料不足，实际调用会更少。实际修复只发生 5 次尝试后停止，剩余 4 题未运行，不为了完成 9 题自动补发请求。

两轮累计 7 次尝试，费用估算为 **0.0070528 元**，即修复轮 `0.0060984` 加首轮 `0.0009544`，未核实账号 invoice / 实际扣费。修复轮新 probe 为 0，自动重试为 0。累计次数名义上还差 3 次才到总上限，但一次性修复账本已经使用并终止，这不是自动补跑或另开第三轮的授权。

修复只明确生成提示中的正文 `[S1]` 引用及 claims `source_ids` 对齐规则，版本为 `m1-structured-qa-citation-alignment-v2`，M2 generation contract 同步使用该版本。答案 parser 仍为 `m1-structured-answer-v1`，schema 和 verifier 不放宽，不自动给模型正文补引用，也不改变默认模型或检索策略。修复预检要求该提示版本匹配，旧提示不能进入修复执行。

## 价格与配置

用户于 2026-10-03 确认账号实名、可用余额、模型权限，以及输入低于 128k 时输入 **0.40 元 / 百万 token**、输出 **3.20 元 / 百万 token**。公开 [官方价格页](https://www.siliconflow.cn/pricing) 作为价格快照依据；未读取账号账单，不把 API usage 推算金额当成已核实的实际扣费。

已知用量的估算公式为 `(input_tokens * 0.40 + output_tokens * 3.20) / 1000000`，使用 Decimal。示例 3,000 输入、800 输出 token 为 0.00376 元，10 次同规模约 0.0376 元。按本轮最大的字节预占和输出预占计算，10 次总预占不超过 0.149248 元。这些都是该价格假设下的估算，不是平台扣费承诺。UTF-8 字节加余量不是提供商 tokenizer 的正式合同；若实际用量超预占，可能已发生额外费用，程序只能停止后续请求，不能撤销已发送的请求。

需要本地 Python 环境、现有 article 索引及对应的公开法律文本目录。`SILICONFLOW_API_KEY` 使用用户已有的本地 `.env` 或进程环境配置，不复制密钥，不修改 `.env`，不把调用授权永久写入环境文件。预检不加载密钥或初始化提供商；真实执行还须同时满足 `--execute` 和进程级 `ALLOW_LIVE_MODEL_CALLS=true`。

`max_tokens` 不应被理解为所有思考费用的独立硬上限，参见 [官方 Chat Completions 文档](https://docs.siliconflow.cn/docs/api/chat-completions-post)。本轮只在返回为空的显式 `reasoning_content` 或 `reasoning_tokens=0` 时确认观察到非思考输出；缺少两者记为未知并停止，有思考内容或非零思考 token 也停止。JSON 模式只作兼容探测，后续仍由本地结构与引用规则验证，不声称提供商保证严格 JSON Schema。

## 命令与单次授权

在仓库目录使用 PowerShell。首轮零调用预检的历史命令为：

```powershell
.venv\Scripts\python.exe -B scripts/live_model_smoke.py --preflight --run-id live_smoke_preflight_20261003
```

首轮预检输出已存在时不能覆盖。首轮付费执行已发生，其授权账本不能复用来“补齐”问题；历史调用与失败证据见 [首轮验收记录](../reports/refactor/LIVE_SMOKE_QWEN35B.md)。

本次修复已执行的零调用预检命令如下。仅指定 `--repair` 也仍是预检，不能隐式开启真实调用；所示预检输出现在已存在，不能覆盖：

```powershell
.venv\Scripts\python.exe -B scripts/live_model_smoke.py --repair --preflight --run-id live_smoke_20261003_repair_preflight
```

预检只读取首轮安全 manifest / summary / ledger，不读取真实 raw；验证首轮成功 probe 和 2 次已用额度，核对固定历史 SHA-256，检查修复提示版本、本地数据来源和 prompt 大小。预检不加载密钥、不初始化提供商、不创建修复授权账本。

以下保留本次已执行的**单次历史**修复命令形状，不得再次运行。真实执行在离线保护测试、独立审查与累计门禁满足后发生，业务结果为 `stopped`，不是整轮通过。代码块按独立 PowerShell 脚本使用，末尾 `exit` 把原生程序退出码传给调用者；直接在交互终端粘贴会结束该终端会话。

```powershell
# 历史执行记录，授权账本已存在，不可重跑或补齐。
$smokePreviousAllow = $env:ALLOW_LIVE_MODEL_CALLS
$smokePreviousDisableDotenv = $env:LEGAL_RAG_DISABLE_DOTENV
$smokeExitCode = 2
$env:ALLOW_LIVE_MODEL_CALLS = "true"
$env:LEGAL_RAG_DISABLE_DOTENV = "false"
try {
    & .venv\Scripts\python.exe -B scripts/live_model_smoke.py --repair --execute --run-id live_smoke_20261003_repair
    $smokeExitCode = $LASTEXITCODE
} finally {
    if ($null -eq $smokePreviousAllow) {
        Remove-Item Env:ALLOW_LIVE_MODEL_CALLS -ErrorAction SilentlyContinue
    } else {
        $env:ALLOW_LIVE_MODEL_CALLS = $smokePreviousAllow
    }
    if ($null -eq $smokePreviousDisableDotenv) {
        Remove-Item Env:LEGAL_RAG_DISABLE_DOTENV -ErrorAction SilentlyContinue
    } else {
        $env:LEGAL_RAG_DISABLE_DOTENV = $smokePreviousDisableDotenv
    }
}
exit $smokeExitCode
```

`LEGAL_RAG_DISABLE_DOTENV=false` 只在已授权的执行进程中允许既有本地 Key 配置被客户端读取，不修改 `.env`。`finally` 同时恢复两个环境变量，不能用恢复操作的成功退出状态覆盖 Python 的退出码。若 CLI 返回 2，外层必须同样报告失败；持久 summary 的 `stopped` 也不能解释为业务成功。

不要把调用开关设置为永久全局变量。`run-id` 仅标识输出，不能更改授权身份或重新获得额度。首轮和修复分别使用以下固定账本：

- 首轮：`artifacts/experiments/.live_authorizations/qwen35b-first-smoke-20261003.json`。
- 修复：`artifacts/experiments/.live_authorizations/qwen35b-repair-smoke-20261003.json`。

修复账本一旦存在则禁止新的 dispatch，即使换输出目录也一样，没有自动 resume。不得为绕过保护删除、移动、覆盖或重建任一账本。首轮 manifest、summary、ledger 和 raw 全部保留原样，修复结果写入独立 ignored 目录 `artifacts/experiments/live_smoke_20261003_repair`。

修复复用首轮已成功的提供商 / JSON 兼容探测，记录 `probe_status=reused_prior_passed`、`probe_calls_this_run=0`，并绑定首轮 manifest、summary、ledger hash。这不是修复轮新发生的探测请求，不计新的 probe token 或费用；修复后的法律提示仍须由实际 RAG case 验证。摘要分别记录本轮 `live_model_calls`、历史 `prior_live_model_calls` 和累计 `total_authorized_calls_attempted`，费用也区分本轮、历史与累计，不把历史消费清零。

本次修复已有新的明确用户授权，因此首轮报告中“后续需要授权”的历史停止条件没有阻挡此次限定修复。但本次修复已经执行并停止；更换模型、开启 Judge、扩样、提高总限额或另开第三次付费执行，仍需新的明确范围及独立记录。本次真实结果见 [修复验收记录](../reports/refactor/LIVE_SMOKE_QWEN35B_REPAIR.md)。

## 上传内容与输出

运行前逐一将索引 chunk 与固定 `Chinese-Laws/Chinese-Laws` 目录的本地原文解析结果比对，再把来源绝对路径替换为文件名。提供商只接收固定探测文本、选定测试问题、提示模板和最多 5 条公开法条证据；不发送整个索引、私人文件、系统路径或账号资料。该检查证明本地来源一致，不证明快照当前有效，也不额外授予未授权语料的上传权。

每轮 manifest 保存代码 HEAD、tracked diff hash、实际关键代码文件 hash、数据集 hash、索引及来源 hash、政策和价格快照。修复还记录当前提示版本及 `experiment_lifecycle.py` 实际 hash，区分旧 probe 兼容证据和新法律提示。包括尚未跟踪的新增代码文件实际 hash，不能只靠 Git diff 标识实验代码。原始问题、完整回答和逐 case trace 只保存在被 Git 忽略的 `artifacts/experiments/live_smoke_*` 本地目录。账本仅保存提示 hash、字节数、预占、用量和稳定错误类别，不保存原始错误、密钥或思考正文。

## 停止与结果解读

原首轮失败的专利期限题在修复轮已通过正文引用对齐检查，说明本次局部提示修复起效。但第 5 条网购题在两轮预检中已有 `Hit@5=0`、`target_coverage=0` 的未命中结果。其修复草稿 schema、引用和免责声明检查通过，实际模式为 `insufficient_evidence`，而既有流水线期望 `evidence_answer`，唯一失败项为 `response_mode_invalid`。这是既有检索及启发式证据充分性判断的质量边界，不是再次缺失正文引用，不临时针对这条题修改期望或放宽 verifier。

降级后的最终受限响应通过检查，runner 仍依据被拒绝的草稿验证结果以 `generated_verifier_failed` 停止；后续 4 题不运行，也不自动重试。已增加只使用合成 fixture 的保护回归，验证前 4 次通过、第 5 次模式失败、最终降级绿灯仍不能掩盖草稿失败，停止后没有新增请求。这是既有停止机制的行为保护测试，不冒充新发现的 runtime bug 修复。

发送前持久预占次数与费用，使用原子替换及排他锁。失败、超时、usage 缺失、非整数用量、总量不等于输入加输出、超预占、模型不符、思考返回或未知、截断、无效 JSON、结构或 verifier 失败均立即停止后续调用。未知费用为 `null`，不是 0；未知或失败的预占不释放。调用后的报告写入失败也不能报告成“零调用”，应从已有账本核对，无法核对时记 unknown。

`schema_valid` 和 `verifier_passed` 仅证明当前工程规则检查；`semantic_support_status` 的 `uncertain` / `not_checked` 不是通过，也不是法律正确性证明。账本 provider row 的 `succeeded` 不能代替草稿 verifier 通过，最终降级响应通过也不能掩盖原草稿失败。不因小样本 smoke 改变默认模型或检索策略。首轮事实见 [首轮验收记录](../reports/refactor/LIVE_SMOKE_QWEN35B.md)，修复结果单独记录；未执行的 30 条评测、独立 Judge、API/worker live 接入和 M7 必须继续明确列为未运行。
