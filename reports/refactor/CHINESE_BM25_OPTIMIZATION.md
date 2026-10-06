# 中文 BM25 组件替换与规则收缩

日期：2026-10-06。状态：用户追加选择的SmartCN固定对照已完成，新增源码候选8f042fb本地累计门禁通过，远端CI尚未全部完成；默认选型仍待确认。前轮04fd四路CI是历史证据，不替代新候选。本轮不合并发布。

## 范围与研究结论

用户授权研究适合本项目的中文 BM25，先列 [TODO](../../docs/refactor/CHINESE_RETRIEVAL_TODO.md)，再逐项实现并推送。当前分支 `codex/chinese-bm25-optimization`，基线 `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`；前轮未提交工作完整保存在前置提交 `914a7e322d2787ec99a2fe1f1117ad5906244e02`，该提交已推送并经远端 ref 核验。[Draft PR #31](https://github.com/1040942669/legal-rag-agent/pull/31) 尚未 ready/merge。本任务不进入 M7，不创建或移动 Tag/Release。

BM25 是评分与索引内核，中文效果还取决于分析器及应用接线。当前实际安装 BM25S 0.3.9、LlamaIndex BM25 0.7.1。后者 `from_defaults(tokenizer=...)` 只警告、不传入实际流程；断网合成探针中自定义函数被调用0次。默认 tokenizer 把连续中文句子当作一个词，不能据此声称已支持合适的中文检索。

第一组有限候选是直接 BM25S + jieba 0.42.1 精确/搜索模式，使用公开依赖原始词典/HMM，固定 k1=1.5、b=.75、lucene，不使用购物词表、gold、人工40/80加分或废止乘数。适配层仅保留合法数值、原Chunk身份、空/OOV处理和稳定排序，未自写新的打分公式。配置身份包括引擎/分词库版本及词典/HMM资源hash；此身份不替代语料/权限/快照身份。

官方依据：[BM25S](https://github.com/xhluca/bm25s)、[jieba](https://github.com/fxsjy/jieba)、[LlamaIndex实际源码](https://github.com/run-llama/llama_index/blob/main/llama-index-integrations/retrievers/llama-index-retrievers-bm25/llama_index/retrievers/bm25/base.py)。Lucene SmartCN、Elasticsearch/OpenSearch IK 是另有运维与运行时要求的成熟中文方案，未测试不能宣称本语料更好。相关部署约束见 [SmartCN](https://www.elastic.co/docs/reference/elasticsearch/plugins/analysis-smartcn) 和 [IK](https://github.com/infinilabs/analysis-ik)。

## 第一次预冻结比较：保留负结果

协议 [chinese-bm25-fixed-v1](../../configs/chinese-bm25-benchmark.json) 在运行前落盘，不在观察结果后调词典、参数或gold。run ID `chinese_bm25_20261006_fixed_first`，UTC开始 `2026-10-05T17:29:40Z`。使用现有已注册120题与19,050条article/205个公开来源文件；108题有检索gold，12无gold题的排名指标为NA，不是拒答正确数。每臂独立新进程、顺序正反两遍，质量分母不因重复而翻倍。

| 候选 | Hit@5 | MRR@5 | 场景形状分层 Hit@5 | 建库毫秒 | 第二遍查询 p95 毫秒 | 全进程峰值 MiB |
| --- | --- | --- | --- | --- | --- | --- |
| 历史 legacy-v1 | 76/108 | 0.610184 | 67/99 | 1093.78 | 361.16 | 467.82 |
| 历史 generic-v3 | 77/108 | 0.614968 | 68/99 | 1139.03 | 346.17 | 450.53 |
| BM25S + jieba precise | 70/108 | 0.548456 | 61/99 | 5508.24 | 15.38 | 298.06 |
| BM25S + jieba search | 72/108 | 0.555709 | 63/99 | 6230.85 | 15.47 | 372.56 |

四臂子进程均exit0，重复结果一致，所列critical源码/输入身份稳定，实际模型调用0。按预声明规则只在两个现代候选中选择，search胜precise，但不代表胜旧基线。search相对legacy为4题改善、8题回退，Hit@5差异95%配对bootstrap区间 `[-0.1019, 0.0278]`。此结果不支持召回质量提升，暂不据此推广默认；后续成熟分析器对照必须新协议、新run，保留本结果。

本轮是直接词汇排名，不含chat风险处置、精确路由、生成、embedding、reranker或checker。历史/现代同时改变分析器、索引文本及人工加分，不归因为BM25公式单一效果。公式另用相同合成tokens对旧评分与BM25S atire+lucene IDF做1e-12容差合同检查，现代运行仍使用lucene。

资源数据是当前Windows机器的单次建库/两遍查询测量，建库包含懒加载及词典初始化，内存为整个新进程含Python/依赖/语料，不是索引独占内存。单一臂顺序、文件系统缓存及并行负载会影响耗时，不能当作生产容量或无偏性能估计。开发集已重复曝光、gold未独立法律审核，不能称独立holdout。

独立审查指出首版critical列表未包含调用的 `evaluation.py` 和 `retrieval_contracts.py`。两文件在本轮保持Git未改动，没有发现对应数值错误，但 `critical_identity_stable` 只证明清单内依赖；后续协议必须补足并显式传bootstrap参数，不能用该字段声称所有依赖均冻结。

本地忽略目录证据：`artifacts/experiments/chinese_bm25_20261006_fixed_first/`。协议SHA256 `0d05df22d648932c14430c748d23e90a8760c7635e17b65adf2d99c2a170f2d4`；原始index SHA256 `0b705f64dcc4235517516def05bbd7ae46377cb6704181fad85afba14f2b8ec2`；summary SHA256 `67877f492b2e94663df7f6b5d38ddb6aebb4551b525056062f6e284e83c5098d`。raw语料/逐题本地结果不上传。

## 第二次预冻结比较：字符召回与排名的取舍

首轮结果后仅增加两项有公开实现依据的有限候选：scikit-learn `CountVectorizer.build_analyzer()` 的 char(1,1) 与 char(1,2)，其余现代参数不变。没有按回退问题添加词表、改gold或调k1/b。官方依据为 [CountVectorizer API](https://scikit-learn.org/stable/modules/generated/sklearn.feature_extraction.text.CountVectorizer.html) 和 [MTEB当前BM25源码](https://raw.githubusercontent.com/embeddings-benchmark/mteb/main/mteb/models/model_implementations/bm25.py)。本适配沿用alnum特征过滤，不称原样复现MTEB，不把字符特征当语义理解。

独立 [v2协议](../../configs/chinese-bm25-benchmark-v2.json) 预先规定：按Hit@5、MRR@5、第二遍p95选择现代候选；只有Hit@5和MRR@5同时不低于同轮legacy才自动符合推广条件。旧v1协议及输出未覆盖。run `chinese_bm25_20261006_char_fixed_second`，同一120题/108有效排名分母/19,050条article。五臂exit0、两遍排名一致，扩充后的source/input冻结检查通过，0模型调用。

| 候选 | Hit@5 | MRR@5 | 场景形状分层 Hit@5 | 建库毫秒 | 第二遍查询 p95 毫秒 | 全进程峰值 MiB |
| --- | --- | --- | --- | --- | --- | --- |
| 历史 legacy-v1 | 76/108 | 0.610184 | 67/99 | 1117.20 | 389.16 | 467.45 |
| 历史 generic-v3 | 77/108 | 0.614968 | 68/99 | 1095.70 | 341.29 | 450.68 |
| BM25S + jieba search | 72/108 | 0.555709 | 63/99 | 6349.35 | 16.36 | 372.63 |
| BM25S + sklearn char | 83/108 | 0.597065 | 75/99 | 4507.30 | 14.52 | 463.13 |
| BM25S + sklearn char-bigram | 79/108 | 0.595215 | 70/99 | 8905.67 | 14.78 | 791.26 |

char是已测试现代候选中召回最高者，较legacy为9改善/2回退，配对Hit@5差异区间 `[0.0093, 0.1296]`。但MRR@5回退，明确引用形状层为8/9（旧版9/9），**未达到预注册自动推广条件**。不能删除排名回退、改门槛或把重复开发集区间称独立泛化证明。是否接受维护成本/前五条召回与靠前排序的取舍，已向用户单独说明并请求选择；未收到选择前不切默认。没有扩展“不断增加候选直到赢”的搜索。

上一段是第二轮结束时点的选择请求。用户随后明确选择继续SmartCN固定对照，见本文第三轮；该追加选择不是接受char的MRR回退或允许改变默认。

此轮仍是直接词汇排名，不包含精确目录路由。并行开发期只锁定清单内运行依赖，结果由manifest中的精确源码hash绑定，而不是把未提交的整个工作区说成一个干净commit。hash清单补入实际使用的evaluation/retrieval_contracts，bootstrap显式传2000/.95/42。性能仍有固定顺序、单次建库和并行负载的限制。协议SHA256 `70404487e28838dc3b97d9ddf17e3194bc288d816471169b8dcd1be12877b5d5`，summary SHA256 `ec4793e771b2718cf246361843806d7eed605e13b88cfe49a7b0383570a59940`，输出保存在同名本地忽略目录。

## 建库成本与实际服务边界

补充固定probe `chinese_bm25_20261006_build_fixed`，脚本 [benchmark_chinese_bm25_build.py](../../scripts/benchmark_chinese_bm25_build.py) 对同一19,050条索引，在一个进程内每臂重新构建4次，不读case/gold、不共享索引，固定无外发。source/index身份稳定、exit0。首次char构建4428.93ms；库已加载后的三次为1620.97/1635.06/1655.67ms。legacy对应首次1075.76ms，后续1084.34/1064.75/1063.76ms。固定查询的char为10.21至11.23ms，legacy为51.65至54.61ms；这些不是120题p95。

当前`PostgresAssistantFactory`仍为run构建检索器，未新增跨run共享缓存。因此热查询更快**不代表当前API端到端更快**，建库、数据库加载、首次依赖导入均需计入。本probe不含PG加载，只是小样本工程成本诊断；无法推导生产吞吐。上述额外取舍已告知用户，不静默增缓存、搜索服务或改推广门槛。manifest SHA256 `d36338fe5eb23a4bc4df5a48b3ae508251f79e6111437711af0395cf07581c23`。

## 已运行工程检查与失败

- T3真实库合同与相关旧回归：94 passed + 4 subtests，16.02s，exit0，`.tmp/chinese-bm25-contract-scope-green.xml`。初始模块未存在的RED、首次分词假设错误造成的1 failed/42 passed均保留；修正合成测试中的词边界，不改词典或评分。benchmark汇总/NA/不覆写证据4项passed。
- T5新尺度反例先6 failed/7 passed，`.tmp/chinese-score-red.xml`；新v2机械分数合同及显式v1历史分派27 passed。BM25/RRF/exact的非正值仍按各自正匹配合同拒绝，signed dense分数不套统一阈值；数值合法不等于语义正确。
- T6 parser默认v3，硬要求需要正向选择；旧v2源码冻结并按原身份重放。新选择与配对54 passed；旧解析156项显式绑定v2通过。基础直接引用和并列列表后置询问仍测试现代版本，不能整体移到旧版本掩盖退化。
- 接线初轮14 failed/92 passed，第二轮4 failed/102 passed；修复纯引用列表作用域，显式保留旧跨句推断测试并新增现代未知行为，升级规则版本期望；最新116 passed/2.87s，`.tmp/chinese-evidence-integration-green.xml`。同一次命令随后读取汇总的临时表达式有SyntaxError，使外层exit1；pytest本身退出0且JUnit116/0/0/0，二者不混写。
- T7标题可用性先真实RED；保留原完整目录与证据，仅隔离不受支持的query grammar提示。普通查询、支持的exact、unsupported明确unknown等50项passed，`.tmp/chinese-catalog-title-root-green.xml`；该开发时点新增4项真实PG尚未执行，后续失败、修复与最终93项结果分别记录如下。

- 字符adapter先15 failed/51 passed，再66 passed/19.88s，分别为`.tmp/chinese-bm25-character-red.xml`与`...-green.xml`。原jieba完整身份保持不变，char实际HMM为null，显式无效HMM参数拒绝。
- 第二协议的失败/超时/缺结果/源码漂移/重复不一致/非回退推广门槛等16项通过，`.tmp/chinese-script-entry-green.xml`。PR中间head `c562eec9e67dd36376be24d7a39bb3c7d79da84a` 的CI run `37350033614` 首先因pytest脚本入口找不到scripts模块收集失败；显式测试pythonpath后，本地相同脚本入口16项通过。此处保留中间失败，最终候选CI单独记录。
- 开发中首次累计扫描119 failed/1855 passed +157 subtests/141.18s，`.tmp/chinese-full-first.xml`。包含尚在RED阶段的15项字符候选和旧规则版本/fixture错配，不能作为冻结候选验收。相关失败由各工作项独立修复，不覆盖原日志。
- T7首次真实隔离PG4项失败，`.tmp/chinese-catalog-pg-first.xml`，证实`lookup_run_article`尚把存储标题套用有界query语法；当时修复与复验待完成，后续已修复并通过下列复验，不覆盖原失败。自建临时PG已停止，日志保留。
- BM25/RRF/hybrid实际成熟子路、API闭集selector、M2实际引擎漂移拒绝及分数尺度共27项通过，`.tmp/chinese-wiring-score-first.xml`。

## 整体接线与独立复核

T4至T7已实现并保存为运行时提交`04fd389945125c81767745bbdce48137921d26fd`，默认策略选择仍待确认。配置身份、历史v2、机械v2/现代general-v3、目录边界和风险处置的职责见 [ADR-007](../../docs/refactor/decisions/ADR-007-chinese-bm25-and-bounded-rules.md)。

独立审查不是仅看现有测试：实际补出了条款局部选择被法名跨度遮盖、异常`条之二之三`截断、继承排除遮盖后续未知、输出v2冒充v3、阶段指纹遗漏、未冻结历史参数及公共profile被服务强制切回legacy等反例。修复分别约束跨度/完整标签、冻结版本/依赖身份和共用有效配置，没有加入购物词表或用gold修运行逻辑。未知跨句指代仍未知，未命中风险词仍不代表安全确认。

- T6当前17文件522 passed +100 subtests，`.tmp/reference-risk-complete-migrations-reviewed-final.xml`；独立风险15项通过。原parser基础测试继续跑现代版本，改变的收缩语义同时断言v2原行为，旧模块SHA保持`2e52d6f0e694c8819827e7b9ee3906b055e3071037f1c3a345de5649bb9da420`。
- M2历史修复过程分别63 failed/71 passed、5 failed/129 passed，源于未显式版本和旧manifest缺query_analysis被错误索引；真实修复后六文件135 passed，但随后公共profile又有新修复，不能累加或借用它当最终门禁。公共profile真实RED1 failed/3 passed，修后80 passed。新schema2共享公共默认；schema1原payload/fingerprint与1.0乘数保持不变。
- T4/T5独立63项通过，`.tmp/chinese-t4-t5-independent-reviewed-green.xml`；真实合成RRF k=200的2/201分数不再被现代0.01阈值误拒，仍semantic not_checked。M2公共解码/implicit-BM25身份14项独立通过，`.tmp/chinese-output-final-frozen-green.xml`。公共测试第一次5失败是新增fixture参数错误，含red文件名的implicit测试实际上12通过，都不冒充产品RED。
- 第二次PG17文件93 passed/95.96s，`.tmp/chinese-service-pg-second.xml`，并完成实际stop/start与独立进程schema8恢复，receipt在`.tmp/isolated-pg-716819adf1e44a9685e02d5cd3bc5cc1/restart-prepared.json`。该轮早于最后公共profile修复，不冒充最终候选；最终复跑见下一节。
- 第一轮累计M2为24/25、exit1、234409ms，`.tmp/chinese-candidate-m2-first.json`。唯一失败检查是全量pytest中2个CLI trace测试替身未带实际助手的evidence_rules_version字段；生产链路不放松，修复替身后CLI+版本专项17 passed，`.tmp/chinese-cli-version-final-green.xml`。
- 当前wheel实际仓库外安装成功，27模块/schema8 smoke通过，`.tmp/chinese-candidate-wheel-probe.json`，wheel SHA256 `cab21c051ed911939f728b2ffd3c29eb02500b5c535e965ab675a442c3f4e78d`。随后逐文件比较110个包源码模块，全部与当前候选相同。wheel合同fixture新增模块前1 failed/23 passed，补齐真实新模块后24 passed，不取消缺文件检查。

## 前轮04fd运行时候选验证与交付：保留历史快照

运行时候选为`04fd389945125c81767745bbdce48137921d26fd`。本地门禁开始前后观察到同一clean HEAD，运行期间没有编辑tracked文件，远端同名分支已核验指向该提交。下面是这一候选的验证，不将较早比较manifest改绑为它，也不把后续仅文档提交冒充已运行的源码身份。

| 检查 | 实际结果 | 证据与边界 |
| --- | --- | --- |
| 累计M2离线门禁 | 25/25 passed，exit0，234906ms | `.tmp/chinese-04fd389-m2-final.json`；UTC18:14:03至18:17:59 |
| 门禁内全量pytest | 2018 passed +157 subtests，180.49s | JUnit2175/0/0/0，jieba上游4项警告；不是模型质量测试 |
| 17文件隔离PostgreSQL | 93 passed，94.32s，exit0 | `.tmp/chinese-04fd389-pg-final.xml`，合成隔离数据 |
| 数据库物理重启 | stop/start后新进程核验passed，head0008 | `.tmp/isolated-pg-4b8e87ff08d64607bc0d63d905a2a806/restart-prepared.json`；自建cluster已停止，日志保留 |
| 安装包隔离检查 | 27模块及schema0008通过 | 上节wheel实际安装；110源码模块逐字节与此候选一致，非仅在checkout导入 |
| 本地Linux broker/worker | not_run | 当前Docker Linux daemon不可用；不得用本地PG替代真实broker证明 |
| 精确源码候选远端CI | run37354368069四路completed/success | [04fd389 CI](https://github.com/1040942669/legal-rag-agent/actions/runs/37354368069)，不替代后续文档head的CI |

远端结果已实际下载并解析：offline/M4/M5/M6累计门禁为25/25、41/41、51/51、58/58，全部exit0；M6累计675759ms，真实worker JUnit76/0/0/0，累计M5 JUnit81/0/0/0。M5/M6回执以预期源码SHA `04fd389...` 与schema `0008_execution_money` 做严格validator复验，errors均为`[]`，无live调用。M6制品ID `11365171011`，API digest `sha256:d9959f8105676e817254a0c3de229f64dad0c5deff4484d91a1ffd4ce9e2c11a`，本地保留`.tmp/chinese-04fd389-ci-m6/`。本地未跑Linux broker与远端实际已跑是不同事实。

已推送的提交顺序：`914a7e3`保存前置改动、`c562eec`成熟中文组件和负结果、`e55a6a4`字符对照与推广门槛、`04fd389`统一配置和规则收缩。[PR31](https://github.com/1040942669/legal-rag-agent/pull/31)仍为Draft，未合并；master仍为`4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`。现有v0.6.0/v0.7.1 Tag及Release不变，本轮不创建Release、不进入M7。应用内PR附件工具两次返回参数错误，停止重试；GitHub PR本身已实际创建和读取核验。

结果文档静态3/3在最终CI回填前后分别通过，artifacts为`.tmp/chinese-result-docs-static-first.json`与`.tmp/chinese-result-docs-static-final.json`，覆盖46份Markdown、2份JSON、326份候选文本，无高置信凭证形状，diff检查通过。记录此结果后的纯元数据收口在提交前重验同组静态。本文是文档提交前快照，后续纯文档提交的SHA/CI通过GitHub读取并在交接回复中报告，不递归创建新的回执提交。

以上是04fd及随后结果文档提交前的历史快照：当时尚待用户对默认取舍的选择，文档提交推送后须独立读取其head CI。没有通过改门槛宣布字符方案自动胜出；`legacy-v1`保持默认，成熟候选显式opt-in。没有独立未曝光集、人工法律验收、真实模型质量或生产容量结论。上述重叠专项不相加为最终分母。真实模型新增调用和费用均0，既有付费历史及账本未改动。后续用户追加SmartCN及当前默认待取舍状态以以下第三轮为准。

## 用户追加选择后的第三轮：SmartCN固定对照

用户于2026-10-06选择继续实测SmartCN，而不是接受字符方案的MRR回退。追加范围从已推送的`e3d32a96d3a37f5ab7c0e559bb000448fb0e603d`继续同一Draft PR31，独立 [v3协议](../../configs/chinese-bm25-benchmark-v3.json) 在实际运行前冻结。实验源码提交`8f042fb46b83c7d2e7eb630c1197546f0d98c3cd`已推送；前两协议、结果、gold和各自源码身份保持原样，不能改绑为该提交。

run `chinese_bm25_20261006_smartcn_fixed_third`，UTC `2026-10-06T02:23:11Z`至`02:25:44Z`。使用同一19,050条article/205个来源、已注册120题；108题为有效排名分母，12无gold题为NA，不推断拒答成功。四臂各用新进程、正反两遍，每个Java臂新建JVM和索引。四个worker均exit0，每臂两遍120题均无执行错误，排名重复不一致为0；源码、输入和Java运行时身份检查稳定，末尾复验无错误，实际新增模型调用和费用均0。

| 候选 | Hit@5 | MRR@5 | 场景形状分层 Hit@5 | 建库毫秒 | 第二遍完整查询p95毫秒 | 采样进程树峰值MiB |
| --- | --- | --- | --- | --- | --- | --- |
| 历史 legacy-v1 | 76/108 | 0.610184 | 67/99 | 1825.46 | 598.91 | 467.63 |
| BM25S + sklearn char | 83/108 | 0.597065 | 75/99 | 10453.38 | 49.06 | 449.42 |
| BM25S + SmartCN同token控制 | 73/108 | 0.506632 | 65/99 | 6459.55 | 54.70 | 555.05 |
| 原生 Lucene + SmartCN | 73/108 | 0.503237 | 65/99 | 4042.68 | 21.31 | 455.85 |

两种SmartCN相对同轮legacy均为4题改善、7题回退，配对Hit@5差异95%区间`[-0.0833, 0.0278]`；明确引用形状层均8/9，旧版9/9。这些是固定开发集直接排名，不是精确目录路由表现。协议仍选char为现代候选首选，`promotion_eligible_modern_arm=null`：char的MRR仍低于legacy，两种SmartCN则Hit@5和MRR都低于legacy。**本轮不支持推广SmartCN，也不支持自动切换char默认。**建议保留当前默认完成交付，不继续以盲增词典、逐题词表或调参数追求本开发集胜出；若用户接受char的维护/召回与排名取舍，另记显式决定，不能把旧自动门禁改成通过。用户最终默认选择尚未回复。

### 机制、身份与成本边界

新增 [Python桥接](../../scripts/smartcn_bridge.py) 与 [Java helper](../../scripts/java/SmartCnBridge.java) 仅供实验，没有接入CLI/API生产selector。实际Java/Javac为17.0.18、Lucene为9.12.3；三份官方Maven JAR的SHA在v3协议中冻结并实际核验。使用原SmartChineseAnalyzer默认标点停用项与随JAR固定的词典，不追加alnum过滤或应用词表。现代索引文本仍是法名、条号、原正文以换行拼接；同一分析器处理文档和查询，文档TF保留，查询按唯一term的OR语义，65536唯一词上限溢出拒绝而不截断。

版本依据为Lucene9.12.3官方 [SmartChineseAnalyzer API](https://lucene.apache.org/core/9_12_3/analysis/smartcn/org/apache/lucene/analysis/cn/smart/SmartChineseAnalyzer.html)、[BM25Similarity API](https://lucene.apache.org/core/9_12_3/core/org/apache/lucene/search/similarities/BM25Similarity.html) 和 [系统要求](https://lucene.apache.org/core/9_12_3/SYSTEM_REQUIREMENTS.html)。官方要求Java11或更高；9.12.3是适配本机现有Java17并冻结可复现身份的实验pin，不是宣称最新版，也不是生产安全认证。官方BM25默认k1为1.2，本实验明确设1.5/b=.75以保持所列现代候选参数一致，不能误称完全使用默认评分配置。

Lucene在top-k截断前以分数降序、chunk ID的UTF-8顺序全局排序；BM25S控制臂使用同一Java导出的token流、lucene/IDF-lucene和numpy float64，没有手写公式强制对齐分数。两臂实际均为1,342,119 tokens、19,050非空文档、0个零token文档，平均非空字段长度70.45244。Lucene采用`intToByte4`长度量化、float分数和非空字段统计；BM25S采用精确长度、float64及全部输入文档总体。相同token不保证相同排序；本轮零token文档为0，不能把实际差异归因为空文档分母不同。

编译在worker前单独完成，992.65ms；复用的是核验source/JAR/classes/JDK身份后的编译类，不是索引。Java启动加建库分别约3103.61ms（控制臂）和3843.09ms（原生）；控制臂另有Python索引1624.54ms。表内建库记录完整构造成本，查询p95记录完整`retrieve` wall time，包含分析和IPC，不使用不同范围的`last_engine_ns`比较。原生内部时间包括分析与搜索，控制臂该字段只有分析、另记Python评分，不能只取更小字段宣传端到端性能。

RSS是worker和活子进程的同时占用之和，含Python/JVM，排除预先javac。20ms是采样等待配置，不是实际50Hz保证；进程枚举另耗时，本轮parent和worker都运行采样器。固定臂顺序、单次冷建库、系统负载及采样开销均影响绝对耗时，第三轮char的10.45秒与第二轮4.51秒不能直接归因为算法变慢。采样峰值不是精确OS峰值或索引独占大小；热查询更快不证明当前每run重建索引的API更快，也不证明生产吞吐。

Python执行网络审计守卫，Java helper只有本地文件/stdio/Lucene操作，未在benchmark中下载依赖、调用Maven或模型。它不是OS级断网或恶意fork sandbox。Java选项和应用密钥环境不转交；超时/协议错误/关闭均回收自有进程树，文件保留本地受控忽略目录，不将JAR、语料或逐题原文上传。

证据目录为`artifacts/experiments/chinese_bm25_20261006_smartcn_fixed_third/`。协议SHA256 `d0c079d1d26e1b1906f39c9cb5c6dcf209950c393c061f572d4a9c2f18e3b8a8`，manifest SHA256 `8458391710e42367bdc3939d45975f00b1fa344cc7b74e3f6e4e9a85039779dd`，summary SHA256 `42959fdc6c480288e9f46e3ab5e69148f5bcacca0633d3855dff043921936a5f`。index SHA与前两轮相同；manifest保留精确critical文件、JAR、编译类和Java launcher身份。数据仍为未独立法律审核的重复开发集，第三次用户追加比较增加选择偏差，不称holdout或法律质量提升。

### 合同验证、失败保留与新候选门禁

- 四文件139 passed/25.64s、exit0，JUnit139/0/0/0、XML time25.624s，`.tmp/smartcn-all-contract-root-first.xml`。其中102项unit进入默认tests；32项真实Java与5项真实Python/JVM合成合同须显式运行，不能说普通CI已跑这些JVM测试。它们验证token流、同分排序、空/OOV、UTF-8、依赖漂移、协议/进程清理等，不提供法律质量证明。
- 初始模块/Java源码未存在的collection或setup错误保留，不冒充产品RED。退出清理失败仍允许选winner的真实失败为53项中1 failed，`.tmp/smartcn-benchmark-failure-contract-first.xml`；修复后严格拒绝选择。末尾输入漂移等新增反例首次已通过，只记录覆盖，不编造RED。
- stdout断管真实28项中1 failed，`.tmp/smartcn-java-contract-final-green.xml`；修复Java双层输出错误检查后28/0，`...-repaired-green.xml`。不能依据文件名中green将原失败写成通过。
- query bound真实日志`.tmp/smartcn-query-bound-red.xml`为4 failed：A/Q溢出和旧1024词边界是协议缺陷；65536长句生成器受分段影响产生额外token是fixture假设错误，修正合成隔断符而非改analyzer。Python唯一词上限先1 failed/1 passed，`.tmp/smartcn-python-query-bound-red.xml`，后46项unit通过；最终Java32项通过。两臂上限一致且不截断，不限制文档TF。
- clean候选`8f042fb...`前后HEAD一致，本地M2累计25/25、exit0、316818ms，UTC `02:25:57Z`至`02:31:15Z`，`.tmp/smartcn-8f042fb-m2-final.json`；全量2120 passed+157 subtests、245.43s、4条上游警告，JUnit2277/0/0/0。不可把重叠专项再加为总分母，也不借04fd门禁冒充新候选。
- [8f042fb CI37403867767](https://github.com/1040942669/legal-rag-agent/actions/runs/37403867767) 在本次回填最新观察时offline/M4/M5 completed/success，M6仍in_progress，尚不写四路通过。后续结果文档head须另读CI，不递归回填自身未来SHA。当前PR31仍Draft，未合并、未发布、未动Tag，M7未启动，`legacy-v1`默认未变。

复现入口如下，须先备齐v3协议中核验过的JAR和Java17；每次实际比较选择未使用的run ID，不覆盖已有第三轮。下面是复现命令，不表示已经额外执行第二次语料比较：

```powershell
uv run --offline --frozen --no-sync python -m pytest -q tests/test_smartcn_bridge.py tests/test_smartcn_benchmark.py
uv run --offline --frozen --no-sync python -m pytest -q scripts/smartcn_contract_tests.py scripts/smartcn_python_contract_tests.py
uv run --offline --frozen --no-sync python scripts/benchmark_smartcn.py --protocol configs/chinese-bm25-benchmark-v3.json --dependency-dir .tmp/smartcn-deps/9.12.3 --run-id chinese_bm25_smartcn_reproduction_001
```

新源码的本地Linux broker/生产服务SmartCN接线、真实模型、独立法律审核及未曝光holdout均未由本追加实验验证；仍不新增搜索服务、共享索引cache或通用安全/语言框架。后续仅核验真实远端门禁、完成结果文档交付与用户默认取舍，不自动追加新候选或付费调用。
