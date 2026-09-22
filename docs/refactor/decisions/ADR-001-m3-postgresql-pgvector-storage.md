# ADR-001: M3 PostgreSQL/pgvector 版本化存储与导入边界

- 状态：Accepted for M3 module A
- 日期：2026-09-22
- 决策范围：`v0.4.0` 的存储、迁移和离线向量导入基础
- 实现提交：`68f89ed0cd1393d8acda7570a83bb0714f57a868`
- 跟踪：[Issue #12](https://github.com/1040942669/legal-rag-agent/issues/12)、[PR #13](https://github.com/1040942669/legal-rag-agent/pull/13)

## 背景

项目在 M0-M2 中保留了本地文件语料、BM25、精确 dense、验证和可回放实验链路。M3 需要新增 PostgreSQL/pgvector，但不能把存储迁移误当成召回质量提升，也不能破坏无需数据库的原 CLI 路径。

本阶段首先要解决的不是 ANN 性能，而是可核验的数据身份和失败边界：同一语料快照有哪些法律版本、法条和分块；向量由哪个 embedding profile 产生；重复导入是否真的相同；导入中途失败是否污染旧数据；迁移文件能否随 wheel 交付；后续 active snapshot 能否在 scope 内原子切换。

## 决策

### 1. 数据库为显式可选 profile

SQLAlchemy、Alembic、psycopg 和 pgvector 放在 `database` optional dependency 中。默认离线命令不需要数据库依赖，也不自动连接 PostgreSQL。数据库 URL 只接受 PostgreSQL + psycopg，并在对象表示和诊断中隐藏凭证。

### 2. 语料身份与向量身份分离

`corpus_hash` 只描述快照、法律版本、法条、分块和它们的有序关系；`bundle_hash` 再加入 embedding profile 与每行向量 hash。这样，同一语料可以合法导入多个 embedding profile，而语料本身不会因为换模型被误判为不同快照。

持久身份使用稳定 ID，不以展示标题代替主键：

- `snapshot_id` 标识不可变语料快照，`scope_id` 标识可见范围。
- `law_id` 标识一部法律，`version_id` 标识明确版本。
- `article_id`、`chunk_id` 保持来源链路；chunk 与 article 是有序多对多关系。
- `profile_id` 由 provider、model、revision、dimension、normalization 和 embedding recipe 共同决定。

### 3. 导入前失败关闭

数据库事务开始前，纯函数 contract builder 必须验证：

- ID 非空、无首尾空白且不超过 schema 长度；集合内不能重复。
- 法律版本和法条映射必须明确；同标题多版本不能靠标题猜测。
- 法律有效期采用 `[valid_from, valid_to)`，未知日期保持 `null`，不从文件名推断。
- 每个 chunk 必须显式给出有序 article 关系，关系必须闭合且不能重复。
- embedding cache 必须是二维、`float32`、有限数，维度必须与 profile 一致。
- `vector` 存储维度超过 16000 时提前返回可操作错误，不截断、不填充、不静默转换 `float64`。
- canonical JSON、内容 hash、向量字节 hash 和 bundle hash 必须可重算并完全一致。

### 4. 不可变行、幂等回执和整事务导入

导入使用一个数据库事务，并取得事务级 PostgreSQL advisory lock。锁用于把管理路径上的并发重复导入串行化，避免两个 writer 同时观察到“尚不存在”；它不是在线查询锁，也不是分布式任务系统的替代物。

已有稳定 ID 的行只能在所有受保护字段与实际向量内容一致时复用。只比较 hash 不足够，repository 会同时回读标量值、关系顺序、维度、向量实际字节 hash 和快照成员。发现冲突时抛出明确错误，不覆盖旧行。

`embedding_imports(snapshot_id, profile_id)` 是不可变导入回执：

- 完全相同的 bundle 重试返回 idempotent，不新增行。
- 同一快照可以有多个 profile，每个 profile 有独立回执。
- 同一快照/profile 的不同 bundle 被视为冲突。
- 任一后段失败使快照、成员、embedding 和回执全部回滚。

### 5. schema 为后续检索和激活保留硬约束

首版 migration 创建 corpus snapshot、law version、article、chunk、关系表、embedding profile、chunk embedding、embedding import、index build 和 active snapshot pointer。重要约束包括：

- embedding 行保存显式 dimension，并由数据库 trigger 与 profile dimension 交叉核对。
- index build 只能引用已经成功导入的 snapshot/profile 组合。
- active pointer 使用包含 scope 的复合外键，不能把一个 scope 指向另一个 scope 的快照。
- migration 先创建 `vector` extension，再创建表和 trigger。

模块 A 只建立这些约束和表，不实现 active pointer 的业务操作，也不声称 index 已构建或 active snapshot 已切换。

### 6. exact 优先，ANN 后置

pgvector 的 exact 查询将在 M3-B 与现有 NumPy dense 基线对照。距离到相似度的转换、稳定并列排序、过滤闭包和 profile/snapshot 匹配必须先通过。ANN 只作为后续可关闭优化；不同维度 profile 的索引策略、维度上限和过滤后少结果行为必须独立记录和测试。

首版 `VECTOR()` 列不固定 typmod 维度，以容纳多个 profile；代价是不能直接建立一个跨所有维度通用的 ANN 索引。后续 index build 必须按明确 profile/维度设计，而不是在本 ADR 中假定一种模型维度。

### 7. 迁移是分发物的一部分

Alembic 环境、revision 和模板作为 package data 进入 wheel。CI 必须在 fresh PostgreSQL database 上升级到 head，运行真实集成测试，再构建 wheel 并确认 migration resources 可从安装包发现。生产 migration role 仍需具备创建 `vector` extension 的权限，或者由数据库管理员预装扩展。

## 被否决的替代方案

1. 直接复用文件名或法名作为法律版本 ID：同标题多版本会产生歧义，无法可靠回滚或精确查条。
2. 将 embedding 模型身份并入 corpus hash：会把同一语料的多 profile 导入误判为多份语料，并让 active snapshot 语义混乱。
3. 遇到重复 ID 直接 upsert 覆盖：会破坏不可变快照、隐藏来源漂移，也无法审计失败导入。
4. 只检查 hash，不检查数据库实际行：历史损坏或错误回填可能保留同一声明 hash，必须回读受保护值和实际向量。
5. 先启用 HNSW/IVFFlat 再测正确性：ANN 误差、过滤 underfill 与 score 方向会混在迁移问题里，难以定位。
6. 用单元 fake 代替 PostgreSQL：无法覆盖 extension、DDL、触发器、事务、并发和 pgvector 的真实类型行为。

## 后果与限制

正面结果：

- 语料、法律版本、分块、向量 profile 和导入回执都有可重算身份。
- 首次导入、并发重试和失败重试有确定语义；旧快照不会被静默覆盖。
- migration 可随 wheel 分发，CI 对 PostgreSQL 18 + pgvector 0.8.6 提供真实证据。
- 后续 exact、过滤、active pointer 和 ANN 可以在同一数据模型上分模块实现。

当前代价与限制：

- 导入使用全局事务 advisory lock，管理路径吞吐有限；当前优先保证确定性。
- 模块 A 尚未提供检索 repository、法条查询、active pointer API、ANN index 或服务级重启证明。
- 本地验证使用 pgvector 0.8.1，CI 使用锁定镜像 pgvector 0.8.6；两个版本证据分别记录。
- 空库迁移已在 CI 覆盖；从“上一版 M3 schema”升级要等存在上一版 revision 后测试。
- Engine 重新连接不等于 PostgreSQL 服务进程重启；M3-T08 的服务重启证据留给后续模块。

## 验证

- contract 单元测试：12 passed。
- 本地 PostgreSQL 18.1 + pgvector 0.8.1 集成测试：9 passed，每次 pytest invocation 创建并删除独立数据库。
- 完整默认回归：`560 passed, 157 subtests passed`。
- PR head `68f89ed0cd1393d8acda7570a83bb0714f57a868` 的 [CI 35697224230](https://github.com/1040942669/legal-rag-agent/actions/runs/35697224230)：离线 job 与 PostgreSQL 18 + pgvector 0.8.6 integration job 均成功。
- 未运行真实模型、远程 embedding、reranker、Judge、完整私人语料或付费 API。

## 后续决策点

- M3-B：exact pgvector 查询、score 转换、稳定排序和所有召回/融合/回退路径的 scope/filter 闭包。
- M3-C：法名 + 条号 + 版本精确查询，active snapshot 原子激活与回滚。
- M3-D：按 profile 的 ANN 策略、过滤 underfill 状态、上一版 migration 升级、服务重启和累计发布门禁。
