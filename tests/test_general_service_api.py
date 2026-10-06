from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from legal_rag.api.app import create_app, CURRENT_ALEMBIC_HEAD
from legal_rag.api.auth import ServicePrincipal, TokenAuthenticator
from legal_rag.api.schemas import RunCreateRequest
from legal_rag.services.run_service import ResourceNotFoundError
from legal_rag.storage.catalog import ArticleLookupBoundary, ArticleLookupRequest, ArticleLookupResult


def test_http_only_accepts_safe_selector_not_provider_authority():
    request = RunCreateRequest(question="test", retrieval={"lexical_profile": "generic-v3"})
    assert request.model_dump(exclude_none=True)["retrieval"]["lexical_profile"] == "generic-v3"
    for untrusted in ({"generation": True}, {"execution_policy": {}}, {"api_key": "redacted"},
                      {"retrieval": {"base_url": "https://untrusted.invalid"}},
                      {"retrieval": {"lexical_profile": "unapproved"}}):
        with pytest.raises(ValidationError):
            RunCreateRequest(question="test", **untrusted)


def test_full_article_route_uses_authenticated_frozen_run_authority_and_reports_exact_miss():
    owner = ServicePrincipal("owner", "scope", "a" * 64)
    stranger = ServicePrincipal("stranger", "foreign", "b" * 64)
    token = "owner-token-" + "a" * 32
    other_token = "other-token-" + "b" * 32
    calls = []
    class Service:
        engine = SimpleNamespace(dispose=lambda: None)
        def lookup_run_article(self, principal, run_id, **values):
            if principal != owner or run_id != "owned-run":
                raise ResourceNotFoundError()
            calls.append((principal, run_id, values))
            request = ArticleLookupRequest(ArticleLookupBoundary("scope", "frozen-snapshot", pointer_revision=3,
                activation_id="activation-3"), values["law_title"], values["article_number"])
            return ArticleLookupResult.not_found(request, reason="no_exact_match")
    supervisor = SimpleNamespace(start=lambda: None, stop=lambda: True, wake=lambda: None)
    app = create_app(service=Service(), authenticator=TokenAuthenticator({token: owner, other_token: stranger}), supervisor=supervisor)
    with TestClient(app) as client:
        params = {"law_title": "合成测试法", "article_number": "第一条"}
        response = client.get("/api/v1/runs/owned-run/articles", params=params, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "not_found" and payload["article"] is None
        assert payload["request"]["boundary_fingerprint"] == ArticleLookupBoundary("scope", "frozen-snapshot", pointer_revision=3, activation_id="activation-3").fingerprint
        assert payload["synthetic_chunk"] is False and payload["evidence_kind"] == "authoritative_article"
        assert client.get("/api/v1/runs/owned-run/articles", params=params,
            headers={"Authorization": f"Bearer {other_token}"}).status_code == 404
        assert client.get("/api/v1/runs/owned-run/articles", params=params).status_code == 401
    assert len(calls) == 1
    assert CURRENT_ALEMBIC_HEAD == "0008_execution_money"
