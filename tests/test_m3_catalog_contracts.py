from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone

import pytest

from legal_rag.storage.catalog import (
    ActiveSnapshotSelection,
    ArticleLookupBoundary,
    ArticleLookupMatch,
    ArticleLookupProvenance,
    ArticleLookupRequest,
    ArticleLookupResult,
    ArticleSnapshotMembership,
    ArticleVersionCandidate,
    CatalogContractError,
    CatalogDataError,
    SnapshotActivationEvent,
    SnapshotActivationResult,
)
from legal_rag.storage.contracts import sha256_json


def _boundary(**changes) -> ArticleLookupBoundary:
    values = {
        "scope_id": "public-law",
        "snapshot_id": "snapshot-2025",
        "law_ids": None,
        "version_ids": None,
        "article_ids": None,
        "pointer_revision": None,
        "activation_id": None,
    }
    values.update(changes)
    return ArticleLookupBoundary(**values)


def _request(**changes) -> ArticleLookupRequest:
    values = {
        "boundary": _boundary(),
        "law_title": "虚构测试法",
        "article_number": "第一条",
        "law_id": None,
        "version_id": None,
        "effective_on": None,
    }
    values.update(changes)
    return ArticleLookupRequest(**values)


def _candidate(
    *,
    law_id: str = "fictional-law",
    version_id: str = "fictional-law-v1",
    article_id: str = "fictional-law-v1-article-1",
    valid_from: str | None = "2024-01-01",
    valid_to: str | None = None,
    verification_status: str = "verified",
) -> ArticleVersionCandidate:
    source_ref = f"fixtures/{version_id}.txt"
    metadata_hash = sha256_json(
        {
            "law_id": law_id,
            "version_id": version_id,
            "title": "虚构测试法",
            "valid_from": valid_from,
            "valid_to": valid_to,
            "verification_status": verification_status,
            "source_ref": source_ref,
        }
    )
    return ArticleVersionCandidate(
        law_id=law_id,
        version_id=version_id,
        article_id=article_id,
        title="虚构测试法",
        article_number="第一条",
        valid_from=valid_from,
        valid_to=valid_to,
        verification_status=verification_status,
        source_ref=source_ref,
        stored_law_version_content_hash="d" * 64,
        law_version_metadata_hash=metadata_hash,
        article_content_hash="a" * 64,
    )


def _found_result(request: ArticleLookupRequest) -> ArticleLookupResult:
    article_payload = {
        "article_id": "fictional-law-v1-article-1",
        "law_id": "fictional-law",
        "version_id": "fictional-law-v1",
        "article_number": "第一条",
        "body": "权威正文。",
        "raw_text": "第一条 权威正文。",
        "source_ref": "fixtures/fictional-law-v1.txt",
        "source_line": 1,
        "parse_status": "from_filename",
    }
    content_hash = sha256_json(article_payload)
    law_version_source_ref = "fixtures/fictional-law-v1.txt"
    law_version_metadata_hash = sha256_json(
        {
            "law_id": article_payload["law_id"],
            "version_id": article_payload["version_id"],
            "title": "虚构测试法",
            "valid_from": "2024-01-01",
            "valid_to": None,
            "verification_status": "verified",
            "source_ref": law_version_source_ref,
        }
    )
    memberships = (
        ArticleSnapshotMembership(
            chunk_id="chunk-1",
            snapshot_ordinal=0,
            article_ordinal=0,
            chunk_content_hash="f" * 64,
        ),
    )
    membership_fingerprint = sha256_json(
        {
            "schema_version": 1,
            "scope_id": request.boundary.scope_id,
            "snapshot_id": request.boundary.snapshot_id,
            "article_id": article_payload["article_id"],
            "memberships": [item.trace_payload() for item in memberships],
        }
    )
    provenance = ArticleLookupProvenance(
        boundary=request.boundary,
        request_fingerprint=request.fingerprint,
        scope_id=request.boundary.scope_id,
        snapshot_id=request.boundary.snapshot_id,
        snapshot_corpus_hash="b" * 64,
        law_id=article_payload["law_id"],
        version_id=article_payload["version_id"],
        article_id=article_payload["article_id"],
        law_version_source_ref=law_version_source_ref,
        stored_law_version_content_hash="d" * 64,
        law_version_metadata_hash=law_version_metadata_hash,
        article_content_hash=content_hash,
        article_payload_hash=content_hash,
        memberships=memberships,
        membership_fingerprint=membership_fingerprint,
    )
    match = ArticleLookupMatch(
        evidence_id=article_payload["article_id"],
        rank=1,
        raw_score=1.0,
        score_kind="exact_key_match",
        title="虚构测试法",
        article_number=article_payload["article_number"],
        body=article_payload["body"],
        raw_text=article_payload["raw_text"],
        source_ref=article_payload["source_ref"],
        source_line=article_payload["source_line"],
        parse_status=article_payload["parse_status"],
        law_id=article_payload["law_id"],
        version_id=article_payload["version_id"],
        article_id=article_payload["article_id"],
        valid_from="2024-01-01",
        valid_to=None,
        verification_status="verified",
        provenance=provenance,
    )
    return ArticleLookupResult.found(request, match)


def test_boundary_is_profile_free_immutable_and_content_addressed() -> None:
    boundary = _boundary(
        law_ids=("fictional-law",),
        version_ids=("fictional-law-v1",),
        article_ids=("fictional-law-v1-article-1",),
        pointer_revision=3,
        activation_id="activation-3",
    )
    same = _boundary(
        law_ids=("fictional-law",),
        version_ids=("fictional-law-v1",),
        article_ids=("fictional-law-v1-article-1",),
        pointer_revision=3,
        activation_id="activation-3",
    )

    assert boundary.fingerprint == same.fingerprint
    assert boundary.trace_payload() == {
        "scope_id": "public-law",
        "snapshot_id": "snapshot-2025",
        "law_ids": ["fictional-law"],
        "version_ids": ["fictional-law-v1"],
        "article_ids": ["fictional-law-v1-article-1"],
        "pointer_revision": 3,
        "activation_id": "activation-3",
    }
    assert "profile_id" not in boundary.trace_payload()
    with pytest.raises(FrozenInstanceError):
        boundary.scope_id = "changed"  # type: ignore[misc]


def test_request_canonicalizes_article_number_and_date_deterministically() -> None:
    request = _request(
        article_number="第一 条",
        version_id="fictional-law-v1",
        effective_on="2025-01-01",
    )
    same = _request(
        version_id="fictional-law-v1",
        effective_on=date(2025, 1, 1),
    )

    assert request.article_number == "第一条"
    assert request.effective_on == date(2025, 1, 1)
    assert request.fingerprint == same.fingerprint
    assert request.trace_payload() == {
        "boundary_fingerprint": request.boundary.fingerprint,
        "law_title": "虚构测试法",
        "article_number": "第一条",
        "law_id": None,
        "version_id": "fictional-law-v1",
        "effective_on": "2025-01-01",
    }


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: _boundary(scope_id=" "), "scope_id"),
        (lambda: _boundary(snapshot_id="snapshot\nother"), "snapshot_id"),
        (lambda: _boundary(law_ids=("law", "law")), "duplicates"),
        (lambda: _boundary(pointer_revision=1), "activation_id"),
        (lambda: _boundary(activation_id="activation-1"), "pointer_revision"),
        (
            lambda: _boundary(pointer_revision=0, activation_id="activation-0"),
            "positive",
        ),
        (lambda: _request(law_title=" 虚构测试法"), "law_title"),
        (lambda: _request(article_number=""), "article_number"),
        (lambda: _request(article_number="条" * 129), "article_number"),
        (lambda: _request(version_id="v1 "), "version_id"),
        (lambda: _request(effective_on="2025-02-30"), "effective_on"),
        (
            lambda: _request(
                boundary=_boundary(law_ids=("allowed-law",)),
                law_id="other-law",
            ),
            "law_id",
        ),
        (
            lambda: _request(
                boundary=_boundary(version_ids=("allowed-v1",)),
                version_id="other-v1",
            ),
            "version_id",
        ),
    ],
)
def test_lookup_contracts_reject_ambiguous_or_noncanonical_inputs(
    factory, message: str
) -> None:
    with pytest.raises(CatalogContractError, match=message):
        factory()


def test_lookup_result_states_are_disjoint_and_do_not_leak_bodies() -> None:
    request = _request()
    found = _found_result(request)
    missing = ArticleLookupResult.not_found(request, reason="no_exact_match")
    ambiguous = ArticleLookupResult.needs_disambiguation(
        request,
        reason="multiple_exact_versions",
        candidates=(
            _candidate(),
            _candidate(
                version_id="fictional-law-v2",
                article_id="fictional-law-v2-article-1",
                valid_from="2025-01-01",
            ),
        ),
    )

    assert found.status == "found"
    assert found.match is not None
    assert found.candidates == ()
    assert missing.status == "not_found"
    assert missing.match is None
    assert missing.candidates == ()
    assert ambiguous.status == "needs_disambiguation"
    assert ambiguous.match is None
    assert [item.version_id for item in ambiguous.candidates] == [
        "fictional-law-v1",
        "fictional-law-v2",
    ]
    assert not hasattr(ambiguous.candidates[0], "body")


def test_lookup_result_accepts_each_reason_only_with_its_proof_shape() -> None:
    request = _request()
    effective_request = _request(effective_on="2025-06-01")
    explicit_effective_request = _request(
        version_id="fictional-law-v1",
        effective_on="2025-06-01",
    )
    denied_request = _request(boundary=_boundary(article_ids=()))
    v1 = _candidate(valid_to="2026-01-01")
    v2 = _candidate(
        version_id="fictional-law-v2",
        article_id="fictional-law-v2-article-1",
        valid_from="2025-01-01",
    )
    unknown = _candidate(
        version_id="fictional-law-v3",
        article_id="fictional-law-v3-article-1",
        valid_from=None,
        verification_status="unknown",
    )
    other_law = _candidate(
        law_id="other-fictional-law",
        version_id="other-fictional-law-v1",
        article_id="other-fictional-law-v1-article-1",
    )

    assert _found_result(request).reason == "exact_match"
    assert (
        ArticleLookupResult.not_found(
            denied_request,
            reason="boundary_denies_all",
        ).reason
        == "boundary_denies_all"
    )
    assert (
        ArticleLookupResult.not_found(request, reason="no_exact_match").reason
        == "no_exact_match"
    )
    assert (
        ArticleLookupResult.not_found(
            explicit_effective_request,
            reason="version_not_effective",
        ).reason
        == "version_not_effective"
    )
    assert (
        ArticleLookupResult.not_found(
            effective_request,
            reason="no_effective_version",
        ).reason
        == "no_effective_version"
    )
    assert (
        ArticleLookupResult.needs_disambiguation(
            request,
            reason="multiple_law_identities",
            candidates=(v1, other_law),
        ).reason
        == "multiple_law_identities"
    )
    assert (
        ArticleLookupResult.needs_disambiguation(
            request,
            reason="multiple_exact_versions",
            candidates=(v1, v2),
        ).reason
        == "multiple_exact_versions"
    )
    assert (
        ArticleLookupResult.needs_disambiguation(
            effective_request,
            reason="overlapping_validity",
            candidates=(v1, v2),
        ).reason
        == "overlapping_validity"
    )
    assert (
        ArticleLookupResult.needs_disambiguation(
            effective_request,
            reason="validity_unknown",
            candidates=(v1, unknown),
        ).reason
        == "validity_unknown"
    )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("found", "no_exact_match"),
        ("not_found", "exact_match"),
        ("needs_disambiguation", "no_effective_version"),
        ("found", "invented_reason"),
    ],
)
def test_lookup_result_rejects_reason_status_mismatches(
    status: str,
    reason: str,
) -> None:
    request = _request()
    match = _found_result(request).match if status == "found" else None
    candidates = (_candidate(),) if status == "needs_disambiguation" else ()

    with pytest.raises(CatalogContractError, match="valid reason"):
        ArticleLookupResult(
            status=status,  # type: ignore[arg-type]
            reason=reason,
            request=request,
            match=match,
            candidates=candidates,
        )


@pytest.mark.parametrize(
    ("lookup_request", "reason", "message"),
    [
        (_request(), "boundary_denies_all", "deny-all boundary"),
        (
            _request(boundary=_boundary(article_ids=())),
            "no_exact_match",
            "deny-all boundary",
        ),
        (_request(), "version_not_effective", "explicit version"),
        (
            _request(version_id="fictional-law-v1"),
            "version_not_effective",
            "effective date",
        ),
        (_request(), "no_effective_version", "effective date"),
        (
            _request(
                version_id="fictional-law-v1",
                effective_on="2025-06-01",
            ),
            "no_effective_version",
            "without an explicit version",
        ),
    ],
)
def test_not_found_reason_requires_matching_request_proof(
    lookup_request: ArticleLookupRequest,
    reason: str,
    message: str,
) -> None:
    with pytest.raises(CatalogContractError, match=message):
        ArticleLookupResult.not_found(lookup_request, reason=reason)


def test_multiple_law_identities_reason_requires_distinct_unpinned_laws() -> None:
    same_law_candidates = (
        _candidate(),
        _candidate(
            version_id="fictional-law-v2",
            article_id="fictional-law-v2-article-1",
        ),
    )

    with pytest.raises(CatalogContractError, match="two unpinned law identities"):
        ArticleLookupResult.needs_disambiguation(
            _request(),
            reason="multiple_law_identities",
            candidates=same_law_candidates,
        )
    with pytest.raises(CatalogContractError, match="two unpinned law identities"):
        ArticleLookupResult.needs_disambiguation(
            _request(law_id="fictional-law"),
            reason="multiple_law_identities",
            candidates=same_law_candidates,
        )


@pytest.mark.parametrize(
    "lookup_request",
    [
        _request(effective_on="2025-06-01"),
        _request(version_id="fictional-law-v1"),
    ],
)
def test_multiple_exact_versions_reason_rejects_date_or_explicit_version(
    lookup_request: ArticleLookupRequest,
) -> None:
    if lookup_request.version_id is None:
        candidates = (
            _candidate(),
            _candidate(
                version_id="fictional-law-v2",
                article_id="fictional-law-v2-article-1",
            ),
        )
    else:
        candidates = (
            _candidate(),
            _candidate(article_id="fictional-law-v1-article-1-copy"),
        )

    with pytest.raises(CatalogContractError, match="multiple_exact_versions"):
        ArticleLookupResult.needs_disambiguation(
            lookup_request,
            reason="multiple_exact_versions",
            candidates=candidates,
        )


def test_multiple_exact_versions_reason_rejects_cross_law_or_single_version() -> None:
    with pytest.raises(CatalogContractError, match="multiple_exact_versions"):
        ArticleLookupResult.needs_disambiguation(
            _request(),
            reason="multiple_exact_versions",
            candidates=(
                _candidate(),
                _candidate(
                    law_id="other-fictional-law",
                    version_id="other-fictional-law-v1",
                    article_id="other-fictional-law-v1-article-1",
                ),
            ),
        )
    with pytest.raises(CatalogContractError, match="multiple_exact_versions"):
        ArticleLookupResult.needs_disambiguation(
            _request(),
            reason="multiple_exact_versions",
            candidates=(
                _candidate(),
                _candidate(article_id="fictional-law-v1-article-1-copy"),
            ),
        )


@pytest.mark.parametrize(
    "lookup_request",
    [
        _request(),
        _request(
            version_id="fictional-law-v1",
            effective_on="2025-06-01",
        ),
    ],
)
def test_overlapping_validity_reason_requires_date_without_explicit_version(
    lookup_request: ArticleLookupRequest,
) -> None:
    candidates = (
        _candidate(valid_to="2026-01-01"),
        _candidate(
            version_id="fictional-law-v2",
            article_id="fictional-law-v2-article-1",
            valid_from="2025-01-01",
        ),
    )
    if lookup_request.version_id is not None:
        candidates = (
            candidates[0],
            _candidate(
                article_id="fictional-law-v1-article-1-copy",
                valid_from="2025-01-01",
            ),
        )

    with pytest.raises(CatalogContractError, match="overlapping_validity"):
        ArticleLookupResult.needs_disambiguation(
            lookup_request,
            reason="overlapping_validity",
            candidates=candidates,
        )


@pytest.mark.parametrize(
    "candidates",
    [
        (_candidate(),),
        (
            _candidate(valid_from="2020-01-01", valid_to="2024-01-01"),
            _candidate(
                version_id="fictional-law-v2",
                article_id="fictional-law-v2-article-1",
                valid_from="2025-01-01",
            ),
        ),
        (
            _candidate(),
            _candidate(
                law_id="other-fictional-law",
                version_id="other-fictional-law-v1",
                article_id="other-fictional-law-v1-article-1",
            ),
        ),
    ],
)
def test_overlapping_validity_reason_requires_two_active_versions_of_one_law(
    candidates: tuple[ArticleVersionCandidate, ...],
) -> None:
    with pytest.raises(CatalogContractError, match="overlapping_validity"):
        ArticleLookupResult.needs_disambiguation(
            _request(effective_on="2025-06-01"),
            reason="overlapping_validity",
            candidates=candidates,
        )


def test_validity_unknown_reason_requires_date_and_an_uncertain_candidate() -> None:
    verified = _candidate()

    with pytest.raises(CatalogContractError, match="effective date"):
        ArticleLookupResult.needs_disambiguation(
            _request(),
            reason="validity_unknown",
            candidates=(verified,),
        )
    with pytest.raises(CatalogContractError, match="unknown validity"):
        ArticleLookupResult.needs_disambiguation(
            _request(effective_on="2025-06-01"),
            reason="validity_unknown",
            candidates=(verified,),
        )


def test_validity_unknown_reason_rejects_expired_verified_or_cross_law_candidates() -> (
    None
):
    unknown = _candidate(valid_from=None, verification_status="unknown")
    expired = _candidate(
        version_id="fictional-law-v2",
        article_id="fictional-law-v2-article-1",
        valid_from="2020-01-01",
        valid_to="2024-01-01",
    )
    other_law_unknown = _candidate(
        law_id="other-fictional-law",
        version_id="other-fictional-law-v1",
        article_id="other-fictional-law-v1-article-1",
        valid_from=None,
        verification_status="unknown",
    )

    with pytest.raises(CatalogContractError, match="currently effective"):
        ArticleLookupResult.needs_disambiguation(
            _request(effective_on="2025-06-01"),
            reason="validity_unknown",
            candidates=(unknown, expired),
        )
    with pytest.raises(CatalogContractError, match="one law identity"):
        ArticleLookupResult.needs_disambiguation(
            _request(effective_on="2025-06-01"),
            reason="validity_unknown",
            candidates=(unknown, other_law_unknown),
        )


def test_found_result_rejects_boundary_provenance_and_exact_score_drift() -> None:
    request = _request()
    result = _found_result(request)
    assert result.match is not None
    match = result.match

    with pytest.raises(CatalogContractError, match="scope"):
        ArticleLookupResult.found(
            request,
            replace(
                match,
                provenance=replace(match.provenance, scope_id="other-scope"),
            ),
        )
    with pytest.raises(CatalogContractError, match="fingerprint"):
        ArticleLookupResult.found(
            request,
            replace(
                match,
                provenance=replace(
                    match.provenance,
                    request_fingerprint="c" * 64,
                ),
            ),
        )
    with pytest.raises(CatalogContractError, match="rank"):
        replace(match, rank=2)
    with pytest.raises(CatalogContractError, match="raw_score"):
        replace(match, raw_score=0.5)
    with pytest.raises(CatalogContractError, match="score_kind"):
        replace(match, score_kind="cosine_similarity")
    with pytest.raises(CatalogContractError, match="law version metadata hash"):
        replace(match, valid_from="2023-01-01")
    with pytest.raises(CatalogContractError, match="membership fingerprint"):
        replace(
            match.provenance,
            memberships=(
                replace(
                    match.provenance.memberships[0],
                    chunk_id="invented-chunk",
                ),
            ),
        )


def test_empty_boundary_selector_is_explicit_deny_all() -> None:
    boundary = _boundary(article_ids=())
    request = _request(boundary=boundary)

    assert boundary.denies_all is True
    result = ArticleLookupResult.not_found(request, reason="boundary_denies_all")
    assert result.status == "not_found"


def test_activation_result_mirrors_revision_chain_and_aware_time_invariants() -> None:
    occurred_at = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    event = SnapshotActivationEvent(
        activation_id="activation-1",
        scope_id="public-law",
        revision=1,
        operation="initial_activate",
        previous_snapshot_id=None,
        target_snapshot_id="snapshot-2025",
        previous_activation_id=None,
        actor="test",
        reason="initial",
        occurred_at=occurred_at,
    )
    selection = ActiveSnapshotSelection(
        scope_id="public-law",
        snapshot_id="snapshot-2025",
        revision=1,
        activation_id="activation-1",
        corpus_hash="e" * 64,
        activated_at=occurred_at,
    )

    result = SnapshotActivationResult(
        selection=selection,
        event=event,
        changed=True,
    )
    assert result.event == event

    with pytest.raises(CatalogDataError, match="revision 1"):
        replace(event, operation="replace")
    with pytest.raises(CatalogDataError, match="requires a predecessor"):
        replace(event, revision=2, operation="replace")
    with pytest.raises(CatalogDataError, match="must differ"):
        SnapshotActivationEvent(
            activation_id="activation-2",
            scope_id="public-law",
            revision=2,
            operation="rollback",
            previous_snapshot_id="snapshot-2025",
            target_snapshot_id="snapshot-2025",
            previous_activation_id="activation-1",
            actor=None,
            reason=None,
            occurred_at=occurred_at,
        )
    with pytest.raises(CatalogDataError, match="timezone-aware"):
        replace(event, occurred_at=occurred_at.replace(tzinfo=None))
    with pytest.raises(CatalogDataError, match="timestamps disagree"):
        SnapshotActivationResult(
            selection=replace(
                selection,
                activated_at=occurred_at + timedelta(seconds=1),
            ),
            event=event,
            changed=True,
        )
