"""Real catalog import/activation and frozen assistants tolerate opaque titles."""

import uuid

import pytest

from integration_tests.test_m3_exact_retrieval import _ArticleSpec, _build_bundle
from integration_tests.test_m3_migration_resilience import _temporary_database
from legal_rag.api.auth import ServicePrincipal
from legal_rag.harness.state import HARNESS_GRAPH_VERSION
from legal_rag.services.execution_policy import ServiceExecutionPolicy
from legal_rag.services.run_service import ResourceNotFoundError, RunService
from legal_rag.services.service_retrieval import PostgresAssistantFactory
from legal_rag.storage.catalog import PostgresLegalCatalogRepository
from legal_rag.storage.migrations import upgrade_database
from legal_rag.storage.repository import PostgresCorpusRepository


@pytest.fixture
def migrated_engine(integration_database_url):
    # No pending runs or LangGraph tables from other integration modules.
    with _temporary_database(integration_database_url, "catalog_title") as engine:
        upgrade_database(engine)
        yield engine


@pytest.mark.parametrize("title", (
    "合成机关" + "补充规定" * 64 + "关于适用《合成甲法》的规定",
    "合成机关关于适用《合成甲法的规定",
    "合成机关关于适用《合成甲法》的规定》",
    "合成机关关于适用" + "《" * 9 + "合成甲法" + "》" * 9 + "的规定",
), ids=("long", "unbalanced_open", "unbalanced_close", "deep_nested"))
def test_import_activation_frozen_factory_and_exact_lookup_keep_complete_identity(migrated_engine, title):
    suffix = uuid.uuid4().hex
    scope_id, snapshot_id = f"title-scope-{suffix}", f"title-snapshot-{suffix}"
    specs = [
        _ArticleSpec(f"plain-law-{suffix}", f"plain-version-{suffix}", "合成甲法",
                     f"plain-article-{suffix}", "第十条"),
        _ArticleSpec(f"opaque-law-{suffix}", f"opaque-version-{suffix}", title,
                     f"opaque-article-{suffix}", "第二条"),
    ]
    bundle = _build_bundle(prefix=f"title-{suffix}", scope_id=scope_id, snapshot_id=snapshot_id,
                           specs=specs, vectors=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    PostgresCorpusRepository(migrated_engine).import_bundle(bundle)
    PostgresLegalCatalogRepository(migrated_engine).activate_snapshot(
        scope_id=scope_id, snapshot_id=snapshot_id, expected_current_snapshot_id=None,
        required_profile_id=bundle.embedding_profile.profile_id,
        actor="catalog-title-fixture", reason="synthetic complete catalog identity")
    owner = ServicePrincipal(f"title-owner-{suffix}", scope_id, bundle.embedding_profile.profile_id)
    service = RunService(migrated_engine, execution_policy=ServiceExecutionPolicy())
    session = service.create_session(owner)
    record, _ = service.create_run(owner, session.session_id, suffix,
        {"question": "正文", "retrieval": {"top_k": 2}}, HARNESS_GRAPH_VERSION)
    worker = f"title-worker-{suffix}"
    claimed = service.claim_next_run(worker, lease_seconds=60)
    assert claimed.run_id == record.run_id
    frozen = service.load_execution_input(record.run_id, worker, lease_epoch=claimed.lease_epoch)
    assistant = PostgresAssistantFactory(migrated_engine)(frozen.to_execution_input())
    assert assistant.retriever.unsupported_law_titles == (title,)
    assert set(assistant.retriever.catalog_law_titles) == {"合成甲法", title}

    ordinary = assistant.retrieve_turn(assistant.prepare_question("正文"), max_followup_rounds=0)
    assert ordinary.route_outcome.route == "lexical" and ordinary.route_outcome.status == "found"
    assert ordinary.evidence_check.sufficient
    assert {article.title for result in ordinary.results for article in result.provenance.articles} == {
        "合成甲法", title,
    }
    assert all(result.provenance.scope_id == scope_id and result.provenance.snapshot_id == snapshot_id
               for result in ordinary.results)

    normal = assistant.retrieve_turn(assistant.prepare_question("《合成甲法》第十条是什么？"),
                                     max_followup_rounds=0)
    assert normal.route_outcome.status == "found"
    assert normal.route_outcome.resolved_pairs == (("合成甲法", "第十条"),)
    assert all(result.provenance.articles[0].article_id == specs[0].article_id for result in normal.results)

    for query in (title + "第二条是什么？", f"《{title}》第二条是什么？"):
        unknown = assistant.retrieve_turn(assistant.prepare_question(query), max_followup_rounds=0)
        assert unknown.route_outcome.status == "needs_disambiguation"
        assert unknown.route_outcome.reason_codes == ("unsupported_catalog_title",)
        assert not unknown.results and not unknown.route_outcome.requested_pairs
        assert not unknown.evidence_check.sufficient and not unknown.evidence_check.followup_queries

    # Explicit full-identity catalog lookup has no query grammar restriction.
    full_article = service.lookup_run_article(owner, record.run_id, law_title=title, article_number="第二条")
    assert full_article.status == "found" and full_article.match.title == title
    assert full_article.match.article_id == specs[1].article_id
    assert full_article.match.body == f"{specs[1].article_id} 正文。"
    assert full_article.match.provenance.snapshot_id == frozen.snapshot_id
    unknown_title = service.lookup_run_article(owner, record.run_id, law_title=title + "未知外文档", article_number="第二条")
    assert unknown_title.status == "not_found" and unknown_title.match is None
    assert service.lookup_run_article(owner, record.run_id, law_title="合成甲法", article_number="第二条").status == "not_found"
    foreign = ServicePrincipal(f"foreign-owner-{suffix}", scope_id, bundle.embedding_profile.profile_id)
    with pytest.raises(ResourceNotFoundError):
        service.lookup_run_article(foreign, record.run_id, law_title=title, article_number="第二条")
    assert service.get_budget(record.run_id).model_attempts_used == 0
    assert service.monetary_snapshot(record.run_id)["attempts"] == 0
