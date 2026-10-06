"""Dedicated PostgreSQL contracts; all completion responses are offline fakes."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update

from integration_tests.m4_support import seed_m4_service_corpus
from integration_tests.m5_support import ensure_persistent_checkpointer, expire_run_lease
from legal_rag.api.app import create_app
from legal_rag.api.auth import ServicePrincipal, TokenAuthenticator
from legal_rag.harness.budget import BudgetExhausted
from legal_rag.harness.runner import GraphRunExecutor
from legal_rag.harness.state import HARNESS_GRAPH_VERSION
from legal_rag.services.execution_policy import GenerationPolicy, ServiceExecutionPolicy
from legal_rag.services.run_executor import ExecutionFailure
from legal_rag.services.governed_calls import GovernedCompletionClient
from legal_rag.services.run_service import (
    CheckpointCompatibilityError, ResourceNotFoundError, RunService,
    ServiceContractError, WorkerLeaseLostError,
)
from legal_rag.services.service_retrieval import PostgresAssistantFactory
from legal_rag.semantic import SemanticPolicy
from legal_rag.llm import CompletionUsage
from legal_rag.storage.schema import run_budget_ledgers, run_external_attempts, run_execution_policies, run_monetary_attempts


@pytest.fixture
def migrated_engine(integration_database_url):
    """Keep pending runs and LangGraph's private tables out of legacy suites."""
    from integration_tests.test_m3_migration_resilience import _temporary_database
    from legal_rag.storage.migrations import upgrade_database
    with _temporary_database(integration_database_url, "general_service") as engine:
        upgrade_database(engine)
        yield engine


def generation(scope_id, **changes):
    values = dict(enabled=True, model="Qwen/fixture", allowed_scope_ids=(scope_id,),
        egress_acknowledged=True, pricing_acknowledged=True, price_revision="fixture-price-r1",
        input_rate="0.4", output_rate="3.2", budget="0.01", max_output_tokens=100)
    return GenerationPolicy(**(values | changes))


def create_case(engine, *, enabled=False, semantic=False, budget="0.01", selector="generic-v3", idempotency_key=None):
    boundary = seed_m4_service_corpus(engine)
    policy = ServiceExecutionPolicy(generation=generation(boundary.owner.scope_id, budget=budget)
        if enabled else GenerationPolicy(), semantic_policy=SemanticPolicy("fixture-checker", "r1", "p1", "a" * 64, True).to_dict() if semantic else None)
    service = RunService(engine, execution_policy=policy)
    session = service.create_session(boundary.owner)
    record, _ = service.create_run(boundary.owner, session.session_id, idempotency_key or uuid.uuid4().hex,
        {"question": "《服务端测试法》第一条规定了什么？", "retrieval": {"top_k": 3, "lexical_profile": selector}}, HARNESS_GRAPH_VERSION)
    claimed = service.claim_next_run("fixture-worker", lease_seconds=60)
    assert claimed.run_id == record.run_id
    frozen = service.load_execution_input(record.run_id, "fixture-worker", lease_epoch=claimed.lease_epoch)
    return service, boundary, record, frozen


def reserve(service, frozen, *, amount=Decimal("0.001"), key="fixture-call"):
    return service.reserve_attempt(frozen.run_id, worker_id="fixture-worker", lease_epoch=frozen.lease_epoch,
        operation_key=key, operation_kind="model", operation_name="generate_answer", request_hash="c" * 64,
        monetary_reservation=amount)


def dispatch(service, frozen, reservation):
    service.mark_attempt_dispatched(reservation.attempt_id, worker_id="fixture-worker", lease_epoch=frozen.lease_epoch)


def settle(service, frozen, reservation, **values):
    service.finish_attempt(reservation.attempt_id, worker_id="fixture-worker", lease_epoch=frozen.lease_epoch,
                          **values)


class OfflineCompletion:
    hidden_retries_disabled = True
    base_url = "https://api.siliconflow.cn/v1"
    model = "Qwen/fixture"
    max_tokens = 100
    enable_thinking = False
    response_format = "json_object"
    follow_redirects = False
    load_environment_file = False
    usage = None

    def __init__(self):
        self.calls = []
        self.last_response_metadata = {}
        self.semantic_status = "supported"
        self.usage = CompletionUsage()

    def complete(self, prompt):
        self.usage.calls += 1
        self.usage.record_tokens(input_tokens=30, output_tokens=10, total_tokens=40)
        self.calls.append("semantic_check" if "Cover every segment" in prompt else "generate_answer")
        self.last_response_metadata = {"input_tokens": 30, "output_tokens": 10, "total_tokens": 40,
            "finish_reason": "stop", "returned_model_matches": True,
            "reasoning_content_reported": True, "reasoning_content_nonempty": False}
        if self.calls[-1] == "semantic_check":
            payload = json.loads(prompt.split("\n", 1)[1])
            return json.dumps({"decisions": [{"segment_id": item["segment_id"], "status": self.semantic_status,
                "source_ids": item["source_ids"] or ["S1"], "reason_codes": []} for item in payload["segments"]]}, ensure_ascii=False)
        return json.dumps({"answer_text": "服务端测试法第一条用于验证 PostgreSQL 检索与安全发布链路[S1]。",
            "answer_mode": "evidence_answer", "claims": [{"claim_id": "c1",
            "text": "服务端测试法第一条用于验证 PostgreSQL 检索与安全发布链路。", "source_ids": ["S1"]}],
            "limitations": [], "clarification_question": None}, ensure_ascii=False)


def test_frozen_selector_and_policy_ignore_later_deployment_configuration(migrated_engine: Engine):
    service, _, record, frozen = create_case(migrated_engine)
    assert frozen.execution_policy["lexical_profile"] == "generic-v3"
    service.execution_policy = ServiceExecutionPolicy(lexical_profile="legacy-v1")
    loaded = service.load_execution_input(record.run_id, "fixture-worker", lease_epoch=frozen.lease_epoch)
    assert loaded.execution_policy == frozen.execution_policy
    assistant = PostgresAssistantFactory(migrated_engine)(loaded.to_execution_input())
    outcome = assistant.retriever.retrieve_outcome(loaded.question, top_k=3)
    assert outcome.status == "found" and outcome.route == "exact_reference"
    assert outcome.results[0].provenance.snapshot_id == frozen.snapshot_id


def test_idempotent_replay_keeps_original_paid_policy_not_changed_server_policy(migrated_engine: Engine):
    key = uuid.uuid4().hex
    service, boundary, record, frozen = create_case(migrated_engine, enabled=True, idempotency_key=key)
    service.execution_policy = ServiceExecutionPolicy()
    replay, replayed = service.create_run(boundary.owner, record.session_id, key,
        dict(record.request_payload), HARNESS_GRAPH_VERSION)
    assert replayed and replay.run_id == record.run_id
    reloaded = service.load_execution_input(record.run_id, "fixture-worker", lease_epoch=frozen.lease_epoch)
    assert reloaded.execution_policy == frozen.execution_policy
    assert reloaded.execution_policy["generation"]["enabled"] is True


def test_full_article_is_not_a_synthetic_chunk_and_owner_scope_is_checked(migrated_engine: Engine):
    service, boundary, record, frozen = create_case(migrated_engine)
    result = service.lookup_run_article(boundary.owner, record.run_id, law_title="服务端测试法", article_number="第1条")
    assert result.status == "found"
    assert "安全发布链路" in result.match.body
    assert result.match.provenance.snapshot_id == frozen.snapshot_id
    assert result.match.provenance.memberships
    for principal in (boundary.other, replace(boundary.owner, scope_id="foreign-scope")):
        with pytest.raises(ResourceNotFoundError):
            service.lookup_run_article(principal, record.run_id, law_title="服务端测试法", article_number="第一条")
    miss = service.lookup_run_article(boundary.owner, record.run_id, law_title="服务端测试法", article_number="第九十九条")
    assert miss.status == "not_found" and miss.match is None


def test_exact_reference_missing_never_uses_lexical_as_success(migrated_engine: Engine):
    _, _, _, frozen = create_case(migrated_engine)
    assistant = PostgresAssistantFactory(migrated_engine)(frozen.to_execution_input())
    outcome = assistant.retriever.retrieve_outcome("《服务端测试法》第九十九条", top_k=3)
    assert outcome.status == "not_found" and not outcome.results


def test_nested_catalog_keeps_frozen_factory_full_article_and_inner_law_ownership(migrated_engine: Engine):
    from integration_tests.test_m3_exact_retrieval import _ArticleSpec, _build_bundle
    from legal_rag.storage.catalog import PostgresLegalCatalogRepository
    from legal_rag.storage.repository import PostgresCorpusRepository

    suffix = uuid.uuid4().hex
    scope_id, snapshot_id = f"nested-scope-{suffix}", f"nested-snapshot-{suffix}"
    outer_title = "合成机关关于适用《合成甲法》的规定"
    specs = [
        _ArticleSpec(f"plain-law-{suffix}", f"plain-version-{suffix}", "合成甲法",
                     f"plain-article-{suffix}", "第十条"),
        _ArticleSpec(f"outer-law-{suffix}", f"outer-version-{suffix}", outer_title,
                     f"outer-article-{suffix}", "第二条"),
    ]
    bundle = _build_bundle(prefix=f"nested-{suffix}", scope_id=scope_id, snapshot_id=snapshot_id,
                           specs=specs, vectors=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    PostgresCorpusRepository(migrated_engine).import_bundle(bundle)
    PostgresLegalCatalogRepository(migrated_engine).activate_snapshot(
        scope_id=scope_id, snapshot_id=snapshot_id, expected_current_snapshot_id=None,
        required_profile_id=bundle.embedding_profile.profile_id,
        actor="nested-catalog-fixture", reason="synthetic full-title identity verification")
    owner = ServicePrincipal(f"nested-owner-{suffix}", scope_id, bundle.embedding_profile.profile_id)
    service = RunService(migrated_engine, execution_policy=ServiceExecutionPolicy())
    session = service.create_session(owner)
    record, _ = service.create_run(owner, session.session_id, suffix,
        {"question": "《合成甲法》第十条是什么？", "retrieval": {"top_k": 3, "lexical_profile": "generic-v3"}},
        HARNESS_GRAPH_VERSION)
    claim = service.claim_next_run("fixture-worker", lease_seconds=60)
    frozen = service.load_execution_input(record.run_id, "fixture-worker", lease_epoch=claim.lease_epoch)
    assistant = PostgresAssistantFactory(migrated_engine)(frozen.to_execution_input())

    ordinary = assistant.retrieve_turn(assistant.prepare_question("正文的一般登记事项"), max_followup_rounds=0)
    assert ordinary.results and ordinary.evidence_check.sufficient
    assert ordinary.route_outcome is None or ordinary.route_outcome.route == "lexical"
    for query, title, number, article_id in (
        (f"《合成甲法》第十条是什么？", "合成甲法", "第十条", specs[0].article_id),
        (f"《{outer_title}》第二条是什么？", outer_title, "第二条", specs[1].article_id),
        (f"{outer_title}第二条是什么？", outer_title, "第二条", specs[1].article_id),
    ):
        retrieved = assistant.retrieve_turn(assistant.prepare_question(query), max_followup_rounds=0)
        assert retrieved.route_outcome.status == "found"
        assert retrieved.route_outcome.resolved_pairs == ((title, number),)
        assert all(result.provenance.snapshot_id == frozen.snapshot_id for result in retrieved.results)
        full_article = service.lookup_run_article(owner, record.run_id, law_title=title, article_number=number)
        assert full_article.status == "found" and full_article.match.article_id == article_id
        assert full_article.match.title == title and full_article.match.body == f"{article_id} 正文。"
        assert full_article.match.provenance.snapshot_id == frozen.snapshot_id
    wrong_inner = assistant.retrieve_turn(assistant.prepare_question("《合成甲法》第二条是什么？"), max_followup_rounds=0)
    assert wrong_inner.route_outcome.status == "not_found" and not wrong_inner.results
    assert service.lookup_run_article(owner, record.run_id, law_title="合成甲法", article_number="第二条").status == "not_found"
    assert service.get_budget(record.run_id).model_attempts_used == 0
    assert service.monetary_snapshot(record.run_id)["attempts"] == 0


def test_policy_corruption_fails_before_factory_or_dispatch(migrated_engine: Engine):
    service, _, record, frozen = create_case(migrated_engine)
    with migrated_engine.begin() as connection:
        connection.execute(update(run_execution_policies).where(run_execution_policies.c.run_id == record.run_id).values(policy_hash="f" * 64))
    with pytest.raises(CheckpointCompatibilityError):
        service.load_execution_input(record.run_id, "fixture-worker", lease_epoch=frozen.lease_epoch)


def test_concurrent_money_reservation_consumes_only_one_count(migrated_engine: Engine):
    service, _, _, frozen = create_case(migrated_engine, enabled=True)
    def attempt(index):
        try:
            return reserve(service, frozen, key=f"concurrent-{index}")
        except BudgetExhausted:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = list(executor.map(attempt, range(2)))
    assert sum(item is not None for item in attempts) == 1
    assert service.get_budget(frozen.run_id).model_attempts_used == 1


def test_actual_usage_releases_only_known_unused_reservation(migrated_engine: Engine):
    service, _, _, frozen = create_case(migrated_engine, enabled=True)
    first = reserve(service, frozen)
    dispatch(service, frozen, first)
    settle(service, frozen, first, status="succeeded", result_hash="d" * 64, monetary_usage=(100, 10))
    snapshot = service.monetary_snapshot(frozen.run_id)
    assert Decimal(snapshot["known_estimated_cost"]) == Decimal("0.000072")
    assert Decimal(snapshot["unresolved_reserved_cost"]) == 0
    second = reserve(service, frozen, key="second")
    assert second.attempt_id != first.attempt_id
    assert service.get_budget(frozen.run_id).model_attempts_used == 2


@pytest.mark.parametrize("reason", ["provider_delivery_unknown", "pricing_identity_unknown", "usage_unknown"])
def test_unknown_money_reservation_never_refunds_or_retries(migrated_engine: Engine, reason):
    service, _, _, frozen = create_case(migrated_engine, enabled=True)
    reservation = reserve(service, frozen)
    dispatch(service, frozen, reservation)
    settle(service, frozen, reservation, status="outcome_unknown", retryable=False,
           error_code="external_outcome_unknown", monetary_unknown_reason=reason)
    snapshot = service.monetary_snapshot(frozen.run_id)
    assert snapshot["has_unknown_cost"] and Decimal(snapshot["unresolved_reserved_cost"]) == Decimal("0.001")
    with pytest.raises(BudgetExhausted, match="external_outcome_unknown"):
        reserve(service, frozen, key="new-operation")
    assert service.get_budget(frozen.run_id).model_attempts_used == 1


def test_known_overrun_is_recorded_without_hiding_actual_cost(migrated_engine: Engine):
    service, _, _, frozen = create_case(migrated_engine, enabled=True)
    reservation = reserve(service, frozen)
    dispatch(service, frozen, reservation)
    settle(service, frozen, reservation, status="succeeded", result_hash="d" * 64, monetary_usage=(10000, 100))
    with migrated_engine.connect() as connection:
        row = connection.execute(select(run_monetary_attempts).where(run_monetary_attempts.c.attempt_id == reservation.attempt_id)).mappings().one()
    assert row["cost_status"] == "overrun" and row["known_cost"] > row["reserved_cost"]
    with pytest.raises(BudgetExhausted):
        reserve(service, frozen, key="after-overrun")


def test_monetary_budget_rejects_before_count_increment(migrated_engine: Engine):
    service, _, _, frozen = create_case(migrated_engine, enabled=True, budget="0.000001")
    with pytest.raises(BudgetExhausted, match="monetary_budget_exhausted"):
        reserve(service, frozen)
    assert service.get_budget(frozen.run_id).model_attempts_used == 0


def test_old_worker_cannot_settle_money_after_lease_takeover(migrated_engine: Engine):
    service, boundary, _, frozen = create_case(migrated_engine, enabled=True)
    reservation = reserve(service, frozen)
    dispatch(service, frozen, reservation)
    expire_run_lease(migrated_engine, frozen.run_id)
    service.recover_stale_runs()
    service.resume_run(boundary.owner, frozen.run_id)
    claimed = service.claim_next_run("worker-new", lease_seconds=60)
    assert claimed.run_id == frozen.run_id and claimed.lease_epoch > frozen.lease_epoch
    with pytest.raises(WorkerLeaseLostError):
        settle(service, frozen, reservation, status="succeeded", result_hash="d" * 64, monetary_usage=(30, 10))
    assert service.monetary_snapshot(frozen.run_id)["has_unknown_cost"]


def test_generator_and_checker_use_same_durable_budget_exactly_once(migrated_engine: Engine):
    ensure_persistent_checkpointer(migrated_engine)
    service, boundary, _, frozen = create_case(migrated_engine, enabled=True, semantic=True)
    client = OfflineCompletion()
    executor = GraphRunExecutor(service, PostgresAssistantFactory(migrated_engine, completion_client_factory=lambda _: client), generate=False)
    executor.execute_claimed(frozen, "fixture-worker", lambda *_: None)
    record = service.get_run(boundary.owner, frozen.run_id)
    assert record.status == "succeeded" and record.result
    assert client.calls == ["generate_answer", "semantic_check"]
    assert service.get_budget(frozen.run_id).model_attempts_used == 2
    with migrated_engine.connect() as connection:
        rows = connection.execute(select(run_monetary_attempts).where(run_monetary_attempts.c.run_id == frozen.run_id)).mappings().all()
    assert len(rows) == 2 and all(row["cost_status"] == "known" for row in rows)
    assert record.result.verification_payload["semantic_support_status"] == "supported"


def test_disabled_generation_and_legacy_runs_never_gain_paid_authority(migrated_engine: Engine):
    ensure_persistent_checkpointer(migrated_engine)
    service, boundary, _, frozen = create_case(migrated_engine)
    executor = GraphRunExecutor(service, PostgresAssistantFactory(migrated_engine), generate=True)
    executor.execute_claimed(frozen, "fixture-worker", lambda *_: None)
    assert service.get_budget(frozen.run_id).model_attempts_used == 0
    assert service.get_run(boundary.owner, frozen.run_id).result.answer_payload["answer_mode"] == "retrieval_only"


@pytest.mark.parametrize("status", ["unsupported", "uncertain", "not_checked", "error"])
def test_required_checker_failure_publishes_safe_limited_result_without_erasing_paid_failure(migrated_engine: Engine, status):
    ensure_persistent_checkpointer(migrated_engine)
    service, boundary, _, frozen = create_case(migrated_engine, enabled=True, semantic=True)
    client = OfflineCompletion()
    client.semantic_status = status
    executor = GraphRunExecutor(service, PostgresAssistantFactory(migrated_engine, completion_client_factory=lambda _: client))
    executor.execute_claimed(frozen, "fixture-worker", lambda *_: None)
    record = service.get_run(boundary.owner, frozen.run_id)
    assert record.status == "completed_with_limits" and record.result
    assert record.stop_reason == "semantic_not_supported_or_unknown"
    assert client.calls == ["generate_answer", "semantic_check"]
    assert service.get_budget(frozen.run_id).model_attempts_used == 2
    assert record.result.answer_payload["answer_mode"] == "insufficient_evidence"
    verification = record.result.verification_payload
    assert verification["passed"] and verification["fallback_used"]
    assert verification["semantic_support_status"] != "supported"
    assert verification["rejected_draft_check"]["semantic_support_status"] == status
    assert verification["rejected_draft_check"]["passed"] is False
    assert Decimal(service.monetary_snapshot(frozen.run_id)["known_estimated_cost"]) > 0


def test_execution_money_migration_refuses_to_discard_frozen_policy_and_attempt_history(integration_database_url: str):
    from alembic import command
    from alembic.runtime.migration import MigrationContext
    from integration_tests.test_m3_migration_resilience import _temporary_database
    from legal_rag.storage.migrations import alembic_config, upgrade_database
    with _temporary_database(integration_database_url, "general_money_history") as engine:
        upgrade_database(engine)
        service, _, _, frozen = create_case(engine, enabled=True)
        attempt = reserve(service, frozen)
        with pytest.raises(RuntimeError, match="valued monetary journal requires a forward repair"):
            with engine.begin() as connection:
                command.downgrade(alembic_config(connection=connection), "0007_m6_jobs_outbox")
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == "0008_execution_money"
            assert connection.execute(select(run_execution_policies).where(
                run_execution_policies.c.run_id == frozen.run_id)).first() is not None
            assert connection.execute(select(run_monetary_attempts).where(
                run_monetary_attempts.c.attempt_id == attempt.attempt_id)).first() is not None


def test_empty_execution_money_migration_can_downgrade_and_upgrade(integration_database_url: str):
    from alembic import command
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import inspect
    from integration_tests.test_m3_migration_resilience import _temporary_database
    from legal_rag.storage.migrations import alembic_config, upgrade_database
    with _temporary_database(integration_database_url, "general_money_empty") as engine:
        upgrade_database(engine)
        with engine.begin() as connection:
            command.downgrade(alembic_config(connection=connection), "0007_m6_jobs_outbox")
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == "0007_m6_jobs_outbox"
        assert "run_monetary_attempts" not in inspect(engine).get_table_names()
        upgrade_database(engine)
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == "0008_execution_money"
        assert {"run_execution_policies", "run_monetary_attempts"} <= set(inspect(engine).get_table_names())


def test_paid_graph_generator_and_checker_publish_two_allowlisted_observations_on_one_ledger(migrated_engine):
    class Collector:
        def __init__(self):
            self.events = []
        def record(self, event):
            self.events.append(event)
    ensure_persistent_checkpointer(migrated_engine)
    service, boundary, _, frozen = create_case(migrated_engine, enabled=True, semantic=True)
    client, sink = OfflineCompletion(), Collector()
    GraphRunExecutor(service, PostgresAssistantFactory(migrated_engine, completion_client_factory=lambda _: client),
        observer=sink).execute_claimed(frozen, "fixture-worker", lambda *_: None)
    models = [event for event in sink.events if event.name == "model.completed"]
    assert [(event.node, event.tool) for event in models] == [("generate", "generator"), ("judge", "judge")]
    assert all(event.status == "succeeded" and event.counts == {"model_calls": 1} for event in models)
    assert all(event.model_input_tokens == 30 and event.model_output_tokens == 10 for event in models)
    assert service.get_budget(frozen.run_id).model_attempts_used == 2
    assert service.monetary_snapshot(frozen.run_id)["attempts"] == 2
    assert service.get_run(boundary.owner, frozen.run_id).result.verification_payload["semantic_support_status"] == "supported"
    records = [event.to_local_record() for event in sink.events]
    assert all(not set(record) & {"prompt", "raw_response", "question", "answer_text", "reasoning_content"} for record in records)


@pytest.mark.parametrize("enabled", [False, True])
def test_explicit_frozen_policy_rejects_ungoverned_planner_even_when_generation_is_off(migrated_engine, enabled):
    ensure_persistent_checkpointer(migrated_engine)
    service, _, _, frozen = create_case(migrated_engine, enabled=enabled)
    client, planner_calls = OfflineCompletion(), []
    def planner(*args):
        planner_calls.append("dispatched")
        return ["uncontrolled planner output"]
    executor = GraphRunExecutor(service, PostgresAssistantFactory(migrated_engine, completion_client_factory=lambda _: client),
                                followup_planner=planner)
    with pytest.raises(ExecutionFailure, match="ungoverned_planner_disabled"):
        executor.execute_claimed(frozen, "fixture-worker", lambda *_: None)
    assert not planner_calls and not client.calls
    assert service.get_budget(frozen.run_id).model_attempts_used == 0
    assert service.monetary_snapshot(frozen.run_id)["attempts"] == 0


def test_worker_receipt_head_helper_reads_real_database_without_creating_a_worker_receipt(migrated_engine):
    from integration_tests.test_m6_worker_real import _database_migration_head
    assert _database_migration_head(migrated_engine) == "0008_execution_money"
    from integration_tests.m5_support import _database_migration_head as m5_database_migration_head
    assert m5_database_migration_head(migrated_engine) == "0008_execution_money"
