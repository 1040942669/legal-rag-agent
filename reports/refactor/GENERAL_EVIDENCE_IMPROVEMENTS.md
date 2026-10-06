# 通用证据合同与受控接线改善验收记录

> 当前状态：W1 至 W8 本地实施、所列适用运行验证与已观察的结果文档静态3/3完成，运行时代码身份未变；法律质量未验收，不是新软件发布或发布回执。各轮失败保留，专项通过不替代最终累计门禁。M6 既有发布保持 released，M7 not_started。

## 版本和范围

- 范围：用户要求针对规则过拟合、引用与证据判定、语义校验、实际服务接线和评测可信度完成方案设计、独立复核、选择与实施。采用 [ADR-006](../../docs/refactor/decisions/ADR-006-general-evidence-first-improvements.md) 的 W1 至 W8，不能用只写审核包代替完整链路。
- 分支：`codex/general-rag-improvements`；基线和当前 HEAD 都是 `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`，dirty=true。该 HEAD 是既有提交，不是新候选 commit；已有 Smoke/lexical 未提交改动全部保留。
- 权限：本次改善只在本地实施及验证，不发新模型请求，不读 Key、旧原始模型回答或私人资料，不上传语料，不 commit/push/PR/合并/Tag/Release，不进入 M7。
- 不改变：旧法条数据与题标、旧 canonical 评分、两次 paid Smoke 的 manifest/summary/账本、旧 A/B 的结果、已发布迁移、M5/M6 Tag 与 Release。新迁移仅追加 `0008_execution_money`。
- 当前默认：排名仍为 `legacy-v1`。`local-lexical-v2` 只保留显式历史/对照，`generic-v3` 初始 opt-in；现代机械证据是 `general-reference-v2`，不是旧购物词面闸门。新语义状态与旧 structural-only passed 分开。

## 问题来源和过拟合边界

电商题实际上是仓库原有消费者权益法律回归题，不是新增电商模块。旧 v3 的 120 题反复用于开发，其中 108 条具有检索 gold、12 条单独验拒答；固定 live 9 题含其中 3 条购物/退货场景。题标由仓库维护，并没有可核验的独立法律 reviewer 或未曝光 holdout，不能称权威法律质量测试。

上一轮 `local-lexical-v2` 同时引入连续汉字边界、query 去重、购物扩展与退货词面必要性检查。唯一旧 A/B 命中数为 76/108 至 78/108，4 改善、2 回退，Hit@5 配对增量区间跨零。这是开发集局部收益，不证明泛化，更不能推断生成答案正确。旧实验原始身份和负结果见 [原报告](LOCAL_LEXICAL_RETRIEVAL.md)，本报告不覆盖它。

当前不继续加购物关键词或 case ID 特判。修复对象是通用合同：原文引用关系、数字合法性、唯一精确键、来源边界、完整可见输出绑定、费用授权、历史版本解码与评测暴露控制。测试覆盖合成甲/乙法律、交换条号、否定/转述/未知指代、无关引用及并发恢复；合成合同不充当真实法律 holdout。解析器仍然是有界规则系统，不是完整自然语言理解；不支持的表达保留 unresolved 并要求澄清，存在保守误澄清的代价。

## 实际处理链与代码责任

已知保守边界必须保留：历史时间标记加未知指代仍可能整体要求澄清，例如“此前依据甲法第十条；请解释该规定现行内容”，或历史甲引用后仅当前解释乙时，历史 unresolved 可令整问澄清。没有再为逐句通过扩展意图词表；这是 bounded grammar 的误挡代价，不是法律质量改善证明。现代 condense 对当前任何明确引用、提及、排除或未知引用保留原文，历史只作独立上下文，不得拼接新增旧 pair。

| 环节 | 具体模块 | 处理与失败机制 |
| --- | --- | --- |
| 输入与授权 | `api/schemas.py`、`services/run_service.py` | API 只开放 top_k 与服务端允许的 lexical selector，不允许 body 提交凭证、provider URL、DSN、scope 或任意模型。认证与会话 owner/scope/profile 冻结到 run；幂等重放沿用原策略。 |
| 执行策略 | `services/execution_policy.py`、`services/service_retrieval.py` | 默认 generation disabled，允许外发的 scope、目的地、模型、价格、输入/输出限制、预算来自服务端。旧 run 不自动获得新付费身份；从已冻结 snapshot/profile 载入 corpus，不追随后来的 active pointer。 |
| 问题与引用 | `chat.py`、`legal_references.py` | 风险路由在检索前；原文 span 区分提及、否定、历史转述与实际请求，形成 `(law, article)` 需求。未知法名、条号或关系不猜测；显式引用绕过模糊归一化和 planner。 |
| 检索 | `services/exact_retrieval.py`、`retrieval.py` | PostgreSQL-bound 路由对明确引用使用 catalog，区分 found/not_found/needs_disambiguation，未命中不改走 BM25 邻条冒充成功。无明确引用沿用允许的 BM25 或显式实验候选。离线未绑定 catalog 的 BM25 不冒充数据库精确查询。 |
| 原始证据 | `storage/retrieval.py`、`reference_evidence.py` | catalog membership 与冻结 corpus/profile 交集后返回原授权 chunk 与 typed provenance；完整法条正文由独立 API 返回，不伪造 chunk/payload hash。top_k、权限过滤之后逐 pair 重算，A5+B10 不满足 A10+B5。 |
| 机械判断 | `evidence.py`、`chat.py` | 候选可用、有限分数、范围有效、明确引用覆盖分开；scope 未配置不是 valid。阶段转换重算机械判定，不能同时改写结果副本后沿用旧 sufficient。机械充分不等于 semantic supported。 |
| 生成 | `chat.py`、`llm.py` | insufficient/clarification/refusal 可以程序化返回；生成前只提供允许的证据和有界历史。正文引用、claims、模式、免责声明保持结构合同，不靠事后补引用让旧草稿通过。 |
| 语义评估 | `semantic.py`、`verifier.py` | 程序切分完整可见正文、limitations 与 clarification；只排除精确服务端免责声明。绑定 question/draft/evidence/scope/checker 身份。required 模式下 missing/unknown/unsupported/error/漂移/覆盖遗漏失败关闭；未配置不造 supported。 |
| 安全发布 | `services/run_executor.py`、`harness/nodes.py` | verify 与 commit 不重新调用 checker。失败草稿保存独立判定，安全 fallback 可通过其有限模式的结构验证，但不能算草稿语义修复。服务公开只含已验证输出与允许元数据，SSE 不泄露 prompt/raw/未验证 draft。 |
| 持久执行与费用 | `harness/runner.py`、`services/governed_calls.py`、`run_service.py` | generation/checker 共用一个预留所有者；派发前以同事务记录次数、Decimal 金额预留及 attempt，检查 lease/epoch/deadline。unknown 保留预留且不自动重试；已知超额真实记录 overrun。per-run 估算不是账户总预算或供应商账单硬上限。 |
| 实验与缓存 | `experiment_adapter.py`、`experiment_runtime.py`、`experiment_runner.py`、`chat_artifacts.py` | 新语义执行使用显式版本和 semantic 角色；生成缓存含绑定 assessment，verify/commit 纯校验。改变 checker 只失效相关阶段，不无端重算分块。旧工件可历史查看，但不能获得新执行授权。 |
| 评测准入 | `evaluation_governance.py`、`governed_protocol.py` | 默认 pending；正式准入验证外部可信签收、数据/语料/代码/协议身份与近重复。密封题目打开前原子锁定 exposure，失败或中断也消费，不能换 run/protocol 再冒充未曝光。推理只见问题，全部完成后才读取 gold 评分。 |

M6 job/outbox/Redis/Celery 仍负责已注册的批评测和预构建导入，不把在线 graph 问答偷偷入队。Redis 不是会话真相源；持久状态、预算和 owner 边界在 PostgreSQL。checkpoint 不承诺任意外部调用 exactly once。

## 最终候选结果及保留的失败

### 最终运行验证（以下历史专项不能替代本表）

所有源代码、测试与配置在此轮验证期间冻结，未发模型请求或做远端写入。完成后仅回写结果文档；文档回写改变全量tracked diff，不能把新的文档身份说成实验时的旧全量身份。文档后已复核原代码scope hash相同，27个critical文件与最终消融manifest逐一相同。

| 检查 | 实际结果 | 本地证据 |
| --- | --- | --- |
| 完整离线pytest | 1752 passed +157 subtests，160.34s，exit0；JUnit1909/0/0/0 | `.tmp/general-repaired-candidate-full-pytest.xml`与同名log |
| 累计M2 offline | mandatory25/25 passed、exit0、217074ms，UTC2026-10-04T19:47:34Z至19:51:13Z；其独立执行的fullpytest1752+157，161.50s、JUnit1909/0/0/0 | `.tmp/general-repaired-candidate-m2-final.json`，SHA256 `982d36fe8c0b866ab20ff2cb7d2389e535c8700444c57c10d0cd11d1041b98fe` |
| 实际PG统一回归 | 150 passed、112.53s、exit0、JUnit150/0/0/0；26新契约+124旧数据库例 | `.tmp/general-pg-repaired-candidate-final.xml`、`.tmp/general-pg-repaired-candidate-identity.json` |
| 实际PG物理重启 | 26 passed/35.66s后，唯一自建cluster真实stop/start，新Python进程验证head0008及M3持久检索状态；exit0，最终cluster已停止 | `.tmp/general-pg-repaired-restart-final.xml`；`.tmp/isolated-pg-86b747ec941a4b97b0f88223c659d569/`的prepare/verify/stop/start日志 |
| 构建及仓库外安装wheel | build与probe均exit0；23模块、实际head0008、source_checkout_isolated=true；513137bytes | `.tmp/general-repaired-candidate-wheel-probe.json`；wheel SHA256 `c7d348644a51a7dc258d7e4d6bdff8a2e4306eff529c358101f5a7cd6832b667` |
| 固定八臂消融 | `offline_components_20261005_repaired` completed，Python/shell exit0；1920执行均成功、960成功重复对一致；120题、108有效配对、unknown0、每臂拒答12/12、provider calls0；528815.737ms；UTC19:48:35Z至19:57:24Z，code/input/dataset stable=true | 新manifest/summary，SHA256 `6c721534e0e897bc5df2f53a1a6d237ab5874e319a53b2f295dcbb38ef0373b9` / `3b0a27e079ce8b0db132c459aebbda338108fa07d16a61892c7612d2d0c5c25d` |
| 结果文档静态/代码身份 | 实际3/3 passed、exit0，UTC2026-10-04T20:06:31Z；43 Markdown/2 JSON/305 candidate文本无高置信秘密形态；27 critical文件未变 | `.tmp/general-improvements-static-first.json`；结果回写后重验同组三项，不替代运行时门禁 |

PG源码scope覆盖legal_rag、integration_tests、tests、scripts、configs、pyproject、workflows，含46个untracked code文件，不含docs/reports/ignored产物；前后hash `77cd924a7d401120f331ab671fab55b7d3d6171facff0e12826c8176fca77994`相同。AB的全量dirtydiff身份为 `6b763cf1e2de5860a4f5051248a51d81332765ac561315f50e754bbe6e2eb2d4`，基线HEAD仍4d9546e；两种不同scope算法不可直接比较。wheel是本地dirty0.7.1兼容候选，不是替换现有Release。同一组pytest在direct/gate中重跑、26服务件与150PG件重叠，不相加制造独立样本数。

### 消融效果与明确不推广

`c`是连续token边界，`d`是query去重，`s`是仅用于历史诊断的购物扩展。每臂质量分母均108，顺序重复只检查一致性，不把质量分母翻倍；旧canonical指标和新typed query-reference ownership sidecar分开。

| c/d/s | Hit@5计数 | MRR | target coverage | gold未命中但typed机械充分 |
| --- | --- | --- | --- | --- |
| 0/0/0（legacy对应） | 76/108 | 0.610184 | 0.660494 | 29 |
| 0/0/1 | 77/108 | 0.619444 | 0.669753 | 28 |
| 0/1/0 | 77/108 | 0.608794 | 0.669753 | 28 |
| 0/1/1 | 78/108 | 0.618054 | 0.679012 | 27 |
| 1/0/0 | 76/108 | 0.611728 | 0.660494 | 29 |
| 1/0/1 | 77/108 | 0.620987 | 0.669753 | 28 |
| 1/1/0（generic-v3对应） | 77/108 | 0.614968 | 0.669753 | 28 |
| 1/1/1（历史v2对应） | 78/108 | 0.624227 | 0.679012 | 27 |

四个控制cell中，连续token对Hit@5差值均0、CI[0,0]；去重差值均1/108、95%CI[-0.0278,0.0463]；历史场景扩展差值均1/108、CI[0,0.0278]包含0。不是独立样本的四倍证据，且这是反复开发的非权威审核集。generic-v3只净增1题，不足以推广默认。每臂flat/typed机械充分都是97/108，generic仍有28条gold未命中而typed机械充分；这直接说明机械检查不是实体法律蕴含门禁。sidecar既不是完整旧购物guard，也不是合法律真值或语义正确率。

generic对baseline为3改善、2退步、74命中不变、29未命中不变；每臂还有8条gold命中但typed机械不足。12条拒答的四个检索指标为NA，双遍共768个指标NA单元，不把它们写零分或加入检索质量分母。当前默认保持legacy-v1，generic-v3 opt-in，不再给现代路径加购物词表/题号特判。实际法律审核、独立holdout、语料现行性、checker实测校准和生产容量仍未证明。

保守误挡也真实存在：`《合成甲法》是什么意思？` 当前会unresolved；temporal/anaphoric历史指代也可能过度澄清。没有为逐句通过继续扩词。此轮核心只保证所列bounded grammar与身份合同，不是通用自然问法能力证明。

### 首次完整冻结候选的真实结果

本节记录第一次完整候选，而不是以先前专项通过代替验收。候选运行期间源码身份保持不变；后续版本 fixture 修复和结果文档回写不属于这个旧身份。

- 累计 M2 offline：`.tmp/general-frozen-candidate-m2-first.json`，UTC `2026-10-04T19:17:07Z` 至 `19:20:42Z`，213939ms，exit1，25项中24 passed、1 failed。失败项为全量 pytest 的三个公共 runner validator 测试；该轮完整 stdout/临时 JUnit 未保留，其他通过总分母不能推算。
- 根因：公共测试 fixture 标记现代 runner schema3、使用现代 call kinds，却只构造旧三角色 usage。生产严格校验正确，不应放宽。修复 fixture 并增加现代缺 semantic 必须拒绝且输入不变的反例后，公共 validator 与完整 bound semantic runtime 25 passed /6.86s；`.tmp/general-runner-public-validator-red.xml` 的3 failed/1 passed与`.tmp/general-runner-public-validator-final.xml`均保留。明确 schema2 的历史纯 view 兼容测试不变。
- 真实 PG 冻结回归：149 passed /109.21s，tests=149/failures=0/errors=0/skipped=0；`.tmp/general-pg-frozen-candidate-final.xml`与`.tmp/general-pg-frozen-candidate-identity.json`。25新契约加124旧数据库例，前后代码diff SHA256 `d805757cffd25bfa3b1cef215005be9c95c67848a25c51546a7234318295bfa8`相等，唯一自建cluster已停止。Linux broker suite未运行。
- 实际候选wheel构建与仓库外安装smoke通过，`.tmp/general-frozen-candidate-wheel-probe.json`；wheel SHA256 `d6ba4b7fa6388807c1b0d05a3dfe2698f0e1dfe6aa28e5376e66254a77615f5f`，23模块导入、实际head0008、source_checkout_isolated=true。仍为本地dirty 0.7.1候选，不是重新发布既有版本。
- 八臂消融 `offline_components_20261005_first` status=failed，外层shell exit1；源码main失败分支返回2，但此轮未独立打印/保存Python `$LASTEXITCODE`，不将推断值冒充实测。495094.77ms，UTC `2026-10-04T19:17:32Z` 至 `19:25:47Z`；固定120题、108检索gold、12拒答，120x8x2全部执行失败，paired_valid=0、unknown=108，每臂质量分母0/指标null。公共错误类别为retrieval_execution_failed，actual_provider_calls=null/unknown，不报质量0或已确认调用0；相同失败的repeat_consistent不算成功AB/BA一致性。code/input/dataset_stable均true。manifest/summary SHA256分别`9ae41f1e648994eec29ceae275b25db637af0956db41d06f5b7ab593178911ba`、`94cb6d72f8961c0a61a03758181b42717ed574fae4eb435d817c401ab541df9e`；保留此失败，诊断后新身份复测，不能覆盖。

首次失败后仅修通用身份与真实评分接线，不调rank参数、不删案例、不改gold：

- 合法文件标题内部引用其他法律，目录406 hints中1条内嵌书名号会令parser抛异常。独立合成复现证明普通BM25被误降澄清，数据库factory在exact router初始化即失败，不只是bench sidecar。完整标题采用有界平衡括号扫描，保持外文件和内法律两个身份，完整known alias覆盖内引用，完整span内部条号不参与外部需求；不平衡/过长/过深明确unknown，malformed catalog仍严格拒绝，未过滤目录条目。
- 初始parser153例18 failed/135 passed、router5 failed、真实PG1 failed均保留。首次平衡修复后quoted问句回归1 failed也保留；两条未知外文件续名加远端请求和一条无条号未知subtitle随后各真实RED。最终用一个相邻标题边界，禁止远端intent洗白未知续名，不按是否有article特判，不扩意图词表。最终9文件331 passed /6.74s，`.tmp/general-reference-nested-boundary-final-green.xml`；真实typed PG import/frozen factory/full article/inner ownership正例1 passed /3.10s，`.tmp/nested-catalog-service-pg-green.xml`。
- 脚本实际风险拒答返回evidence=None，与strict scorer的必需EvidenceCheck不兼容。仅实际拒答、空results、实际riskflags、out_of_scope terminal允许评分投影，计算empty实际证据sufficient=False，并保留terminal/risk/projection原因。原RetrievedTurn不改变，非拒答或不一致stage继续失败/unknown。初始3 failed/17 passed RED与后续scope证据均保留；没有造支持或回答，也没有放宽scorer。
- 修复后重新冻结全部候选，再以全新run执行完整pytest/M2门禁、150件PG、物理PG重启、wheel和固定八臂；在这些新运行完成前不宣称最终通过。先前149和新增1不是同候选150的替代证明。

专项 pytest 以 `.venv\Scripts\python.exe -B -m pytest -q` 执行；累计门禁使用 `uv run --offline --frozen --no-sync pytest`。显式 `LEGAL_RAG_DISABLE_DOTENV=true`、`ALLOW_LIVE_MODEL_CALLS=false`。下列轮次是历史专项快照，重叠，不能相加成独立总样本或代替最终完整候选。JUnit 均在 ignored `.tmp`，文件名含 green 不代表实际结果一定 passed。

| 检查 | 实际选择器/环境 | 结果与证据 |
| --- | --- | --- |
| 精确路由 RED | `tests/test_reference_route_chat.py`，trusted fake | 10 failed；`.tmp/general-reference-route-chat-red.xml`。确认状态丢失、模糊回退和错误充分性后修复。 |
| 路由、历史执行、机械重算 | 上述文件及 M1 verifier、M2 artifacts/stages、M3 bound retrieval 与绑定语义文件 | 最后一轮对应重算修复 200 passed + 89 subtests / 1.42s；`.tmp/general-reference-mechanical-rebinding-green.xml`。早期 2 failed 的身份重绑定 RED 保留。 |
| typed provenance 日期 | `tests/test_bound_semantic_protocol.py` 与 bound chat、governed service calls、execution policy | RED 1 failed + 22 passed，实际 date JSON TypeError；沿用已有 strict typed codec 后 GREEN 48 passed / 0.51s，`.tmp/general-semantic-bound-date-red.xml`、`...-green.xml`。没有 default=str 放宽未知对象。 |
| 执行版本与非模型字段 | bound semantic chat/protocol、reference route、M1/M2/M3 上述核心组合 | RED 2 failed + 10 passed；GREEN 183 passed + 89 subtests / 1.34s，`.tmp/general-semantic-execution-contract-green.xml`。新旧规则不能混执行，程序化输出不得携带假 assessment。 |
| W7 与 legacy 兼容 | governance/protocol、旧 registry/scoring/artifacts | 144 passed / 9.00s、exit 0；`.tmp/general-governance-legacy-final.xml`。安全 CLI 子进程另 2 passed / 1.30s，`.tmp/general-governance-cli-safe-run.xml`。只证明准入合同，不代表实际法律审核完成。 |
| W1/W3 初步专项 | parser/reference/evidence/lexical/ablation/scoring 等组件组合 | 288 passed + 4 subtests / 1.51s；`.tmp/general-reference-components-scoped-final.xml`。随后独立复核又发现 bare alias、非法数字和否定关系反例，新的 RED 13 failed + 85 passed 保留；该旧 GREEN 不覆盖后续 parser 修复。 |
| W5/W6 实际 PG | 自建随机 loopback PostgreSQL 18.1/pgvector 0.8.1 cluster，合成数据；`integration_tests/test_general_service_execution.py` 和适用旧 M3/M4/M5/M6 数据库文件 | 首轮新14件 13 passed/1 failed，日期修复后通过；旧124件 122 passed/2 failed 是 current-head 断言仍固定0007。扩展统一145件首次137 passed/8 failed，暴露保存白名单丢失与测试库隔离问题；修复后 fresh 145 passed /100.84s，`.tmp/general-pg-final-isolated.xml`。随后24件专项29.81s通过，含 checker观察与default-off拒外部planner；新增真实DB head helper的统一149件仍待运行。日志与 RED 保留，不把中间失败文件当绿色。 |
| M2 新语义接线 | `tests/test_m2_bound_semantic_runtime.py` 与旧 M2 六文件组合 | 初始 RED 9 failed；下层权限控制 RED 2 failed+12 passed，strict decoder漂移RED2项均保留。最终134 passed /90.58s、exit0，`.tmp/m2-bound-semantic-scoped-final.xml`。新20项fake合同覆盖独立预算、provider失败、缓存与身份漂移；旧 authentic provider-free 历史工件另行复核中。 |
| 新候选 wheel 合同 | `tests/test_release_wheel_probe.py` | 显式0008参数与 runtime 包要求 RED4 failed；GREEN24 passed /0.58s、exit0，`.tmp/general-wheel-contract-first-green.xml`。默认旧0007合同保留，实际候选构建/仓库外安装 smoke 尚未运行。 |
| 当前与历史迁移门禁 | `tests/test_quality_gate.py tests/test_m6_quality_gate.py tests/test_release_wheel_probe.py` | 新显式migration合同RED3项；GREEN130 passed /1.51s、exit0，`.tmp/general-gate-wheel-migration-first-green.xml`。M6 validator默认历史0007，当前CI从checkout图传显式0008；新worker回执将读取实际DB而非硬编码旧head，真实Linuxworker仍not_run。 |

以上是按时间保留的历史专项，不是当前尚待运行清单；最终适用工程结果以最前面的最终表为准。真实Linux prefork/Redis worker、生产容量、checker校准和第三paid Smoke仍未运行；数据库物理重启本次已有真实证据，不借用已发布M5/M6历史绿灯。

## 评测、审核和金额

### 冻结前最后复核补充

- root 独立反例先真实失败：程序化有限回答可直接改写 limitations 后绕过判断，current request 被 condense 添入旧 pair，supported assessment 可覆盖三种 error。共享运行时/工件合同、原文 current authority 与 error/assessment 一致性修复后，核心7文件191 passed+89 subtests /1.41s，`.tmp/general-core-independent-fixes-final.xml`；独立 reviewer 三文件58 passed /0.37s，`.tmp/general-core-independent-review-final.xml`。没有靠自动补引用或修改 gold。
- 新 checker 实际抛异常会生成全 error decisions，往返工件、safe fallback 和 commit 保留原失败且不追加调用。commit 恶意 mapping 原测试仅将注入时刻移至 verify 之后，原无状态改变的原子性断言不变；verify 现在会更早拒绝同一恶意输入。首轮 root joint 3 failed/321 passed 的字段名与 fixture时序错误保留。
- exact 引用超16 unique pair 两例 RED 后改为有界 clarification：唯一 `too_many_references`、空结果/双方 pair、零 catalog/lexical/model。小问题不能伪装 overflow。最后 route/semantic/gate/wheel8文件208 passed /2.16s，`.tmp/general-overflow-core-gate-final.xml`；服务独立组合49 passed /1.10s，`.tmp/exact-reference-overflow-green.xml`。
- authentic provider-free 历史 M2 四目录共8例曾被新字段 trace 对账误判 corrupt；只对明确旧 schema 的缺省字段做历史 projection，新规则不放松。严格真实 shape RED2 failed/1 passed 后修复，最终7文件137 passed /91.57s，`.tmp/m2-bound-semantic-authentic-scoped-final.xml`。四目录均 complete、各2 succeeded/0 corrupt，所有 JSON 前后 hashes 相等，未读 raw/paid 历史。
- 当前 M5/M6 新测试回执读取实际 DB revision，同SHA不同head拒绝混写；历史发布回执与 validators默认6/7保持不动，当前preflight显式传候选8。真实PG统一149 passed /105.79s，`.tmp/general-pg-producer-sorted-acceptance.xml`。其前147 passed/2 failed的不排序共享fixture污染证据保留，不修改生产恢复/schema断言。
- 独立临时PG额外25件服务契约30.41s通过后，实际对唯一自建data目录 stop/start，由新的Python进程验证 M3 snapshot、catalog、exact/ANN、activation及schema8仍一致；整个脚本exit0，原共享5432与凭证均未触及。ignored根 `.tmp/isolated-pg-d3bfa8dffc1840768d3aea0eb7890988/` 保留 prepare/verify/stop/start logs，最后cluster已停止。此证据不是新 paid 请求重发或供应商 exactly-once 保证。
- 当前候选 `generic-v3` 的 [配置示例](../../configs/generic-v3.yaml) 只设置 retrieval，不开启生成/Judge。只按 freeze 后实际完整门禁和消融结果决定工程验收，不推广默认。

- 新eight-arm消融已经完成，实际效果和错误分母见最前面的最终结果表；现代typed pair审计与旧canonical Hit@5分开，未把运行成功当法律质量提升。
- 已生成120题的 pending 审核包，本地 SHA-256 `d269faf315ebcfaf1f9617bd474a4ea66d24ce274f688539d941b9f7611f9f2e`。`human_review_complete=false`、`holdout_admitted=false`；反复曝光的回归集不能改名为 holdout。未提供真实可信法律审核 gold 或独立未曝光集，正式准入应拒绝。
- 本次新真实模型调用 0，新增模型费用 0。旧两次 paid Smoke 累计7次、8056 total token、估算0.0070528元仍是历史估算，未核验供应商实际账单；未使用其剩余名义额度。
- 不因任何工程通过声称法律准确率提高、语料覆盖现行法律、checker 能等同人工判断或达到生产吞吐。

## 隐私、兼容、发布与下一步

- 原未提交代码、旧原始工件和已发布迁移保留；只追加新版本合同与迁移。旧工件有明确历史解码，不静默提升权限或 supported。
- 结果文档相对链接、STATE/manifests、秘密形态静态首轮实际3/3通过；结果回写后只重验同组三项及diff，不改变运行时代码。静态扫描不是完整隐私审计。候选文档仅含代码位置、合成行为、摘要和hashes，不复制法条语料、raw draft、Key或私人内容。
- 本次 commit/push/PR/Tag/Release 都没有产生；远端 CI not_run。HEAD 本身的旧 Release/CI 不是 dirty 改善候选证据。
- 回退排名选择 legacy-v1；停用 paid 策略不删账本。含已冻结策略或金额记录的0008库拒绝无损假设的降级，优先前向修复；不删除持久数据或 Redis/PG 卷。
- 停止边界：结果回写后只重验同组文档静态并汇报，停止本次本地改善。W1至W8的实现与适用工程验证完成，质量证据不足不伪造证明；若以后做模型质量评估、Linuxworker门禁或发布，需另行安排，不擅自复用旧付费额度或进入M7。
