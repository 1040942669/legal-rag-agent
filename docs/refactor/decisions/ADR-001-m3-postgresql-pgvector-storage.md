# ADR-001: M3 PostgreSQL/pgvector 版本化存储与强制检索边界

- 状态：Accepted for M3 modules A and B；M3 overall remains `in_progress`
- 日期：2026-09-25
- 决策范围：`v0.4.0` 的版本化存储、离线向量导入、exact pgvector 与 mandatory retrieval boundary/filter
- 模块 A 实现提交：`68f89ed0cd1393d8acda7570a83bb0714f57a868`
- 模块 B 实现提交：`4bc912b09931803d53b4ca3ffdc81d302ba65813`
- 跟踪：[Issue #12](https://github.com/1040942669/legal-rag-agent/issues/12)、draft [PR #13](https://github.com/1040942669/legal-rag-agent/pull/13)

本 ADR 的 Accepted 只表示模块 A 与模块 B 的决策和实现证据已被接受，不表示 M3 已发布。M3-C、M3-D 尚未完成，PR #13 仍为 draft，`v0.4.0` Tag 与 GitHub Release 均不存在。

## 背景

项目在 M0-M2 中保留了本地文件语料、BM25、精确 dense、验证和可回放实验链路。M3 新增 PostgreSQL/pgvector，但不能把存储迁移误当成质量提升，也不能让数据库路径绕过已有融合、回退、重排和生成阶段的过滤边界。

模块 A 先建立可核验的数据身份与失败边界：一个快照包含哪些法律版本、法条、chunk 和有序关系；向量由哪个 embedding profile 产生；重复导入是否完全相同；失败是否污染旧数据；migration 能否随 wheel 交付。

模块 B 解决 exact 与授权边界：pgvector distance 如何转换成与旧 dense 相同方向的 score；并列结果如何稳定；scope/snapshot/profile/law/version/article/effective date 是否在 `LIMIT` 前生效；多 article chunk 是否可能披露越界文本；typed provenance 是否能穿过 BM25、RRF、adaptive、rerank、chat 和 artifact，而不会退化成一个可伪造的 trace 字段。

## 决策

### 1. 数据库为显式可选 profile

SQLAlchemy、Alembic、psycopg 和 pgvector 放在 `database` optional dependency 中。默认离线命令不需要数据库依赖，也不自动连接 PostgreSQL。数据库 URL 只接受 PostgreSQL + psycopg，并在对象表示和公开诊断中隐藏凭证。

未绑定数据库边界的 M0-M2 检索和 artifact 仍可使用，但不得被描述成经过数据库授权边界校验的结果。

### 2. 语料身份、向量身份与 query encoder 身份分离但必须闭合

`corpus_hash` 只描述 snapshot、法律版本、法条、chunk、有序关系和 source manifest；`bundle_hash` 再加入 embedding profile 与每行 float32 向量 hash。这样，同一语料可以合法导入多个 profile，而语料不会因换模型被误判为另一个 snapshot。

持久身份使用稳定 ID，不以展示标题代替主键：

- `snapshot_id` 标识不可变语料快照，`scope_id` 标识可见范围。
- `law_id` 标识一部法律，`version_id` 标识明确版本。
- `article_id`、`chunk_id` 保持来源链路；chunk 与 article 是有序多对多关系。
- `profile_id` 绑定 provider、model、revision、dimension、normalization、query/document prefix、metadata recipe 与 encoder adapter contract。

query encoder 必须暴露与数据库完全相同的 immutable profile identity。在线 version-bound encoder 只接受可核验的 `sentence_transformers` Hub `owner/model` 和 40 位小写 commit SHA revision；不接受本地 repository-shaped 路径，不允许 `trust_remote_code=True`。当前 SiliconFlow adapter 不能证明服务端 revision 不可变，因此不用于 version-bound query encoder。

### 3. 导入与查询向量统一失败关闭

共享 vector canonicalizer 对 document embedding 和 query embedding 应用同一规则：

- 输入必须是一维或二维 contract 要求的有限数值，并规范成精确 little-endian float32。
- 维度必须与 profile 一致；存储维度超过 pgvector 16000 上限时提前报错。
- normalized profile 要求非零且 L2 norm 在容限内，不静默归一化错误向量。
- model revision、normalization、prefix、metadata recipe 与 adapter contract 的变化都会改变 profile identity，不能复用旧 import receipt。

构造 exact retriever 时先核对 snapshot serviceability、完整 profile identity 和 `embedding_imports(snapshot_id, profile_id)` 的 `validated` 回执。构造失败时不能先调用 encoder。query vector contract 失败时不能进入 distance query。

### 4. 不可变行、幂等回执和整事务导入

导入使用一个数据库事务，并取得事务级 PostgreSQL advisory lock。已有稳定 ID 的行只有在受保护字段、实际向量、成员集合与关系顺序全部一致时才能复用。只比较声明 hash 不足够，repository 会回读标量、关系、维度和实际向量字节 hash。

`embedding_imports(snapshot_id, profile_id)` 是不可变导入回执：

- 完全相同的 bundle 重试返回 idempotent，不新增行。
- 同一 snapshot 可以有多个 profile，每个 profile 有独立回执。
- 同一 snapshot/profile 的不同 bundle 是冲突，不执行覆盖。
- 任一后段失败使 snapshot、成员、embedding 和回执全部回滚。

### 5. `0002` 在数据库层关闭不可变内容的旁路写入

Alembic `0002_m3_immutable_rows` 在 `0001_m3_storage` 之上增加以下约束：

- law version、article、chunk、chunk-article relation、snapshot membership、embedding profile、chunk embedding 和 import receipt 禁止 update/delete。
- corpus snapshot 的 identity 字段不可更改；只允许受控的状态迁移与对应时间戳变化。
- snapshot membership 只能在父 snapshot 为 `building` 时追加。
- chunk-article relation 与 law-version article 集合在相关 snapshot 完成 validation 后关闭。
- embedding dimension trigger 继续交叉核对 embedding 行与 profile dimension。

这些 trigger 防止应用 repository 之外的普通 SQL 静默篡改内容寻址数据。active snapshot pointer 的业务 API 和原子切换仍属于 M3-C，不能仅凭底层状态 trigger 声称已完成。

### 6. exact 使用 `<#>`，显式恢复 inner-product score 方向

pgvector `<#>` 返回 negative inner product distance。exact repository 使用：

`score = -distance`

对外统一成越大越好的 inner-product score，并将 `-0.0` 规范为 `0.0`。trace 同时记录 raw score、pgvector distance、operator 与 score kind，避免未来把距离和相似度方向混用。

SQL 排序为 distance 升序、`snapshot_ordinal` 升序、`chunk_id` 升序。RRF、adaptive merge 与 rerank 也使用稳定次级键。同 query vector、snapshot、profile 与候选集合的 pgvector exact 顺序和 score 必须与 NumPy dense 基线在明确浮点容限内一致。

### 7. 一个 immutable typed boundary 贯穿 SQL、融合、回退与生成

`RetrievalBoundary` 是 frozen typed object，至少绑定 `scope_id`、`snapshot_id` 和 `profile_id`，并可携带 `law_ids`、`version_ids`、`article_ids`、`article_numbers` 与 `[valid_from, valid_to)` 的 `effective_on`。

hard selector 的执行规则是：

- exact SQL 在排序和 `LIMIT` 前应用 scope、serviceable snapshot、validated receipt、profile 与 article relation 条件。
- BM25 只在 PostgreSQL 先加载出的 bounded corpus 上建索引，不允许全库检索后补过滤。
- 空 selector tuple 明确 deny-all；非法或重复 selector、非法日期和非法 `top_k` 在检索前失败关闭。
- 多 article chunk 必须至少有一条关系匹配，并且所有关联 article 都满足完整组合 selector；任一关系不匹配则拒绝整个 chunk，避免暴露混合文本。

boundary fingerprint 只用于 trace 与 artifact 一致性，不是授权 token。运行时信任来自完整 typed boundary 与 typed provenance 的结构比较。

### 8. typed provenance 是每条 bound result 的强制组成部分

`RetrievalProvenance` 保存完整 boundary、scope/snapshot/profile、chunk ID、chunk content hash、重新水合后的 payload hash、snapshot ordinal、embedding hash 和有序 article provenance。

数据库关系是展示 metadata 的权威来源。repository 在读取时重算 chunk payload 和持久化 float32 vector hash；text、metadata、关系或向量漂移都会拒绝结果。可变 `Chunk.metadata` 和 trace 不能替代 provenance。

下游闭包规则如下：

- RRF 的 BM25/dense 叶子必须同时 unbound，或共享完全相同的 typed boundary。
- RRF、adaptive direct/multi-plan/follow-up、rerank 在每次输入、合并和输出处复验 provenance。
- 同一 `chunk_id` 若携带不同 payload 或 provenance，拒绝合并，不按较高分数覆盖。
- chat 捕获 retrieval boundary，对 evidence 做 defensive copy，并在 staging、generation 和 verification 前复验；任何 completion 调用前必须先通过边界检查。

### 9. artifact 采用追加式兼容，bound 数据不得静默降级

search result、retrieved turn 与后续 chat stage artifact 序列化完整 typed boundary/provenance，并在恢复时重新执行结构和 compatibility metadata 校验。

M2 既有 unbound artifact 与 hash chain 保持可读。兼容 decoder 不会把 legacy unbound 数据自动升级为可信 bound 数据；一旦 artifact 带 boundary marker，缺少完整 provenance、boundary 不一致、payload hash 漂移或伪造 trace 都会失败关闭。空结果的 bound retrieval 也显式保存捕获边界。

### 10. migration 与新增 retrieval modules 都是分发物

Alembic environment、template、`0001`、`0002` 和新增 retrieval contract/storage modules 必须进入 wheel。CI 在 fresh PostgreSQL database 上升级到 head，执行真实 integration tests，再构建 wheel 并核对资源可发现性。

### 11. exact 优先，ANN、activation 与最终发布门禁后置

模块 B 只接受 exact correctness 与 mandatory boundary closure。ANN 是 M3-D 的可关闭优化，必须按 profile/dimension 隔离，并独立验证过滤 underfill 的解释状态或受控 exact fallback。法名 + 条号 + 版本查询和 active snapshot 原子激活/回滚属于 M3-C。

因此：

- M3-T02 核心 exact 等价为 `passed / accepted for module B`，最终 release candidate 仍需累计复验。
- M3-T04 的 exact/BM25/RRF/adaptive/rerank/chat 边界闭包为 `passed / accepted for module B`，最终 release candidate 仍需累计复验。
- M3-T06 的 query profile/dimension/normalization mismatch 已补齐模块 B 路径，但 M3 总体仍为 partial，不能提前完成。
- M3-T01、M3-T07、M3-T08 保持 partial；M3-T03、M3-T05 保持 pending。

## 被否决的替代方案

1. 直接复用文件名或法名作为法律版本 ID：同标题多版本会产生歧义，无法可靠过滤、回滚或精确查条。
2. 将 embedding 模型身份并入 corpus hash：会把同一语料的多 profile 导入误判成多份语料。
3. 遇到重复 ID 直接 upsert 覆盖：会破坏不可变快照、隐藏来源漂移，也无法审计失败导入。
4. 只检查声明 hash，不检查数据库实际行：历史损坏或旁路写入可能保留旧 hash，必须回读 payload 与向量。
5. 直接把 `<#>` 值当作相似度：会反转排序语义，使离线 dense 对照产生假结论。
6. 先全局 top-k 再应用 filter：目标 scope/version 的合法结果可能已被越界候选挤出，并造成错误 underfill。
7. 多 article chunk 只要任一 article 匹配就返回：会把同一 chunk 中不匹配法条的文本一并披露。
8. 只在 trace 写 boundary fingerprint：trace 和 metadata 是可变兼容字段，不能证明结果确实来自该 boundary。
9. 让 RRF、adaptive 或 reranker 信任上游且不复验：合并、去重或 fallback 可能重新引入越界或冲突 payload。
10. 用分支名、tag 或任意 revision 字符串绑定在线 HF 模型：引用可移动；version-bound serving 必须使用不可变 commit SHA，且关闭 remote code。
11. 先启用 HNSW/IVFFlat 再测正确性：ANN 误差、过滤 underfill 与 score 方向会混在一起，难以定位。
12. 用单元 fake 代替 PostgreSQL：无法覆盖 extension、DDL、trigger、事务、`<#>`、并发和真实 pgvector 类型行为。

## 后果与限制

正面结果：

- corpus、法律版本、chunk、embedding profile、import receipt 和 query encoder 都有可重算身份。
- exact score 方向、稳定 tie 与旧 dense 等价有真实 PostgreSQL 证据。
- hard filter 在 `LIMIT` 前生效，并通过 typed provenance 闭合到 BM25、RRF、adaptive、rerank、chat 和 artifact。
- migration `0002` 将应用层不可变假设提升成数据库约束；读取仍会重算 payload/vector hash 作为纵深防护。
- M2 unbound 路径与 artifact 可继续使用，数据库能力仍是 optional profile。

当前代价与限制：

- 导入使用全局事务 advisory lock，管理路径吞吐有限；当前优先保证确定性。
- exact 查询会回读向量并重算 hash，优先保证可核验性，尚未优化成高吞吐 serving 路径。
- 模块 B 尚未提供法名 + 条号产品查询、active pointer API、ANN index、服务级重启证明或最终 M3 cumulative gate。
- fresh database 已执行 `0001` 到 `0002` 的完整链，Engine reconnect 后 exact 检索已通过；带既有数据的上一版升级与 PostgreSQL 服务进程 restart 仍待 M3-D。
- 本地使用 pgvector 0.8.1，canonical CI 使用锁定的 pgvector 0.8.6；两类证据分别记录。

## 验证

### 本地

- 完整默认回归：`612 passed, 157 subtests passed`。
- M3 PostgreSQL integration：`16 passed`。
- M3-B targeted offline/contracts：`210 passed`。
- wheel：`60` entries，包含 Alembic `0001`/`0002` 与新增 retrieval modules。
- `uv lock --check`、targeted Ruff format/check、`git diff --check`：passed。
- 真实模型、远程 embedding、reranker、Judge 和付费调用：0。

### CI

- 精确 head：`4bc912b09931803d53b4ca3ffdc81d302ba65813`
- [GitHub Actions run 36045040845](https://github.com/1040942669/legal-rag-agent/actions/runs/36045040845)：`success`
- `offline / Python 3.12.13`：success；继续运行现有 M2 cumulative offline gate，不等于 M3 最终 gate。
- `m3 storage / PostgreSQL 18 + pgvector 0.8.6`：success；fresh migration、16 项 integration tests、wheel build 和 resource check 均通过。
- quality-gate artifact：`sha256:77be3dd106aabf0999c1d84519451bdaff4b7a88c3a68c32421d682702aafdca`
- storage artifact：`sha256:4f06ece14c2b178552b2ccacd285aa7120d202dcf527ae0369b219eb6e5fe113`

## 审查结论与后续优化

当前审查无 P0、无 P1。有两个非阻塞 P2：

1. snapshot 在 retriever 构造后被 archive 时，查询期 repository 仍会拒绝结果，但当前 `retrieve()` 可能先完成一次 encoder 调用。后续可在编码前增加轻量 serviceability preflight，避免无效 provider 计算。
2. 关系闭包 trigger 可能锁定多个父 snapshot。后续可在 `FOR UPDATE` 前按 `snapshot_id` 显式 `ORDER BY`，使用确定锁顺序进一步降低并发死锁概率。

这两个 P2 不允许被写成 P0/P1，也不改变模块 B 已通过 exact/filter 核心验收的事实；它们需要进入后续工程优化记录并在最终 gate 中复查。

## 发布边界

- 模块 A：accepted and pushed。
- 模块 B：accepted and pushed。
- M3：`in_progress`。
- PR #13：draft，尚未合并。
- M3-C / M3-D：尚未完成。
- `v0.4.0` Tag / GitHub Release：不存在。
- 本 ADR 不构成发布回执，本次不得创建 M3 发布回执。
