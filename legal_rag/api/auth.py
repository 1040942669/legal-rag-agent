from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any, Mapping


class AuthenticationError(ValueError):
    """Raised when an HTTP credential cannot be authenticated."""


class AuthenticationConfigurationError(ValueError):
    """Raised when the server-side token registry is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class ServicePrincipal:
    user_id: str
    scope_id: str
    profile_id: str

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("user_id", self.user_id, 128),
            ("scope_id", self.scope_id, 255),
            ("profile_id", self.profile_id, 64),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > maximum
                or any(ord(character) < 32 for character in value)
            ):
                raise AuthenticationConfigurationError(
                    f"{name} is invalid in the token registry"
                )
        if len(self.profile_id) != 64 or any(
            character not in "0123456789abcdef" for character in self.profile_id
        ):
            raise AuthenticationConfigurationError(
                "profile_id in the token registry must be a lowercase SHA-256 value"
            )


class TokenAuthenticator:
    """Map opaque bearer tokens to server-controlled identities.

    Tokens are never used as database identities and are not exposed by this
    object.  Comparison uses :func:`secrets.compare_digest`; request headers,
    query parameters, and bodies cannot override the resulting principal.
    """

    def __init__(self, token_principals: Mapping[str, ServicePrincipal]) -> None:
        if not token_principals:
            raise AuthenticationConfigurationError(
                "at least one bearer token must be configured"
            )
        validated: list[tuple[str, ServicePrincipal]] = []
        for token, principal in token_principals.items():
            if (
                not isinstance(token, str)
                or token != token.strip()
                or len(token) < 32
                or len(token) > 512
                or any(ord(character) < 33 or ord(character) == 127 for character in token)
            ):
                raise AuthenticationConfigurationError(
                    "bearer tokens must be opaque printable values of 32-512 characters"
                )
            if not isinstance(principal, ServicePrincipal):
                raise AuthenticationConfigurationError(
                    "token registry values must be ServicePrincipal objects"
                )
            validated.append((token, principal))
        self._entries = tuple(validated)

    @property
    def principals(self) -> tuple[ServicePrincipal, ...]:
        """Return de-duplicated identities without exposing credential values."""

        unique: dict[tuple[str, str, str], ServicePrincipal] = {}
        for _, principal in self._entries:
            unique[(principal.user_id, principal.scope_id, principal.profile_id)] = principal
        return tuple(unique.values())

    @classmethod
    def from_json(cls, value: str) -> TokenAuthenticator:
        try:
            raw = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise AuthenticationConfigurationError(
                "LEGAL_RAG_AUTH_TOKENS_JSON must be valid JSON"
            ) from exc
        if not isinstance(raw, dict):
            raise AuthenticationConfigurationError(
                "LEGAL_RAG_AUTH_TOKENS_JSON must be an object"
            )
        registry: dict[str, ServicePrincipal] = {}
        for token, principal_payload in raw.items():
            if not isinstance(principal_payload, dict) or set(principal_payload) != {
                "user_id",
                "scope_id",
                "profile_id",
            }:
                raise AuthenticationConfigurationError(
                    "each token must map to user_id, scope_id, and profile_id"
                )
            registry[token] = ServicePrincipal(**principal_payload)
        return cls(registry)

    def authenticate_header(self, authorization: str | None) -> ServicePrincipal:
        if not isinstance(authorization, str):
            raise AuthenticationError("bearer authentication is required")
        scheme, separator, credential = authorization.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not credential
            or credential != credential.strip()
        ):
            raise AuthenticationError("bearer authentication is required")
        match: ServicePrincipal | None = None
        for configured_token, principal in self._entries:
            if secrets.compare_digest(credential, configured_token):
                match = principal
        if match is None:
            raise AuthenticationError("bearer authentication is invalid")
        return match


def principals_from_mapping(value: Mapping[str, Mapping[str, Any]]) -> dict[str, ServicePrincipal]:
    """Test/configuration helper that still applies the production validation."""

    return {
        token: ServicePrincipal(
            user_id=str(payload["user_id"]),
            scope_id=str(payload["scope_id"]),
            profile_id=str(payload["profile_id"]),
        )
        for token, payload in value.items()
    }
