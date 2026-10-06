# 中文检索组件替换与规则收缩 TODO

日期：2026-10-06。状态：组件与接线已提交推送，本地最终工程验证及源码候选四路CI通过；结果文档交付与默认取舍待收口，尚未推广新默认。

## 用户选择后的追加范围：SmartCN固定对照

2026-10-06，用户选择第3项“继续实测SmartCN”。从已推送、clean且四路CI通过的`e3d32a96d3a37f5ab7c0e559bb000448fb0e603d`继续同一PR31；fresh origin/master仍为`4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`。这次选择授权新的离线对照，不表示已经接受字符方案回退、切换默认或增加搜索服务。

| ID | 追加工作 | 验收边界 | 状态 |
| --- | --- | --- | --- |
| S1 | 固定Lucene版本、依赖来源和独立v3比较协议 | Java17兼容9.12.3，官方三JAR校验；四臂和选择规则在真实运行前冻结，前两协议/结果不动 | 版本与下载校验完成，协议已落盘 |
| S2 | 实验专用SmartCN桥接与合成合同 | 原生Lucene全局排序、同token的BM25S控制；UTF-8、空/OOV、重复词、top-k、异常输入、进程回收、依赖身份；不接生产默认 | 独立复核通过；四文件139 passed/25.64s，含37项真实Java/Python-JVM合成合同，详见下文 |
| S3 | 四臂固定本地语料实测 | legacy、BM25S char、BM25S SmartCN、Lucene SmartCN；108排名分母、两遍、全进程树RSS、建库/JVM/IPC成本、0模型 | not_run |
| S4 | 独立复核、结果与GitHub交接 | 保留所有负结果，建议受已测指标和部署成本约束；相关门禁、commit/push/现有PR更新，不合并发布 | not_run |

新协议为 [chinese-bm25-smartcn-fixed-v3](../../configs/chinese-bm25-benchmark-v3.json)。SmartCN默认停用项只是标点；不追加购物词或根据本轮失误改变词典。Lucene默认k1为1.2，本轮显式与既有现代候选一致固定1.5/b=.75。Java依赖仅保留本地忽略目录，不将JAR、完整语料或逐题原文推送。不同引擎的长度量化/数值精度差异单独记录，不宣称控制臂分数必须相同。

S2冻结前证据：`.tmp/smartcn-all-contract-root-first.xml`，139/0/0/0、exit0；其中102项纯合成unit进入默认tests，32项真实Java及5项真实Python/JVM测试需显式执行，不将未配置Java的默认CI写成已跑它们。独立复核修正退出清理失败仍可选胜者、评测结束前最后窗口输入漂移、两臂65536 unique-query-term上限不一致、stdout断管子进程继续等待等通用合同，未动语料/gold/词典/排名参数。保留对应初始缺模块collection错误和真实失败日志，不把所有首次运行都称产品RED。

## 目标、权限与基线

用户要求调研适合本项目的中文 BM25，先列优化清单，再逐项实施并推送 GitHub。本轮允许本地修改、安装必要公开依赖、离线评测、commit、push 及供 review 的 PR。完成以代码、适用验证及远端分支可核验为准；不创建软件 Release，不移动 M5/M6 Tag，不进入 M7。真实模型默认关闭，无新付费额度，不上传语料、原始模型回答、密钥或私人材料。

核验基线：origin `1040942669/legal-rag-agent`，默认 `master`；远端 HEAD、origin/master 与本地 HEAD 均为 `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`。工作区包含前轮已审查的 Smoke、lexical、W1-W8 未提交改动。先将其作为明确的前置提交保存，再实施本清单；不 reset/stash 或覆盖。旧试验、旧付费账本和历史报告保持原身份。

“最好”指所列候选在固定本项目条件下的选择，不宣称存在对所有中文语料最好的 BM25。现有120题为 repeated development、未完成人工法律审核；实测可支持本项目工程选型，不把它改名为独立 holdout。

## 固定工作项与验收边界

| ID | 工作项 | 验收证据 | 状态 |
| --- | --- | --- | --- |
| T0 | 核验并保存现有工作，建立本次分支与可审查提交顺序 | 当前 Git/origin/PR/Tag/Release、候选秘密扫描、前置提交 SHA | 914a7e3已推送，Draft PR31 |
| T1 | 官方资料和实际版本源码研究：BM25内核与中文分析器分开比较 | 独立研究结论、来源、LlamaIndex tokenizer 实际行为、候选和环境限制 | 首组研究完成，负结果后补成熟分析器对照 |
| T2 | 冻结候选及离线比较协议，运行真实本地语料比较 | 不变 corpus/cases、固定配置、逐题错误、Hit@k/MRR/分层/耗时/内存、零模型调用、不可覆盖 run ID | 两组完成；char83/108但MRR回退，不自动推广 |
| T3 | 接入成熟 BM25 内核与选定中文 analyzer，缩减自写评分职责 | 真实库测试、中文匹配、OOV/空查询/top-k/重复词/稳定排序、源与配置身份；保留当前旧默认和显式历史复现，现代候选opt-in | BM25S/jieba/sklearn字符适配及最终离线验证通过；默认取舍待确认 |
| T4 | 统一 CLI、API、实验构造配置并适配缓存/版本 | 同配置同结果、废止乘数一致、冻结恢复不漂移、scope/snapshot隔离、旧工件不能静默套新策略 | 实现、独立专项、04fd389最终本地门禁通过 |
| T5 | 去除通用机械检查对原始分数0.01的依赖 | 同排序重标度、合法RRF k=200、BM25/dense/exact score kind；缺证据、非法分数、越权仍拒绝 | 实现及独立专项通过，历史阈值按原版保留 |
| T6 | 收缩语言规则的硬裁决权，修复风险误拒答和引用错误要求 | 词信号触发的用途未决先澄清，不把命中当作违法证明；后置否定/转述/只请求另一引用、多法配对；合成挑战集只作合同验证 | 实现，522+100子测试及独立反例复核通过 |
| T7 | 对齐目录导入、激活和引用路由的标题能力 | 超长/嵌套/不平衡标题不导致无关问答构造崩溃；明确处理失败，不静默丢目录；真实PG验证 | 04fd389最终PG93项与物理重启通过，保留HTTP300字符上限 |
| T8 | 独立复核、完整适用验证、文档与GitHub交付 | 累计离线门禁、相关PG/恢复/wheel、最终秘密和diff检查、精确commit/push/PR/CI状态、README/CHANGELOG/STATE/HANDOFF及验收报告 | 本地M2为25/25，2018+157子测试；PG93和27模块wheel通过；04fd389四路CI通过，文档交付收口中 |

实现按照可独立验证的工作项提交。并行开发仅限独占文件、明确接口；最后对整体候选复核。所有未运行项记录原因，失败不覆写为成功。

审计后的范围收敛：不新增通用 intent/safety 框架、模型分类调用或可信判断器体系。复用已有提及/澄清/终态合同，词命中不自称确认违法；自由文本用途无法确认时明确未知，不承诺仅靠离线规则完成合法用途问答。仅为真正变化的规则/严格字段做版本兼容，不全链路机械升版。新增共享索引缓存不是验收前提，先测建库和查询成本；已有缓存键的身份隔离仍是必需合同。

## 候选与当前研究事实

- 当前实际安装：`bm25s 0.3.9`、`llama-index-retrievers-bm25 0.7.1`。后者 `from_defaults(tokenizer=...)` 只警告、没有将参数传给实际 tokenizer；现有适配器采用默认英文分析设置。不能直接打开旧开关或传入无效参数就称支持中文。
- Python候选：直接复用 `bm25s` 评分和索引，分别评估 jieba 精确模式、搜索模式；文档与查询共用冻结 analyzer。普通词典来自公开依赖，不按本次失败题增补词表。需要先验证自定义splitter的实际接口。
- 服务型候选：Lucene SmartCN、Elasticsearch/OpenSearch IK。官方支持中文不等于在本语料上最优；宿主现有Java17，当前Docker Linux daemon不可达，未实测的候选明确标记。
- `rank_bm25` 也是现成评分库，但中文分词仍需独立提供，且本项目已依赖bm25s。不能仅更换算法包名便归因中文质量收益。
- 当前默认手写策略的连续词边界、40/80加分、废止乘数、风险词及引用语法分别分析，不能把效果全部归因于BM25公式。

研究来源：

- [BM25S官方仓库](https://github.com/xhluca/bm25s)
- [LlamaIndex BM25官方集成](https://developers.llamaindex.ai/python/framework/integrations/retrievers/bm25_retriever/)
- [jieba官方仓库](https://github.com/fxsjy/jieba)
- [OpenSearch similarity](https://docs.opensearch.org/latest/im-plugin/similarity/)
- [Elasticsearch Smart Chinese](https://www.elastic.co/docs/reference/elasticsearch/plugins/analysis-smartcn)
- [IK官方仓库](https://github.com/infinilabs/analysis-ik)

## 设计约束和评价

复用成熟组件负责分词、索引、BM25计算和top-k；应用保留授权过滤、不可变快照、原始来源、准确的法条身份、受控模型调用和持久结果。索引复用必须绑定内容及配置，不能跨权限或snapshot混用。

不得以手写confidence或原始分数表示法律正确概率。确切引用可用于精确目录检索；复杂自然语言意图没有足够把握时不伪造hard requirement。风险判断需要防范/讨论与实施意图的双向反例，不能靠不断堆同义词修补。历史严格版本按原身份解码；修复语义明确升级，不能悄悄改变旧结果。

比较协议在运行前落盘：相同已注册语料与题目、相同top-k，公开候选参数与索引文本，分开统计明确引用和场景问法。已有regression质量指标不变；新增误拒答/误澄清及引用关系挑战集由独立期望驱动，不用生产parser给自身生成gold。除排名外记录build/query耗时、资源及维护成本。引擎替换与analyzer差异尽可能用控制组拆分，不在看到结果后改同一run的候选或阈值。

已有W7签收/曝光工具保持独立、显式用途。本轮不再扩充治理框架；人工法律审核、语料现行性和独立未曝光集缺失会限制质量声明，但不阻止完成成熟组件迁移与已复现缺陷修复。

## 关联资料

- [本轮决策与职责边界 ADR-007](decisions/ADR-007-chinese-bm25-and-bounded-rules.md)。
- [执行规则](AGENT_EXECUTION.md)、[主方案](MASTER_PLAN.md)第13/14节。
- [前轮设计](decisions/ADR-006-general-evidence-first-improvements.md)、[前轮验收](../../reports/refactor/GENERAL_EVIDENCE_IMPROVEMENTS.md)。
- 当前结果与下一步会回写本文件及独立验收报告，不把计划状态填写为通过。
- [验收记录](../../reports/refactor/CHINESE_BM25_OPTIMIZATION.md)保留两组比较、召回/排名取舍、测试RED、最终本地结果与尚未运行项；实现完成不等于默认推广或整个任务完成。
