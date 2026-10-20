# 检索重构交接（R0-R5）

本文件独立于 M0-M7 发布治理，记录检索链路重构的计划与进度。下一个接手者（Claude 或 Codex）先读本文件，再看 `git log` 核对实际状态。

## 背景结论（2026-10-21 审计）

- `legal_rag/data.py` 对无书名号行头的 38 部法律用未锚定的条号正则切条，正文中的"依照本法第X条"被切成假条文：695 个假片段、427 组同法同条号冲突、282 条正文被截断；章节标题被粘进上一条正文（297 条）。
- 评测按"法律名 + 条号"判命中，假片段也算命中；没有 recall@20、全部目标条文召回、条号写法（阿拉伯数字/简称）覆盖。
- 详细审计结论见对话记录；路线图 R0-R5 如下。

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| R0 | 评测先行：语料完整性审计、条号规范化工具、`retrieval-audit` 深度检索审计、合成条号查询集、冻结基线 | in_progress |
| R1 | 修解析器：条号行头只认行首或句末标点后且带分隔符；章节标题/目录不再粘进正文；重跑基线对比 | not_started |
| R2 | 统一 QueryParser（法律别名 -> law_id、条号 -> int）+ CitationResolver 精确查表 | not_started |
| R3 | 词面索引重写：倒排 + BM25F，删除加法 boost | not_started |
| R4 | 服务层：按 snapshot 缓存索引、词面与向量解耦、单一检索配置来源 | not_started |
| R5 | 混合召回 + reranker 调优，adaptive 只传结构化字段 | not_started |

## R0 任务清单

- [x] `legal_rag/article_numbers.py`：中文/阿拉伯条号 <-> int；语料 19021 个条号往返零差异。
- [x] `legal_rag/corpus_integrity.py`：同法同条号冲突、引用碎片疑似、未终止条文、粘连标题统计；接入 `profile_dataset`。修复前真实语料：427 组冲突、676 个碎片、1076 条未终止、361 条粘连标题。
- [x] `legal_rag/retrieval_audit.py` + CLI `retrieval-audit`：深度 N 检索，Hit@k、MRR@10、全部目标条文召回、分题型、gold 歧义计数、延迟；输出到 `artifacts/retrieval_audit/`（已 gitignore）。
- [x] `legal_rag/citation_cases.py` + CLI `generate-citation-cases`：从语料生成全称/简称/阿拉伯数字三种写法的条号查询集。
- [x] 测试全部离线：`tests/test_article_numbers.py`、`tests/test_corpus_integrity.py`、`tests/test_retrieval_audit.py`。全量 1028 passed；`tests/test_m4_service_schema.py::test_m4_migration_remains_packaged_below_the_m6_single_head` 依赖 venv 中已安装的发行包文件清单（venv 以 editable 安装主 checkout），与本次改动无关，需 deselect。

## R1 任务清单

- [ ] 条号行头识别：行首（允许 Markdown 装饰前缀）或句末标点后且后接空白；行内引用不再切条。
- [ ] 章节/目录标题行不粘进条文正文。
- [ ] 更新 `tests/test_data.py` 中与新行为冲突的断言，并补回归测试（引用不切条、标题不粘连）。
- [ ] 用修复后语料生成 `eval_cases/citation_lookup_v1.jsonl`；在 R0 提交与 R1 提交上分别跑 v3 与合成集审计，写入 `reports/retrieval/R0_R1_baseline.md`。

## 运行环境约定

- Python：`D:/Study/Python_project/LLM-application/.venv/Scripts/python.exe`，在本 worktree 根目录运行时导入的是本 worktree 的 `legal_rag`。
- 语料：`D:/Study/Python_project/LLM-application/Chinese-Laws/Chinese-Laws`（不入库）。
- 测试：`python -m pytest -q`（离线）。
- 只在当前分支本地 commit；未经用户要求不 push、不开 PR、不打 Tag。

## 进度记录

- 2026-10-21 01:50 开始 R0。
