# ADR-004: M6 durable batch jobs and redacted observations

- Status: Accepted; v0.7.0 release and receipt verified, v0.7.1 runtime observation patch published, independent patch receipt pending
- Date: 2026-10-02 (original decision 2026-09-29)
- Affected milestone and versions: M6 / released `v0.7.0` and correctness patch `v0.7.1`
- Base: `76a038936ddfd900f98ad8709fedcc50c07063d3` (`origin/master` after M5 finalization)
- Tracking: [Issue #25](https://github.com/1040942669/legal-rag-agent/issues/25), GitHub [Milestone 7](https://github.com/1040942669/legal-rag-agent/milestone/7)

## Context

M2 already has provider-free resumable experiments; M3 already has a validated, atomic PostgreSQL corpus import and snapshot activation; M5 already has a bounded online runner. Neither the M4 supervisor nor M5 checkpoint is a broker-backed batch platform. A process can stop after accepting a batch request but before publishing a broker message, after publishing but before recording delivery, or after committing one item before acknowledging its message. Redis cannot be the only record of a job, its result, or its progress.

The new work is limited to two genuinely long-running jobs: offline batch evaluation and prebuilt corpus/index ingestion. It does not move online chat nodes into Celery or introduce live embedding/model calls. A single Celery message only identifies a PostgreSQL job; it is not an instruction containing a source path, private text, credential, or authorization scope.

## Options considered

| Option | Benefit | Failure or cost | Decision |
|---|---|---|---|
| Call Celery from the HTTP transaction | Simple request path | Commit/publish gap loses work or sends a message for a rolled-back job | Rejected |
| Treat Redis result state as authoritative | Avoids a database job model | Redis loss/TTL destroys progress and permission boundary | Rejected |
| PostgreSQL job, item ledger and transactional outbox; Redis broker; Celery workers | Durable acceptance and progress, redelivery and lease reconciliation | Dispatcher, worker lifecycle and schema migration need operational care | Chosen |
| Queue every online graph node | Uniform scheduling appearance | Changes M5 recovery, costs and latency without M6 evidence | Out of scope |

## Decision

`POST /api/v1/evaluations` and `POST /api/v1/ingestions` accept only authenticated, scope/profile-bound, server-registered opaque references and an idempotency key. An acceptance transaction creates the job and its outbox row together. `GET /api/v1/jobs/{id}` and cancellation use the owner derived from user, scope and profile, returning the same non-enumerating 404 for unknown or foreign jobs. The 202 response means *accepted in PostgreSQL*, not that Redis delivery or execution has already succeeded.

The dispatcher claims pending outbox rows with short PostgreSQL leases and sends JSON messages containing only `job_id` and `schema_version`. If Redis is unavailable, it records a machine-readable delivery error and retries later; the accepted job remains queryable. The worker uses late acknowledgement, rejection when a worker child is lost, bounded attempts, prefetch 1 and a database lease epoch. These are at-least-once mechanisms, not an exactly-once claim. The PostgreSQL item ledger and immutable business artifacts reconcile repeat delivery; stale lease owners cannot advance progress. A recovery scan can requeue an expired job after a prematurely acknowledged duplicate. Automatic recovery creates at most five outbox rows per job, including the initial row, with exponential spacing for new rows. An unconfirmed publish of the same pending row may still be retried, so this is not a hard cap on broker publish calls. When the row budget is exhausted, a waiting job exposes `delivery_unconfirmed` rather than growing the broker queue without bound or claiming success. A retained Redis message can still be claimed after workers return; if all such messages are lost, further recovery needs authorized operator reconciliation because this version has no ordinary-user redrive API. Queue and execution times are visible independently of broker internals.

Evaluation reuses M2 offline dataset identity and per-case immutable completion artifacts; it neither invokes a live model nor invents a second evaluator. Ingestion revalidates a prebuilt M3 import plan, atomically imports its snapshot, optionally builds the HNSW index, validates the persisted bundle, then activates the snapshot last. `received`, `parsed` and `embedded` are validation of prebuilt artifacts, not live parsing or embedding in the worker. Progress is at the case or durable stage boundary, not a fabricated per-chunk counter. A failed index build leaves the previous active snapshot in place. The final activation, `activated` item and job terminal success now share one PostgreSQL transaction: the job row is locked first and its lease owner, epoch and cancellation state are checked before catalog activation, so takeover or cancellation serializes with the business pointer write. Cancellation remains best effort before this transaction; once success commits, it cannot undo the completed operation.

M6 observations are typed execution facts. A local JSONL sink is opt-in; Langfuse OTLP export is separately opt-in and requires an explicit data-flow acknowledgement. Remote fields are allowlisted and identifiers pseudonymized; raw query, answer, evidence text, credentials, tokens and personal information are excluded. Export uses a bounded queue and short timeout and cannot change the job outcome. The existing detailed local retrieval trace remains local.

## Verification and limits

M6-T01 through T05 and T07 require real PostgreSQL/Redis/Celery integration, including worker process loss and duplicate delivery. T06 uses provider-free unit/API tests for redaction, explicit opt-in and failure isolation; it is not a live Langfuse-send claim. Unit fakes cannot substitute for real component gates. The release report must list exact executed commands, environment, JUnit counts, candidate SHA and any tests not run. Live model, external embedding, production capacity, real legal quality and provider exactly-once remain unverified. PostgreSQL `0007_m6_jobs_outbox` is an application schema change; downgrading after valued jobs exist is not a data-preserving rollback.

## Runtime observation correction after v0.7.0

The final requirements audit found that the event schema supported rich facts but production job callbacks did not record them, M5 nodes were not connected, and idempotent submission could emit a false queued event. Green T01-T07 mechanism gates alone did not prove all of plan section 11.6. The alternatives were to narrow that requirement to a schema-only feature, or connect existing execution facts and strengthen its tests. The latter is chosen: it completes the original M6 scope without changing the M5 controller, defaults, durable checkpoint envelope, or queueing online nodes.

The released v0.7.1 patch observes actual validated node/attempt boundaries and immutable artifact lookups. Run/session identifiers come from frozen execution; batch case-attempt IDs are namespaced by the validated experiment. Reserved budgets remain separate from proven client calls. Retry counts come from durable attempts, not a reset local loop. Monotonic current invocation durations and database queue wait retain different meanings. Historical cache source calls and compute durations are not counted as current work. Token components require explicit per-component provider reporting coverage; unknown components and unverified prices stay null. Failure categories are stable allowlisted values, not exception text. Local and remote sinks remain default-off/best-effort, and remote tokens, raw evidence IDs, text and secrets remain excluded.

The cost is additional execution-boundary instrumentation and tests. No new database migration or dependency is required. The already published v0.7.0 annotated Tag/Release remains fixed. v0.7.1 passed its own exact-head/master gates, isolated installed-wheel proof and normal merge, and was published on 2026-10-02. Its independent documentation receipt and governance gates must still pass before M6 closes. See the [patch acceptance report](../../../reports/refactor/M6-observability-patch.md).

## Rollback and replacement conditions

Stop accepting new jobs, stop dispatch, let active workers drain or mark jobs explicitly, and return to the existing provider-free CLI/import workflow. Retain PostgreSQL job and artifact records for reconciliation; do not delete Redis or database volumes to simulate success. Use a forward repair migration if valued M6 rows exist. Reconsider queue splitting or a different broker only after queue-wait, utilization and failure evidence show a concrete need.

## Sources

- [M6 plan and acceptance matrix](../MASTER_PLAN.md)
- [Celery task acknowledgement and worker-loss semantics](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
- [Langfuse native OpenTelemetry integration](https://langfuse.com/integrations/native/opentelemetry)
- [M2 acceptance report](../../../reports/refactor/M2.md), [M3 acceptance report](../../../reports/refactor/M3.md), [M5 acceptance report](../../../reports/refactor/M5.md)
