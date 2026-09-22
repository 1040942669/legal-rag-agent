# Changelog

本文件记录候选与已发布版本的用户可见变更。历史实验数字仍以对应报告中的语料、模型和时间条件为准。

## [Unreleased]

尚无未发布变更。

## [0.3.0] - 2026-09-22

### Added

- 严格实验 manifest 与分阶段精确缓存契约，区分 fresh、cache 和零外调 replay；缓存键按阶段依赖定向失效。
- 不可变逐 case attempt/complete artifact、校验和、损坏清单、兼容性检查和 pending commit 恢复。
- 通用 work-unit runner，支持 session group 内顺序执行、不同 work unit 并发、重试预算、断点续跑、checkpoint hash chain、分层计时和外部调用账本。
- 真实 evaluation/chat adapter 与 `ConversationMemory` 导出恢复，逐阶段保存 query、retrieval、generation、verification、judge 和最终结果。
- provider timeout、并发上限、可重试/不可重试错误分类，以及默认关闭的真实模型调用闸门。
- 冻结数据集 registry、四种 evaluation mode、artifact-only 聚合，以及 `experiment plan/run/resume/aggregate/replay` 生命周期 CLI。
- 累计 M2 离线门禁，将 M0 的 7 项、M1 的 10 项和 M2-T01 至 M2-T08 合并为 25 项机器可读检查。

### Changed

- 包版本升级为 `0.3.0`；实验命令不加载 `.env`，必须显式选择模式和输入，生成模式在未授权时失败关闭。
- replay 必须使用新的 experiment ID 和精确缓存命中，不能静默回退 fresh；聚合只读已持久化 artifact，发布内容寻址且不可覆盖。
- fresh/cache/replay 的真实调用数、来源调用 provenance 与耗时分开记录；失败、损坏、终止、耗尽重试和未运行均保留显式状态。

### Security

- 真实模型调用默认关闭；本里程碑的验收、示例运行和发布门禁均未读取本地 `.env`，未调用在线或付费模型。
- 实验身份记录真实 Git HEAD、dirty/untracked 摘要、配置、数据、语料、索引和阶段实现指纹，resume/replay 对不兼容事实失败关闭。

### Known limitations

- 当前可直接执行的生命周期后端是 provider-free BM25；`smoke-generation` 和 `full-regression` 只可规划，未提供预算与 provider 时不会运行。
- M2 证明实验身份、恢复、回放、聚合和指标口径的工程语义，不证明真实法律回答质量提高；本次未复跑完整法律语料、dense embedding、reranker 或模型 Judge。
- 生命周期命令当前要求 Git 源码工作区，以便读取真实 commit 与实现指纹；尚未引入跨进程任务队列、数据库或生产服务。

## [0.2.0] - 2026-09-20

### Added

- M1 结构化回答兼容层，显式区分 `evidence_answer`、`insufficient_evidence`、`needs_clarification` 与 `out_of_scope`。
- schema、证据目录、引用 ID/对齐、可注入快照/权限范围、免责声明、回答模式和未知语义状态的分层验证结果。
- 评测指标 schema v2，记录应答/应拒答/应澄清、服务失败和 Judge 三态分母，并单独计算拒答召回与过度拒答。
- 明确标注为虚构且非法律内容的 M1 改前/改后回归 fixture。

### Changed

- 免责声明和普通法律陈述中的“不得”“不能”不再被当作拒答；受限模式只接受有界模板。
- 伪造、未对齐，或在显式 `VerificationContext` 下越权的来源会使生成草稿失败；最终降级响应会再次验证后才交付。
- Retrieval-only 不再拼接检索文本冒充回答，也不运行 answer verifier 或 Judge；不可用指标使用 `null + reason`，旧 CSV 仍保留 `-1` 兼容哨兵。
- Judge 超时、传输与格式错误独立归因并排除质量均值；评测输出拒绝覆盖已有历史文件。
- Normalizer、生成回答和 Judge 的模型 JSON 使用精确字段集，拒绝重复 key、非标准数值、孤立 surrogate、超限或资源异常输入；生成回答与 Judge 失败关闭，Normalizer 使用稳定错误码并确定性回退。

### Security

- 当可信调用方显式注入 `VerificationContext` 时，不在允许快照或权限范围内的证据会在进入生成提示和返回 sources 之前被移除；当前 CLI 尚无认证身份、租户隔离或默认快照约束。
- Trace 分开保存被拒绝的生成尝试与最终交付结果；被拒绝草稿正文、schema 详情、畸形引用片段和生成 source ID 列表不进入普通 Trace，只保留安全字段与计数。
- 引用和拒答边界对全角/Unicode 括号、格式控制符、组合标记、filler 和不可见字符做保守失败关闭；正确引用只接受精确 ASCII `[S正整数]`。
- M1 累积离线门禁显式禁用 dotenv 与真实模型调用，CI 上传按 commit 和重跑编号区分的机器可读报告。

### Known limitations

- 当前语义支持仍是确定性词面启发式，只能可靠表达 `uncertain` 或 `not_checked`；没有声称已验证法律正确性。
- `VerificationContext` 是供可信上层注入的集成边界，不等于当前 CLI 已实现用户鉴权或多租户隔离。
- 本候选只运行离线 fake/fixture 测试，没有调用真实生成模型、Embedding、reranker 或 LLM Judge，也没有复跑历史法律质量实验。

## [0.1.1] - 2026-09-19

### Added

- M0 工作区与远端基线审计，明确区分真实代码、历史结果和未运行项目。
- 明确标注为完全虚构、非真实法律条文的 BM25 离线 smoke fixture，并在测试中硬性阻断网络。
- 统一 JSON 质量门禁，覆盖离线测试、包导入、CLI help、文档链接、JSON 状态和变更文件凭证风险。
- 对 pull request 和 `master` push 生效的最小权限 GitHub Actions 离线检查。
- 起始工作区中已经完成的 Phase 4B 实验平台，包括 cache v2、可选 reranker adapter、实验矩阵和成本/尾延迟聚合。

### Changed

- 包版本升级为 `0.1.1`，`pyproject.toml` 成为版本权威来源，运行时版本从 distribution metadata 读取。
- CLI 只在解析出实际业务命令后加载 `.env`；执行 `--help` 不再接触本地凭证文件。
- README 与历史执行文档区分旧 Phase 0-4B 演进和新的 M0-M7 增量主线。

### Security

- CI 不注入模型密钥，不执行真实模型、完整语料、Embedding 或 reranker 任务。
- 质量门禁只扫描 Git 候选文件，不读取被忽略的 `.env`、私人材料、完整语料或本地模型产物。

### Known limitations

- 本版验证离线工程基线，不复跑历史真实语料或模型质量实验。
- 当前 verifier 仍是启发式结构与行为检查；引用编号存在不等于语义支持已经被证明。
- Python `>=3.10` 是声明兼容范围，本阶段本地与 CI 必需门禁固定验证 Python 3.12 系列。
