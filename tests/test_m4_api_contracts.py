from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import legal_rag.storage.database as database_module
from legal_rag.api.auth import (
    AuthenticationConfigurationError,
    AuthenticationError,
    ServicePrincipal,
    TokenAuthenticator,
)
from legal_rag.api.schemas import RunCreateRequest, SessionCreateRequest
from legal_rag.api.settings import ServiceSettings, load_environment_configuration
from legal_rag.storage.database import (
    DatabaseConfigurationError,
    DatabaseSettings,
    create_database_engine,
)


PROFILE_ID = "a" * 64
SECOND_PROFILE_ID = "b" * 64
PRIMARY_TOKEN = "primary-token-" + "a" * 32
SECONDARY_TOKEN = "secondary-token-" + "b" * 32

SERVICE_ENVIRONMENT_VARIABLES = (
    "LEGAL_RAG_DATABASE_URL",
    "LEGAL_RAG_DATABASE_CONNECT_TIMEOUT_SECONDS",
    "LEGAL_RAG_DATABASE_POOL_TIMEOUT_SECONDS",
    "LEGAL_RAG_DATABASE_STATEMENT_TIMEOUT_MS",
    "LEGAL_RAG_DATABASE_LOCK_TIMEOUT_MS",
    "LEGAL_RAG_AUTH_TOKENS_JSON",
    "LEGAL_RAG_QUESTION_MAX_CHARACTERS",
    "LEGAL_RAG_MESSAGE_PAGE_MAX",
    "LEGAL_RAG_HISTORY_MAX_MESSAGES",
    "LEGAL_RAG_HISTORY_MAX_CHARACTERS",
    "LEGAL_RAG_IDEMPOTENCY_TTL_SECONDS",
    "LEGAL_RAG_RUN_LEASE_SECONDS",
    "LEGAL_RAG_EXECUTOR_TIMEOUT_SECONDS",
    "LEGAL_RAG_SUPERVISOR_POLL_SECONDS",
    "LEGAL_RAG_SSE_POLL_SECONDS",
    "LEGAL_RAG_SSE_HEARTBEAT_SECONDS",
)


def _principal(
    *,
    user_id: str = "user-server-owned",
    scope_id: str = "scope-server-owned",
    profile_id: str = PROFILE_ID,
) -> ServicePrincipal:
    return ServicePrincipal(
        user_id=user_id,
        scope_id=scope_id,
        profile_id=profile_id,
    )


def _token_payload() -> dict[str, dict[str, str]]:
    return {
        PRIMARY_TOKEN: {
            "user_id": "user-alpha",
            "scope_id": "scope-alpha",
            "profile_id": PROFILE_ID,
        },
        SECONDARY_TOKEN: {
            "user_id": "user-beta",
            "scope_id": "scope-beta",
            "profile_id": SECOND_PROFILE_ID,
        },
    }


def _clear_service_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in SERVICE_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


def test_bearer_tokens_map_only_to_server_configured_principals() -> None:
    authenticator = TokenAuthenticator.from_json(json.dumps(_token_payload()))

    primary = authenticator.authenticate_header(f"Bearer {PRIMARY_TOKEN}")
    secondary = authenticator.authenticate_header(f"bearer {SECONDARY_TOKEN}")

    assert primary == ServicePrincipal(
        user_id="user-alpha",
        scope_id="scope-alpha",
        profile_id=PROFILE_ID,
    )
    assert secondary == ServicePrincipal(
        user_id="user-beta",
        scope_id="scope-beta",
        profile_id=SECOND_PROFILE_ID,
    )
    assert primary != secondary


@pytest.mark.parametrize(
    "token",
    [
        "short-token",
        " " + "a" * 32,
        "a" * 32 + " ",
        "a" * 31 + "\n",
        "a" * 513,
    ],
)
def test_token_registry_rejects_short_or_unsafe_tokens(token: str) -> None:
    with pytest.raises(
        AuthenticationConfigurationError,
        match="opaque printable values of 32-512 characters",
    ):
        TokenAuthenticator({token: _principal()})


def test_token_registry_rejects_empty_or_non_principal_mappings() -> None:
    with pytest.raises(
        AuthenticationConfigurationError,
        match="at least one bearer token",
    ):
        TokenAuthenticator({})

    with pytest.raises(
        AuthenticationConfigurationError,
        match="ServicePrincipal objects",
    ):
        TokenAuthenticator({PRIMARY_TOKEN: object()})  # type: ignore[dict-item]


@pytest.mark.parametrize(
    ("authorization", "message"),
    [
        (None, "required"),
        ("", "required"),
        (PRIMARY_TOKEN, "required"),
        (f"Basic {PRIMARY_TOKEN}", "required"),
        ("Bearer", "required"),
        ("Bearer ", "required"),
        (f"Bearer  {PRIMARY_TOKEN}", "required"),
        (f"Bearer {PRIMARY_TOKEN} ", "required"),
        (f"Bearer\t{PRIMARY_TOKEN}", "required"),
        (f"Bearer {'z' * 32}", "invalid"),
    ],
)
def test_malformed_or_unknown_authorization_is_rejected(
    authorization: str | None,
    message: str,
) -> None:
    authenticator = TokenAuthenticator({PRIMARY_TOKEN: _principal()})

    with pytest.raises(AuthenticationError, match=message):
        authenticator.authenticate_header(authorization)


@pytest.mark.parametrize(
    "payload",
    [
        {"question": "What law applies?", "user_id": "client-user"},
        {"question": "What law applies?", "scope_id": "client-scope"},
        {"question": "What law applies?", "profile_id": PROFILE_ID},
        {"question": "What law applies?", "session_id": "client-session"},
        {"question": "What law applies?", "unexpected": True},
    ],
)
def test_run_body_cannot_carry_identity_or_extra_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError) as raised:
        RunCreateRequest.model_validate(payload)

    assert raised.value.errors()[0]["type"] == "extra_forbidden"


def test_session_body_also_forbids_client_identity_and_extra_fields() -> None:
    with pytest.raises(ValidationError) as raised:
        SessionCreateRequest.model_validate(
            {"title": "My session", "user_id": "client-user"}
        )

    assert raised.value.errors()[0]["type"] == "extra_forbidden"


def test_question_is_trimmed_and_default_retrieval_options_are_bounded() -> None:
    request = RunCreateRequest(question="  \n  What law applies?\t ")

    assert request.question == "What law applies?"
    assert request.retrieval.top_k == 5

    assert RunCreateRequest(
        question="Question", retrieval={"top_k": 1}
    ).retrieval.top_k == 1
    assert RunCreateRequest(
        question="Question", retrieval={"top_k": 20}
    ).retrieval.top_k == 20


@pytest.mark.parametrize(
    ("question", "error_type"),
    [("", "string_too_short"), (" ", "value_error"), ("\r\n\t", "value_error")],
)
def test_question_must_contain_non_whitespace_characters(
    question: str,
    error_type: str,
) -> None:
    with pytest.raises(ValidationError) as raised:
        RunCreateRequest(question=question)

    assert raised.value.errors()[0]["type"] == error_type


def test_question_schema_enforces_hard_input_ceiling() -> None:
    assert len(RunCreateRequest(question="q" * 100_000).question) == 100_000

    with pytest.raises(ValidationError) as raised:
        RunCreateRequest(question="q" * 100_001)

    assert raised.value.errors()[0]["type"] == "string_too_long"


@pytest.mark.parametrize("top_k", [0, 21])
def test_retrieval_top_k_must_stay_within_public_request_limits(top_k: int) -> None:
    with pytest.raises(ValidationError):
        RunCreateRequest(question="Question", retrieval={"top_k": top_k})


def test_service_settings_use_defaults_without_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_service_environment(monkeypatch)

    settings = ServiceSettings.from_env()

    assert settings == ServiceSettings()
    assert settings.graph_version == "m5-bounded-v1"


def test_service_settings_parse_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_service_environment(monkeypatch)
    overrides = {
        "LEGAL_RAG_QUESTION_MAX_CHARACTERS": "4096",
        "LEGAL_RAG_MESSAGE_PAGE_MAX": "75",
        "LEGAL_RAG_HISTORY_MAX_MESSAGES": "24",
        "LEGAL_RAG_HISTORY_MAX_CHARACTERS": "50000",
        "LEGAL_RAG_IDEMPOTENCY_TTL_SECONDS": "7200",
        "LEGAL_RAG_RUN_LEASE_SECONDS": "90",
        "LEGAL_RAG_EXECUTOR_TIMEOUT_SECONDS": "45.5",
        "LEGAL_RAG_SUPERVISOR_POLL_SECONDS": "0.5",
        "LEGAL_RAG_SSE_POLL_SECONDS": "0.75",
        "LEGAL_RAG_SSE_HEARTBEAT_SECONDS": "10.25",
    }
    for variable, value in overrides.items():
        monkeypatch.setenv(variable, f"  {value}  ")

    settings = ServiceSettings.from_env()

    assert settings.question_max_characters == 4096
    assert settings.message_page_max == 75
    assert settings.history_max_messages == 24
    assert settings.history_max_characters == 50_000
    assert settings.idempotency_ttl_seconds == 7200
    assert settings.lease_seconds == 90
    assert settings.executor_timeout_seconds == 45.5
    assert settings.supervisor_poll_seconds == 0.5
    assert settings.sse_poll_seconds == 0.75
    assert settings.sse_heartbeat_seconds == 10.25


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("LEGAL_RAG_QUESTION_MAX_CHARACTERS", "0", "between 1 and 100000"),
        ("LEGAL_RAG_MESSAGE_PAGE_MAX", "101", "between 1 and 100"),
        ("LEGAL_RAG_HISTORY_MAX_MESSAGES", "101", "between 1 and 100"),
        ("LEGAL_RAG_HISTORY_MAX_CHARACTERS", "100001", "between 1 and 100000"),
        ("LEGAL_RAG_HISTORY_MAX_MESSAGES", "many", "must be an integer"),
        ("LEGAL_RAG_EXECUTOR_TIMEOUT_SECONDS", "0", "greater than 0"),
        ("LEGAL_RAG_SUPERVISOR_POLL_SECONDS", "61", "at most 60"),
        ("LEGAL_RAG_SSE_POLL_SECONDS", "soon", "must be a number"),
        ("LEGAL_RAG_SSE_HEARTBEAT_SECONDS", "nan", "greater than 0"),
    ],
)
def test_service_settings_reject_invalid_environment_values(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    message: str,
) -> None:
    _clear_service_environment(monkeypatch)
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValueError, match=message):
        ServiceSettings.from_env()


@pytest.mark.parametrize(
    "serialized",
    [
        "not-json",
        "[]",
        "{}",
        json.dumps({PRIMARY_TOKEN: {"user_id": "user-alpha"}}),
        json.dumps(
            {
                PRIMARY_TOKEN: {
                    "user_id": "user-alpha",
                    "scope_id": "scope-alpha",
                    "profile_id": PROFILE_ID,
                    "client_override": "forbidden",
                }
            }
        ),
        json.dumps(
            {
                PRIMARY_TOKEN: {
                    "user_id": "user-alpha",
                    "scope_id": "scope-alpha",
                    "profile_id": "not-a-sha256",
                }
            }
        ),
    ],
)
def test_token_json_rejects_malformed_or_unsafe_registries(serialized: str) -> None:
    with pytest.raises(AuthenticationConfigurationError):
        TokenAuthenticator.from_json(serialized)


def test_environment_loader_parses_database_tokens_and_service_limits_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_service_environment(monkeypatch)
    database_url = "postgresql+psycopg://service:secret@db.invalid/legal_rag"
    monkeypatch.setenv("LEGAL_RAG_DATABASE_URL", database_url)
    monkeypatch.setenv(
        "LEGAL_RAG_AUTH_TOKENS_JSON",
        "  " + json.dumps(_token_payload()) + "  ",
    )
    monkeypatch.setenv("LEGAL_RAG_QUESTION_MAX_CHARACTERS", "2048")

    database, authenticator, service = load_environment_configuration()

    assert database.url == database_url
    assert database.redacted_url == (
        "postgresql+psycopg://service:***@db.invalid/legal_rag"
    )
    assert database.connect_timeout_seconds == 5
    assert database.pool_timeout_seconds == 5.0
    assert database.statement_timeout_ms == 30_000
    assert database.lock_timeout_ms == 5_000
    assert authenticator.authenticate_header(f"Bearer {PRIMARY_TOKEN}").user_id == (
        "user-alpha"
    )
    assert service.question_max_characters == 2048


def test_service_database_engine_applies_bounded_client_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_create_engine(url: str, **kwargs: object) -> object:
        captured["url"] = url
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(database_module, "create_engine", fake_create_engine)
    settings = DatabaseSettings(
        "postgresql+psycopg://service:secret@db.invalid/legal_rag",
        connect_timeout_seconds=7,
        pool_timeout_seconds=8.5,
        statement_timeout_ms=9_000,
        lock_timeout_ms=4_000,
    )

    engine = create_database_engine(settings)

    assert engine is sentinel
    assert captured == {
        "url": settings.url,
        "pool_pre_ping": True,
        "pool_timeout": 8.5,
        "connect_args": {
            "connect_timeout": 7,
            "options": "-c statement_timeout=9000 -c lock_timeout=4000",
        },
    }


def test_environment_loader_requires_database_and_token_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_service_environment(monkeypatch)

    with pytest.raises(DatabaseConfigurationError, match="LEGAL_RAG_DATABASE_URL"):
        load_environment_configuration()

    monkeypatch.setenv(
        "LEGAL_RAG_DATABASE_URL",
        "postgresql+psycopg://service:secret@db.invalid/legal_rag",
    )
    with pytest.raises(
        AuthenticationConfigurationError,
        match="LEGAL_RAG_AUTH_TOKENS_JSON is required",
    ):
        load_environment_configuration()


def test_legacy_cli_import_and_help_are_isolated_from_optional_service() -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = "\n".join(
        [
            "import importlib.abc",
            "import sys",
            "class RejectOptionalServiceImports(importlib.abc.MetaPathFinder):",
            "    def find_spec(self, fullname, path=None, target=None):",
            "        if fullname == 'legal_rag.api' or fullname.startswith('legal_rag.api.'):",
            "            raise AssertionError(f'legacy CLI imported {fullname}')",
            "        if fullname == 'legal_rag.storage' or fullname.startswith('legal_rag.storage.'):",
            "            raise AssertionError(f'legacy CLI imported {fullname}')",
            "        return None",
            "sys.meta_path.insert(0, RejectOptionalServiceImports())",
            "from legal_rag import cli",
            "assert not any(name == 'legal_rag.api' or name.startswith('legal_rag.api.') for name in sys.modules)",
            "assert not any(name == 'legal_rag.storage' or name.startswith('legal_rag.storage.') for name in sys.modules)",
            "try:",
            "    cli.main(['--help'])",
            "except SystemExit as exc:",
            "    assert exc.code == 0",
            "else:",
            "    raise AssertionError('--help did not exit')",
        ]
    )
    environment = os.environ.copy()
    environment.pop("LEGAL_RAG_DATABASE_URL", None)
    environment.pop("LEGAL_RAG_AUTH_TOKENS_JSON", None)

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "versioned Chinese law text snapshots" in completed.stdout
