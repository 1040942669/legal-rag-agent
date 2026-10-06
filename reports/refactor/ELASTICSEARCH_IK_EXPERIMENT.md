# Elasticsearch + IK 固定开发集实验

日期：2026-10-07。状态：固定源码 `dde6fc3c568b461aa84cfa38351478b239eb20db` 的实验与本地累计门禁完成；这是实验接线，不是生产默认迁移。

结论：本配置的 IK 为 Hit@5 `71/108`、MRR@5 `0.550307`，低于同轮 legacy 的 `76/108`、`0.610184`，不支持推广 IK。char 仍以 `83/108` 取得最高现代候选召回，但 MRR `0.597065` 回退，自动推广资格仍为空。默认保持 `legacy-v1`，不通过补词典、调参或改门槛追求本开发集胜出。

## 范围与固定机制

用户明确授权追加 IK 实测。沿用 [中文检索 TODO](../../docs/refactor/CHINESE_RETRIEVAL_TODO.md)、[ADR-007](../../docs/refactor/decisions/ADR-007-chinese-bm25-and-bounded-rules.md)，独立 [v4 协议](../../configs/chinese-bm25-benchmark-v4.json) 在运行前冻结。前三轮及原 gold 不改写，历史结果见 [组件替换报告](CHINESE_BM25_OPTIMIZATION.md)。

| 臂 | 固定配置 |
| --- | --- |
| legacy-v1 | 原自写历史实现，k1=1.5、b=.75、法名/条号加分40/80、废止乘数.5 |
| BM25S + sklearn char | BM25S0.3.9 lucene/IDF-lucene、float64；sklearn1.9.0 `CountVectorizer.build_analyzer` char(1,1)，保留alnum特征；无人工加分 |
| Elasticsearch + IK | ES9.1.4/IK9.1.4、原生Lucene10.2.2、bundled JDK24.0.2；显式BM25 k1=1.5、b=.75、discount_overlaps=true |

相同19,050个article块、205个来源文件、注册120题；108题有检索gold，12题排名NA，不推断拒答成功。各臂新worker，两遍正反题序，质量分母不因重复翻倍。实际Python为3.12.13。

现代索引文本固定为法名、条号、原正文按换行拼接，文档TF保留。ES用 `ik_max_word` 建索引，`ik_smart` 分析查询，逐题去重后构造term OR、minimum_should_match=1；这是明确的比较适配，不是默认 `match` 重写。1024唯一query词溢出拒绝而不截断，无短语、模糊、同义词扩展、fallback或boost。

一个primary shard、零replica；逐条核对bulk成功，refresh后19,050文档，冻结写入。服务端按score降序、chunk ID keyword的UTF-8顺序排序后截top5。保留原Chunk身份；partial、timeout、非法响应、重复ID、身份漂移均为执行失败，合法空/OOV检索仍是有效miss，不投影成服务错误。

## verified run 的实际质量

唯一最终结果主线为 `chinese_bm25_20261007_ik_fixed_fourth_verified`，证据在本地ignored `artifacts/experiments/chinese_bm25_20261007_ik_fixed_fourth_verified/`；UTC `2026-10-06T17:20:44Z` 至 `17:24:14Z`。三臂worker全部exit0，共720条succeeded；每臂每遍120个唯一ID、108有效排名、12 NA、0 error。排名和指标重复不一致均0。

| 候选 | Hit@3 | Hit@5 | MRR@5 | target coverage | 明确引用形状Hit@5 | 场景形状Hit@5 |
| --- | --- | --- | --- | --- | --- | --- |
| legacy-v1 | 70/108 | 76/108 | 0.610184 | 0.660494 | 9/9 | 67/99 |
| BM25S char | 78/108 | 83/108 | 0.597065 | 0.725308 | 8/9 | 75/99 |
| ES + IK | 66/108 | 71/108 | 0.550307 | 0.617284 | 9/9 | 62/99 |

明确引用层是既有文本形状分层，不是精确目录路由评测；场景层另有12题NA。上述指标沿用原canonical scorer，不表示法律结论或引用语义正确率。

相对同轮legacy，char为9题改善/2题回退，配对Hit@5差异95% bootstrap区间 `[0.0093, 0.1296]`；IK为3改善/8回退，区间 `[-0.1019, 0.0093]`。各配对分母108，resamples=2000、confidence=.95、seed=42。独立按ID重算aggregate、分层、成对计数/区间与summary一致。

选择重算为 `(complete=True, selected=char, promotion_eligible=None)`。两个现代候选参与候选排序，只有Hit@5和MRR均不低于同轮legacy才符合原工程门槛。没有改门槛或自动切默认，也不能以两遍重复当作两份独立样本。

## 修复后的 OOV 诊断

IK官方指出 `ik_smart` 不是 `ik_max_word` 的子集。最终诊断使用正文精确term `_count`，逐词验证0至19,050的document frequency，`df=0`才记OOV；它在两遍计时后执行，不参与排名或词典调整。[IK官方说明](https://github.com/infinilabs/analysis-ik/blob/master/README.md)

120条诊断全部completed；逐题去重后的token计数累加1,733，其中415个OOV。118题部分OOV、2题无OOV、0题全OOV、0题零token；120题检索均非空。1,733不是整批120题互相去重后的词汇量。诊断token与计时阶段query token逐ID相同，OOV列表与实际零频率一致。

这些计数不能证明415个OOV造成IK全部回退，更不能将旧错误的“全OOV”作为质量原因；同轮缺少只改变分词器的完整因果控制。

## 单次性能与资源边界

| 候选 | 完整建库ms | 第二遍客户端查询p95 ms | 采样进程树峰值MiB |
| --- | --- | --- | --- |
| legacy-v1 | 1616.51 | 612.04 | 468.02 |
| BM25S char | 7061.24 | 30.11 | 449.29 |
| ES + IK | 23166.64 | 59.41 | 1656.56 |

ES建库包括启动16979.31ms与索引/bulk/refresh/冻结6187.33ms。额外诊断46578.20ms单列，不计入query p95。查询wall time包含分析和search两次HTTP请求；server `took`不是客户端总延迟。ES文档总token数未采集，为 `not_collected/null`，不能补估。

worker采样自身及活后代的同时RSS，包含ES JVM；配置等待20ms，parent所有权采样200ms。两层采样、进程枚举和调度都有开销，不保证实际50Hz，不是精确OS峰值或索引独占内存。下载/解压/安装在worker前，不含在表内建库。

固定臂顺序、单次冷建库、两遍查询和系统负载限制绝对性能判断；不跨轮拼接recovery1或前三轮性能，不声称当前每run重建索引的API更快、生产容量更高或服务部署已验收。

## 身份、依赖与网络

源码、输入、运行时前后稳定。独立核对19个critical文件与当前源码hash、原index hash，三臂两遍case ID/gold mask均与manifest一致；最终自有配置检查 `checked=true/stable=true`，cleanup=true、worker failure=null。新增模型调用0，模型费用0元。

| 身份 | SHA-256 |
| --- | --- |
| v4协议文件 | `841202ffe398157d8214123db7f576b02211b5127a043148ee3ea4d90a35c91e` |
| verified manifest | `d14ea5cb045c766d9378c126c9778309eb812b6d37739bc4aa7c2b5c30316c51` |
| verified summary | `e9e62b33e77966def40104982894e5733107214acfbfb89e7135b4b08d637968` |
| article index | `0b705f64dcc4235517516def05bbd7ae46377cb6704181fad85afba14f2b8ec2` |
| 规范化case文件 | `d14a91055687cb3d430332a874c3bb88f452186d38933d8df5f29f0981c0de74` |

ES Windows ZIP来源及公开SHA-512在v4 dependencies冻结：[发行归档](https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-9.1.4-windows-x86_64.zip)、[官方校验值](https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-9.1.4-windows-x86_64.zip.sha512)。匹配 [IK9.1.4归档](https://get.infini.cloud/elasticsearch/analysis-ik/9.1.4) 的SHA-256为 `0bf5c3e809212dc4646e34c9e81169007c4bc400526675d37a4fd7d6b285e2db`，是本地下载内容来源记录，不冒充独立发布签名。

[隔离helper](../../scripts/isolated_elasticsearch.py) 的 `verify_dependencies(home, {"elasticsearch": elastic_archive, "analysis-ik": ik_archive})` 核对归档pin及实际安装文件；`verify_home`冻结安装资源/默认词典/config/JDK身份。版本9.1.4是兼容与复现pin，不是最新版或生产安全推荐；不是上轮Java17/Lucene9.12.3的运行环境。

默认词典未修改，扩展和远程词典为空，无按题增词或热更新。Python只允许manifest拥有的字面127.0.0.1端口，拒绝DNS、代理、redirect、UDP与其它外连；其它worker的守卫拒绝网络连接、解析与发送，不等于禁止创建所有socket。ES HTTP/transport仅loopback，配置下载器/ML/watcher关闭，应用密钥环境不传递。实验关闭security自动设置，不是生产安全部署；JVM没有OS防火墙隔离，不能声称air gap或恶意子进程sandbox。

## 失败、修正与精确门禁

- `chinese_bm25_20261007_ik_fixed_fourth` 保留 `interruption.json`：只有legacy结果完整，其它两臂缺失、无完整summary，原因与进程exit code均unknown；不补造成功或winner。
- `chinese_bm25_20261007_ik_fixed_fourth_recovery1` 在 `c241980c3f84263471f7a0078b0542aa460f53ed` 完成720条排名，但错误使用text字段 `_terms_enum`，返回完整空词表，诊断失真。`diagnostic-audit.json`明确标记 `ranking_completed_diagnostics_invalid`，原输出保留；summary SHA为 `ca1a23800d28539b0e7667b59072065eb88fcc4fd0b59280c198416602513f8f`，不作为有效OOV证据。
- c241门禁保留 `.tmp/ik-c241980-m2-cancelled.json`，状态 `cancelled_superseded_validation`，不是passed也不是产品测试失败。不能借用它证明dde6通过。
- dde6仅修post诊断的精确term频率合同，并增加回归后以新run完整复跑；不改ranking/index/query/字典/gold/protocol。独立比较recovery1与verified三臂两遍720条ranking/metrics及config identity，全部相同；本文只用verified性能。

| 检查 | 实际结果与证据 |
| --- | --- |
| 合成离线合同 | 135 passed，console1.72s，JUnit135/0/0/0、XML1.631s；`.tmp/ik-final-diagnostic-source-contracts.xml` |
| 显式真实本地ES | 8 checks passed，helper elapsed24231.35ms，命令wall26.54s；`.tmp/ik-real-synthetic-text-oov-fixed/result.json`，不是默认CI已启动ES |
| dde6累计M2 | clean HEAD门禁前后不变；UTC17:26:07Z至17:30:37Z，25/25 passed、exit0、267732ms；`.tmp/ik-dde6fc3-m2-final.json` |
| 门禁内pytest | 2255 passed +157 subtests，206.58s，4条上游jieba warnings；JUnit2412/0/0/0，专项不再相加 |

最新实际读取 [dde6 source CI37502817897](https://github.com/1040942669/legal-rag-agent/actions/runs/37502817897) 已四路completed/success。该源码绿灯不预先证明本报告后续文档head的CI；最终远端状态须另读精确head。

结果文档静态3/3通过：47份Markdown链接、2份JSON状态/manifest、343份候选文本，无高置信凭证形状，`.tmp/ik-result-docs-static-final.json`。记录该结果后的纯元数据收口在commit前重验同组静态，不为CI状态回填反复运行已经通过的全量本地测试。

## 复现与未验证项

先按v4 dependencies备齐公开归档、匹配plugin与原本地授权index，使用上述依赖验证函数核对后再运行；benchmark/helper不下载或安装依赖。下列是复现入口，不表示额外运行过另一轮；directory/run ID须全新，不能覆盖现有证据。

```powershell
$env:LEGAL_RAG_DISABLE_DOTENV='true'; $env:ALLOW_LIVE_MODEL_CALLS='false'; $env:HF_HUB_OFFLINE='1'
uv run --offline --frozen --no-sync python -m pytest -q tests/test_elasticsearch_ik_benchmark.py tests/test_elasticsearch_ik_adapter.py tests/test_isolated_elasticsearch.py
uv run --offline --frozen --no-sync python scripts/elasticsearch_ik_contract_tests.py --engine-home .tmp/ik-deps/9.1.4/elasticsearch-9.1.4 --directory .tmp/ik-reproduction-contract_001
uv run --offline --frozen --no-sync python scripts/benchmark_elasticsearch_ik.py --protocol configs/chinese-bm25-benchmark-v4.json --engine-home .tmp/ik-deps/9.1.4/elasticsearch-9.1.4 --run-id chinese_bm25_ik_reproduction_001
```

本次只是直接词汇排名。未测试OpenSearch、生产CLI/API/M2的IK集成、授权PG加载后的API延迟、生产容量、安全部署、独立法律审核/现行性、未曝光holdout或真实模型/语义checker质量，均为not_run/not_accepted。ES与BM25S在norm量化、float、字段统计和运行时上不同；历史与现代还改变索引文本、人工加分，不能归因BM25公式或分词的单一因果。

120题仍为重复开发集，第四次用户追加比较增加选择偏差，bootstrap区间不代表未见法律泛化。[PR31](https://github.com/1040942669/legal-rag-agent/pull/31)保持Draft，未合并、未创建Release、未移动既有Tag；生产默认未改变，M7未开始。原始归档、语料和逐题输出只留本地ignored目录，不上传密钥、私人资料或未授权语料。
