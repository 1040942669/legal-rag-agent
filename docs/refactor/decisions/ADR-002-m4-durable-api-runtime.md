# ADR-002: M4 durable API runtime and fenced single-process supervisor

- Status: Accepted and shipped in `v0.5.0`; milestone governance state is `released_receipt_pending` until the independent receipt and closure sequence completes
- Date: 2026-09-29
- Affected milestone and planned version: M4 / `v0.5.0`
- Base: `61f1065fc6d5678d0d57114e2e361294d04906b4`
- Verified implementation head: `43e6a506bd62bb0d02395cc3397801815a01bd16`
- Tracking: [Issue #16](https://github.com/1040942669/legal-rag-agent/issues/16), [Milestone 5](https://github.com/1040942669/legal-rag-agent/milestone/5), merged [PR #17](https://github.com/1040942669/legal-rag-agent/pull/17), stabilization [PR #18](https://github.com/1040942669/legal-rag-agent/pull/18), receipt [PR #19](https://github.com/1040942669/legal-rag-agent/pull/19)

This ADR originally accepted the implemented M4 architecture as a release candidate. The release outcome recorded below preserves that decision history: PR #17 and the test-only stabilization PR #18 were normally merged, exact release-target CI passed, and annotated `v0.5.0` plus its GitHub Release were published. The independent receipt/governance sequence remains deliberately separate and must complete before the milestone is marked fully `released`.

## Context

M3 established versioned PostgreSQL/pgvector corpus storage, immutable activation history, exact retrieval, and a typed retrieval boundary. It did not provide an authenticated HTTP service or a durable task lifecycle. A service layer introduces failure modes that an in-process chat call does not have:

- two users must not enumerate or operate each other's sessions, runs, events, or evidence;
- concurrent retries must not create duplicate user messages, runs, or final answers;
- an active snapshot can change while a request is queued or running;
- an application process can stop after accepting work but before publishing a result;
- a timed-out provider call can keep running even after its delivery deadline;
- an SSE client can disconnect immediately before a terminal event is committed;
- a database row lock can delay a worker until after its lease has expired;
- an unverified generation draft must not cross the HTTP, event, result, message, or log boundary.

M4 needs durable acceptance and observable progress without claiming the M5 guarantee of checkpointed, exact cross-process execution recovery. It also must preserve the existing `legal-rag` CLI: installing or invoking the legacy CLI must not require FastAPI, service configuration, or a running database.

## Options considered

| Option | What it solves | Cost and failure boundary | Decision |
|---|---|---|---|
| A. FastAPI background tasks with in-memory sessions and status | Small implementation and low initial latency | Accepted work, idempotency state, history, and events disappear with the process; multiple workers diverge | Rejected |
| B. PostgreSQL source of truth with a bounded single-process supervisor | Durable sessions, atomic idempotency, ordered events, ownership, restart classification, and explicit lease fences | Does not provide distributed throughput or exact provider-call recovery | Chosen for M4 |
| C. Distributed queue plus checkpointed graph execution | Multi-worker scheduling and resumable workflow state | Expands scope into M5/M6, adds broker and delivery semantics before the service contract is stable | Deferred |

## Decision

### 1. PostgreSQL is the service source of truth

The service persists `sessions`, `runs`, `messages`, `run_results`, `idempotency_keys`, and `run_events` through Alembic revision `0005_m4_api_sessions`. The supervisor's wake event is only a latency optimization. It is not task ownership, durable state, or recovery evidence.

Every owned read includes the authenticated `user_id`. A resource that does not exist and a resource owned by another user produce the same non-enumerating not-found behavior. Bearer credentials map to a server-controlled `(user_id, scope_id, profile_id)` principal; URL/query credentials and identity overrides are rejected.

### 2. Transactions are short and align with durable state transitions

Database transactions protect state transitions and their corresponding records. They do not remain open while retrieval, generation, verification, or another external provider call executes.

Run creation locks the owned session row, checks idempotency before the active-run invariant, freezes the serving configuration, and atomically inserts:

- the queued run;
- one user message;
- the idempotency binding;
- the first `run.queued` event.

Successful publication atomically writes the safe result, final assistant message, terminal `answer.final` event, run status, event sequence, and revision. Failure and cancellation likewise update the state and append their terminal event in the same short transaction. A client therefore cannot observe a terminal run without the corresponding durable terminal record.

### 3. Idempotency and one-active-run are database invariants

An idempotency key is scoped by the composite primary key `(user_id, session_id, idempotency_key)` and binds a canonical request hash to one run for a finite TTL.

- Same key and same canonical request returns the existing run and sets the API replay indicator.
- Same key and a different request fails with an idempotency conflict.
- Expired keys can be removed only inside the serialized session transaction before reuse.

The application checks for an existing active run to return an attributable conflict, but correctness does not rely on that check. PostgreSQL also has the partial unique index `uq_runs_one_active_per_session`, covering statuses `queued`, `running`, and `interrupted`. Concurrent different keys therefore cannot create two active runs even if both application checks race. `interrupted` deliberately continues to occupy the active slot in M4 until the owner cancels it.

### 4. Each run freezes its complete execution boundary

Run acceptance locks and validates the active snapshot pointer and validated embedding import, then persists:

- authenticated user and scope;
- `snapshot_id`, activation `revision`, and `activation_id`;
- `profile_id`;
- typed retrieval boundary fingerprint;
- retrieval configuration hash;
- graph version;
- canonical request payload and request hash.

A later snapshot activation does not retarget an already accepted run. The worker loads only this frozen input plus bounded history from successfully completed turns. A request for a non-active snapshot or a profile without a validated import fails before work is accepted.

### 5. Worker delivery is fenced by owner, revision, sequence, lease, and database wall clock

The M4 supervisor is intentionally single-process. It claims queued work in a short transaction, assigns a finite `lease_owner` and `lease_expires_at`, and advances the run revision and event sequence. Every stage event and terminal publication re-locks the run and verifies the current worker lease before writing.

State transitions use optimistic compare-and-swap on the previous `revision` and `event_sequence`. A cancellation, stale recovery, or competing terminal transition therefore fences a late callback even if the original execution thread is still running.

Lease checks use PostgreSQL `clock_timestamp()`, not transaction-scoped `now()`. PostgreSQL freezes `now()` at transaction start; a worker waiting on a row lock could otherwise acquire the lock after expiry and still be evaluated against an obsolete timestamp. The real PostgreSQL regression holds the run row past a one-second lease and proves that the delayed worker cannot publish.

### 6. Restart recovery is classification, not fabricated resume

At startup and periodically, the supervisor drains stale recovery in bounded batches before claiming new work. Only `running` rows whose lease has expired are moved to `interrupted` with a durable terminal event. Completed history and results remain readable after both a real PostgreSQL service restart and an independent application process restart.

M4 does not reconstruct an in-flight provider call, node-local state, or side effect. `POST /api/v1/runs/{id}/resume` first enforces ownership and then returns an explicit `resume_not_supported_until_m5` response. The owner must cancel an interrupted run before starting a replacement. LangGraph checkpoints and exact node-level recovery remain M5 work.

### 7. SSE publishes replayable safe events, not generation drafts

Run events use a per-run monotonic sequence stored in PostgreSQL. SSE uses that sequence as the event ID and supports replay after `Last-Event-ID` or the validated `after` cursor. Disconnecting and reconnecting reads an ordered suffix and never creates another run.

Persisted event types and their payloads use closed allowlists. Stage events expose bounded counts, stable status, and safe reason fields. They do not expose raw model output, credentials, prompts, draft text, arbitrary exception text, or an unverified answer.

The stream does not terminate merely because a run status became terminal. If a terminal transition races with an empty event poll, the stream compares its cursor with the durable `event_sequence`, refetches, and emits the missing terminal event before closing.

An answer is publishable only when the safe verification payload reports `passed=true`. A rejected pre-fallback draft is not inserted into `run_results`, `messages`, `run_events`, HTTP responses, or normal diagnostics.

### 8. Timeout and shutdown favor fencing over unsafe thread termination

Python cannot safely terminate an arbitrary thread that is blocked inside a provider library. When execution exceeds its deadline or shutdown begins:

- the callback delivery token is cleared immediately;
- the thread is quarantined;
- later callbacks and terminal publication are rejected by the callback token and database lease/revision fence;
- no additional work is claimed after the bounded quarantine capacity is exhausted;
- readiness becomes false when that capacity is exhausted.

`stop()` returns false while the supervisor loop, active execution, or a quarantined execution remains alive. The FastAPI lifespan disposes the SQLAlchemy engine only after the supervisor confirms a complete stop. If shutdown is incomplete, the engine remains valid for a still-draining thread and the process records an incomplete shutdown state rather than creating a use-after-dispose race.

Cancellation is therefore a durable delivery guarantee, not a claim that an external provider computation was physically revoked. The API returns `provider_revoked=false`.

### 9. Database failure and resource limits are explicit service states

The service configures bounded connect, pool, statement, and lock timeouts. Readiness checks the database, migration head, pgvector availability, active snapshot/profile configuration, and supervisor recovery state. Database failures return a stable redacted error envelope rather than connection details. Input length, event cursor, idempotency key, pagination, history size, and payload size are bounded before expensive work or persistence.

### 10. The service remains optional and provider-free by default

FastAPI, Uvicorn, SQLAlchemy, Alembic, psycopg, and pgvector remain in the optional `service` dependency group. The new `legal-rag-api` entry point imports them lazily. The existing `legal-rag` CLI can still import and display help without service dependencies, API configuration, or a database process.

The production wiring exercised by M4 uses the real PostgreSQL corpus/retrieval path but constructs `LegalChatRunExecutor(generate=False)`. Candidate verification does not call a live generation model, remote embedding model, reranker, LLM judge, or paid API.

## Rejected alternatives

1. Store session and run state only in the FastAPI process. This loses accepted work and event history on restart and cannot support deterministic idempotency.
2. Hold a database transaction open across retrieval or model execution. This turns provider latency into lock duration, pool starvation, and an ambiguous retry boundary.
3. Treat the application pre-check as sufficient for one-active-run. Concurrent requests can both pass that check; the partial unique index is the final invariant.
4. Scope an idempotency key globally or only by user. A caller should be able to reuse a human-generated key in a different session without cross-session aliasing.
5. Use PostgreSQL `now()` for lease validation. Its transaction-start semantics can authorize a worker after its real lease deadline when row-lock waiting is involved.
6. Stream raw tokens or draft answers and retract them after verification. Retraction cannot undo disclosure, so only safe stage summaries and verified final answers cross the service boundary.
7. Mark every non-terminal run interrupted on startup. A non-expired lease may still belong to a live worker; recovery only acts after the lease is actually stale.
8. Automatically retry or resume interrupted work. Provider side effects are not exactly-once, and M4 has no node checkpoint proving a safe resume point.
9. Kill timed-out execution threads. Python provides no safe general thread termination mechanism; fencing and bounded quarantine preserve state correctness.
10. Allow unlimited timed-out threads while continuing to claim work. This converts provider hangs into unbounded resource growth, so readiness fails closed at the quarantine limit.
11. Introduce Redis, Celery, or a distributed scheduler in M4. Those mechanisms belong to a later scale and observability milestone after the durable API contract is fixed.
12. Use live models to make service tests look realistic. M4 accepts infrastructure and state-machine behavior, not a new legal-quality claim; deterministic provider-free execution gives reproducible evidence without budget or privacy expansion.

## Validation and evidence

The exact verified implementation head is `43e6a506bd62bb0d02395cc3397801815a01bd16`.

- Full default test suite: `801 passed, 157 subtests passed`.
- Real PostgreSQL/pgvector integration suite: `79 passed`.
- Cumulative M0-M4 gate: `41/41` mandatory records passed.
- M4-T01 through M4-T08 cover owner isolation, concurrent idempotency, one-active-run conflicts, process restart, ordered SSE replay, rejected-draft containment, attributable failures and lease expiry, production wiring, and legacy CLI isolation.
- A real PostgreSQL service stop/start preserved migration `0005_m4_api_sessions`, corpus state, active pointer, exact retrieval, catalog behavior, and ANN receipt verification.
- Two independently started application processes proved that completed history/results survive, an expired running lease becomes `interrupted`, and cancellation allows a new replacement run.
- Live or paid model, embedding, reranker, and judge calls: `0`.

These tests establish the service, persistence, concurrency, isolation, and failure contracts. They do not establish production deployment readiness, legal correctness, semantic entailment, or improved model answer quality.

## Consequences and known limits

Positive consequences:

- Accepted HTTP work has a durable identity and audit trail.
- User isolation, idempotency, one-active-run, and terminal publication have database enforcement rather than process-local convention.
- Snapshot activation changes cannot silently retarget an accepted run.
- A late or timed-out execution cannot publish after losing its delivery boundary.
- SSE reconnect is replay, not a second execution.
- The legacy CLI remains independent of the optional service stack.

Costs and limits:

- The M4 supervisor deliberately processes one execution at a time and is not a distributed task platform.
- A timed-out or cancelled provider call may continue consuming its own thread until the provider returns; M4 fences delivery but cannot revoke arbitrary external computation.
- An incomplete embedded shutdown can temporarily retain the database pool so a draining thread does not use disposed dependencies.
- The static Bearer token registry is a testable M4 authorization boundary, not a production identity provider, token rotation service, or TLS termination layer.
- Readiness validates the configured active serving boundary, not every row of a large corpus on each probe.
- `interrupted` work is durable but not resumable until M5 supplies checkpoint semantics.
- Provider-free verification proves wiring and safety behavior, not live-model latency, cost, answer quality, or legal validity.

## Rollback and replacement conditions

- Code rollback uses a new revert or fix PR. Do not force-push, rewrite shared history, or move an existing release tag.
- The service can be disabled while preserving the legacy CLI and M3 storage/retrieval path.
- Alembic revision `0005_m4_api_sessions` downgrade drops M4 session, run, message, result, idempotency, and event tables. Do not run it against valued service history without an explicit backup/export and an approved migration plan. A forward fix is preferred.
- If timed-out provider calls regularly exhaust the quarantine bound, stop claiming work and replace or isolate that provider before increasing concurrency.
- If multi-process throughput becomes necessary, preserve these PostgreSQL identities and fences while adding the M6 queue/observability layer; do not replace them with broker delivery assumptions.
- If exact interrupted-run continuation is required, add M5 node checkpoints and side-effect-aware resume rules before enabling the resume endpoint.

## Release boundary

- The architecture decision was accepted at candidate time and shipped without changing the production semantics described in this ADR.
- PR #17 was normally merged as `e78984f548dc0885e3bdf26d62565f39dd61af01`. Its first master run exposed a runner-sensitive 150ms test budget and correctly blocked release.
- PR #18 changed only that test budget, passed exact-head CI, and was normally merged as release target `670e005a081cffa36a75af2b202e50eb2b859c3d`; the exact target then passed both release jobs.
- Annotated tag `v0.5.0` peels to `670e005a...`; the non-draft, non-prerelease GitHub Release and `docs/refactor/receipts/M4.json` exist and have been remotely verified.
- Receipt PR #19 exists and its first candidate passed both exact-head jobs. Its corrected final head, normal merge, merge-target master CI, Issue #16 closure, Milestone 5 closure and documentation-only finalization remain pending.
- Until the independent governance facts are remotely verified, M4 remains `released_receipt_pending`, not fully `released`; the software tag is immutable and is not moved to include receipt documents.

## Sources

- [M4 master plan](../MASTER_PLAN.md), especially M4-T01 through M4-T08 and the release state machine.
- `legal_rag/storage/alembic/versions/0005_m4_api_sessions.py`
- `legal_rag/services/run_service.py`
- `legal_rag/services/supervisor.py`
- `legal_rag/services/run_executor.py`
- `legal_rag/services/service_retrieval.py`
- `legal_rag/api/app.py`
- `legal_rag/api/auth.py`
- `legal_rag/api/settings.py`
- `integration_tests/test_m4_http_api.py`
- `integration_tests/test_m4_process_restart.py`
- `integration_tests/test_m4_run_service_db.py`
- `integration_tests/test_m4_service_wiring.py`
- `scripts/quality_gate.py`
