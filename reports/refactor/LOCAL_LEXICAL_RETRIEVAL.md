# 本地词汇检索与证据安全边界验收

## 版本和范围

- 本次为已发布 M6 之后、用户明确要求继续完善的局部本地修复，不是 M7 或新软件发布。唯一最终实验 Run ID 为 `offline_lexical_ab_20261004_first`。
- 实际分支为 `codex/live-smoke-qwen35b`，HEAD、当时本地 `origin/master` 均为 `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`。origin 为 `https://github.com/1040942669/legal-rag-agent.git`，默认远端分支引用为 `origin/master`。工作区已有首轮及引用修复的未提交改动，本次保留，不 reset/stash/覆盖；本报告没有未来候选 SHA。
- 目标是修复可离线复现的连续词边界、口语购买词汇检索及明确法律/条号证据匹配错误，验证固定案例回退、缓存身份与权限边界。
- 明确不做：第三轮真实/付费模型、Judge、embedding、reranker、LLM 改写、adaptive/follow-up、扩大权限语料、修改 verifier、自动补引用、生产或真实 worker 接入、commit/push/PR/Tag/Release/M7。
- [ADR-005](../../docs/refactor/decisions/ADR-005-local-lexical-retrieval.md)接受显式候选机制并记录取舍，不等于推广默认。`legacy-v1` 仍是默认，`local-lexical-v2` 必须显式 opt-in。[lexical-v2.yaml](../../configs/lexical-v2.yaml)在实验后新增，配置只选择本轮已验证候选，不改变算法。

当前状态为 `offline_experiment_completed_candidate_opt_in`。唯一 A/B 已完整执行并保存零提供商调用证据；独立审查、工程累计 M2 门禁及结果回写后的 metadata 静态复核均通过。本轮局部本地修复完成并停止，不等于真实回答或法律质量已经证明。

## 失败复现与实际改动

上一轮真实引用修复在第五题 `v3_scene_online_return` 停止：目标检索覆盖为 0，启发式充分性却为 true，模型输出 `insufficient_evidence` 与固定预期 `evidence_answer` 不符。该历史见[真实修复轮报告](LIVE_SMOKE_QWEN35B_REPAIR.md)，没有为本轮重写旧结果或放行模型自报不足。

| 文件/模块 | 改动原因 | 对外行为变化 | 兼容性 |
| --- | --- | --- | --- |
| [retrieval.py](../../legal_rag/retrieval.py) | 旧 bigram 可跨标点/英文/数字相连；口语词汇缺少有限规范词 | 显式 v2 只在连续汉字片段生成 bigram，去重 query 特征，保留 document TF，追加有限完整词特征 | 默认 legacy、原 query/法名/条号、来源和权限不变；候选重建 BM25 统计 |
| [config.py](../../legal_rag/config.py)、[cli.py](../../legal_rag/cli.py)、`configs/default.yaml` | 实际检索策略需要明确选择和 trace | 增加 validated profile 选择；默认值仍为 legacy | 不暗开 adaptive、模型调用或新预算 |
| [evidence.py](../../legal_rag/evidence.py) | 法名与条号跨结果/混合法律误拼，substring 法名混淆，异常分数假充分 | 精确别名、单显式法的每条同法归属、全分数有限检查；候选退货必要文字条件 | EvidenceCheck schema/reason 模式和 verifier 不变；不足走原受限响应与有界流程 |
| [experiment_lifecycle.py](../../legal_rag/experiment_lifecycle.py) | 候选与证据实现不能混用旧缓存 | 分阶段实现指纹纳入词汇/证据关键实现，trace/manifest 记录实际 text version | 历史身份不改写，版本变化拒绝不兼容复用 |
| [test_bm25_lexical_profile.py](../../tests/test_bm25_lexical_profile.py) | 证明候选不会注入法律答案或宽泛购物推断 | 26 个纯合成 profile/词汇回归 | 无评测 gold 分支或提供商请求 |
| [test_lexical_evidence_safety.py](../../tests/test_lexical_evidence_safety.py) | 先复现证据假充分和生成模式边界 | 56 个纯合成安全、权限、拒答和模式回归 | 高分/必要词不变成 semantic supported |
| [offline_retrieval_ab.py](../../scripts/offline_retrieval_ab.py)、[test_offline_retrieval_ab.py](../../tests/test_offline_retrieval_ab.py) | 固定两 profile 成对比较且保留失败和身份 | 120 条既有题、16 条合成挑战；每题 A/B 再 B/A，21 个脚本回归 | 裸 direct、top-5、无补检索/生成；重复运行不增加独立质量样本数 |

### 证据 guard 的准确合同

法名只去空白、外层书名号和 `中华人民共和国` 前缀，不把《甲法》与《甲法实施细则》或《合同法》与《劳动合同法》当作同法。若分析器识别到的法名在上述归一化后只有一部法，每个显式条号都必须在同结果中证明属于该法。有 typed `provenance.articles` 时逐条核对 `(title, article_number)`；否则只接受法名集合唯一为该法的 Chunk。混合多法的未类型化 Chunk 不能仅凭法名/条号 union 通过。多部显式法之间的未知条号关系不猜测，不能声称已经解决多法关系理解。

NaN、Infinity、bool、非实数等异常分数一律产生 `invalid_scores`；不能由另一个高分掩盖。已有有限低分仍使用 `low_scores`。这是数值正确性保护，不是统一提高 BM25 阈值或跨检索器分数校准。

附加词汇条件只作用于纯 BM25、所有结果 trace 为 `local-lexical-v2`、且 query 的有限词汇函数明确识别退货意图的检索集：至少同一个 `result.chunk.text` 同时出现 `退货` 和 `商品/货物/物品`，否则 `missing_goods_return_anchor`，在无补检索本轮流程中终止生成，合成测试确认模型调用为 0。不要求同时出现网络/购买，避免把商品质量退货文字不当地排除；不作用于 legacy、dense、否定购买、金融/非购物或未识别题型。

该条件只是词汇必要条件，不证明适用法律、可退货资格、期限、例外、引用蕴含或法律正确性。词汇出现后仍可能不相关；不新增 semantic supported。`not_checked` 仍表示未做语义支持判断，`uncertain` 仍保留未知，不等于通过。已有高分但不相关负例、scope 过滤后重算、检索前拒答均保留。模型自报 `insufficient_evidence` 不能覆盖预期模式，草稿仍因 `response_mode_invalid` 失败；最终安全 fallback 通过不能改写草稿失败。

## 验收结果

环境为本地 Windows 11 / AMD64 / CPython 3.12.13 / `.venv`。本轮无 `.env`、密钥、旧真实 raw 或提供商请求；原始本地检索 traces 留 ignored 目录。下表时间为真实时钟或工件时间，不用报告时间冒充测试时间。

| Test ID | 实际命令 | executed_at / 环境 | mandatory | 状态 | 退出码 | 证据位置/摘要 |
| --- | --- | --- | --- | --- | --- | --- |
| LEX-PROFILE-RED | `.venv/Scripts/python.exe -B -m pytest tests/test_bm25_lexical_profile.py -q --junitxml=.tmp/lexical-profile-red.xml` | JUnit start `2026-10-04T15:50:54.400521+08:00`，精确终止 UTC 未单独捕获 | yes | failed，预期 RED | 1 | 11 failed；主 agent 工具回执和 JUnit |
| LEX-NEGATIVE-RED | 同命令，JUnit `.tmp/lexical-profile-negative-red.xml` | JUnit start `2026-10-04T15:55:24.855287+08:00` | yes | failed，负例复现 | 1 | 6 failed / 11 passed，保留不当词汇扩展失败 |
| LEX-NEGATIVE-GREEN | 同命令，JUnit `.tmp/lexical-profile-negative-green.xml` | JUnit start `2026-10-04T15:56:52.833949+08:00` | yes | passed | 0 | 17 passed in 0.18s，后续新增更严格 bounded 负例 |
| LEX-BOUNDED-GREEN | 同命令，JUnit `.tmp/lexical-profile-bounded-green.xml` | JUnit start `2026-10-04T15:59:26.710316+08:00` | yes | passed | 0 | 26 passed in 0.25s；最终也纳入 139 联合 GREEN |
| LEX-EVIDENCE-RED | `.venv/Scripts/python.exe -B -m pytest tests/test_lexical_evidence_safety.py -q --junitxml=.tmp/lexical-evidence-safety-red.xml` | UTC 2026-10-04 08:10:33 至 08:10:35，合成 fixture | yes | failed，预期 RED | 1 | 16 failed / 21 passed in 0.53s；法条误拼、精确法名和异常分数失败 |
| LEX-PAIR-RED | 同命令，JUnit `.tmp/lexical-evidence-safety-pair-red.xml` | UTC 08:14:09 至 08:14:12，补 typed/untyped 混合法测试 | yes | failed，预期 RED | 1 | 18 failed / 23 passed in 0.48s；没有更改断言迎合旧实现 |
| LEX-PAIR-GREEN | `.venv/Scripts/python.exe -B -m pytest tests/test_lexical_evidence_safety.py tests/test_phase3.py tests/test_m1_verification.py -q --junitxml=.tmp/lexical-evidence-safety-green.xml` | UTC 08:14:41 至 08:14:43，离线 fake | yes | passed | 0 | 98 passed + 89 subtests in 0.34s |
| LEX-RETURN-RED | `.venv/Scripts/python.exe -B -m pytest tests/test_lexical_evidence_safety.py -q --junitxml=.tmp/lexical-evidence-safety-return-red.xml` | UTC 08:16:08 至 08:16:10，合成退货负例 | yes | failed，预期 RED | 1 | 5 failed / 51 passed in 0.46s；四种不完整 anchor 及未停止生成 |
| LEX-SAFETY-FINAL | `.venv/Scripts/python.exe -B -m pytest tests/test_lexical_evidence_safety.py tests/test_bm25_lexical_profile.py tests/test_phase3.py tests/test_m1_verification.py -q --junitxml=.tmp/lexical-evidence-safety-final-green.xml` | UTC 08:16:53 至 08:16:56，最终冻结版本 | yes | passed | 0 | 139 passed + 89 subtests in 0.44s；原 phase3/M1 断言保留 |
| LEX-AB-SCRIPT-RED | `.venv/Scripts/python.exe -B -m pytest -q tests/test_offline_retrieval_ab.py` | 精确 UTC 未捕获，runner 工具回执 | yes | failed，预期实现 RED | 1 | 6 failed / 14 passed in 0.59s，EvalCase fixture 缺 keywords；之前脚本未创建时 collection error 亦保留 |
| LEX-AB-SCRIPT-GREEN | 同命令 | 精确 UTC 未捕获，runner 工具回执，无 JUnit | yes | passed | 0 | 首轮 20 passed in 0.23s；补分数漂移/cohort 后 21 passed in 0.29s，最终 critical path 固定后 21 passed in 0.26s |
| LEX-AB-TARGETED | `.venv/Scripts/python.exe -B -m pytest tests/test_bm25_lexical_profile.py tests/test_offline_retrieval_ab.py -q --junitxml=.tmp/lexical-ab-targeted-before-experiment.xml` | JUnit start `2026-10-04T16:07:39.847013+08:00`，运行前专项 | yes | passed | 0 | 47 passed in 0.40s；JUnit time 0.329s，不是最终累计 gate |
| LEX-AB-EXECUTE | `.venv/Scripts/python.exe -B scripts/offline_retrieval_ab.py --run-id offline_lexical_ab_20261004_first`；外层 JSON 安全字段投影后显式传递 child exit | summary UTC 08:17:45Z 至 08:20:57Z，本地固定语料 | yes | completed | child 0 / shell 0 | elapsed 191551.2456ms；code/input/dataset stable=true，provider 0；唯一 manifest/summary |
| LEX-DIFF-CHECK | `git diff --check` | UTC 08:16:34 后工具回执 | yes | passed | 0 | 只有已有 LF/CRLF 提示，不是内容错误；早于最终结果文档 |
| LEX-M2-GATE | `.venv/Scripts/python.exe -B scripts/quality_gate.py --milestone M2 --mode offline --output .tmp/local-lexical-m2-first.json`；PowerShell 显式捕获并传递 child exit | UTC 2026-10-04T08:30:01Z 至 08:33:16Z，本地离线冻结候选 | yes | passed | child 0 / shell 0 | 25/25 mandatory、25/25 checks，193624ms；全量 1294 passed + 157 subtests in 139.51s / JUnit 1451/0/0/0；dotenv 禁用，live 禁止；静态 41 MD / 2 JSON / 268 candidate 文本 |
| LEX-METADATA-STATIC-FINAL | `quality_gate._candidate_static_records(repo_root, "M2")`，结果回写后的候选 metadata/链接/秘密形态静态复核 | UTC 2026-10-04T08:38:24Z 至 08:38:25Z | yes | passed | 0 | 3/3 passed，41 candidate Markdown 链接、2 JSON 状态、268 candidate 文本凭证形态检查无风险；artifact `.tmp/local-lexical-final-static.json`，不覆盖前述 gate |
| LEX-INDEPENDENT-TEST | `.venv/Scripts/python.exe -B -m pytest tests/test_bm25_lexical_profile.py tests/test_lexical_evidence_safety.py tests/test_offline_retrieval_ab.py tests/test_m3_bound_retrieval.py tests/test_m2_experiment_lifecycle.py::test_stage_contract_invalidation_is_directional -q --junitxml=.tmp/offline-lexical-independent-final.xml` | UTC 2026-10-04 08:23:48 至 08:23:51，独立 fake / fixture，禁 live 和 dotenv | yes | passed | 0 | 134 passed in 1.23s，包含 typed boundary 和缓存方向失效 |
| LEX-INDEPENDENT-REVIEW | 独立只读关键实现及 safe summary/manifest 审核，六旧 hash 复核 | UTC 2026-10-04，本轮独立 reviewer 回执，精确书面完成时间未单独捕获 | yes | passed | N/A，非命令 | 2 个回退 ID 和 heuristic 假充分明确保留；默认不推广；主 agent 已确认独立审查通过 |
| LEX-RUFF | `.venv/Scripts/python.exe -B -m ruff check --select E9,F63,F7,F82,I scripts/offline_retrieval_ab.py tests/test_offline_retrieval_ab.py` | runner 实际尝试，本地工具缺失 | no | not_run | 启动失败 | No module named ruff；未安装、未声称检查通过 |
| LEX-REMOTE-CI / LIVE / M6-SERVICE | 无 | 本轮无授权或不在范围 | no | not_run | N/A | 无远端 CI、第三付费轮、人工法律评审、真实 PostgreSQL/Redis/Celery 复验 |

另一次 GREEN 收集命令把 `test_bm25_lexical_profile.py` 误拼为复数 `test_bm25_lexical_profiles.py`，UTC 08:16:32 至 08:16:34，exit 1、零测试。随后先正确运行三文件得到 113 + 89 subtests，再按上表正确四文件运行最终 GREEN。零测试没有算通过，最终 JUnit 使用正确收集结果。脚本最早的 collection error 没有捕获精确退出码、UTC 或 JUnit，未补猜这些历史字段。

## 实验与指标

本轮两臂共享新证据 guard，比较变量为 lexical profile，不是完整旧 evidence 与新 evidence 的消融实验。相同 205 份既有本地 Chinese-Laws 文件、19,050 个 article Chunk、top-5、k1=1.5、b=0.75、law boost=40、article boost=80、废止法律乘数 0.5。direct、adaptive=false、follow-up=0；不使用旧阶段缓存。A/B 与 B/A 是每题两臂先后顺序，不是题集整组逆序。每臂每题两次实际 preparation + bare retrieval + evidence 检查；首遍质量指标仅算一次，第二遍核对隔离和顺序一致。

120 条 v3 中检索 gold 为 108，拒答 12 单独计算，不能让拒答进入检索命中分母。v3 是重复使用的开发回归，不是 holdout。额外 16 条合成挑战为 8 条人工设置 gold + 8 条无 gold 负向词汇控制，未经权威法律审查，同样不是 holdout。原 generation-subset 的 30 条只是本轮 120 条的重叠 cohort，27 gold + 3 refusal，并未生成任何回答，不是独立新增验证集。

| 指标 | legacy-v1 | local-lexical-v2 | 样本数/分母 | 结论 |
| --- | --- | --- | --- | --- |
| v3 Hit@3 | 70/108 = 0.648148 | 73/108 = 0.675926 | 108 paired gold | +0.027778，配对 95% CI [0.0000, 0.0648] |
| v3 Hit@5 | 76/108 = 0.703704 | 78/108 = 0.722222 | 108 paired gold | +0.018519，CI [-0.0278, 0.0648] 跨 0，不证明可普遍提高 |
| v3 MRR | 0.610184 | 0.624227 | 108 paired gold | +0.014043，CI [-0.0176, 0.0534] 跨 0 |
| v3 target coverage | 0.660494 | 0.679012 | 108 paired gold | +0.018519，CI [-0.0278, 0.0648] |
| v3 拒答路由 | 12/12 | 12/12 | 12，检索前路由 | 只证明固定拒答流程，不证明法律回答质量 |
| heuristic sufficient | 105/108 | 105/108 | 108 gold，非语义支持 | 仍有高分假充分，不能作为准确率 |
| sufficient 但未命中 gold | 30 | 28 | 108 gold，按摘要原统计 | 必要条件没有解决一般语义充分性 |
| 合成挑战 Hit@5 | 7/8 = 0.875 | 8/8 = 1.000 | 8 synthetic gold | +0.125，CI [0.0000, 0.3750]，小样本非 holdout |
| 合成负例误激活 | N/A | 0/8 | 8 无 gold controls | 只验证当前词汇规则未误扩展，不当作回答正确 |
| generation cohort Hit@5 | 18/27 = 0.666667 | 19/27 = 0.703704 | 与 v3 重叠的 27 gold | 仅检索 cohort；真实生成质量字段 unavailable/retrieval_only |
| 全部实际 provider 调用 | 0 | 0 | 完整实验 | 本轮模型成本 0，不等于本地 CPU/内存成本为 0 |

配对 bootstrap 按 case 差值采样，seed=42、2,000 resamples、95% confidence；CI 不给两个不同实验的总分作跨语料因果解释。

### 逐组差异与负结果

| case_type | gold 分母 | legacy Hit@5 | candidate Hit@5 | 命中变化 improved / regressed |
| --- | --- | --- | --- | --- |
| article_lookup | 25 | 25/25 | 25/25 | 0 / 0 |
| multi_article | 15 | 12/15 | 12/15 | 0 / 0 |
| semantic_scenario | 30 | 14/30 | 15/30 | 2 / 1 |
| hard_negative | 12 | 10/12 | 11/12 | 1 / 0 |
| cross_law | 12 | 10/12 | 10/12 | 0 / 0；MRR 0.666667 降至 0.611108 |
| adaptive_vague | 4 | 0/4 | 0/4 | 0 / 0；题型名不表示本次开 adaptive |
| adaptive_multi_intent | 4 | 2/4 | 1/4 | 0 / 1 |
| adaptive_contradictory | 3 | 2/3 | 3/3 | 1 / 0 |
| adaptive_many_law_hints | 3 | 1/3 | 1/3 | 0 / 0；MRR 0.166667 降至 0.111100 |

主题集四个 Hit@5 改善为 `v3_hardneg_pipl_basis`、`v3_scene_online_return`、`v3_scene_bike_deposit`、`v3_adaptive_contra_overtime`；两个回退为 `v3_scene_work_injury`、`v3_adaptive_multi_return_info`，均从第 5 位命中变为 top-5 未命中。74 条保持命中、28 条保持未命中、unknown=0。不能以净增加 2 条覆盖 2 条回退，也不新增针对这些 case ID 的运行时补丁。

原停止题 `v3_scene_online_return` 在 legacy 本轮首遍仍未命中，candidate 首位命中且 coverage=1；两次顺序结果一致。该变化仅说明候选检索改善了这一旧问题的证据输入，未调用模型，也未证明原整轮 paid Smoke 通过。全部 cohort execution_failed=0、repeat_inconsistent=0 不等于每题法律目标命中。

### 本地耗时和费用

| 测量项 | legacy-v1 | local-lexical-v2 | 计时边界 |
| --- | --- | --- | --- |
| BM25 建立 | 1467.127 ms | 1275.907 ms | 单次建立，单独记录，不含在查询样本 |
| v3 median | 329.408 ms | 312.172 ms | 每臂 240 个 preparation/retrieve/evidence 样本，含拒答路由 |
| v3 P95 | 724.151 ms | 705.891 ms | 同上；不是纯 BM25 或在线服务 P95 |
| 合成挑战 median | 135.212 ms | 142.120 ms | 每臂 32 个样本，含无 gold controls |
| 合成挑战 P95 | 396.374 ms | 388.582 ms | 同上，小样本单机数据 |

不是生产 SLO、吞吐量或稳定加速证明。本轮没有真实模型及新增费率账单，模型费用记录为 0；此前两 paid 轮累计 7 次、估算 0.0070528 元仍原样保留，未核实提供商实际账单，也没有用未消耗名义预算重新开额度。

## 身份与不可变证据

运行期间 `code_stable/input_stable/dataset_stable=true`；原始 JSON/JSONL 均留 ignored `artifacts/experiments/offline_lexical_ab_20261004_first/`。公开候选只保留本报告的数值、ID、hash、规则和复现脚本，不复制 traces、法条正文或私人内容。报告和配置等后续文档写入会改变工作区 dirty 身份，因此不能称最终文档与实验 dirty hash 完全相同；核心实现 hash 可单独比对。

| 身份 | SHA-256 / 值 |
| --- | --- |
| 实验 HEAD | `4d9546e06cfe8ff44660943ffd2dd353ac2e61cc`，dirty=true，不是候选 commit |
| 实验 diff_hash | `abe55326b075461e0f514e1d24b631d78ceee2b021299cc5b43f06c403dfd3aa` |
| manifest | `506c47fb2d0d1cc0583ee2f0acf7384a80fc7fcfec71fa1fc11c19d02f2ee068` |
| summary | `eec042f09fe0e03d5ea681bb185799415b76250a3542e4152f2f282f17024f74` |
| 205 份来源集合 | `e2bbf87b71394702bfd1f7301f3d58d0a9ac452b24cdb5561e3b4727dc3fd232` |
| article index | `0b705f64dcc4235517516def05bbd7ae46377cb6704181fad85afba14f2b8ec2` |
| v3 case_file_hash，UTF-8 LF v1 | `d14a91055687cb3d430332a874c3bb88f452186d38933d8df5f29f0981c0de74` |
| v3 case_set_hash | `8d40c72daf89fc32946a90b107bdb1451a67a8ec29d37138f6cb2e3d8027c1c3` |
| 16 条合成挑战 | `d3f105cf10c06bd1990927a6bfc876759050b42090c44058ea4b65a7e8d4e7f0` |
| retrieval.py | `98fe07cbebafbb1f1a7d51e74a6471cf214ddf07ee023c98cf17724f162ed323` |
| evidence.py | `185ee3648691f610da52ceead7a317ff52cec4483e6dcafbe3307ddca489efd5` |
| safety tests | `36bddcf4b21946c250ec4262b927d0a277dd733231d78a573d400db922b77a8d` |
| AB script | `6af21b8f57c4d6560ced2d8ad550f103a96e7b9d507977945017dfbafff6aef5` |
| retrieval implementation fingerprint | `b29063c6cb9c7c9b71f0e46e8db33b1ae3ca465ad27f8931ec84df5d17dac808` |
| verification implementation fingerprint | `0be2d56d3ad188fd7c4345788ff38de9180775d6ed2bac2dce72821f81576420` |

候选 query/document text version 分别为 `bm25-query-local-lexical-v2`、`bm25-document-contiguous-v2`；legacy 均为 `bm25-tokenize-v1`。完整 critical file 列表及各阶段指纹在本地 manifest。本轮 120 条文件 hash 不应与前轮 9 条子集的 LF hash 混用。

本轮只读 hash 复核确认下列六份旧 paid 证据未变，未读取其原始模型回答：

| 旧记录 | 保留 SHA-256 |
| --- | --- |
| first ledger | `3d841f013f26941bc1c772f972ab4e242b493e611129eaa446a8ef834f3fa1f8` |
| first summary | `99072a7152d2b29f77dccdfad0d1b25ffa3e77c431808c422821e98a8fa34cb6` |
| first manifest | `79fa56a4ffd4d6503567674e39fefa5fcbc8f1b5dc12793c39170def263f0644` |
| repair ledger | `e27fd7f67976d67a229dcaf9ee1f2c2afa3859391b860865708088938b4fc812` |
| repair summary | `b7ee004a24081e39ca4debd0da6cc2054aa2579a9698dcad7ba5c5bc68f454ef` |
| repair manifest | `c12e10aae625545d3b6029f9cc9240206898b9b1be152ee4ea8bf73a5aa92ae1` |

## 隐私与变更审查

- [x] 本轮仅新增本地代码、合成测试、配置示例与脱敏文档；无密钥/旧 raw/网络模型请求或上传。
- [x] 原 CLI、默认 profile、旧数据、题标、失败和 paid 账本保留。未放宽 scope/snapshot/profile 边界，未编辑 verifier 或用模型自报不足绕过规则。
- [x] 报告只比较同一最终 Run ID 两臂，分母明确；新结果不与旧报告的不同代码/条件汇总混比。
- [x] 仅声称已执行的检索/合成合同；没有将未知语义、工程通过、安全 fallback 或执行完成解释成法律准确率。
- [x] 独立关键实现、A/B 工件及旧 hash 审核通过，独立专项 134 passed；不代替完整门禁。
- [x] 全候选累计 M2 offline gate 25/25 passed，full pytest 1294 + 157 subtests，所有 mandatory 无 skip；与本轮冻结代码对应。
- [x] 结果回写后的三项静态秘密/链接/状态复核通过，真实时间和独立 artifact 如上表；不冒充未知攻击全部安全。

## 发布前判断

`not_applicable_local_only`，不是 `ready_for_release`。默认推广证据不足：2 个 Hit@5 回退，主指标 CI 跨 0；候选仅 opt-in。独立关键实现及结果审查、累计 M2 gate 与结果回写后的静态复核已通过，局部本地修复完成并停止。任何失败均保留，不扩展推进范围。未知法律语义、语料时效、gold 权威性、完整新提示真实生成、权限外未知攻击与生产性能仍未验证。

回退排名选择 `legacy-v1`，不删除旧数据/账本或移动已发布 Tag；新 evidence 的数值与明确归属正确性保护并不以恢复旧 substring 误判作为回退。无数据库 migration、任务服务改变或外部调用；数据和历史运行保持只读。若日后需要改 guard 语义或推广默认，先另定义固定质量/回退边界，不以本轮结果扩展范围。

## 发布后核验

本轮 commit / push / PR / Tag / Release 均未创建；远端 CI `not_run`，没有新软件 candidate、release target 或发布回执。既有 M5/M6 发布状态与标签保持不变；不重复 M5 回执，不把本地 dirty HEAD 的既有发布或历史 CI 当作本轮证据。

## 下一步与停止条件

本轮局部本地修复、唯一 A/B、独立 review、累计离线门禁和结果回写后的静态复核均已完成，README/STATE/HANDOFF 与明确 opt-in 示例已准备并交接；到此停止。主 agent 在最后 metadata 事实回填后会另做 fresh 静态确认，保留上述 first 静态 artifact 不覆盖。保留候选和回退数据，不补第三 paid 轮、不自动推广默认、不做 case 专用补丁、不扩样 Judge 或进入 M7。完整法律质量和新的付费复验需另行明确授权与验收边界。
