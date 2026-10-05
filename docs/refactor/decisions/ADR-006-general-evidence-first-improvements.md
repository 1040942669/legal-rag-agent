# ADR-006: 通用证据合同与受控服务接线

状态：accepted，方案经三路独立复核及四项澄清后选择；W1 至 W8 已完成本地工程实施与适用运行时验证，首轮结果文档静态门禁3/3通过，运行时代码身份未变，不是法律质量验收或软件发布
日期：2026-10-05
影响里程碑和版本：已发布 M6 后的通用质量与接线改善，不进入 M7，不移动既有 Tag，不产生发布回执

## 背景

目标是针对已确认问题完成方案设计、独立复核、选择和实施。范围包括规则过拟合风险、显式引用配对与精确路由、机械证据与语义证据的区别、CLI/API 接线、付费安全、评测独立性和历史兼容，不能用只写审核包替代完整链路改善。

设计前基线为 `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`，本地分支为 `codex/general-rag-improvements`，保留既有未提交 Smoke/lexical 改动。实现仍在此 HEAD 上的 dirty 工作区，本轮没有产生新 commit、push、PR、Tag 或 Release。真实失败、题标、历史 manifest、summary、账本、M5/M6 发布和回执均保持原样。

以下是设计启动前已确认的基线事实，不是本次改善后的实现现状；完成后的工程能力与验证见 W1 至 W8 及 G01 至 G09：

- 网购退货是既有法律回归题，不是新增电商业务模块。现有 120 题反复用于开发，没有权威审核的独立 holdout。
- `local-lexical-v2` 混合连续汉字边界、query 去重和购物扩展；指定表面词的退货闸门可误放税务记录、误挡同义表达。已有 A/B 未拆分组件，Hit@5 为 76/108 到 78/108，4 改善、2 回退，增量 CI 跨零。
- 现有证据检查只在单部显式法律时核验条号所有权。多法请求 A10+B5 可以被 A5+B10 错误覆盖。显式法名和条号仍主要进入 BM25 boost，catalog lookup 尚未接入聊天路径。
- verifier 的 passed 主要代表结构和行为通过。语义状态未绑定真实输入，也未形成 required 发布门禁；模型声明的 claims 不保证覆盖全部可见正文。
- 默认 API 使用 PostgreSQL-bound legacy BM25、禁用生成，retrieval schema 只有 top_k。独立 Smoke 金额账本不等于在线服务金额预算。
- M5 PostgreSQL ledger 有次数/轮次而无金额；恢复默认不重发 unknown，但同进程 timeout 的内部循环仍可能重试。启用 paid service 前必须处理重复计费窗口。
- registry v1 明确拒绝 holdout，旧 scorer 用扁平目标。修改 canonical scoring 必须有历史版本解码，不能靠 code fingerprint 代替兼容合同。

## 可选方案

| 方案 | 改善范围 | 成本与失败边界 | 选择 |
| --- | --- | --- | --- |
| A. 继续补购物词表、anchor、boost | 可改善下一条已见话术 | 场景规则无界，不能解决配对或蕴含，评测过拟合风险持续 | 不作为现代策略 |
| B. 直接启用 dense/RRF/reranker | 可能改善词汇错位 | 有资源、延迟、缓存成本，相关性分数不是语义证明；当前无独立提升证据 | 作为对照候选，不单独替代证据合同 |
| C. 通用类型化证据合同、精确引用路由、冻结服务策略、绑定语义评估、评测治理 | 对每个已确认问题提供可测试机制 | 需跨类型、序列化、数据库、执行器和评测版本协同；真实质量仍需独立审核 | 推荐底座，与 B 做受控比较 |
| D. 模型自由规划并调用所有工具 | 增加开放处理能力 | 费用、未知调用、注入面及验证复杂度增加，未证明必要性 | 不选 |

## 决定

选择 C。2026-10-05 的三个独立 reviewer 分别复核了规则/语义/缓存、服务/精确路由/费用、评测治理与兼容。第一轮指出默认策略冲突、提及与需求混淆、fallback 范围和历史恢复身份四项 blocker，修正后全部接受。该轮 accepted 只表示方案获准，不是实施证明。随后按下述合同实施、独立反例复核，并完成重新冻结候选的适用本地运行时门禁及首轮结果文档静态检查；本次结果回写后由 root 再做最终静态复核，不进入 ready_for_release。

每个工作包按先有反例、再实现、再独立审查执行；下面列的是已实施的完整工程合同。工程验证只证明覆盖到的行为，不代表法律真值、完整自然语言理解、独立泛化效果或生产能力。

### W1: 通用引用与机械证据合同

- 新解析器保留原文 span，区分提及与真正请求，输出确定的 `(law_title, article_number)` requirements 和歧义状态。不做法名与条号的笛卡尔积，不读取 case ID/gold。否定、排除、转述或历史引用不能只因字面出现就升级 hard requirement；关系不明时澄清，覆盖“解释 A10，不是 B5”和“对方曾引用 B5”等反例。
- 规范化书名号、空白、国家前缀及通用数字条号；合法嵌套书名号采用有界平衡扫描，完整外层文件与内层法律保留独立身份。已知完整 alias 不被内层引用拆走，未知续名不能被远处请求词洗白成内层法条需求；不平衡、过长或过深保留 unknown。相关法律不能因子串包含而合并，未识别/歧义法名不猜测。
- 明确多法、多条需求按逐 article typed provenance 检查；授权过滤和 top_k 截断后重算。明确但缺证据时不足，关系歧义时澄清。
- 新机械状态区分有候选、范围有效、引用需求覆盖。兼容 sufficient 不代表 semantic supported；未配置范围检查保持 not_configured，而非 true。

### W2: 精确路由与完整法条

- 明确唯一引用在 frozen scope/snapshot 内调用既有 catalog lookup，保留 found/not_found/needs_disambiguation。不使用跟随 active pointer 的 lookup，不用 BM25 邻条伪满足未命中。
- 聊天证据返回原授权、profile-valid Chunk 和原 provenance，通过 lookup membership 与 bound corpus 交集及身份复核，不把完整法条正文塞入旧 Chunk 伪造 payload hash。
- 增加独立的完整法条查询能力，沿用 ArticleLookupMatch 的完整 body 和独立 ArticleLookupProvenance。完整正文与 chunk 检索必须分别命名、序列化和验收。
- route outcome 为不可变请求结果，不依赖共享 last_status；条数有界，任何 required pair 遗漏不得充分。超过 16 个唯一明确 pair 返回空结果的 `too_many_references` clarification，不截断后冒充 found，小问题也不能伪造 overflow。

### W3: 不含购物特判的召回与组件消融

- 新版本通用 lexical 候选不含购物对象/渠道/退货词表或 anchor。legacy-v1 当前继续作为兼容默认排名，local-lexical-v2 保留为显式历史/对照候选，不继续追补场景词；generic-v3 初始 opt-in。保留旧排名不意味着保留现代证据检查中的错误配对或场景硬闸门。
- 分别比较连续 token 边界、query 去重、场景扩展和 evidence 策略。旧 canonical 评分保持原口径，新 typed-pair 审计单独标识，不能混算。
- 复用既有 dense/RRF/rerank 为允许名单内候选；没有合法本地模型或新调用授权时不下载、不调用，不把 fake 分数当模型收益。
- 默认排名推广需要预冻结阈值、独立法律审核数据及回退审查。当前新工程合同可以落实，排名候选不因开发集净增两题自动成为默认。

### W4: 绑定完整可见输出的语义协议

- 建立 typed SemanticAssessment/Checker，绑定实际 draft、完整可见正文、引用 evidence payload、scope/boundary、checker revision/prompt/config hash。
- 程序产生确定的可见输出 segment IDs，要求全部覆盖；不能只核验模型自行挑选的 claims。理由/limitations 等可见自由文本也属于绑定输入，不靠自动补引用修正旧草稿。
- 状态分为 not_checked/uncertain/supported/unsupported/error。无 checker 不造 supported；required checker 的未知、错误、输入漂移、遗漏或 unsupported 都不能整体通过。
- 保留 structural_passed 与 semantic status 及 required gate 的区别。fallback 不能改写原草稿失败。
- required checker 的实体蕴含门禁适用于 evidence_answer。原始可见正文及所有可见自由字段全部绑定；模型不能自行通过改 mode 免检，因为模式仍由证据/风险合同决定。程序化、无实体法律断言的 limited/refusal/clarification fallback 按新模式结构验证，可发布且 semantic not_checked/not_applicable，原草稿 required 失败必须保留，不把 fallback 当作语义修复。
- checker adapter 可真实接线，但本轮只用可信 fake 测试，无新模型请求。模型 checker 的判断不是权威法律审核。

### W5: 冻结服务配置与真实安全接线

- CLI/API/batch 的配置必须对应实际策略。API 仅开放服务端允许名单内安全 selector，不允许 body 提交 Key、provider URL、DSN、scope 或任意模型。
- run 创建时持久冻结执行策略及 hash。幂等 key/body 重放返回原 run 的原策略，不因部署配置改变而重新解释。恢复校验策略、graph 和 boundary。
- 旧 run 保留旧 provider-disabled 身份，不能自动获得新 paid 权限。lexical profile 与 embedding profile ID 分开。
- 受控 generation policy 默认 disabled，明确 provider/model/destination、环境变量凭证引用、外发 scope、价格确认、prompt/input/output 上限、调用和费用上限。可信 fake 能验证接线，真实 adapter 不默认调用。

### W6: 持久金额预留与未知结果

- 新 append-only Alembic migration，不编辑已发布 migrations。服务 ledger 使用 Decimal/Numeric 或整数最小货币单位，冻结价格政策，不用 float。
- 最终 prompt preflight 在派发前完成；调用次数、金额预留和 attempt 在同事务持久化，校验 lease owner/epoch/deadline。旧 worker 不得预留、结算或退款。
- 成功按完整已知 usage 记录估算费用；timeout、未知用量、模型/价格身份不一致保留全额预留，不写零、不自动退款，paid 内部 unknown 路径不重试。
- SDK 隐藏重试为零，controlled provider error 传递到 harness；语义模型请求同样计次数与金额，不绕过 ledger。
- generator 与 checker 使用同一 per-run governed invoker 的操作视图，checker operation identity 绑定 draft/evidence/checker；harness 与 wrapper 只能有一个预留所有者，不能双扣。已知 usage 超出预留时记录真实估算与 overrun，不改零、不用数据库约束隐藏超额。
- per-run 政策不是账户全局预算，也不是供应商最终账单硬保证。实际账单/全局配额仍须供应商控制，限制必须在报告中保留。

### W7: 法律 gold 和独立评测治理

- 提供 strict UTF-8 审核包与准入机制，默认 pending、human_review_complete=false、holdout_admitted=false。不伪造 reviewer、法律审核或现行性。
- 正式 governed protocol 与 legacy registry v1 分开版本化；旧工件按旧评分规则解码。新 typed targets 不悄悄替代旧 canonical metrics。
- 准入验证绑定 dataset、可信审核凭证、corpus、许可、跨开发池近重复报告、冻结协议与候选代码身份。哈希不是审核签名；自填 human=true 不可信。
- 未曝光 holdout 由独立 curator 准备，不在公开仓库提交题目/gold。先冻结指标、分层、阈值、预算和候选，再原子 reserve，一次协议可包含 paired A/B。
- 暴露身份按 dataset/content fingerprint 锁定，不按 protocol/run ID。runner 读取密封问题前先原子 reserve；开题、失败、中断都记录 consumed/exposed，不能换 protocol/run ID 恢复未曝光身份；并发只准入一次。读过失败再调参后降为 regression。
- trusted receipt 必须按显式 trust policy 验证真实 MAC/签名，production policy 不接受 test_only signer。密码学只证明持钥者签收，不能证明真人资质或法律真值。runtime inference input 仅含问题与运行事实，gold 只在完成后进入 sidecar scorer。
- 本轮实现机制，不伪称实际已有独立法律审核 holdout；真实准入缺证据时必须拒绝。这是质量证据限制，不是省略治理执行机制的理由。

### W8: 序列化、缓存、文档和验收

- 新解析/证据/语义模块进入实际使用阶段的 implementation fingerprint。生成/验证缓存绑定 draft/evidence/scope/完整 segment/checker 身份，改变 checker 不无端重算 chunking。
- strict artifact schema 明确升级与旧版本分派。旧 artifact 仅按旧 rules 解码用于历史查看，不能将旧 structural-only passed、裸 supported 当作新 checker 评估，新执行/恢复必须验证新输入与策略身份。旧原始工件保持不变。
- 更新 README、验收与 HANDOFF/STATE 的本次独立节点，保留旧历史及 M7 not_started。报告每项真实命令、失败、未运行项、费用、commit/PR/Tag/Release 状态。

## 验证与代价

| ID | 必需证据 | 当前状态 |
| --- | --- | --- |
| G01 | 多法明确/歧义、条号数字归一化、交换归属、过滤后重算、无 gold 推理输入 | 工程合同通过；历史/qualifier、嵌套目录及未知外层身份反例已修复并纳入最终全量回归。有界 grammar 仍可能保守澄清，不证明法律质量。 |
| G02 | exact 三态、版本/日期、active pointer 改变、split/mixed/full article、未命中无 fuzzy fallback | fake 与真实 PG 合同通过；统一 150 项含 26 项新服务契约及 124 项既有数据库测试，完整法条与原 chunk 身份分开。 |
| G03 | 场景词不存在、组件消融、AB/BA 隔离、错误与 NA 分母、旧指标/历史 decoder 兼容 | 完整八臂消融 `offline_components_20261005_repaired` 已完成：120 题，108 检索 gold + 12 拒答，paired_valid=108、unknown=0、实际 provider calls=0，代码/输入/数据身份稳定。仍是开发回归集，legacy-v1 默认不推广。 |
| G04 | 全可见输出覆盖、所有语义状态、draft/evidence/scope/checker 漂移、required fail-closed | 绑定、漂移、错误和 required 安全失败合同通过；真实 PG 保留 rejected draft 判定并发布有限 fallback。可信 fake 只证明接线，真实 checker 尚未校准。 |
| G05 | API selector 真接线、default-off、禁止 body 解锁模型、幂等冻结、resume 配置漂移 | 实际服务与真实 PG/default-off 合同通过；幂等、冻结策略、恢复及 observer/helper 纳入最终统一回归，默认生成仍关闭。 |
| G06 | 预留前零 dispatch、Decimal、金额不足、unknown 无退款/重试、并发与旧 epoch、checker 计账 | Decimal/unknown/并发/stale epoch/shared checker 真实 PG 合同通过；追加0008并保留原迁移。无真实付费调用，不是账户预算或供应商账单硬保证。 |
| G07 | strict pending 审核包、伪凭证拒绝、cross-pool duplicate、单协议暴露原子性、不可复用 | W7/legacy 专项及最终全量回归通过；审核包仍 pending、human_review_complete=false、holdout_admitted=false。没有真实法律审核或独立 holdout，不准入。 |
| G08 | 旧 M1-M6 回归、缓存方向性、schema migration、safe SSE 和隐私静态审查 | 1752 tests + 157 subtests、150 项 PG、实际隔离 PG 重启及安装 wheel 均通过；明确历史解码和旧发布证据不变。首轮结果文档的链接/STATE/秘密静态门禁3/3通过；结果回写后只重验同组静态，Linux broker not_run。 |
| G09 | 独立最终代码复核和累计离线/隔离 PostgreSQL 适用门禁 | 独立反例复核与修复后累计 M2 offline 25/25、全量 pytest、统一 PG、物理重启、wheel 及首轮静态3/3通过；不借历史绿灯，27 critical文件与最终消融身份相同。root 对本次结果回写后重验同组静态，尚不是 ready_for_release。 |

每项状态以 [当前独立验收记录](../../../reports/refactor/GENERAL_EVIDENCE_IMPROVEMENTS.md) 的真实命令和轮次为准，专项数字重叠不能相加；上表不是 ready_for_release 判断。

### 重新冻结后的最终本地运行时证据

以下是修复后同一冻结代码候选的独立完整运行，不能将彼此重叠的测试数量相加：

- 全量 pytest：1752 passed + 157 subtests passed /160.34s；JUnit 1909 tests、0 failures、0 errors、0 skipped，XML time=160.333s。证据 `.tmp/general-repaired-candidate-full-pytest.xml` 与对应 log。
- 累计 M2 offline：25/25 mandatory passed、exit0、217074ms；证据 `.tmp/general-repaired-candidate-m2-final.json`。
- 真实隔离 PostgreSQL：150 passed /112.53s，JUnit 150/0/0/0；证据 `.tmp/general-pg-repaired-candidate-final.xml` 与 `.tmp/general-pg-repaired-candidate-identity.json`。前后 HEAD/branch 及 scoped code diff SHA256 `77cd924a7d401120f331ab671fab55b7d3d6171facff0e12826c8176fca77994` 相同，仅自建随机 loopback cluster 被启动和停止，共享5432未触及。
- 物理 PostgreSQL 重启：26 项服务契约 /35.66s 通过后，对自建 data 目录实际 stop/start，新 Python 进程验证 snapshot/catalog/exact/ANN/activation 与 `0008_execution_money` 一致；最后 cluster 已停止。证据 `.tmp/general-pg-repaired-restart-final.xml` 及独立集群 prepare/stop/start/verify logs。此结果不证明 provider exactly-once。
- 实际 wheel 构建、仓库外安装 smoke：23 个 installed runtime 模块、migration head `0008_execution_money`、source_checkout_isolated=true，全部通过；证据 `.tmp/general-repaired-candidate-wheel-probe.json`。本地 dirty 候选不是重新发布的0.7.1。
- 完整八臂离线消融：`offline_components_20261005_repaired` status=completed，528815.737ms；120 题、108 检索 gold、12 拒答，paired_valid=108、unknown=0、actual_provider_calls=0，code/input/dataset_stable 均 true。旧 canonical 指标与现代 typed pair 审计分开；效果小数与回退分层见验收报告，不据此推广默认或声称独立泛化。

首次完整冻结的 M2 24/25 及 3 个公共 validator fixture 失败、首次消融 1920 次执行失败均保留，不改写成成功。现代 schema3 fixture 漏 semantic usage role 的修复没有放宽生产严格校验，明确 schema2 历史纯 view 合同不变。合法嵌套标题与未知外层身份修复发生在解析器/实际服务合同；严格拒答评分只对真实风险拒答的空证据做显式 sidecar 投影，不伪造支持或改变原 RetrievedTurn。未改排名参数、题目或 gold；首次失败的 provider calls 仍为 unknown/null，不因重跑为0而回写旧结果。

首轮结果文档静态门禁已实际3/3 passed、exit0，UTC `2026-10-04T20:06:31Z`，证据 `.tmp/general-improvements-static-first.json`；检查43份 Markdown、2份 JSON及305份 candidate 文本，未发现高置信秘密形态。结果文档编辑前已按原算法复核 scoped code identity，仍为 `77cd924a7d401120f331ab671fab55b7d3d6171facff0e12826c8176fca77994`；该 scope 不含 docs/reports。root 将对这些结果回写后再做 final static 复核，本记录不提前声称该后续轮次通过。本轮新真实模型调用0、新增模型费用0。legacy-v1 仍为默认，generic-v3 仍为 opt-in。权威法律审核、语料现行性、独立 holdout、live checker 校准、第三 paid Smoke、真实账户账单、真实 Linux Redis/prefork broker 和生产容量均未验证。本轮不读取 Key/旧 raw、不上传语料、不做远端写，也没有新 commit/PR/Tag/Release。不能通过全部拒答缩小功能来伪称准确率提升。

## 回退和替换条件

保留旧版本工件与 opt-in 历史复现，数据库只增加迁移；现代 required 安全检查失败关闭，不能回退到已知错误配对或把 unknown 改成成功。停用 paid policy 不删除金额账本；停接新任务不清空持久卷。候选排名回退不等于取消来源/配对正确性保护。

若复核发现接口身份不一致、scope 漂移、无可信金额/评审边界、历史 decoder 不兼容或组件收益不可归因，修正方案后再实施，不以调整断言或改写旧报告让门禁通过。

## 来源

- [项目执行规则](../AGENT_EXECUTION.md)、[主方案](../MASTER_PLAN.md)第 6、10、11、13、14 节。
- [ADR-005](ADR-005-local-lexical-retrieval.md)、[上轮本地实验](../../../reports/refactor/LOCAL_LEXICAL_RETRIEVAL.md)、[真实修复轮](../../../reports/refactor/LIVE_SMOKE_QWEN35B_REPAIR.md)。
- 当前工作区的 query/evidence/verifier/chat、storage catalog/retrieval、service factory/run service、harness、experiment registry/scoring 的独立复核、零调用反例与最终本地运行时记录。设计、工程验证、法律质量、生产能力和发布状态分别记录。
