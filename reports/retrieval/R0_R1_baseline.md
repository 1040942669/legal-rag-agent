# R0/R1 检索基线：解析修复前后对比

> **已被取代**：本报告基于后来停用的旧本地语料（部分文件来源不明）。v1.0.0 冻结语料上的数字见 [v1.0.0 基线报告](V1_0_BASELINE.md)；解析器修复的结论仍然成立。

> 生成于 2026-10-21。只跑了离线 BM25，没有调用任何模型或付费 API。数字只对应下面列出的本地语料快照和提交，不是对任意未来语料都成立的常数。

## 运行条件

| 项目 | 值 |
|---|---|
| R0（修复前解析器） | `ade8f18`：只新增审计工具，解析逻辑与 `master` `4d9546e` 相同 |
| R1（修复后解析器） | `89b197a` |
| 语料 | 本地 `Chinese-Laws/Chinese-Laws`，205 个文件（不入库） |
| 检索器 | 自研 BM25，`configs/default.yaml` 默认参数（k1=1.5, b=0.75, law_boost=40, article_boost=80, deprecated_penalty=0.5），article chunk |
| 审计 | `legal-rag retrieval-audit --retriever bm25 --depth 20`，直接调用 `retrieve(question, top_k=20)`，不走 adaptive |
| 评测集 | `eval_cases/legal_eval_cases_v3.jsonl`（108 条有 gold）；`eval_cases/citation_lookup_v1.jsonl`（611 条，LF 换行下 SHA-256 `ef7b1977...c9b85`） |

Dense、RRF 和 reranker 没有重跑：R1 改变了 38 部法律的 chunk，旧 embedding cache 已经不匹配，而 CPU 重建 bge-large cache 预计要 2 小时以上（见 `reports/RESULTS_SUMMARY.md` 第 7 节）。

## 1. 语料完整性

| 指标 | R0 | R1 |
|---|---:|---:|
| 条文记录数 | 19050 | 18356 |
| 同法同条号冲突 | 427 组 / 1118 条 | 3 组 / 6 条 |
| 疑似引用碎片 | 676 | 0 |
| 未以终止标点结尾的条文 | 1076 | 10 |
| 正文粘连章节标题 | 361 | 0 |

R0 的根因：没有书名号行头的 38 部法律（刑法、公司法、保险法、专利法、个人信息保护法等）在行内任何位置遇到"第X条"都会切出一条新条文，所以：

- "依照本法第二百六十四条的规定定罪处罚" 被切成假条文 "第二百六十四条的规定定罪处罚。"
- 被引用的那一条丢掉后半段。例如劳动合同法第四十六条在 R0 中只剩"……支付经济补偿:（一）劳动者依照本法"，七项情形全部丢失；R1 中完整为 280 字，结尾"（七）法律、行政法规规定的其他情形。"
- 章节标题和目录被粘进上一条正文末尾。

R1 剩余问题来自源文件本身，不是解析器能负责的：

- 3 组冲突：`中华人民共和国期货和衍生品法.txt` 第 97 行、`中华人民共和国涉外民事关系法律适用法.txt` 第 51 行把多个引用写成了带书名号的行头。
- 10 条未终止：刑事诉讼法末尾的网页残留（"编 辑： … << 返回首页 / 相关文章"）、刑法第四百五十二条后附的决定清单、三部税法的"附：…税目税率表"、两个无编号小标题，以及上面的畸形行。

## 2. v3 评测集（108 条有 gold）

| 指标 | R0 | R1 |
|---|---:|---:|
| Hit@1 | 0.556 | 0.546 |
| Hit@3 | 0.648 | 0.657 |
| Hit@5 | 0.704 [0.611, 0.787] | 0.713 [0.630, 0.796] |
| Hit@10 | 0.769 | 0.778 |
| Hit@20 | 0.796 [0.713, 0.870] | 0.815 [0.741, 0.889] |
| 全部目标条文召回@5 | 0.661 | 0.661 |
| 全部目标条文召回@20 | 0.789 | 0.807 |
| MRR@10 | 0.620 | 0.620 |
| gold 状态 | unique 90 / **ambiguous 18** | unique 108 |
| 平均 / P95 延迟 | 266 / 420 ms | 260 / 389 ms |

解读：

- 排序指标的变化都在置信区间内，**R1 的价值是正确性，不是分数**。
- R0 中有 18 条 gold（17%）在语料里对应多条同号记录，命中可能落在碎片上。例如 `v3_multi_severance_calc` 在 R0 的 rank-1 "命中"其实是从劳动合同法第九十七条"……依照本法第四十六条规定应当支付经济补偿的"处切出来的碎片；R1 中真正的第四十六条排在第 2。Hit@1 少的那一条就是这个样例。
- R0 的条号查询 top-5 常被同一条的多个碎片占满（如"刑法第二百六十四条"的前两名都是 264 条的副本），R1 释放出这些位置，multi_article 的 Hit@20 从 0.933 升到 1.000，cross_law 从 0.833 升到 0.917。
- semantic_scenario（30 条）Hit@5 仍是 0.467，adaptive_vague 仍是 0/4：这是词面检索的词汇鸿沟，靠修解析器和调 BM25 解决不了，留给 R5 的混合召回加 reranker。

## 3. 合成条号查询集（611 条）

每部法律按固定种子抽 1 条同号唯一的条文，分别用三种写法提问：`《全称》第X条`、`简称第X条`、`简称第N条`（阿拉伯数字）。gold 来自 R1 语料；R0 中有 9 条 gold 是歧义的。

| 写法 | 样例数 | R0 Hit@1 | R1 Hit@1 | R1 Hit@5 | R1 Hit@20 |
|---|---:|---:|---:|---:|---:|
| 全称 | 205 | 203/205 | 203/205 | 1.000 | 1.000 |
| 简称 | 201 | 197/201 | 197/201 | 1.000 | 1.000 |
| 阿拉伯数字 | 205 | 2/205 | 2/205 | 0.049 | 0.293 |
| 合计 | 611 | 0.658 | 0.658 | 0.681 [0.643, 0.717] | 0.763 |

结论：

1. **阿拉伯数字条号基本不可用**：Hit@1 只有 2/205。`extract_article_terms` 和条号 boost 只认中文数字，"264" 只是一个普通 ASCII 词，命中靠运气。
2. **简称失败全部是近名法律混淆**，正好对应 `metadata_boost` 的子串匹配：
   - "保险法第九十八条" -> 社会保险法第九十八条排第 1；
   - "环境保护法第六十四条" -> 海洋环境保护法排第 1；
   - "能源法第七十五条" -> 节约能源法排第 1。
3. 全称的 2 条 Hit@1 失败都是司法解释文件：排第 1 的是该文件无条号的标题/前言记录（parse_status `unmatched`），正确条文排第 2 和第 4。简称的第 4 条失败是同一部法律内的排序问题（石油天然气管道保护法第四十条排在第六十条前）。这些都留给 R2 的精确查表一并覆盖。
4. v3 的 25 条 article_lookup 全部用中文数字，Hit@5 一直是 1.0，所以旧评测集看不到上面两个问题。

## 4. 对下游的影响

- R1 改变了 38 部法律的条文切分和 article_id/chunk_id。任何基于旧解析器构建的产物都需要重建后才能和新代码一致：`artifacts/indexes/*`、embedding cache（`cache-health` 会因语料指纹变化判为 invalid），以及 PostgreSQL 中导入的 snapshot。本次没有重建这些产物，也没有改动数据库。
- 服务路径 `PostgresAssistantFactory` 仍是每个 run 重建 BM25（见审计结论），延迟问题留给 R3/R4。

## 5. R2 验收目标

用同一个合成集作回归门槛：

- 阿拉伯数字 Hit@1 从 2/205 提升到 >= 0.99；
- 简称 Hit@1 = 1.000（不再出现近名法律混淆）；
- 全称和 v3 article_lookup 不得退化。

## 复现命令

```powershell
$DATA = "<Chinese-Laws 语料目录>"
python -m legal_rag.cli profile-data --data-dir $DATA --profile-dir <out>/profile --report-dir <out>/profile
python -m legal_rag.cli build-index --data-dir $DATA --profile-path <out>/profile/data_profile.json --index-root <out>/indexes --chunk-strategy article
python -m legal_rag.cli retrieval-audit --index-dir <out>/indexes/article --retriever bm25 `
  --cases eval_cases/legal_eval_cases_v3.jsonl --cases eval_cases/citation_lookup_v1.jsonl `
  --depth 20 --data-dir $DATA --output-dir <out> --prefix r1_bm25
# 重新生成合成集（需与 R1 解析器搭配，结果应与入库文件逐字节一致）
python -m legal_rag.cli generate-citation-cases --data-dir $DATA --output eval_cases/citation_lookup_v1.jsonl
```

R0 数字在 `git worktree add <dir> ade8f18` 得到的独立工作区中，用同样的命令生成。
