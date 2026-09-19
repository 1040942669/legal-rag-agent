# Changelog

本文件记录候选与已发布版本的用户可见变更。历史实验数字仍以对应报告中的语料、模型和时间条件为准。

## [Unreleased]

### Added

- M1 结构化回答兼容层，显式区分 `evidence_answer`、`insufficient_evidence`、`needs_clarification` 与 `out_of_scope`。
- schema、证据目录、引用 ID/对齐、快照/权限范围、免责声明、回答模式和未知语义状态的分层验证结果。
- 评测指标 schema v2，记录应答/应拒答/应澄清、服务失败和 Judge 三态分母，并单独计算拒答召回与过度拒答。
- 明确标注为虚构且非法律内容的 M1 改前/改后回归 fixture。

### Changed

- 免责声明和普通法律陈述中的“不得”“不能”不再被当作拒答；受限模式只接受有界模板。
- 伪造、未对齐或越权来源会使生成草稿失败；最终降级响应会再次验证后才交付。
- Retrieval-only 不再拼接检索文本冒充回答，也不运行 answer verifier 或 Judge；不可用指标使用 `null + reason`，旧 CSV 仍保留 `-1` 兼容哨兵。
- Judge 超时、传输与格式错误独立归因并排除质量均值；评测输出拒绝覆盖已有历史文件。

### Security

- 不在允许快照或权限范围内的证据会在进入生成提示和返回 sources 之前被移除。
- Trace 分开保存被拒绝的生成尝试与最终交付结果，避免失败草稿被误当作用户收到的回答。

### Known limitations

- 当前语义支持仍是确定性词面启发式，只能可靠表达 `uncertain` 或 `not_checked`；没有声称已验证法律正确性。
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
