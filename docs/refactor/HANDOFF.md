# M6 执行交接

## 当前任务：中文检索组件替换与规则收缩

2026-10-06 用户授权先研究中文 BM25、编写 TODO、逐项实施并推送 GitHub。当前任务以 `STATE.json.chinese_retrieval_optimization` 和 [优化清单](CHINESE_RETRIEVAL_TODO.md) 为准。下文 local-only 是前轮历史范围，不限制本次已明确授权的 commit/push/review PR；本次不新建 Release、不移动 Tag、不进入 M7、不进行真实模型调用。

开始时核验 HEAD、origin/master 及远端默认 master 均为 `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`。已有 Smoke、lexical、W1-W8 改动完整保存为前置提交 `914a7e322d2787ec99a2fe1f1117ad5906244e02`。本轮分支 `codex/chinese-bm25-optimization`，运行时改善提交 `04fd389945125c81767745bbdce48137921d26fd` 已包含T4-T7；其后文档head `e3d32a96d3a37f5ab7c0e559bb000448fb0e603d` 与当前仅实验源码 `8f042fb46b83c7d2e7eb630c1197546f0d98c3cd` 均已提交并核验推送到 [Draft PR #31](https://github.com/1040942669/legal-rag-agent/pull/31)。本节是8f之后的结果文档回填，不虚填自身未来commit身份。

前两次预冻结比较保留原身份，见 [本轮报告](../../reports/refactor/CHINESE_BM25_OPTIMIZATION.md)。首轮jieba search72/108低于legacy76/108；第二轮成熟sklearn char83/108，但MRR@5 .597065低于legacy .610184。用户选择第3项继续实测Lucene SmartCN，第三轮 `chinese_bm25_20261006_smartcn_fixed_third` 已completed：legacy Hit@5 76/108、MRR .610184，char 83/108、.597065，BM25S＋SmartCN控制臂73/108、.506632，原生Lucene＋SmartCN73/108、.503237。四臂两pass无排名漂移，代码/输入/实际运行身份稳定，错误与真实模型调用均0。没有候选满足Hit@5和MRR双项不回退；selected_modern_arm=char仅是候选排序，promotion_eligible_modern_arm=null。默认仍legacy，不推广SmartCN，不继续试到赢；用户是否接受char取舍的新选择尚待回复。

SmartCN只增加实验脚本、独立v3协议和本地Java stdio桥，不接生产API、服务selector或共享索引缓存，不改旧协议、语料或gold。139项合同检查通过，组成是102单元＋37显式实际JVM，不能称远端Java CI。8f042fb的本地M2为25/25 passed、exit0、316818ms，其中2120 passed＋157 subtests、245.43s、JUnit2277/0/0/0，4条jieba上游警告；artifact `.tmp/smartcn-8f042fb-m2-final.json`，门禁前后均核验同一clean HEAD。120题仍是重复开发集，只有108检索gold；12无gold题排名NA不代表拒答成功，不能声称独立法律泛化。原失败及各自身份见验收报告，不合并专项分母。

旧运行时04fd的本地累计M2为25/25 passed、234906ms，其中2018 passed+157子测试、180.49s、JUnit2175/0/0/0；17文件隔离PG为93 passed/94.32s，并完成真实stop/start与新进程schema0008恢复。实际仓库外installed-wheel27模块通过，110个包源码模块与候选逐字节一致。本轮生产代码未改，未重跑本地PG/wheel；这些旧证据严格保留04fd身份，不冒充8f新验收。

源码候选 [04fd389精确CI37354368069](https://github.com/1040942669/legal-rag-agent/actions/runs/37354368069) 四路全部completed/success；已下载核验offline/M4/M5/M6累计门禁分别25/25、41/41、51/51、58/58。真实Linux worker JUnit76/0/0/0、累计M5为81/0/0/0，回执严格绑定04fd389与schema0008。仅本地Linux broker因Docker daemon不可用not_run，不能把它与远端实际验证混淆。`c562eec`测试入口收集失败及后续修复原样保留。

旧文档e3d32a9的 [CI 37356943303](https://github.com/1040942669/legal-rag-agent/actions/runs/37356943303) 已四路成功。当前8f042fb的 [CI 37403867767](https://github.com/1040942669/legal-rag-agent/actions/runs/37403867767) 在本次回填观察时已offline/M4/M5 completed/success，M6仍运行中。上述旧CI与当前部分完成状态分开记录，不提前写当前四路通过，也不代表未来文档head。

下一步只完成第三轮结果文档静态检查及push，再独立读取当前源码和后续文档head的精确CI状态，不递归回填未来SHA。用户尚未选择保留默认交付或接受char的MRR取舍；在明确答复前不切默认、不追加实验。PR保持Draft，不合并发布，不移动已有Tag，不进入M7。

最初审计复现的风险词误拒、引用后置否定/转述误归属、分数尺度误判及目录标题合同不一致均按机制修复并增加反例；初始216项旧测试不是修复验收。实施收敛为成熟评分/分析组件、配置与历史身份、规则裁决权收缩，不添加通用意图框架或共享缓存。每run仍重建索引，char已加载后约1.62至1.66秒，legacy约1.06至1.08秒，热查询更快不能称API端到端更快。复杂指代未知、风险无信号不等于安全、机械通过不等于法律语义支持，详见 [ADR-007](decisions/ADR-007-chinese-bm25-and-bounded-rules.md)。

> 以下是前轮历史状态，以各自原始权限和提交时点解释；当前任务只以最上面的中文检索节点为准。M6 保持 released，M7 未开始；旧发布与门禁不能替代当前候选验证。

## 前轮历史改善：通用证据合同与受控服务接线

- 当前分支 `codex/general-rag-improvements`，HEAD/基线为 `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`，dirty=true。保留既有未提交 Smoke/lexical 代码，不 reset/stash/覆盖；本次未 commit/push/PR/合并/Tag/Release。
- 用户要求完整设计、复核、择优实施，选择 [ADR-006](decisions/ADR-006-general-evidence-first-improvements.md) 的 W1 至 W8。适用的本地工程实施、独立复核、修复候选运行时门禁及已观察的结果文档静态3/3已完成，原源码scope与27个critical文件身份未变；结果回写后仅重验同组静态。这不是法律质量验收、生产证明或新 Release。真实 RED/GREEN 与处理链见 [独立改善验收记录](../../reports/refactor/GENERAL_EVIDENCE_IMPROVEMENTS.md)。
- 消费者退货题是既有法律回归，不是新增电商模块。旧购物扩展仅保留显式历史对照；新 generic-v3 不含购物特判，初始 opt-in，legacy-v1 仍为排名默认。现代机械合同逐 `(law, article)` 检查，不沿用旧场景硬闸门，不把高分或机械充分当语义 supported。
- 精确路由已连接到 PostgreSQL-bound assistant 和 chat/graph；not_found/needs_disambiguation 不得以 fuzzy fallback 冒充 found。完整法条查询与原授权 chunk 分开。生成后使用完整可见输出绑定 assessment；required 未知/失败只能发布安全有限模式并保留草稿失败摘要。
- 服务冻结允许名单、外发和价格策略，generation 默认 disabled；generation/checker 共用 durable count/Decimal money ledger。仅追加0008，旧 run 不获得 paid 权限，unknown 保留预留不重试，不承诺供应商账单硬上限。
- W7 strict pending/可信准入/一次 exposure lock 已实现并验证，实际120题审核包仍 `human_review_complete=false`、`holdout_admitted=false`。没有真实法律 gold 审核或独立未曝光集，不能把规则/回归绿灯当泛化证据，默认排名不推广。
- 有界解析器不是完整自然语言理解，仍有保守误澄清。例如 `《合成甲法》是什么意思？` 当前为 unresolved；历史时间标记加未知指代，或历史甲引用后仅当前解释乙，也可能整体要求澄清。保留这些已知代价，不继续为逐句话术扩展意图词表。

### 修复后最终候选的真实结果

- 全量 pytest：`1752 passed + 157 subtests passed in 160.34s`，JUnit `1909/0/0/0`，`.tmp/general-repaired-candidate-full-pytest.xml`。专项数字重叠，不与全量结果相加。
- 累计 M2 offline gate：`25/25 passed`、exit 0、217074ms，`.tmp/general-repaired-candidate-m2-final.json`。这是修复后完整候选本轮证据，不借用首轮或旧 M5/M6 门禁。
- 真实隔离 PostgreSQL 统一回归：150 passed /112.53s，JUnit `150/0/0/0`，`.tmp/general-pg-repaired-candidate-final.xml`。包含新服务合同及适用旧 M3/M4/M5/M6 数据库回归。
- 当前候选物理 PostgreSQL 重启：26 passed /35.66s，`.tmp/general-pg-repaired-restart-final.xml`；实际 stop/start 后由新的 Python 进程验证 schema8 与持久状态。prepare/stop/start/fresh-process verify 证据保留在 `.tmp/isolated-pg-86b747ec941a4b97b0f88223c659d569/`，不把进程内模拟当服务重启。
- 实际候选 wheel 构建、仓库外安装与 smoke：23 runtime modules、head `0008_execution_money`、`source_checkout_isolated=true`；SHA-256 `c7d348644a51a7dc258d7e4d6bdff8a2e4306eff529c358101f5a7cd6832b667`，`.tmp/general-repaired-candidate-wheel-probe.json`。这是本地 dirty 候选，不是重新发布现有0.7.1。
- 八臂消融 `offline_components_20261005_repaired` 已 completed，528815.737ms；固定旧120题含108检索 gold 与12拒答，paired_valid=108、unknown=0、actual_provider_calls=0，code/input/dataset 身份均 stable。运行完成只证明本轮比较可计算，不是独立 holdout 或默认推广；精确组件收益与回退见独立验收记录。
- 文档首轮真实静态：3/3 passed、exit0，UTC `2026-10-04T20:06:31Z`，`.tmp/general-improvements-static-first.json`；覆盖43个 Markdown、2个 JSON 和305个候选文本，高置信凭证形态无匹配。主执行流程将对本次结果回写后的文档做最终静态复核，后续轮次尚未执行，不能提前称 passed。

### 保留失败与停止边界

- 首次完整候选 M2 gate 为24/25、exit1，三项公共 validator fixture 失败；首轮完整 pytest 分母未保存，不补造总数。首次八臂 `offline_components_20261005_first` 的1920条执行均失败，paired_valid=0、unknown=108、指标为 NA，actual_provider_calls=null/unknown 仍保留，不能改写为质量0或已确认调用0。
- 后续仅修通用完整标题身份、未知标题边界与真实拒答评分投影，不删题、不改 gold、不调 rank 参数。各轮 RED、首轮 manifest/summary、两份 paid Smoke 和旧 lexical 实验身份、账本与 hashes 均保持原样；新重跑使用独立 run ID。
- 本次新真实模型调用0、新增模型费用0，无 Key/旧raw/私人资料读取或语料上传，无远端写。未运行项仍为真实 Linux broker/prefork、Ruff（未安装）、checker 校准、权威法律审核与现行性核验、独立未曝光 holdout、第三 paid Smoke、生产容量及远端 CI。工程通过不证明法律准确率提升、checker 等同真人或供应商账单硬上限。
- 停止边界：结果回写后只重验同组文档静态，汇报已完成的本地明确范围后停止；不擅自追加付费调用、commit/push/PR/合并/Tag/Release或启动 M7。M5/M6 发布历史与 M7 `required=true / not_started` 保持不变。

## 历史本地修复：词汇检索候选与证据闸门

- 用户要求继续完善并在修改后汇报。本轮只做可离线复现的检索与证据检查，不重开已使用的付费账本、不上传密钥或原始语料、不进行远端写操作、服务部署或 M7。
- 保留默认 `legacy-v1`；新增显式 opt-in `local-lexical-v2` 和 [配置示例](../../configs/lexical-v2.yaml)。候选保持原始 query、风险判断与法名条号提取，只追加有界实物购买词汇；否定、非实物购买、金融和多主题语句保守不扩展。新版本 trace/cache 身份不同，不改 Chunk、scope/profile/snapshot 或语料。
- 证据检查修正精确法名、显式单法律与条号归属及非法分数；纯 BM25 v2 的商品退货问题新增同结果必要词检查。合法多法问题不推断条号笛卡尔关系；高分、词汇齐全与来源有效仍不代表语义 supported。模型自报 insufficient_evidence 仍不能覆盖流水线预期模式，既有 verifier 和付费 runner 不变。
- 唯一 A/B 为 `offline_lexical_ab_20261004_first`，UTC `2026-10-04T08:17:45Z` 至 `08:20:57Z`，child/shell exit 0，191551 ms。120 条旧回归包含 108 检索 gold 与 12 拒答；另有 16 合成检查。每题 A/B 与 B/A 重复不漂移，服务失败/unknown/provider 为 0，代码/数据/语料身份稳定。
- Hit@5 `76/108 -> 78/108`，MRR `0.610184 -> 0.624227`，coverage `0.660494 -> 0.679012`；4 题改善、2 题回退，差异 95% CI `[-0.0278, 0.0648]` 跨 0，故不推广默认。网购退货题未进前 5 变为第 1；工伤、退货与信息多问两题从第 5 跌出前 5。12 拒答两臂 12/12；候选仍有 28 个未命中但启发式充分的案例，不能声称语义覆盖问题已解决。
- 专项最终 139 passed + 89 subtests、exit 0，JUnit `.tmp/lexical-evidence-safety-final-green.xml`；独立复测 134 passed、exit 0，`.tmp/offline-lexical-independent-final.xml`。早期 RED 与输入字段修正记录保留。本轮冻结候选的完整累计 M2 offline 门禁为 25/25 passed、child/shell exit 0、193624 ms，UTC `2026-10-04T08:30:01Z` 至 `08:33:16Z`；全量 `1294 passed + 157 subtests in 139.51s`，JUnit `1451/0/0/0`，artifact `.tmp/local-lexical-m2-first.json`。这是本轮真实结果，不借用旧门禁；详见 [验收报告](../../reports/refactor/LOCAL_LEXICAL_RETRIEVAL.md)。
- 实验 manifest / summary SHA-256 为 `506c47fb2d0d1cc0583ee2f0acf7384a80fc7fcfec71fa1fc11c19d02f2ee068` / `eec042f09fe0e03d5ea681bb185799415b76250a3542e4152f2f282f17024f74`。完整输出仅本地 ignored，公开候选仅代码、合成测试与脱敏总结。随后仅补配置示例和文档，不更改已测算法。
- 分支 `codex/live-smoke-qwen35b`，HEAD `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`，来自核验后的最新 origin/master；原未提交代码均保留。本次 commit / push / PR / Tag / Release 均未创建；旧 M5 `v0.6.0`、M6 `v0.7.1`、回执与 M7 not_started 不变。真实 API 第三轮、30 题真实生成、Judge、法律人工评审、真实 DB/broker 故障和远端 CI 为 not_run；Ruff 未安装，not_run。
- 结果回写后的静态三项 3/3 passed、exit 0，UTC `2026-10-04T08:38:24Z` 至 `08:38:25Z`：41 个候选 Markdown 链接、2 个 JSON 状态与 268 个候选文本的凭证风险检查通过，artifact `.tmp/local-lexical-final-static.json`；不覆盖全量门禁或旧实验输出。独立文档审查与代码/历史 hash 复核继续保留。
- 下一条动作：本轮局部改进与离线验证已完成，运行时代码和测试保持冻结，汇报后停止。候选仅显式可选，默认不变；完整 live Smoke 和法律语义质量验收仍未完成。不自动补付费请求、复用旧额度、发版或进入 M7。

## 历史局部实验：引用格式修复与限额复测

- 用户明确要求继续测试和修 bug。本次只补足 `answer_text` 正文同句引用、句末标点之前放引用、`claims.text` 单句复制的提示要求；不修改既有 verifier 判定、不自动补造引用、不改变 parser schema。提示版本为 `m1-structured-qa-citation-alignment-v2`，生成 manifest 同步版本以避免旧缓存混用。
- 首轮原 manifest、summary、账本三份证据按原 SHA-256 只读核验，失败与费用完整保留。修复复测身份为 `qwen35b-repair-smoke-20261003`，共用一次性 canonical 账本；旧 2 次 / 0.0009544 元仍计入原总限额，最多再发 8 次、剩余政策额度 1.9990456 元。已通过的 probe 只引用旧证据，不再次付费运行。
- 执行前累计 M2 offline 门禁 25/25 passed，exit 0；全量 1190 passed + 157 subtests in 131.65s，JUnit 1347/0/0/0，artifact `.tmp/live-smoke-repair-before-m2.json`。独立安全审查通过，专项 177 passed in 3.99s，M2 prompt cache 定向失效回归 1 passed in 0.59s；真实运行 critical code hash 与审查一致。
- `live_smoke_20261003_repair` 已真实执行并停止，shell/child exit 均为 2。新增 5 次调用、5694 输入 / 1194 输出 / 6888 总 token，估算 0.0060984 元；与首轮合计 7 次、6688 输入 / 1368 输出 / 8056 总 token、0.0070528 元，未核验实际账单。probe 只复用历史证据，本轮 probe 调用 0，无自动重试或额外请求。
- 前 4 题草稿 schema 与 verifier 通过，首轮专利题的正文引用缺陷得到实测验证。第 5 题网购退货 hit_at_5 / coverage 均为 0，模型选择 insufficient_evidence，而流水线预期 evidence_answer，草稿因 `response_mode_invalid` 被拒绝；此时正文引用检查没有失败。这是历史已有的 BM25 语义检索与启发式证据判定局限，不是新提示引入的检索回归；不放宽 mode 合同或强迫模型编造答案。处理 5/9 题、草稿通过 4/5，剩余 4 题 not_run；最终安全 fallback 通过不能把整轮算成功。
- 新修复账本已存在，本轮到此停止，不自动第三轮，不用剩余额度补跑。首轮三份 SHA-256 原样保留，新 manifest / summary / ledger 和局部成功、整轮失败分别记录在 [修复复测报告](../../reports/refactor/LIVE_SMOKE_QWEN35B_REPAIR.md)。已新增合成模式失败保护回归，保留 runtime 不变；最终四文件专项 178 passed in 4.59s，JUnit 178/0/0/0，artifact `.tmp/live-smoke-repair-targeted-final.xml`。下一步若要改检索或语义证据策略，需要单独明确范围和质量证据，新的付费复验另行授权。
- 本次继续本地未提交分支 `codex/live-smoke-qwen35b`，HEAD 仍为 `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`；本次无远端写操作，不创建 commit / push / PR / Tag / Release，不移动既有 M5/M6 Tag，不进入 M7、30 条完整评测、Judge 或真实 API/worker 服务接入。
- 首次执行后 M2 门禁为 failed / exit 1、24/25：全量 1191 + 157 subtests / JUnit 1348/0/0/0 已通过，唯一失败是报告链接指向 ignored 历史文件。已仅移除超链接并保留本地历史定位，不修改检查器或 runtime；失败保存在 `.tmp/live-smoke-repair-after-m2.json`。文档修正后的完整复验已 25/25 passed、exit 0、172669 ms；全量 1191 + 157 subtests in 122.66s / JUnit 1348/0/0/0，独立 artifact `.tmp/live-smoke-repair-after-m2-final.json`（UTC 2026-10-02T17:16:12Z 至 17:19:06Z）。不会覆盖或隐去首次失败；最后仅回写真实结果并做候选静态复核。
- 结果回写后的静态三项全部 passed：39 个候选 Markdown、2 个 JSON、261 个候选文本的凭证风险检查，artifact `.tmp/live-smoke-repair-final-static.json`；`git diff --check` exit 0。顶层 `STATE.next_action` 已更新为修复后的停止交接，首轮旧动作另存历史键。到此停止，未运行的质量评测、服务 live 接入、Ruff 和 M7 保持未运行，不以工程绿灯代替真实法律质量结论。

## 历史局部实验：首轮 Qwen3.5-35B Smoke

- 用户接受首轮策略，已于 2026-10-03 确认价格、实名、余额与模型权限。本次只限固定模型、article + BM25 top-5、1 probe + 9 个已有回归题、最多 10 次尝试 / 2 元政策预算。默认 live 禁止不变，本轮授权独立记录；不启动 M7，不扩大到 Judge 或 30 条评测。
- 分支 `codex/live-smoke-qwen35b` 从 fresh origin/master `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc` 创建，HEAD 仍为该基线，本次文件未提交。origin 默认 master；开始时工作区 clean。已只读核验没有 open PR、既有 Release 与 Tag；未改本地 master、未强推、未移动 Tag。
- 已实现 provider keyword-only opt-in 限制、持久单次预算包装和固定 smoke CLI，以及 3 份回归测试；使用说明见 [LIVE_MODEL_SMOKE](../LIVE_MODEL_SMOKE.md)，真实结果与保留的失败见 [验收记录](../../reports/refactor/LIVE_SMOKE_QWEN35B.md)。新输出目录不能重开固定授权账本；未知费用不报 0，不自动重试。
- 执行前累计 M2 offline 门禁 25/25 passed，全量 1144 + 157 subtests、JUnit 1301/0/0/0；代码与文档独立安全审查通过。真实运行 `live_smoke_20261003_first` 已停止：probe passed，第一题草稿 schema 通过但正文缺 `[S1]`，claims 绑定 S1 后引用对齐失败 `citation_ids_invalid`，降级为 insufficient_evidence。共 2 次尝试、994 输入 / 174 输出 / 1168 总 token，估算费用 0.0009544 元，未核验实际账单；剩余 8 题 not_run。不能把最终安全 fallback 的 verifier 通过当成草稿成功。
- 固定授权账本已存在，不能换目录或删除账本补跑。已新增相同格式的合成 fake 回归，三个专项 132 passed；执行后累计 M2 再次 25/25 passed，全量 1145 + 157 subtests in 124.34s，JUnit 1302/0/0/0，artifact `.tmp/live-smoke-after-execute-m2.json`。运行时代码 hash 未改变，最终只补结果文档。本轮到此停止，不再发请求；后续提示改进或新的付费轮次须另行明确授权。
- 本次无 commit / push / PR / Tag / Release，无真实 Langfuse 实发、私人资料上传或生产部署。已有 M5/M6 发布和治理事实不变；本节不是新发布回执。

## 既有事实：M6 软件、独立回执和治理已完成

- M6 的 `execution_status` 与 milestone `status` 均为 `released`，独立回执 `receipt_status=verified`，发布剩余事项为空。[Issue #25](https://github.com/1040942669/legal-rag-agent/issues/25) 于 `2026-10-02T12:27:11Z` 以 `completed` 关闭；[Milestone 7](https://github.com/1040942669/legal-rag-agent/milestone/7) 于 `12:27:23Z` 关闭，`12:27:32Z` 核验为 open 0、closed 3。这些治理动作在精确 receipt master 门禁通过后发生。M7 在总体路线图仍为必需项，保持 `not_started`、`required=true`，只是不进入本次 M6 执行范围。
- 独立 [回执 PR #29](https://github.com/1040942669/legal-rag-agent/pull/29) 的最终 head 为 `51cbe21a9e08b3df68a8eeed296adfb11d3a0ca7`；[CI 37003455439](https://github.com/1040942669/legal-rag-agent/actions/runs/37003455439) 精确绑定该 head，四路全部 success，于 `2026-10-02T12:08:05Z` 更新完成。PR 于 `12:10:19Z` 普通 merge 为 `7ec13709d90fad1a01b85b9558a3bb8924a0846d`，不是 squash：两父提交为软件 master `582eb8949c1150fc7a12761bd46fbda9c173ef62` 和回执 final head `51cbe21a...`。[精确 merge-target master CI 37005116491](https://github.com/1040942669/legal-rag-agent/actions/runs/37005116491) 四路全部 success，于 `12:25:27Z` 完成。
- 首个回执候选 `359def8477a9121f2bbd62ccc97bf4c20df86e16` 的 [CI 37002990833](https://github.com/1040942669/legal-rag-agent/actions/runs/37002990833) 已于 `2026-10-02T11:53:14Z` cancelled/superseded，不是 passed。final head `51cbe21a...` 的本地 M0 文件为 ignored `.tmp/m6-v071-receipt-final-head-m0-gate.json`，`7/7` mandatory、exit 0、122887 ms；全量 `1013 passed, 157 subtests passed in 118.92s`，JUnit `1170/0/0/0`。早期真实失败与取消记录保留，不改写为成功。
- 回执 master 的 offline/M4/M5/M6 mandatory 分别为 `25/25`、`41/41`、`51/51`、`58/58`，全部 passed、exit 0。M6 累计 gate 为 560308 ms，worker JUnit `76/0/0/0`、累计 M5 JUnit `81/0/0/0`；全量 `1013 + 157 subtests`、JUnit `1170/0/0/0`，M4 integration `79/0/0/0`。fault/worker closed-schema validator errors 均为 `[]`，绑定精确回执 master，`live_model_calls=false`。十场景及 recovery demo passed；真实 PostgreSQL 18 重启、新进程 verification、vector `0.8.6` 和 head `0007_m6_jobs_outbox` 已核验，M5 fault 回执自己的 schema head `0006_m5_harness_recovery` 不混同为当前数据库 head。
- 回执 master M6 artifact ID `11226250664`，API digest `sha256:35442ff5d6b44e3f63e2a62a4d4336f4bcb8ecd1d53829e64290f22d1537b2a9`；PR final-head M6 artifact ID `11224654642`，digest `sha256:449a9832b6c1b5649a293ab753730c21e5c68fd513610c27fedb0e9e598746f2`。实际回执 master M6 wheel 为 `0.7.1`、438731 bytes、101 entries，SHA-256 `4974cf563c3fdac18b8bd13ce5d18052e6e11405f31a387eba08e97f0c011708`；仓库外 installed-wheel smoke passed，13 modules、jobs CLI、head `0007_m6_jobs_outbox` 及 `source_checkout_isolated=true` 均核验。四路主制品目录为 `.tmp/m6-receipt-final-pr-ci-37003455439` 与 `.tmp/m6-v071-receipt-master-ci-37005116491`；master 三路独立交叉核验目录为 `.tmp/m6-v071-receipt-master-secondary-37005116491`。
- 软件发布事实保持不变：PR #28 final head `d6fc26882237ca149ab38c7e944b32a260e0430b` 普通 squash 合并至 `582eb8949c1150fc7a12761bd46fbda9c173ef62`，软件精确 head/master CI `36998001012` / `36999853797` 已通过。annotated `v0.7.1` 与正式 Release `401759927` 仍固定软件目标 `582eb894...`，不改指向文档回执或最终化提交。已发布软件 master 的 wheel 仍为 438489 bytes，SHA-256 `00a37901b690c8a2059f930c63310de431b2eef7c591c89a40c31580f6259aa3`；不被上述回执文档构建 wheel 替代。旧 `v0.7.0` 的 31 个原始 M6 字段及 `initial_release_history`、M5 `v0.6.0` 均完整保留。

## 历史快照：非递归治理最终化自身验证后停止

当前分支 `codex/m6-v071-finalization` 从 fresh `origin/master` `7ec13709d90fad1a01b85b9558a3bb8924a0846d` 创建，该 SHA 只是已核验回执 master 和此次最终化基线，不是本次尚未提交文档自己的 SHA。冻结本次唯一最终化提交后再读取实际 candidate SHA，运行适用本地门禁和自身精确 head 四路 CI；按当时 review/保护规则正常合并自身 PR，再核验自身精确 merge-target master 四路 CI，然后停止。当前文档中的未来 finalization SHA、PR、merge、CI URL 均为 null，门禁状态为 `not_run`，没有提前声明绿灯。

这是一次非递归最终化，不为上述最终化再生成跟进回执，不发布新版本或新 Release，不再创建 M5 回执，不移动已发布 Tag，不启动 M7。测试期间不得修改任何 tracked 候选文件，包括文档，因为 M2 身份包含整个 tracked `diff_hash`。真实/付费模型、远端 Langfuse 实发、私人资料、未授权语料和生产部署仍未启用。

---

## 历史：v0.7.1 软件已发布、独立回执待完成快照

- 当前里程碑仍为 M6，状态 `released_receipt_pending`，不是整个 M6 已完成。`v0.7.1` 运行时观测补全软件已通过精确软件 head 和发布目标 master 四路 CI，annotated Tag 与正式 Release 已远端核验；独立回执自身门禁、合并及治理关闭尚未发生。
- 软件 [PR #28](https://github.com/1040942669/legal-rag-agent/pull/28) final head 为 `d6fc26882237ca149ab38c7e944b32a260e0430b`；[精确 candidate CI 36998001012](https://github.com/1040942669/legal-rag-agent/actions/runs/36998001012) 的 offline、M4 service、M5 fault、M6 worker 全部 success。PR 于 `2026-10-02T11:13:49Z` 普通 squash 合并为 `582eb8949c1150fc7a12761bd46fbda9c173ef62`；该精确 push 的 [master CI 36999853797](https://github.com/1040942669/legal-rag-agent/actions/runs/36999853797) 亦为 4/4 success。软件 head 与 merge target 的 tree 同为 `3e0f3aa4d3c012bc868e4f495b23edbaaa225e42`。
- annotated `v0.7.1` Tag object 为 `a4d7c84087ba32ad183efd20275778f5f473bd9c`，peeled target 精确为软件 merge commit `582eb894...`。正式 [Release v0.7.1](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.7.1)，ID `401759927`，于 `2026-10-02T11:30:49Z` 发布；在 `11:31:17Z` 读回确认非 draft、非 prerelease，targetCommitish 为该软件提交，附件 0。不重复发布，也不移动已发布 Tag。
- 当前文档回执分支为 `codex/m6-v071-release-receipt`，从 fresh `origin/master` `582eb894...` 创建，仅补写软件已发生的发布事实。新回执路径为 `docs/refactor/receipts/M6-v0.7.1.json`；首个候选 `359def8477a9121f2bbd62ccc97bf4c20df86e16` 已 commit、正常 push，并建立 [Draft PR #29](https://github.com/1040942669/legal-rag-agent/pull/29)。该 SHA 不是最终文档 head；本次更新不自引用未来 SHA。最终 receipt head、merge 和对应 master CI 仍 pending/null，不把旧 PR #27 当作此次补丁回执。
- `STATE.json` 的当前 M6 primary 字段及 `last_verified_release` 指向 `v0.7.1`；旧 `v0.7.0` 软件、测试、发布和 PR #27 独立回执字段完整保留在 `initial_release_history`，其完整证据仍在 `receipts/M6.json`。M5 `v0.6.0` 与初版 `v0.7.0` 的 Tag、目标和 Release 均保持不变，不为 M5 新建回执。
- [Issue #25](https://github.com/1040942669/legal-rag-agent/issues/25) 与 GitHub [Milestone 7](https://github.com/1040942669/legal-rag-agent/milestone/7) 在 `2026-10-02T11:31:17Z` 核验仍 open，Milestone 有 1 个 open issue。在新独立回执通过自身门禁、正常合并且精确 receipt merge-target master CI 成功之前不关闭治理事项。M7 未开始。

## 历史：软件发布证据与当时下一条可执行动作

- 软件发布目标 master 的 M6 累计 gate 为 `58/58` mandatory passed、exit 0、464392 ms；合并 worker/观测专项 JUnit `76/0/0/0`，156.040 s。M6-T06 为 `42 passed in 23.05s`，M6-T07 合计 `4 passed in 41.49s`，明确包含 1 个真实 broker 并发用例和 3 个真实 PostgreSQL graph 观测用例，不把全部 4 个称为 broker 用例。M5 fault 与 M6 worker closed-schema validators errors `[]`，回执绑定精确 `582eb894...`，`live_model_calls=false`。
- 同一 master run 的 offline/M2、M4、M5 gate 分别为 `25/25`、`41/41`、`51/51`；各路全量 `1013 passed, 157 subtests passed`，JUnit `1170/0/0/0`。独立 M4 integration 为 `79/0/0/0`，M5 fault 为 `81/0/0/0`；M5 十场景与 recovery demo 均 passed。实际 PostgreSQL service restart、新进程 verification 和独立应用重启通过，不用单元 fake 冒充这些机制的证据。
- master M6 artifact ID `11224096162`，API digest `sha256:6368207aa99ac72126f32257b69bee668a3e3cc8e90aa7c66b2ca51fb261c769`。实际 M6 wheel 为 `0.7.1`、438489 bytes、101 entries，SHA-256 `00a37901b690c8a2059f930c63310de431b2eef7c591c89a40c31580f6259aa3`；离线仓库外 installed-wheel smoke passed，13 个 runtime modules、jobs CLI 和 migration head `0007_m6_jobs_outbox` 通过，`source_checkout_isolated=true`。M4/M5 job 的 wheel 哈希不同，各自只用于各自 probe，不替代 M6 wheel。
- 三路制品在独立 ignored `.tmp/m6-patch-master-secondary-36999853797` 下载核验；完整四路主证据在 `.tmp/m6-patch-master-ci-36999853797`。实际内部文件哈希及 candidate/master provenance 由本次新回执记录，早期本地 wheel 仅是提交前证据，不冒充上述最终 CI wheel。完整软件范围、限制及保留的失败见 [补丁报告](../../reports/refactor/M6-observability-patch.md)。

下一步冻结独立 `v0.7.1` 回执 PR #29 的最终文档候选，核验其适用本地门禁及精确 receipt head 四路 CI，按 review/保护规则正常合并，再核验精确 receipt merge-target master CI。仅在该链路全部通过后关闭 Issue #25/Milestone 7，补写真实治理最终化事实，然后停止，不进入 M7。本次不创建新软件版本、不重复 Release、不移动 `v0.7.1`、`v0.7.0` 或 `v0.6.0`。测试运行期间不能修改任何 Git 候选文件，因为 M2 身份包含整个 tracked `diff_hash`。真实或付费模型、远端 Langfuse 实发、私人资料、未授权语料及生产部署均未启用。

---

## 历史：v0.7.1 软件发布前观测补全候选

- 当前里程碑仍为 M6，整体状态 `in_progress`。`v0.7.0` 软件与其独立回执已经完成；此次 `v0.7.1` 补丁已连接 `MASTER_PLAN.md` §11.6 的实际运行观测机制并通过本地运行路径回归，补丁状态为 `ready_for_review / pending_exact_final_ci`。最终候选门禁、发布、独立回执及治理尚未完成，因此不关闭 M6，也不把旧门禁通过等同于完整 M6 完成。
- 当前工作分支 `codex/m6-observation-completion` 从已核验的 `origin/master` `0dcafb87dd8537d66f6486febf73fd0d1258b0ed` 创建。首个已冻结本地基线 `10004b4f7b992f6a192d9b6674108bcea3f7b06d` 已 commit、正常 push，并建立 open 的 [Draft PR #28](https://github.com/1040942669/legal-rag-agent/pull/28)。该 SHA 只标识首个本地验证基线；本次文档更新尚未提交，不自引用未来提交 SHA，最终候选 SHA 应在冻结后读取并在后续证据中记录。
- 软件补丁计划为 `v0.7.1`，用于完成 M6 运行时观测及真实性修正，不是只为文档生成软件版本。已经发布的 `v0.7.0` 和 M5 `v0.6.0` 保留原 Tag、目标提交和 Release，不移动或重复发布。
- 原确认缺口包括：typed Observation 的预算、重试、缓存、证据、模型用量与耗时字段主要只在人工构造测试中出现，M2 工件和 M5 在线 run 缺少统一关联，以及幂等 API 重放可能把既有其它状态的 job 误记成 queued。实际调用点和可信持久事实已经连接，并新增运行路径回归；这些机制在本地已验证，仍须通过最终精确候选四路 CI，不提前声称发布完成。
- 首个冻结基线包含 jobs dispatcher/handlers、API 装配与状态事件、M5 graph/node/runner 观测、M2/local Trace 关联、provider 用量可用性以及对应观测回归。本次只补记 `STATE.json` 与本文件的当前事实；主执行流程负责冻结最终代码及文档候选和收口最终证据。
- [Issue #25](https://github.com/1040942669/legal-rag-agent/issues/25) 与 GitHub [Milestone 7](https://github.com/1040942669/legal-rag-agent/milestone/7) 于 `2026-10-02` 只读复核仍 open，Milestone 有 1 个 open issue。两者在完整补丁和独立回执通过之前保持 open；M7 未开始，也不在本次授权范围。

## 历史：已核验的 v0.7.0 软件与独立回执

- 原软件 [PR #26](https://github.com/1040942669/legal-rag-agent/pull/26) final head `b9400ab289618707a53ee6b14f6ac1cee4af2ee1` 和 release target `28517b6f323253baf638ba60c887d10630dd0bf1` 各自的四路 CI 均成功。annotated `v0.7.0` object `f40715c16e90b429a7c49a6347092114fefc787d` 固定 peeled 到该软件提交，正式 [Release](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.7.0) 已于 `2026-09-29T10:39:52Z` 发布，非 draft、非 prerelease。
- 独立回执 [PR #27](https://github.com/1040942669/legal-rag-agent/pull/27) 的 final head `5d20b4256582b36159333b9998844d148b104d01` 经 [CI 36559972128](https://github.com/1040942669/legal-rag-agent/actions/runs/36559972128) 四路 success，于 `2026-09-29T11:21:13Z` 普通 merge 为 `0dcafb87dd8537d66f6486febf73fd0d1258b0ed`。该精确 merge-target [master CI 36561210905](https://github.com/1040942669/legal-rag-agent/actions/runs/36561210905) 亦为 offline、M4 service、M5 fault、M6 worker 四路 success；上述 GitHub 元数据在 `2026-10-02` 再次只读核验。
- final receipt head/master artifact 均有 `58/58` mandatory passed、exit 0、worker JUnit `41/0/0/0`，以及 `0.7.0` wheel 隔离 smoke passed。master M6 artifact digest 为 `sha256:1d1cf3e74c428b8ac82bfee22fe80bb6404c19615993db3d4d9c8929ca70e898`，内部 gate JSON SHA-256 为 `b963528a81f26ee3b9d1253c071988c2e9e2132fed2f2214fd14485d4c3a7df7`；下载的 worker 回执精确绑定 `0dcafb87...`。完整 final-head/master 摘要及内部文件哈希补入 [M6 回执](receipts/M6.json)。这些是历史已发布机制的证据，不覆盖新的未提交补丁。
- 修正了历史回执中软件 master M5 artifact digest 少一个末尾字符的抄写错误，GitHub Actions API 核验正确值为 `sha256:1bfbc884675b77bcf9e62df295431e318658ca0cd75c1b893821d15348a94d4c`；没有改动软件、测试结果或任何远端发布对象。

## 历史：v0.7.1 软件发布前验证边界与待办

新的 `v0.7.1` 定向 jobs/API/M2 回归 `40 passed in 33.57s`，隔离 PostgreSQL durable 回归 `27 passed in 8.23s`，旧位置参数与打包检查 `8 passed in 0.54s`。本地 commit 前真实 wheel 为 `0.7.1`、440990 bytes、SHA-256 `1526add72ce32c5ec8db92fb5aa6fc4e886198328241bdbff5bae4645fc66028`；M6 仓库外隔离 smoke passed，`source_checkout_isolated=true`，新增模块导入及 migration head `0007_m6_jobs_outbox` 均通过。该 wheel 是提交前本地临时证据，不是最终 CI wheel。

首个冻结本地基线 `10004b4f7b992f6a192d9b6674108bcea3f7b06d` 的 M0 离线基础门禁已真实通过：`2026-10-02T10:47:05Z` 至 `10:49:22Z`，status `passed`、exit 0、`7/7` mandatory、136510 ms；内部全量 `1013 passed, 157 subtests passed in 132.70s`，JUnit `1170/0/0/0`。结果保留在 ignored `.tmp/m6-observation-committed-m0-gate.json`，不是本次尚未提交文档的最终候选证据。

此前全量门禁的旧生成元数据、可编辑安装文件清单及运行期间 tracked `diff_hash` 漂移失败均保留在 [补丁报告](../../reports/refactor/M6-observability-patch.md)，没有弱化断言。测试期间不得修改任何 Git 候选文件，包括文档，因为 M2 身份绑定整个 tracked diff，而不只运行时代码。

[初始 PR CI 36997422129](https://github.com/1040942669/legal-rag-agent/actions/runs/36997422129) 启动在首个冻结基线 `10004b4...`，记录时仍在运行；即使该轮通过，也不能代替本次最终文档 head 的四路 CI。最终候选尚未冻结，精确 final-head CI、merge/master CI、Tag/Release、独立回执及治理均未完成，不得复制旧 `58/58`、`41/0/0/0` 或旧 wheel SHA 作为补丁通过证据。

下一步先冻结完整代码和本次文档候选，核验适用本地门禁及 PR #28 精确 final head 四路 CI，再按 §13 的 review/保护规则检查、普通 merge、精确 master CI、annotated `v0.7.1` 和 Release、独立回执及治理收口顺序推进。任一门禁失败记录真实状态和失败证据；不强推、不 admin merge、不绕过 review。Issue #25/Milestone 7 在完整链路通过前保持 open。真实或付费模型、远端 Langfuse 实发、私人资料和未授权语料均未获启用。完成 M6 后停止，不进入 M7。

---

## 历史：v0.7.0 软件已发布，独立回执待完成

- M6 软件状态为 `released_receipt_pending`，不是发布失败，也尚不能把独立回执和 Issue/Milestone 治理写作完成。M6 从已核验的 M5 finalization `origin/master` `76a038936ddfd900f98ad8709fedcc50c07063d3` 开始；M5 `v0.6.0` Tag/Release 保持不变。
- 软件分支 `codex/m6-async-jobs` 的 [PR #26](https://github.com/1040942669/legal-rag-agent/pull/26) 最终 head 为 `b9400ab289618707a53ee6b14f6ac1cee4af2ee1`；[精确 head CI 36553279892](https://github.com/1040942669/legal-rag-agent/actions/runs/36553279892) 的 offline、M4 service、M5 fault、M6 worker 四路全部成功。PR 于 `2026-09-29T10:19:15Z` 普通 squash 合并，实际 merge commit 为 `28517b6f323253baf638ba60c887d10630dd0bf1`。该精确 master commit 的 [CI 36554828645](https://github.com/1040942669/legal-rag-agent/actions/runs/36554828645) 同样四路全部成功。
- 远端 annotated `v0.7.0` Tag object 为 `f40715c16e90b429a7c49a6347092114fefc787d`，peeled target 为上述软件 merge commit `28517b6...`；[GitHub Release v0.7.0](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.7.0) 于 `2026-09-29T10:39:52Z` 发布，已核验非 draft、非 prerelease，附件 0。Tag 不随回执文档提交移动，也不重复创建 Release。
- 独立回执工作分支 `codex/m6-release-receipt` 从已发布的 `origin/master` `28517b6...` 创建，仅修改文档。首个回执候选 `09bc3713c901e9ea11f48cda0fdb6b3ec441ae2b` 已 commit、正常 push，建立 [Draft PR #27](https://github.com/1040942669/legal-rag-agent/pull/27)；本次补记会产生新的最终文档 head，因此不得把首轮 CI 代替该最终 head、回执 merge 或 master CI。
- 跟踪 [Issue #25](https://github.com/1040942669/legal-rag-agent/issues/25) 和 GitHub [Milestone 7](https://github.com/1040942669/legal-rag-agent/milestone/7) 仍 open；应在独立回执正常合并且精确 merge-target master CI 成功后再关闭。M7 尚未开始，也不在本次范围。
- 本次授权覆盖在门禁和 review/保护规则满足时的正常 commit、push、PR、合并及发布。没有强推、admin merge 或 review 绕过；默认禁用真实或付费模型调用，不上传凭证、私人资料或未授权语料。

## 当前验证与可证明边界

- 发布目标 master run `36554828645` 的 M6 artifact 记录累计门禁 `58/58` mandatory passed、0 failed、exit 0；真实 PostgreSQL/Redis/Celery worker JUnit `41/0/0/0`，M6 `0.7.0` wheel 隔离安装/smoke passed，`live_model_calls=false`。M6-T01 至 T05、T07 是真实服务场景；T06 是 provider-free 观测单元。PR head `b9400ab...` 的四路 CI 也通过。具体证据、复现、早期失败和不运行项见 [M6 验收报告](../../reports/refactor/M6.md)，回执中应绑定精确 master artifact digest/文件哈希。
- 本地已执行 M0 离线基础门禁 `7/7` mandatory passed，内部全量 `977 passed, 157 subtests`、JUnit `1134/0/0/0`；安全修正后的隔离 PostgreSQL suite 曾 `44 passed`，最后定向 claim 隔离测试加入后按 CI 顺序 M6 数据库 suite `24 passed`。更早 code-frozen head `ed0c980...` 的四路 CI 也是通过，但不能代替最终软件 PR head 或 master 证据。
- 独立文档回执候选本地 M0 基础门禁再次 `7/7` mandatory passed、exit 0、109620 ms；内部全量 `977 passed, 157 subtests`、JUnit `1134/0/0/0`，35 个候选 Markdown 链接、STATE/manifest 与 244 个 Git-candidate 文本文件秘密形态检查通过。这不代替回执 PR 最终精确 head CI；临时结果保留在 ignored `.tmp/m6-receipt-m0-gate.json`。
- [回执 PR #27 首轮 CI 36558052677](https://github.com/1040942669/legal-rag-agent/actions/runs/36558052677) 对精确 head `09bc3713c901e9ea11f48cda0fdb6b3ec441ae2b` 四路全部 success：offline、M4 service、M5 fault、M6 worker。四个远端 artifact digest 与内部 M6 文件哈希写入 `docs/refactor/receipts/M6.json`；它仍是首轮候选证据，当前补记后的最终文档 head 需要重新跑 CI。
- 未运行真实法律语料/在线模型质量、人工法律评审、付费模型实验、生产部署及生产容量/SLO；因此本版只声称工程机制的 provider-free 验证，不声称法律回答质量或生产效果。隔离本地 PostgreSQL 55436 已停止，既有 5432 未触及；工具策略两次拒绝删除精确 ignored `.tmp/m6-pg-b8853e7d36d6` 与 `.tmp/m6-restart-receipt-b8853e7d36d6.json`，本地临时产物仍在且未上传。
- 已知可靠性边界：激活、item 与 job terminal 在同事务并受 lease/epoch/cancel fence；默认每 job 最多自动生成 5 条 outbox 记录并退避，耗尽时状态 `queued/delivery_unconfirmed`。同一 pending row 的未确认 broker 发送仍可重试，这不是发送调用硬上限。若预算耗尽且 broker 消息全失，需授权运维人工对账；不提供普通用户无限重投，不承诺 exactly-once。

## 下一条可执行动作

核验本次回执证据补记后的最终 PR #27 文档 head 四路精确 CI，核查 review/保护规则后正常合并，并核验回执 merge-target master CI。随后关闭 Issue #25/Milestone 7，补写真实治理事实；非递归 finalization PR 仍必须正常通过自己的门禁，不生成软件版本或移动 `v0.7.0`。任何一步未完成都保留 `released_receipt_pending`，不得再发一次 Release。完成本次 M6 范围后停止，不进入 M7。

---

## 历史：M6 发布前候选交接

## 当前事实

- M6 `v0.7.0` 在 `codex/m6-async-jobs` 已形成代码冻结候选，状态 `ready_for_release`，不表示已合并或发布。基线是已核验 `origin/master` `76a038936ddfd900f98ad8709fedcc50c07063d3`。该提交合并了 M5 finalization PR #24，[master CI 36504995613](https://github.com/1040942669/legal-rag-agent/actions/runs/36504995613) 三项全部成功。M5 `v0.6.0` 仍已发布，Tag 不移动，也不为 M5 新增回执。
- M6 跟踪 [Issue #25](https://github.com/1040942669/legal-rag-agent/issues/25) 和 GitHub [Milestone 7](https://github.com/1040942669/legal-rag-agent/milestone/7)。[Draft PR #26](https://github.com/1040942669/legal-rag-agent/pull/26) 的代码冻结 head 为 `ed0c980c8f6a5bf126c10d8876e49342f4403444`，[四路精确 head CI 36550803746](https://github.com/1040942669/legal-rag-agent/actions/runs/36550803746) 全绿。先前 `a7f4bc5...` 的 [CI 36546785727](https://github.com/1040942669/legal-rag-agent/actions/runs/36546785727) 也为四路成功，但早于激活 fence 与有界 outbox 重投修正，不能替代新候选门禁。安全修正提交为 `fde7f455ac3b43036887ee6c547cdf30e46c6318` 和 `6baaffa9b5ade8f2a1a27b1228ba6624501406d6`。本次交接文档后续提交无法自引用自身 SHA，仍需在该文档最终 head 重跑精确 CI。M6 尚无 Tag 或 Release。
- 本次授权：在门禁和仓库 review/保护规则满足时可 commit、push、创建 PR、正常合并并发布；不能强推或绕过 review。默认禁用 live/paid model，不上传凭证、私人资料、未授权语料。

## 实施中的 M6 文件与边界

- `legal_rag/jobs/`：服务端注册引用、PostgreSQL job/item/outbox、dispatcher、Celery worker 与 M2/M3 业务 handler；新增 `legal_rag/storage/alembic/versions/0007_m6_jobs_outbox.py` 和对应中央 schema metadata。
- `legal_rag/api/app.py`、`schemas.py`、`command.py`：owner-scoped 202 创建、状态/取消，`LEGAL_RAG_JOB_REGISTRY_PATH` opt-in 装配与 M6 schema readiness；不把在线 M5 graph 节点入队。
- `legal_rag/observability/`：typed local observation、默认关闭且需明示授权的脱敏 Langfuse exporter；失败不能改变主任务。
- `scripts/quality_gate.py`、`.github/workflows/quality-gate.yml` 与 M6 专项测试：58 项累积 gate 和独立 Linux PostgreSQL/Redis/Celery job 已在代码冻结 head 通过，最终文档 head 仍须重新验证；M6 在合并及发布前仍不是 `released`。
- `README.md`、`CHANGELOG.md`、`docs/README.md`、`reports/refactor/M6.md`、`decisions/ADR-004`：任务流程、可证明范围、配置、回滚和真实证据。

## 当前验证与待办

- 本地全量离线 `uv run --offline --frozen --no-sync pytest -q tests`：首轮 `976 passed, 157 subtests passed in 99.36s`；M5 wheel 工作流版本修正后的工作区复测 `976 passed, 157 subtests passed in 95.27s`，exit 0。聚焦工作流/门禁单元 `102 passed in 0.78s`。这些是修正前的本地历史记录，不代替后述 `ed0c980...` 精确 CI。
- 修正后本地 M0 累计基础门禁 `7/7` mandatory passed，exit 0，耗时 97829 ms；其中再次执行全量离线测试，Markdown 链接、STATE/manifest 和 Git-candidate 秘密形态扫描全部通过。门禁 JSON 保存在 ignored `.tmp`，不上传。
- 隔离 PostgreSQL 18.1/pgvector 0.8.1 集成：M6 store 初轮 9/9；修复中央 Alembic metadata 后，M6 store + M5 schema + M3 migration `22 passed in 18.84s`；加测 rollback 与 downgrade guard 后 store 13/13。累计 DB suite 在真实独立服务重启前 `37 passed in 22.93s`、重启后 `37 passed in 20.15s`；`m3_restart_probe` prepare/实际 stop/start/new-process verify 全部 exit 0。旧 M4 current-head 断言更新为 0005→0006→0007 后，聚焦 unit 27/27。既有 5432 未触及。
- 保留失败：初次跨迁移 2 failed/10 passed，原因是 0007 表缺中央 metadata；旧 M4 schema current-head 断言初次 1 failed/26 passed。均已定向修复并复测。完整细节见 [M6 报告](../../reports/refactor/M6.md)。
- 首轮精确 head [CI run 36544268358](https://github.com/1040942669/legal-rag-agent/actions/runs/36544268358)：offline、M4、M6 三路 success；M6 真实 Redis/Celery JUnit 32/0/0/0 覆盖 T01-T05/T07，T06 观测单元为 6/0/0/0，58/58 累计 gate、真实 PostgreSQL service restart 和隔离 0.7.0 wheel probe 通过。T04 是不可达 loopback Redis 地址的真实连接失败/恢复，并非停机共享 Redis 容器。M5 job 在 wheel probe 因固定 `--expected-version 0.6.0` 与 0.7.0 候选冲突而失败；随后改为读取候选版本并在第二轮重跑通过。首轮整体 3/4，不是发布门禁通过。
- 第二轮精确 head `a7f4bc5...` 的 [CI run 36546785727](https://github.com/1040942669/legal-rag-agent/actions/runs/36546785727) 四路 success；M5 0.7.0 兼容 wheel probe、M6 32/0/0/0 真实 worker JUnit、58/58 累计 gate 均通过。但这轮发生在发布前复核发现的两处风险修正之前，不能作为后续新代码的门禁。
- 代码冻结精确 head `ed0c980c8f6a5bf126c10d8876e49342f4403444` 的 [CI run 36550803746](https://github.com/1040942669/legal-rag-agent/actions/runs/36550803746) 已核验四路 success：offline、M4、M5、M6。M6 artifact ID `11025178041` 中累计 gate `58/58`，真实 PostgreSQL/Redis/Celery worker JUnit `41/0/0/0`，M6 `0.7.0` wheel 隔离安装与 smoke 成功，`live_model_calls=false`。M6-T01 至 T05、T07 为真实服务场景，T06 为 provider-free 观测单元。此结果覆盖激活 fence、有界 outbox 及定向领取测试，但后续仅文档提交会产生新 PR head，必须再跑四路门禁。
- 已 push 的 `ed0c980...` 代码冻结 head 在本地再次运行 M0 离线基础门禁：7/7 mandatory passed，exit 0，103264 ms；内部全量 `977 passed, 157 subtests`，JUnit `1134/0/0/0`，链接、STATE 与 243 个 Git-candidate 文本文件秘密形态扫描通过。该本地结果同样不能代替最终文档 head 的远程 CI。
- 安全修正本地初验：`integration_tests/test_m6_outbox_bounded_recovery_db.py`、`test_m6_handlers_db.py`、`test_m6_job_store_db.py` 与 M3 catalog/M5 schema/M3 migration 在隔离 PostgreSQL 55436 随机数据库合计 `44 passed in 19.78s`。最终导入激活改为同事务 job lease/epoch/cancel fence、pointer/item/terminal 提交；每个 job 默认最多自动创建 5 条 outbox 记录并指数退避，耗尽时 `queued/delivery_unconfirmed` 可见，迟到 worker 领取会清 warning；同一 pending 记录的未确认发送仍可重试。若所有 broker 消息丢失，尚需授权运维人工恢复，不能声称无限自动续跑。
- 安全修正后的 M0 本地基础门禁两轮均 7/7 mandatory passed；第二轮内部全量 `977 passed, 157 subtests`、JUnit 1134/0/0/0、STATE/链接/243 个 Git-candidate 文件秘密形态检查通过。该全量轮次早于最后的定向 outbox 领取隔离测试与文档精度修正；之后聚焦 unit `110 passed`、按 CI 文件顺序 M6 store/handler/outbox 数据库 suite `24 passed in 5.94s`，最终候选仍需精确 head CI。新门禁测试曾因误用 `scripts` 包导入得到 `1 failed, 102 passed`，改为既有文件加载方式后 `103 passed`；失败未当作通过证据。
- 尚未完成：最终文档 head 的四路 exact-head CI、review/仓库规则核验、普通 merge、精确 merge-target master CI、`v0.7.0` Tag/Release 与独立回执。隔离本地 cluster 经 data_directory/端口/PID 三重核对后再次正常停止，`pg_isready` 为 no response；既有 5432 未触及。工具策略此前两次拒绝清理精确 `.tmp/m6-pg-b8853e7d36d6` 和 `.tmp/m6-restart-receipt-b8853e7d36d6.json`，忽略的本地临时产物仍在，未上传。

## 下一条可执行动作

核对 PR #26 仅文档候选新精确 head 的四路 CI。全部成功后，按 `MASTER_PLAN.md` §13 的 review、普通 merge、精确 master CI、Tag、Release、独立回执顺序推进。若 CI、review、schema 或测试任一门禁不满足，停在真实状态，不造发布声明。M7 不在本次范围。

---

## 历史：M5 执行交接

## 当前状态

- 当前里程碑：M5，版本 `v0.6.0`。
- 当前状态：`released`。软件发布、独立发布回执、回执 merge-target master CI、Issue 与 Milestone 治理关闭均已完成并远端核验。
- 软件分支：`codex/m5-harness-recovery`。
- 软件 PR：[PR #22](https://github.com/1040942669/legal-rag-agent/pull/22)，final head `aa737e8d1f77214277c0544ce069d36c2b2161ff`，普通 merge commit `832acaafaf5633e76daed7a62a73755187fca51e`。
- 精确 PR-head CI：[run 36497021956](https://github.com/1040942669/legal-rag-agent/actions/runs/36497021956)，3/3 jobs success。
- 精确 release-target master CI：[run 36498443123](https://github.com/1040942669/legal-rag-agent/actions/runs/36498443123)，3/3 jobs success。
- annotated Tag：`v0.6.0`，Tag object `c0ef0721ab49da0d7840b76e741a52e35b8941d2`，peeled target 精确为 `832acaafaf5633e76daed7a62a73755187fca51e`。
- GitHub Release：[v0.6.0 - Durable Harness Recovery](https://github.com/1040942669/legal-rag-agent/releases/tag/v0.6.0)，于 `2026-09-28T23:41:41Z` 发布，非 draft、非 prerelease，附件 0。
- 当前 finalization 分支：`codex/m5-release-finalize`，基线为回执普通 merge commit `3436e9ad41c7455aa5f31117ab7ece3f4ea847c1`。该分支只记录已经发生的治理事实，不新增产品能力，也不生成递归回执。
- 独立回执 PR：[PR #23](https://github.com/1040942669/legal-rag-agent/pull/23)。first candidate `3eead1deb511325c91d47b31f9c39f2868607b07` 的 [CI run 36500603258](https://github.com/1040942669/legal-rag-agent/actions/runs/36500603258) 与 final head `9dd6ec3867f05dd207ec15657861028f138167fc` 的 [CI run 36501403167](https://github.com/1040942669/legal-rag-agent/actions/runs/36501403167) 均为 3/3 jobs success。PR 于 `2026-09-29T00:13:59Z` 普通合并为 `3436e9ad41c7455aa5f31117ab7ece3f4ea847c1`，该精确 commit 的 [master run 36502063863](https://github.com/1040942669/legal-rag-agent/actions/runs/36502063863) 亦为 3/3 success。
- 跟踪项：[Issue #21](https://github.com/1040942669/legal-rag-agent/issues/21) 于 `2026-09-29T00:23:07Z` 关闭；确认 `open_issues=0` 后，跟踪 M5 的 GitHub [Milestone 6](https://github.com/1040942669/legal-rag-agent/milestone/6) 于 `2026-09-29T00:23:22Z` 关闭。这里的 GitHub Milestone 编号不表示路线图 M6 已开始。
- 本里程碑没有真实或付费模型调用，没有生产部署，没有强推、admin merge、auto-merge、review 绕过或保护规则绕过。
- M6 未开始；M5 完成后必须停止。

## 软件提交与发布对象

1. `4a7dc54e15fd0f7a48d0acea78dd91945890e8db`：`feat(m5): add durable harness foundations`
2. `d6f50c8e719b257bd5a3fea36e867268b3cb8243`：`feat(m5): execute bounded recoverable graph`
3. `ae65907e5e34abaa2461c28d420e7ee23e2630f3`：`fix(m5): preserve legacy executor rollback path`
4. `27c0179b176f8474365db1c805787324482343e5`：`test(m5): prove cross-process fault recovery`
5. `a0dd830c879abd6b17288abb1034857c4c5b3696`：`fix(m5): close recovery and follow-up gaps`
6. `7ba6ae831ba6f3cf0fa5739f87033fffe59bc763`：`ci(m5): enforce release fault receipts`
7. `aaa56131743da94d25b38beb670a4b6f8c4d7f53`：`test(m5): preserve legacy restart fixture readiness`
8. `979c12fab74ec0911bfa93fd9de07f7437c1eb5d`：`style(m5): format restart fixture`
9. `aa737e8d1f77214277c0544ce069d36c2b2161ff`：`docs(m5): prepare durable recovery candidate`
10. `3eead1deb511325c91d47b31f9c39f2868607b07`：M5 独立回执 first candidate
11. `9dd6ec3867f05dd207ec15657861028f138167fc`：`docs(m5): verify receipt candidate`

`aa737e8...` 是软件 PR 的最终 head，`832acaaf...` 是通过 master CI 的软件发布目标，`9dd6ec3...` 是回执 PR 的最终 head，`3436e9a...` 是通过 master CI 的回执合并目标。Tag 永久留在软件发布目标，不跟随后续 receipt 或 finalization 文档提交移动。

## 完整运行链路

### 1. HTTP、持久服务与有界图

M4 的 Bearer owner isolation、session、message、run、idempotency、SSE 与 immutable result 继续作为外层服务边界。M5 的 `GraphRunExecutor` 只在冻结的 graph version 为 `m5-bounded-v1` 时接管执行：

```text
POST /api/v1/sessions/{session_id}/runs
  -> authenticate owner
  -> freeze scope / snapshot / embedding profile / graph identity / config
  -> create idempotent durable run and absolute deadline
  -> supervisor claims lease epoch
  -> GraphRunExecutor loads PostgreSQL checkpoint and application projection
  -> analyze_query
  -> route
  -> retrieve
  -> merge_evidence
  -> check_evidence
       -> enough: generate -> verify -> persist_result
       -> insufficient and budget remains: plan_followup -> retrieve
       -> exhausted/deadline/unknown: persist limited terminal result
  -> atomically publish result, assistant message and final event
  -> SSE clients replay the durable event suffix
```

旧 graph version 继续使用稳定的 `LegalChatRunExecutor`，没有被 M5 强制迁移。图是唯一自适应控制循环，旧 adaptive/planner loop 不与它叠加。

### 2. PostgreSQL 双层持久状态

- LangGraph `PostgresSaver` 保存框架 channel/checkpoint。
- Alembic `0006_m5_harness_recovery` 增加应用拥有的 `run_checkpoints`、`run_budget_ledgers`、`run_external_attempts`、verified artifact 和恢复字段。
- 框架 checkpoint 决定图从哪里继续；应用投影决定 owner、graph/schema/hash 兼容性、可信 checkpoint pointer、预算、审计和是否允许发布。
- 只信任当前 run row 指向的、同一 lease epoch 的应用 checkpoint。旧 namespace 中的孤儿 framework checkpoint 不能推进业务状态。
- `InMemorySaver` 无法通过 readiness 或 M5-T10；普通启动不会静默建表或退回内存 saver，`legal-rag-api --migrate` 才显式运行 Alembic 与 saver setup。

### 3. deadline、预算、retry 与 side-effect journal

- run 创建时写入绝对 `execution_deadline_at`，resume 不重置。
- 默认上限为 2 个检索轮次、每轮 3 个 query、8 次工具、4 次模型、4 次 embedding、单操作 1 次重试、90 秒 deadline 和 `top_k=5`。
- 外部动作前先在 durable ledger 中预留额度，再记录 `reserved -> dispatched -> succeeded / failed / outcome_unknown / abandoned_before_dispatch`。
- `429` 和暂时网络错误只在预算内有限重试；`400`、`401`、schema 或权限错误不盲重试。
- 达到预算或 deadline 后保存 `completed_with_limits` 与机器可读 `stop_reason`，不会为了得到答案无限循环。

### 4. 三类崩溃窗口

1. 检索 checkpoint 已持久化、生成尚未开始：新进程从保存节点继续，不重复整段检索。
2. provider 或 planner 已 dispatch，甚至已返回，但结果尚未进入可信 artifact/checkpoint：保守记为 `outcome_unknown`，已预留预算不退款，默认不重发。
3. immutable result、assistant message 和 final event 已在同一事务落库，但图末 checkpoint 尚未完成：恢复时对账已有唯一结果，不生成第二个答案。

Checkpoint 无法撤回外部请求，也无法证明 provider exactly-once。M5 只做到保守记账、默认避免重复调用、披露 `possible_duplicate_cost=true`，不承诺零重复计费。

### 5. lease epoch 与 fencing

- 每次有效 claim/resume 递增 `lease_epoch`。
- heartbeat、budget、attempt、checkpoint、artifact、result、event 和 terminal transition 都验证 worker identity、epoch、revision 与数据库 wall clock。
- 两个进程同时 resume，只有一个取得执行权。
- takeover 后旧 owner 的 checkpoint 写和 terminal result 写均被拒绝，数据库行数保持不变。

### 6. 显式 resume 与 clarification follow-up

- `POST /api/v1/runs/{id}/resume` 先执行非枚举 owner isolation，再校验状态、graph/schema/hash、deadline、lease 与未知外部结果。
- M5 不无限自动恢复。stale work 先进入 `interrupted`，由 owner 显式 resume 或受控恢复命令继续。
- `needs_clarification` 是 answer-bearing 终态。用户补充信息会在同一 session 创建带 `parent_run_id` 的新 run，而不是复活旧 run。
- child run 有独立 deadline、预算、事件和结果，父 run 的已消耗额度保持不变；parent identity 进入幂等 hash，M4 无 parent 的 request hash 保持兼容。

### 7. 工具与数据安全边界

- 模型可见工具固定为 `search_laws`、`get_article`、`get_neighbors`、`inspect_evidence_metadata`。
- user、scope、snapshot、profile、数据库 DSN 和权限来自服务端冻结状态，不接受证据文本或模型输出覆盖。
- checkpoint state 是关闭字段的严格 JSON envelope，拒绝未知字段、非有限数值、连接对象、客户端、凭证、prompt、raw draft 和未验证答案正文。
- fault receipt、日志和发布回执只保存分类、计数、hash、PID 与公开 GitHub 元数据，不保存问题正文、证据正文、数据库 URL 或私密资料。

## M5-T01 至 M5-T10

| ID | 已验证机制 | 发布证据 |
|---|---|---|
| M5-T01 | hard kill 后跨 PID 从检索 checkpoint 恢复，retrieval invocation 保持 1 | passed |
| M5-T02 | provider 内、provider 返回后 artifact 前、planner 返回后 checkpoint 前三个窗口均保守 unknown，不退款且默认不重复调用 | passed |
| M5-T03 | result 落库后图结束前 kill，恢复只对账已有结果，不产生第二答案 | passed |
| M5-T04 | follow-up loop 受 durable budget 限制；clarification 创建 parent-linked 新 run，父预算不变 | passed |
| M5-T05 | `429`/timeout 有限 retry，`400`/`401` 不 retry，attempt 次数可审计 | passed |
| M5-T06 | 两进程竞争 resume 只有一个有效 owner；旧 owner checkpoint/terminal 写均被 fence | passed |
| M5-T07 | graph/schema/checkpoint 不兼容时显式失败且 pointer 不移动 | passed |
| M5-T08 | prompt injection 不能选择未知工具、覆盖冻结 scope 或泄露 sentinel | passed |
| M5-T09 | resume 保留原 absolute deadline，过期后新增 dispatch 为 0 | passed |
| M5-T10 | `InMemorySaver` 验收失败，必须使用真实 PostgreSQL saver 与不同 PID | passed |

## 验证与制品证据

### 本地精确软件候选 `aa737e8...`

- 全量 provider-free：`921 passed, 157 subtests passed in 76.08s`。
- 精确 M5 suite：`81 passed in 44.34s`，JUnit `81/0/0/0`。
- 10 个 fault scenario 全部 passed，closed-schema validator errors `[]`。
- one-command recovery demo：passed，scenario `M5-T01`，候选 SHA 精确匹配。
- 真实 PostgreSQL 18 service stop/start 后新进程复核：passed；本地 pgvector `0.8.1`，migration `0006_m5_harness_recovery`。
- M0-M5 累计 gate：`51/51`，全量测试仍为 `921 + 157 subtests`。
- 本地 wheel：393143 bytes、87 entries，SHA-256 `52e1910fb3890992f2c4aa7d2a3f39b65bcaf529c219b7a1769e806a61b4c9fa`；9 个 M5 runtime 文件、9 个 optional service dependencies、8 个仓库外隔离模块导入通过。

### 精确 PR head 与 release target

- PR #22 exact-head run `36497021956`：offline、M4 service、M5 fault 三个 jobs 全部 success。
- release-target master run `36498443123`：同样 3/3 success。
- master M5 JUnit：81 tests、0 failures、0 errors、0 skipped，time 75.248s。
- master M5 gate：51/51，duration 326375ms。
- master PostgreSQL：18；pgvector：0.8.6；migration head：`0006_m5_harness_recovery`；真实 service restart passed。
- master M5 wheel：391093 bytes、87 entries、SHA-256 `05e78eaf8430a92244a570f7f97e418b6a413fec899477509be029b6f234afeb`。
- 三个 master artifact digest 与内部文件 SHA-256 已写入 `docs/refactor/receipts/M5.json`。
- 真实或付费模型调用：0。

### 独立回执 final head 与 merge target

- PR #23 final head `9dd6ec3867f05dd207ec15657861028f138167fc` 的 [run 36501403167](https://github.com/1040942669/legal-rag-agent/actions/runs/36501403167)：offline、M4 service、M5 fault 三个 jobs 全部 success。
- final-head artifact digest：offline `sha256:8af81ba947dcf0f2ea7dc98eb4611c6acc97e84aabd10b8830a3b902e368e845`；M4 `sha256:ff9aeadef3be9a8b8f4aca993b4058f26126d171a80c06394983deb93f5e2202`；M5 `sha256:89f164949bd30288b12f93469de2035bcf93033ebd67a1c8414990528ddeff37`。
- PR #23 于 `2026-09-29T00:13:59Z` 以普通 merge commit `3436e9ad41c7455aa5f31117ab7ece3f4ea847c1` 合并；没有 admin、auto、squash、rebase 或强推。
- 精确 receipt merge-target [master run 36502063863](https://github.com/1040942669/legal-rag-agent/actions/runs/36502063863)：3/3 jobs success。artifact digest：offline `sha256:c55adfdad0d6cc47ea8921d9a7676990dbac23b89c823e16b1383435cfe3a129`；M4 `sha256:3d70c2fe5ea2b919f8e818475af45e5e2d3b5daafd55b41f21d316287b3d743d`；M5 `sha256:cdeceb76af01fcb6453a3fc9b6f702ad2670d9bffef3ec5ac6b0074b85e9fcd6`。
- final-head 与 merge-target 的全部内部 gate、JUnit、restart、demo、wheel probe 和 wheel SHA-256 已写入 `docs/refactor/receipts/M5.json`；Issue #21 与 Milestone 6 只在 merge-target CI 成功后关闭。

### 保留的失败证据

- 较早 `7ba6ae8...` 的 PR run `36495569685` 在 M4 service job 得到 `78 passed, 1 failed`。旧 M4 process fixture 没有建立 M5 persistent checkpointer，导致 readiness 未就绪。修复把遗留 fixture 明确固定为 `m4-linear-v1`，没有弱化生产 M5 的 fail-closed readiness；最终 PR 与 master 三路 CI 均通过。
- 第一次本地 restart prepare 使用复用数据库，遇到 snapshot activation conflict；该次没有进入重启。
- 第二次新数据库完成重启但名称不满足 integration fixture guard；第一次累计 gate 因相同 guard 失败关闭。随后使用 guard-compliant fresh database 完整重跑并通过。
- 这些失败保留在报告中，没有被冒充为发布成功证据。

## 已完成的治理链与最终化边界

1. 软件 PR #22 exact-head CI、普通 merge、release-target master CI、annotated Tag 与 GitHub Release 已完成。
2. 独立 receipt PR #23 first candidate 和 final head 的精确三路 CI 已完成。
3. receipt PR 已普通合并，精确 receipt merge-target master 三路 CI 已完成。
4. Issue #21 与 Milestone 6 已按依赖顺序关闭，M5 机器状态现在是 `released`。
5. 当前 `codex/m5-release-finalize` 只补写上述外部事实；该 PR 仍要求 exact-head 三路 CI、普通 merge 和精确 merge-target master CI。
6. finalization 不递归生成新 receipt，不移动 `v0.6.0`，不修改产品代码；完成后停止，不启动 M6。

## 已知限制与优先提升点

- provider exactly-once 仍不可证明。优先引入 provider 原生 idempotency key、请求账单对账和 unknown outcome 运维队列。
- supervisor 仍是受控本地调度，不是分布式 worker 系统。队列、公平调度、背压、指标、trace 和告警属于尚未开始的 M6 范围。
- PostgreSQL saver 与应用 checkpoint projection 不在一个跨表原子事务中。当前依靠可信 pointer、epoch fencing 和失败关闭；后续可评估 outbox/commit protocol。
- wheel 在不同 CI job 中内容等价但字节 SHA 不同，说明构建尚未 byte-for-byte reproducible。后续应固定归档时间戳与构建环境。
- checkpoint retention、归档、租约清理、attempt 对账和长时间运行容量尚无生产级 SLO。
- 静态 Bearer registry、loopback fixture 和 provider-free fake 不是生产 IdP、TLS、rate limit、容量或灾备验证。
- 真实法律语料覆盖、时效性、在线模型质量、引用蕴含、成本、P95/P99 延迟和人工法律评审未在 M5 验证。
- `0006` downgrade 会删除 M5 checkpoint projection、budget、attempt 和 artifact 数据。对有价值历史必须先备份并优先前向修复。
- GitHub Actions 对 pinned `actions/upload-artifact` 给出 Node.js 20 deprecated、强制 Node.js 24 的非阻塞警告，后续应升级 action pin。

## 停止条件

M5 软件、独立 receipt、receipt merge-target CI、Issue 与 Milestone 已真实核验，因此里程碑状态是 `released`。操作性停止条件只剩非递归 finalization PR 的 exact-head CI、普通 merge 和精确 merge-target master CI；任一步失败都要记录真实阻塞，不得移动 Tag、编造递归回执或启动 M6。finalization 完成后停止。
