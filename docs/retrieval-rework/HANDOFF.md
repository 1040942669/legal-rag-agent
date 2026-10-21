# 检索重构交接（R0-R5）

本文件独立于 M0-M7 发布治理，记录检索链路重构的计划与进度。下一个接手者（Claude 或 Codex）先读本文件，再看 `git log` 核对实际状态。

## 背景结论（2026-10-21 审计）

- `legal_rag/data.py` 对无书名号行头的 38 部法律用未锚定的条号正则切条，正文中的"依照本法第X条"被切成假条文：695 个假片段、427 组同法同条号冲突、282 条正文被截断；章节标题被粘进上一条正文（297 条）。
- 评测按"法律名 + 条号"判命中，假片段也算命中；没有 recall@20、全部目标条文召回、条号写法（阿拉伯数字/简称）覆盖。
- 详细审计结论见对话记录；路线图 R0-R5 如下。

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| R0 | 评测先行：语料完整性审计、条号规范化工具、`retrieval-audit` 深度检索审计、合成条号查询集、冻结基线 | done（随 v1.0.0 发布） |
| R1 | 修解析器：条号行头只认行首或句末标点后且带分隔符；章节标题/目录不再粘进正文；重跑基线对比 | done（随 v1.0.0 发布） |
| R2 | 统一 QueryParser（法律别名 -> law_id、条号 -> int）+ CitationResolver 精确查表 | done（随 v1.0.0 发布） |
| R3 | 词面索引重写：倒排 + BM25F，删除加法 boost | cancelled（v1.0.0 范围冻结，见 `docs/PROJECT_SCOPE.md` D4） |
| R4 | 服务层：按 snapshot 缓存索引、词面与向量解耦、单一检索配置来源 | cancelled（同上；已知局限记录在 PROJECT_SCOPE 第 5 节） |
| R5 | 混合召回 + reranker 调优，adaptive 只传结构化字段 | cancelled（同上；需要留出集才有意义） |

## R0 任务清单

- [x] `legal_rag/article_numbers.py`：中文/阿拉伯条号 <-> int；语料 19021 个条号往返零差异。
- [x] `legal_rag/corpus_integrity.py`：同法同条号冲突、引用碎片疑似、未终止条文、粘连标题统计；接入 `profile_dataset`。修复前真实语料：427 组冲突、676 个碎片、1076 条未终止、361 条粘连标题。
- [x] `legal_rag/retrieval_audit.py` + CLI `retrieval-audit`：深度 N 检索，Hit@k、MRR@10、全部目标条文召回、分题型、gold 歧义计数、延迟；输出到 `artifacts/retrieval_audit/`（已 gitignore）。
- [x] `legal_rag/citation_cases.py` + CLI `generate-citation-cases`：从语料生成全称/简称/阿拉伯数字三种写法的条号查询集。
- [x] 测试全部离线：`tests/test_article_numbers.py`、`tests/test_corpus_integrity.py`、`tests/test_retrieval_audit.py`。全量 1028 passed；`tests/test_m4_service_schema.py::test_m4_migration_remains_packaged_below_the_m6_single_head` 依赖 venv 中已安装的发行包文件清单（venv 以 editable 安装主 checkout），与本次改动无关，需 deselect。

## R1 任务清单

- [x] 条号行头识别：行首（允许 Markdown 装饰前缀）或句末标点后且后接空白；行内引用不再切条（`legal_rag/data.py` `find_article_headers`）。
- [x] 章节/目录/Markdown 序号标题行不粘进条文正文；宪法句末行内标题剥离。
- [x] `tests/test_data.py`：`test_chapter_title_not_parsed_as_article` 的旧断言（标题成为 unmatched 记录）改为"标题被丢弃"；新增 6 个回归测试。
- [x] 修复后真实语料：18356 条，冲突 3 组（源文件 期货和衍生品法 第97行、涉外民事关系法律适用法 第51行本身畸形），碎片 0，粘连标题 0，未终止 1076 -> 10（剩余为源数据网页残留、附表引用、畸形行）。
- [x] 用修复后语料生成 `eval_cases/citation_lookup_v1.jsonl`（611 条：205 full / 201 short / 205 arabic，seed 20261021，per_law 1）。
- [x] 在 R0 提交 `ade8f18` 与 R1 提交 `89b197a` 上分别跑 v3 与合成集 BM25 审计，写入 `reports/retrieval/R0_R1_baseline.md`。要点：v3 Hit@5 0.704 -> 0.713（区间内）、R0 有 18/108 条 gold 歧义；合成集阿拉伯数字 Hit@1 仅 2/205，简称失败全部是近名法律子串混淆。

## R2 任务清单

- [x] `legal_rag/query_parser.py`：语料标题生成唯一别名表（205 部法律 409 个别名，0 歧义），NFKC 后最长别名优先、互不重叠，书名号内只认完整标题；条号（中文、阿拉伯、全角、列表、之X）规范化为语料写法；条号归属同句内最近的前置法律，否则归属查询中唯一的法律。
- [x] `BM25Retriever`：法律加权与废止降权豁免改为等值；分词前把条号改写成语料写法；(法律, 条号) 精确命中加 `EXACT_CITATION_BOOST` 并按引用顺序置顶。删除 `extract_law_hints`、`build_known_law_hints`、`FALLBACK_LAW_HINTS`、`extract_article_terms`。
- [x] 测试：新增 `tests/test_query_parser.py`（15 个），`tests/test_phase4.py` 改用已解析法律名并删除旧 hint 测试。全量 1046 passed（同样 deselect 打包测试）。
- [x] 真实语料审计写入 `reports/retrieval/R2_citation_resolver.md`：合成集 611/611 Hit@1（阿拉伯数字 2 -> 205、简称 197 -> 201、全称 203 -> 205），v3 108 条名次逐条不变。门槛全部通过；1.000 部分由合成集构造决定，局限见报告第 5 节。
- [ ] 未做（见报告第 5 节）：人工俗称表（民诉法解释等）、条号范围展开、"本法"指代、RRF/reranker 置顶（R5）、`query.py` `COMMON_LAW_HINTS` 替换（会改变 chat artifact 输出）。

## 已知并行工作

主 checkout（`D:/Study/Python_project/LLM-application`）停在未合并分支 `codex/chinese-bm25-optimization`，即草稿 PR #31（"Replace custom Chinese BM25 and narrow rule-based decisions"，最后提交 2026-10-07 `22e1aa3`，已包含 `master` `4d9546e`，相对它多 13 个提交、165 个文件）。它改动了 `legal_rag/retrieval.py`（`BM25Retriever.__init__`/`retrieve`、`build_retriever`、`tokenize`）、`legal_rag/services/service_retrieval.py`，新增 `chinese_bm25.py`、`bm25_settings.py`、`legal_references.py`（服务层引用解析 `legal-reference-v3`）、`services/exact_retrieval.py`（精确检索路由）等。它不改 `data.py`，与 R0/R1 基本不冲突；与 R2 目标重叠，`retrieval.py` 合并必冲突。R2 为了不阻塞默认 BM25 路径先行落地，并把改动限制在 `BM25Retriever` 内部和一个新模块；R3 开工前必须由仓库所有者决定 PR #31 合并、取舍或废弃。

## 运行环境约定

- Python：`D:/Study/Python_project/LLM-application/.venv/Scripts/python.exe`，在本 worktree 根目录运行时导入的是本 worktree 的 `legal_rag`。
- 语料：`D:/Study/Python_project/LLM-application/Chinese-Laws/Chinese-Laws`（不入库）。
- 测试：`python -m pytest -q`（离线）。
- 只在当前分支本地 commit；未经用户要求不 push、不开 PR、不打 Tag。

## 进度记录

- 2026-10-21 01:50 开始 R0。
- 2026-10-21 02:10 R0、R1 完成。分支 `1040942669/bm25-retrieval-redesign`（Orca 自动从 `1040942669/killifish` 改名）：`ade8f18` R0 工具，`89b197a` R1 解析修复，随后一个提交加入合成集、基线报告、CHANGELOG 与本文件。全量离线测试 1034 passed（deselect 上述打包测试；该测试在未改动的 `4d9546e` 上同样失败）。未 push、未开 PR、未重建 PostgreSQL snapshot 或 embedding cache。
- 2026-10-21 14:40 开始 R2（R0/R1 未 push，仍在本地分支上）。15:10 前后完成：新增 `query_parser.py`，BM25 精确查表，报告 `reports/retrieval/R2_citation_resolver.md`。复用 R1 索引审计，没有重建任何产物，没有调用模型。
- 2026-10-21 下午：所有者确认 v3 由大模型生成、补录文件没有可核验的来源，并授权直接拍板。冻结为 v1.0.0：换用公开语料固定 revision 并提交语料清单，补"第X条之Y"解析与证据检查条号比对，生成开发集 dev-v1 与合成集 v2，R3-R5 取消。

## 结束（v1.0.0）

检索重构在 R2 结束。2026-10-21 所有者授权直接拍板后，项目范围冻结为 `v1.0.0`（决定记录见 `docs/PROJECT_SCOPE.md`）：

- 语料换成 Hugging Face `Kuugo/Chinese_Law` revision `a829f47d` 的 236 个原始文件，用 `configs/corpus/kuugo_chinese_law_a829f47d.manifest.json` 逐字节校验；旧的本地快照因来源不明停用。
- 解析器补上"第X条之Y"，证据检查改为按规范化条号比对。
- 评测改用 `eval_cases/legal_eval_dev_v1.jsonl`（v3 复核后删 7 条）和 `eval_cases/citation_lookup_v2.jsonl`，基线见 `reports/retrieval/V1_0_BASELINE.md`。
- R3-R5 取消；草稿 PR #31 关闭不合并。本文件不再更新。
