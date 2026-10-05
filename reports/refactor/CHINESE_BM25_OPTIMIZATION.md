# 中文 BM25 组件替换与规则收缩

日期：2026-10-06。状态：进行中，尚未完成最终默认选型、全量门禁或合并发布。

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

## 已运行工程检查与失败

- T3真实库合同与相关旧回归：94 passed + 4 subtests，16.02s，exit0，`.tmp/chinese-bm25-contract-scope-green.xml`。初始模块未存在的RED、首次分词假设错误造成的1 failed/42 passed均保留；修正合成测试中的词边界，不改词典或评分。benchmark汇总/NA/不覆写证据4项passed。
- T5新尺度反例先6 failed/7 passed，`.tmp/chinese-score-red.xml`；新v2机械分数合同及显式v1历史分派27 passed。BM25/RRF/exact的非正值仍按各自正匹配合同拒绝，signed dense分数不套统一阈值；数值合法不等于语义正确。
- T6 parser默认v3，硬要求需要正向选择；旧v2源码冻结并按原身份重放。新选择与配对54 passed；旧解析156项显式绑定v2通过。基础直接引用和并列列表后置询问仍测试现代版本，不能整体移到旧版本掩盖退化。
- 接线初轮14 failed/92 passed，第二轮4 failed/102 passed；修复纯引用列表作用域，显式保留旧跨句推断测试并新增现代未知行为，升级规则版本期望；最新116 passed/2.87s，`.tmp/chinese-evidence-integration-green.xml`。同一次命令随后读取汇总的临时表达式有SyntaxError，使外层exit1；pytest本身退出0且JUnit116/0/0/0，二者不混写。
- T7标题可用性先真实RED；保留原完整目录与证据，仅隔离不受支持的query grammar提示。普通查询、支持的exact、unsupported明确unknown等50项passed，`.tmp/chinese-catalog-title-root-green.xml`；新增4项真实PG测试尚未执行。

未完成：最终分析器选型、统一生产配置/恢复身份、风险处置接线、完整累计门禁、真实PG/恢复/wheel、新候选CI及最终交接。所有上列专项不能替代最终候选验收。真实模型新增调用和费用均0。
