# ADR-003: M5 durable harness, budget ledger, and explicit recovery

- Status: Accepted, shipped in `v0.6.0`, independently receipted, and fully released
- Date: 2026-09-29
- Affected milestone and planned version: M5 / `v0.6.0`
- Base: `061ffcbd75a5305d1bbf61b02dd4a1eef6d37bac`
- Final software head: `aa737e8d1f77214277c0544ce069d36c2b2161ff`
- Release target: `832acaafaf5633e76daed7a62a73755187fca51e`
- Tracking: [Issue #21](https://github.com/1040942669/legal-rag-agent/issues/21), GitHub [Milestone 6](https://github.com/1040942669/legal-rag-agent/milestone/6) for M5, merged software [PR #22](https://github.com/1040942669/legal-rag-agent/pull/22), and merged receipt [PR #23](https://github.com/1040942669/legal-rag-agent/pull/23)

This ADR records the architecture shipped by PR #22 and the published `v0.6.0` software release. Exact PR-head and release-target master CI, the annotated Tag, and the non-draft GitHub Release are verified. Independent receipt PR #23 also passed exact final-head and merge-target master CI before Issue #21 and Milestone 6 were closed, so the milestone state is fully `released`.

## Context

M4 made sessions, run identity, frozen retrieval boundaries, events, results, and lease ownership durable in PostgreSQL. A process that disappeared while running work was conservatively classified as `interrupted`. M4 deliberately did not claim that it could restore a node, reconcile an in-flight provider call, or continue a graph without repeating prior work.

M5 must enable explicit continuation while preserving every M4 safety boundary. Recovery introduces failure windows that a normal request/response execution does not have:

- a process can stop after retrieval is checkpointed but before generation starts;
- an external request can be reserved, dispatched, completed remotely, or returned locally without its result becoming durable;
- an answer can be committed before the graph writes its final framework checkpoint;
- two application processes can try to resume the same run;
- an old worker can write a framework checkpoint after losing its application lease;
- a resume can occur after the original absolute deadline;
- a stale or incompatible checkpoint can appear valid enough for a framework to load but unsafe for the application to continue;
- evidence can contain text that asks the model to change scope, call an unauthorized tool, or disclose secrets;
- a clarification turn can accidentally mutate or reset the budget of its parent run;
- a process-local saver can make a demo look resumable while losing all state across real process replacement.

The design therefore needs both a workflow checkpoint mechanism and an application transaction model. A framework checkpoint alone is not proof that a business result, budget debit, lease, or provider outcome is safe.

## Options considered

| Option | What it solves | Cost and failure boundary | Decision |
|---|---|---|---|
| A. Keep the M4 executor and restart every interrupted run from the beginning | Minimal new runtime code | Repeats retrieval and model work, resets process-local limits, cannot distinguish unknown provider outcomes | Rejected |
| B. Use LangGraph with `InMemorySaver` | Convenient local graph iteration and same-process demonstrations | State disappears on process replacement; cannot meet T01, T02, T03, or T10 | Rejected for durable execution |
| C. Use PostgreSQL `PostgresSaver` plus an application checkpoint projection, durable ledger, attempt journal, and lease fencing | Cross-process node recovery with explicit business trust boundary, budget persistence, and attributable unknown outcomes | More schema and reconciliation logic; still cannot make arbitrary external providers exactly-once | Chosen |
| D. Introduce a distributed queue and transactional outbox for all graph nodes | Adds independent worker scheduling and delivery infrastructure | Expands into M6 before the recovery contract is stable; queue delivery still does not make a provider side effect exactly-once | Deferred |

## Decision

### 1. LangGraph is the single bounded controller

M5 uses one graph version, `m5-bounded-v1`, with the following node order:

```text
analyze_query -> route -> retrieve -> merge_evidence -> check_evidence
    -> [plan_followup -> retrieve]
    -> generate -> verify -> persist_result
```

The bracketed follow-up branch can execute only while the durable limits permit it. LangGraph controls transitions; it does not reimplement query analysis, retrieval, evidence merging, answer generation, or verification algorithms.

The legacy adaptive switches are rejected for an M5 executor because a second planner or retry loop would bypass the graph ledger. Unaccounted LLM condensation is also rejected. Each model, embedding, or tool operation must pass through the same budget and attempt contracts.

The default limits are two retrieval rounds, three queries per round, eight tool calls, four model calls, four embedding calls, one retry per external operation, a 90-second absolute deadline, and retrieval `top_k=5`.

### 2. Graph state is a closed, versioned JSON value

The graph state contract has schema version `1` and an exact field set. It accepts only bounded JSON data and rejects non-finite numbers, unknown fields, recursive sensitive names, executable objects, clients, connections, prompts, credentials, and raw drafts.

Run identity, user, session, frozen scope, snapshot, profile, graph version, limit configuration, and absolute deadline are immutable across a node transition. The state is revalidated after each node. `answer_draft_ref` remains null under the current contract because unverified draft material must not enter durable checkpoints.

The application payload is stored under a single LangGraph `payload` channel. This avoids collision with framework-reserved checkpoint identifiers and keeps the application schema independently versioned.

### 3. Persistent PostgreSQL saving is mandatory

Durable M5 execution accepts only LangGraph's PostgreSQL saver. `InMemorySaver` is rejected at configuration and acceptance boundaries even when a same-process run would appear to work.

The serializer uses strict JSON-compatible behavior without pickle fallback. This prevents an unexpected Python object from turning the checkpoint store into an executable deserialization boundary.

LangGraph owns its private checkpoint tables. The application owns `runs.last_checkpoint_*` and `run_checkpoints`, which project a trusted checkpoint identity, hash, schema version, graph version, last completed node, and lease epoch. The two stores have different responsibilities:

- the framework store reconstructs graph channels and pending transitions;
- the application projection determines which framework checkpoint a run is allowed to trust.

Each lease receives an isolated framework thread identity. LangGraph's root namespace is currently empty, so the application namespace remains explicit in the projection. A stale worker may leave an orphan checkpoint in its old framework thread, but it cannot advance the run's trusted pointer after losing the application lease.

### 4. Budgets and external attempts are business records

`run_budget_ledgers` stores shared counters for rounds, queries, tools, models, embeddings, and retries. A restart reads these counters rather than reconstructing allowance from graph position.

Every external operation reserves its budget in a short PostgreSQL transaction before dispatch. The corresponding journal record moves through the following states:

- `reserved`
- `dispatched`
- `succeeded`
- `failed`
- `outcome_unknown`
- `abandoned_before_dispatch`

A reservation is not refunded after a crash. This prevents a restart from converting process failure into extra model or tool budget.

A stale `reserved` attempt with no dispatch evidence becomes `abandoned_before_dispatch`. A stale `dispatched` attempt becomes `outcome_unknown`. A successful provider result that is not strictly older than the latest trusted application checkpoint is also treated as unknown with reason `provider_result_not_durable`. Equality is not accepted as proof that the result was checkpointed.

### 5. Unknown external outcomes do not silently retry

The runtime does not claim exactly-once execution for arbitrary providers. It cannot atomically commit a remote side effect and a local PostgreSQL checkpoint.

For an unknown model or planner result, the default policy consumes the reserved budget, records the uncertainty, and stops with `completed_with_limits` and an explicit reason instead of silently issuing another request. The API and report can disclose possible duplicate cost. They must not claim that cancellation revoked remote work or that no duplicate provider charge is possible.

Normal retry policy is separate from crash recovery. HTTP 429 and bounded timeout classes may retry once when their configured budget permits. HTTP 400 and 401 are terminal and are not retried blindly.

### 6. Resume is explicit and preserves the original deadline

Only an owned M5 run in `interrupted` state can request resume. Repeating the same request is idempotent. The service validates the checkpoint schema, graph version, identity fields, hash, and trusted pointer before making the run claimable. A failed compatibility check leaves the checkpoint pointer and resume request unchanged.

Resume never resets the absolute deadline or durable budget. If the deadline has elapsed, the executor makes no new external dispatch and resolves the run as `completed_with_limits` with an attributable stop reason.

Automatic resume is not the default. A process restart can classify stale work, but a user or controlled caller must explicitly authorize continuation after cost or outcome uncertainty.

### 7. Lease epoch fences checkpoint and terminal writes

Every claim increments `lease_epoch`. Heartbeats renew only the same owner and epoch. Checkpoint publication, stage events, attempt reconciliation, and terminal publication verify the current owner, epoch, run revision, event sequence, lease, and PostgreSQL wall clock.

Two processes can contend for a resume, but only one can retain a valid epoch. The losing or expired process cannot change the business checkpoint pointer or publish a terminal result, even if it writes an orphan framework checkpoint.

After an immutable result, assistant message, and final event have committed, the same epoch may still write the final graph checkpoint. This narrow rule lets the framework finish without reopening the already committed business result. Any other epoch remains fenced.

### 8. Recovery rules depend on the durable boundary

M5 distinguishes three principal crash windows:

1. Retrieval checkpoint committed before generation. Resume loads the trusted checkpoint and does not repeat retrieval.
2. Provider or planner dispatched, returned, or recorded success before its result was durably checkpointed. Recovery marks the attempt unknown and does not refund budget or silently repeat the call.
3. Final business publication committed before the graph final checkpoint. Recovery reconciles the unique existing result and never creates a second answer.

This distinction is why a generic “retry the node” rule is not acceptable.

### 9. Clarification creates a new lineage-bound run

`needs_clarification` is a terminal answer-bearing result. A user's follow-up creates a new run in the same session and may reference the prior run through `parent_run_id`.

The parent must be owned by the same user, belong to the same session, and be in an allowed answer-bearing terminal state. The child receives an independent ledger and deadline. It can include the safe parent result in history, but it cannot reset or mutate the parent's consumed budget.

Requests without a parent retain the M4 canonical request hash. Requests with a parent bind both the canonical request and parent identity, so one idempotency key cannot alias two different lineages.

### 10. Tools are closed and server-bound

The graph exposes only four read-only tools:

- `search_laws`
- `get_article`
- `get_neighbors`
- `inspect_evidence_metadata`

Authenticated user, scope, snapshot, profile, database connection, SQL, and retrieval boundary come from trusted server state. A model cannot override them in tool arguments. Evidence is untrusted data; instructions embedded inside it cannot register a tool, widen the frozen scope, reveal a prompt or secret, or write to the database.

### 11. M4 compatibility remains explicit

The old `LegalChatRunExecutor` remains available for non-M5 graph versions. The API enables resume only for compatible M5 runs; an old M4 run keeps the explicit unsupported behavior. The existing CLI remains independent of the optional service stack.

`legal-rag-api --migrate` runs Alembic through `0006_m5_harness_recovery` and then initializes the PostgreSQL saver schema. Starting without `--migrate` validates the configured checkpointer and fails readiness if it is missing or incompatible; it does not silently fall back to memory.

The default M5 service executor remains provider-free with `generate=False`. Release-candidate verification therefore exercises persistence, recovery, and safety without an unbudgeted live model call.

## Rejected alternatives

1. Restart every interrupted run from `analyze_query`. It duplicates completed retrieval and can exceed the user's original model/tool allowance.
2. Store counters only in graph state. A stale or orphan framework checkpoint could then restore obsolete allowance; the ledger must be transactional application data.
3. Refund a reservation when the worker disappears. Once dispatch may have happened, a refund can create hidden duplicate cost.
4. Treat provider return as durable success. A local return value can disappear before artifact or checkpoint commit.
5. Retry every unknown attempt. This turns crash recovery into unbounded duplicate external side effects.
6. Use framework checkpoint time alone to order provider results. Equality and cross-store timing do not prove a business transaction happened first.
7. Let the newest framework checkpoint win. A stale lease can write after a new owner exists; only the application pointer is trusted.
8. Share one framework thread across lease epochs. Orphan writes from an old owner would be harder to isolate and reason about.
9. Reset deadline on resume. Repeated resume calls would create an unbounded execution lifetime.
10. Resume automatically at service startup. The operator or user would lose the chance to assess an unknown cost or side effect.
11. Reopen a `needs_clarification` run. This would rewrite a terminal audit record and mix two user inputs under one run identity.
12. Allow the model to select arbitrary tools or tool boundary fields. Evidence prompt injection could escape the frozen authorization and corpus scope.
13. Put raw drafts, prompts, clients, or credentials in checkpoint state. Persistent recovery data would become a disclosure and deserialization hazard.
14. Treat LangGraph checkpointing as the application's transactional truth. Framework state does not atomically publish a result, debit a provider budget, or own the service lease.

## Validation and evidence

Release validation is recorded in `reports/refactor/M5.md` and `docs/refactor/receipts/M5.json`:

- local exact candidate: 921 passed and 157 subtests passed;
- final software head `aa737e8...`: exact-head CI run 36497021956, all three jobs successful;
- release target `832acaaf...`: master CI run 36498443123, all three jobs successful;
- M5 suite: 81 tests, 0 failures, 0 errors, 0 skipped on both release paths;
- M0-M5 cumulative gate: 51/51;
- M5-T01 through M5-T10: all passed with a closed exact-SHA fault receipt;
- real PostgreSQL 18, pgvector 0.8.6, persistent PostgreSQL checkpointer, service restart, and migration head `0006_m5_harness_recovery`;
- one-command recovery demo and isolated M5 wheel probe: passed;
- annotated `v0.6.0` Tag object `c0ef0721...` peeled to the verified release target;
- independent receipt PR #23 final head `9dd6ec3...`: exact-head CI run 36501403167, all three jobs successful;
- receipt PR #23 normally merged at `2026-09-29T00:13:59Z` as target `3436e9a...`: master CI run 36502063863, all three jobs successful;
- Issue #21 closed at `2026-09-29T00:23:07Z` and Milestone 6 closed at `2026-09-29T00:23:22Z` after the receipt merge-target CI;
- live or paid model, remote embedding, reranker, and judge calls: 0.

An earlier run on `7ba6ae8...` exposed and preserved a legacy M4 fixture readiness regression. The repair pinned that legacy fixture to `m4-linear-v1` without weakening M5 fail-closed production readiness. The final PR-head and release-target runs passed the repaired M4 path.

## Consequences and known limits

Positive consequences:

- retrieval work can survive a hard process stop and be reused by an explicit resume;
- deadlines and all major external-operation budgets survive process replacement;
- an unknown provider result has an explicit durable status instead of being disguised as a safe retry;
- stale workers cannot advance the business checkpoint pointer or publish an answer;
- an already committed final result is reconciled instead of generated twice;
- prompt injection cannot expand the server-defined tool or retrieval boundary;
- clarification preserves the parent audit record and receives a new bounded execution identity.

Costs and limits:

- two PostgreSQL persistence layers must be operated and reconciled: framework checkpoint tables and application service tables;
- a framework checkpoint write and a business transaction are not globally atomic;
- conservative unknown-outcome handling can stop a run that might have been safe to retry;
- external provider exactly-once, remote cancellation, and zero duplicate billing remain impossible to promise without provider cooperation;
- the supervisor is not a general distributed work queue;
- migration downgrade can destroy M5 recovery state and is not a normal rollback path for valued data;
- tests establish infrastructure contracts, not legal correctness, current-law completeness, model quality, or production capacity.

## Rollback and replacement conditions

- Revert or repair through a new PR. Do not force-push shared history or move an existing release tag.
- New work may select the stable legacy executor by graph version. An existing M5 run must use a compatible runner, be explicitly terminated, or go through a versioned migration.
- If PostgreSQL checkpoint readiness fails, stop accepting M5 recovery work. Do not downgrade silently to `InMemorySaver`.
- Preserve attempt journals and consumed budgets for unknown outcomes. Do not erase them to make a retry appear free.
- Prefer a forward schema fix. Before any `0006` downgrade, back up or export valued run state and obtain an explicit migration plan. The downgrade rejects known M5-only terminal statuses and events but otherwise still removes M5 application tables and columns.
- A future M6 queue must preserve the M5 lease epoch, checkpoint pointer, budget, attempt, and terminal publication invariants. Broker acknowledgement is not a substitute for these fences.

## Release boundary

The software release boundary is complete:

- PR #22 was normally merged without admin, auto, squash, rebase, or force behavior;
- final exact-head and release-target master CI both passed all three jobs;
- annotated `v0.6.0` and a non-draft, non-prerelease GitHub Release exist and are remotely verified;
- `docs/refactor/receipts/M5.json` records the software release facts and immutable artifact hashes.

The governance boundary is also complete:

- independent receipt PR #23 final head passed all three exact-head jobs;
- PR #23 was normally merged as `3436e9ad41c7455aa5f31117ab7ece3f4ea847c1`, without admin, auto, squash, rebase, or force behavior;
- that exact receipt merge target passed all three master jobs;
- Issue #21 and Milestone 6 were then closed in order;
- the documentation-only finalization is non-recursive, does not add product behavior, and does not move `v0.6.0` from the software release target.

M5 is therefore fully `released`. After the finalization PR itself passes exact-head CI, is normally merged, and its exact merge target passes master CI, execution stops with M6 still `not_started`.

## Sources

- [M5 master plan](../MASTER_PLAN.md), especially M5-T01 through M5-T10 and the release state machine
- `legal_rag/harness/state.py`
- `legal_rag/harness/budget.py`
- `legal_rag/harness/checkpoint.py`
- `legal_rag/harness/tools.py`
- `legal_rag/harness/graph.py`
- `legal_rag/harness/nodes.py`
- `legal_rag/harness/runner.py`
- `legal_rag/services/run_service.py`
- `legal_rag/services/supervisor.py`
- `legal_rag/storage/alembic/versions/0006_m5_harness_recovery.py`
- `integration_tests/m5_support.py`
- `integration_tests/test_m5_*.py`
- `scripts/m5_recovery_demo.py`
- `scripts/release_wheel_probe.py`
- `scripts/quality_gate.py`
- `.github/workflows/quality-gate.yml`
