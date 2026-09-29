# ADR-004: M6 durable batch jobs and redacted observations

- Status: Proposed while M6 integration gates are running
- Date: 2026-09-29
- Affected milestone and planned version: M6 / `v0.7.0`
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

The dispatcher claims pending outbox rows with short PostgreSQL leases and sends JSON messages containing only `job_id` and `schema_version`. If Redis is unavailable, it records a machine-readable delivery error and retries later; the accepted job remains queryable. The worker uses late acknowledgement, rejection when a worker child is lost, bounded attempts, prefetch 1 and a database lease epoch. These are at-least-once mechanisms, not an exactly-once claim. The PostgreSQL item ledger and immutable business artifacts reconcile repeat delivery; stale lease owners cannot advance progress. A recovery scan can requeue an expired job after a prematurely acknowledged duplicate. Queue and execution times are visible independently of broker internals.

Evaluation reuses M2 offline dataset identity and per-case immutable completion artifacts; it neither invokes a live model nor invents a second evaluator. Ingestion revalidates a prebuilt M3 import plan, atomically imports its snapshot, optionally builds the HNSW index, validates the persisted bundle, then activates the snapshot last. `received`, `parsed` and `embedded` are validation of prebuilt artifacts, not live parsing or embedding in the worker. Progress is at the case or durable stage boundary, not a fabricated per-chunk counter. A failed index build leaves the previous active snapshot in place. Cancellation is best effort at these boundaries; it cannot undo a completed external or database operation.

M6 observations are typed execution facts. A local JSONL sink is opt-in; Langfuse OTLP export is separately opt-in and requires an explicit data-flow acknowledgement. Remote fields are allowlisted and identifiers pseudonymized; raw query, answer, evidence text, credentials, tokens and personal information are excluded. Export uses a bounded queue and short timeout and cannot change the job outcome. The existing detailed local retrieval trace remains local.

## Verification and limits

M6-T01 through M6-T07 require real PostgreSQL/Redis/Celery integration, including worker process loss and duplicate delivery. Unit fakes establish contracts but cannot substitute for that gate. The release report must list exact executed commands, environment, JUnit counts, candidate SHA and any tests not run. Live model, external embedding, production capacity, real legal quality and provider exactly-once remain unverified. PostgreSQL `0007_m6_jobs_outbox` is an application schema change; downgrading after valued jobs exist is not a data-preserving rollback.

## Rollback and replacement conditions

Stop accepting new jobs, stop dispatch, let active workers drain or mark jobs explicitly, and return to the existing provider-free CLI/import workflow. Retain PostgreSQL job and artifact records for reconciliation; do not delete Redis or database volumes to simulate success. Use a forward repair migration if valued M6 rows exist. Reconsider queue splitting or a different broker only after queue-wait, utilization and failure evidence show a concrete need.

## Sources

- [M6 plan and acceptance matrix](../MASTER_PLAN.md)
- [Celery task acknowledgement and worker-loss semantics](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
- [Langfuse native OpenTelemetry integration](https://langfuse.com/integrations/native/opentelemetry)
- [M2 acceptance report](../../../reports/refactor/M2.md), [M3 acceptance report](../../../reports/refactor/M3.md), [M5 acceptance report](../../../reports/refactor/M5.md)
