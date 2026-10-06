"""Optional catalog hints must not read nonexistent lexical-index state."""

from types import SimpleNamespace

from legal_rag.chat import LegalChatAssistant
from legal_rag.embedding_contracts import EmbeddingProfileIdentity
from legal_rag.retrieval_contracts import RetrievalBoundary
from legal_rag.storage.retrieval import PgVectorExactRetriever


def test_vector_adapter_without_catalog_hints_can_prepare_question_without_egress():
    profile = EmbeddingProfileIdentity("fixture", "fixture/model", "r1", 3, True, "", "", False)
    boundary = RetrievalBoundary("scope-a", "snapshot-a", profile.profile_id)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("preparing a question must not encode or search")

    repository = SimpleNamespace(
        validate_context=lambda filters, expected_profile: expected_profile,
        search_vector=forbidden,
    )
    encoder = SimpleNamespace(profile=profile, encode_query=forbidden)
    retriever = PgVectorExactRetriever(repository, encoder=encoder, filters=boundary)
    # This adapter does not load a lexical corpus. Hints are an optional API;
    # lack of hints must not trigger a hidden database/provider operation.
    assert retriever.known_law_hints == ()
    assistant = LegalChatAssistant(retriever, model="offline-fixture", completion_client=SimpleNamespace(complete=forbidden))
    prepared = assistant.prepare_question("《合成甲法》第一条有哪些要求？")
    assert prepared.original_question == "《合成甲法》第一条有哪些要求？"
    assert calls == []
    assert retriever.retrieval_boundary == boundary
