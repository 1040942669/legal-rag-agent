"""Exact legal references use catalog keys, never a fuzzy success fallback."""
from __future__ import annotations

from copy import deepcopy

from legal_rag.legal_references import canonical_law_title, canonical_article_number, parse_legal_references
from legal_rag.models import SearchResult
from legal_rag.retrieval_contracts import validate_retrieval_top_k
from legal_rag.retrieval_outcomes import RetrievalOutcome
from legal_rag.storage.catalog import ArticleLookupBoundary, ArticleLookupRequest
from legal_rag.storage.retrieval import BoundCorpus, BoundaryBoundRetriever


class _SelectedChunks:
    name = "exact_reference"

    def __init__(self, entries):
        self.entries = tuple(entries)

    def retrieve(self, query, top_k=5):
        return [SearchResult(deepcopy(entry.chunk), 1.0, rank, self.name,
                             {"score_kind": "exact_key_match", "raw_score": 1.0})
                for rank, entry in enumerate(self.entries[:top_k], 1)]


class ExactReferenceRetriever:
    name = "bound_reference_router"

    def __init__(self, *, corpus: BoundCorpus, lexical: BoundaryBoundRetriever, catalog,
                 pointer_revision: int, activation_id: str):
        if not isinstance(corpus, BoundCorpus) or lexical.retrieval_boundary != corpus.boundary:
            raise ValueError("exact router requires one authoritative frozen corpus")
        self._corpus = BoundCorpus(corpus.boundary, tuple(deepcopy(corpus.entries)))
        self._lexical, self._catalog = lexical, catalog
        self._lookup_boundary = ArticleLookupBoundary(
            scope_id=corpus.boundary.scope_id, snapshot_id=corpus.boundary.snapshot_id,
            law_ids=corpus.boundary.law_ids, version_ids=corpus.boundary.version_ids,
            article_ids=corpus.boundary.article_ids,
            pointer_revision=pointer_revision, activation_id=activation_id,
        )
        self._titles = {}
        for entry in self._corpus.entries:
            for article in entry.provenance.articles:
                self._titles.setdefault(canonical_law_title(article.title), set()).add(article.title)

    @property
    def retrieval_boundary(self):
        return self._corpus.boundary

    @property
    def boundary_fingerprint(self):
        return self.retrieval_boundary.fingerprint

    @property
    def known_law_hints(self) -> tuple[str, ...]:
        return tuple(sorted(self._titles))

    def retrieve(self, query: str, top_k: int = 5):
        return list(self.retrieve_outcome(query, top_k).results)

    def retrieve_outcome(self, query: str, top_k: int = 5) -> RetrievalOutcome:
        limit = validate_retrieval_top_k(top_k)
        analysis = parse_legal_references(query, known_law_titles=tuple(self._titles))
        pairs = tuple(dict.fromkeys((item.law_title, item.article_number) for item in analysis.requirements))
        if len(pairs) > 16:
            # Reject the whole oversized request before any unresolved branch,
            # lookup or truncation. No subset may masquerade as exact coverage.
            return RetrievalOutcome((), "exact_reference", "needs_disambiguation", ("too_many_references",))
        if analysis.unresolved:
            return RetrievalOutcome((), "exact_reference", "needs_disambiguation",
                                    ("unresolved_reference",), pairs)
        if not pairs:
            results = tuple(self._lexical.retrieve(query, top_k=limit))
            return RetrievalOutcome(results, "lexical", "found" if results else "not_found",
                                    ("lexical_results" if results else "lexical_no_results",))
        selected_by_pair = []
        reasons, ambiguous = [], False
        for law_title, article_number in pairs:
            titles = self._titles.get(law_title, set())
            if len(titles) != 1:
                ambiguous |= len(titles) > 1
                reasons.append("law_identity_ambiguous" if titles else "law_not_in_snapshot")
                selected_by_pair.append(())
                continue
            request = ArticleLookupRequest(self._lookup_boundary, next(iter(titles)), article_number,
                                           effective_on=self.retrieval_boundary.effective_on)
            lookup = self._catalog.lookup_article(request)
            if lookup.status != "found":
                ambiguous |= lookup.status == "needs_disambiguation"
                reasons.append("version_ambiguous" if lookup.status == "needs_disambiguation" else "exact_article_not_found")
                selected_by_pair.append(())
                continue
            match = lookup.match
            if (match.provenance.boundary != self._lookup_boundary
                or canonical_law_title(match.title) != law_title
                or canonical_article_number(match.article_number) != article_number):
                raise ValueError("catalog result is outside frozen reference authority")
            memberships = {item.chunk_id: item for item in match.provenance.memberships}
            entries = []
            for entry in self._corpus.entries:
                member = memberships.get(entry.chunk.chunk_id)
                if member is None or member.chunk_content_hash != entry.provenance.chunk_content_hash or member.snapshot_ordinal != entry.provenance.snapshot_ordinal:
                    continue
                if any((article.law_id, article.version_id, article.article_id) ==
                       (match.law_id, match.version_id, match.article_id)
                       for article in entry.provenance.articles):
                    entries.append(entry)
            selected_by_pair.append(tuple(sorted(entries, key=lambda entry: (entry.provenance.snapshot_ordinal, entry.chunk.chunk_id))))
            if not entries:
                reasons.append("no_authorized_chunk_membership")
        # Cover one original chunk for each pair before adding additional parts.
        selected, seen = [], set()
        for round_index in range(max(map(len, selected_by_pair), default=0)):
            for entries in selected_by_pair:
                if round_index < len(entries) and entries[round_index].chunk.chunk_id not in seen:
                    selected.append(entries[round_index])
                    seen.add(entries[round_index].chunk.chunk_id)
        bound = BoundaryBoundRetriever(_SelectedChunks(selected), corpus=self._corpus)
        results = tuple(bound.retrieve(query, top_k=limit))
        resolved = tuple(pair for pair, entries in zip(pairs, selected_by_pair)
                         if any(result.chunk.chunk_id in {entry.chunk.chunk_id for entry in entries} for result in results))
        if ambiguous:
            status = "needs_disambiguation"
        elif len(resolved) != len(pairs):
            status = "not_found"
            if all(selected_by_pair):
                reasons.append("top_k_cannot_cover_references")
        else:
            status = "found"
            reasons.append("exact_reference_chunks")
        return RetrievalOutcome(results, "exact_reference", status, tuple(dict.fromkeys(reasons)), pairs, resolved)
