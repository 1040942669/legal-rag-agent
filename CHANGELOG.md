# Changelog

本文件记录已发布版本的用户可见变更。历史实验数字仍以对应报告中的语料、模型和时间条件为准。

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
