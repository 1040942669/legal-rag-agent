"""The producer must observe DB identity; these are not worker receipts."""
from contextlib import contextmanager

import pytest

from integration_tests import test_m6_worker_real as producer


class Database:
    def __init__(self, revisions):
        self.revisions, self.queries = revisions, []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, statement):
        self.queries.append(str(statement))
        return self

    def scalars(self):
        return self

    def all(self):
        return self.revisions


@pytest.mark.parametrize("head", ["0007_m6_jobs_outbox", "0008_execution_money"])
def test_receipt_identity_comes_from_unique_actual_database_head(head):
    database = Database([head])
    assert producer._database_migration_head(database) == head
    assert len(database.queries) == 1 and "alembic_version" in database.queries[0]


@pytest.mark.parametrize("heads", [[], ["0007_m6_jobs_outbox", "0008_execution_money"],
                                   [None], ["injected\nrevision"], ["x" * 65]])
def test_missing_multiple_or_unsafe_database_identity_never_becomes_a_receipt(heads):
    with pytest.raises(pytest.fail.Exception, match="unique database migration head"):
        producer._database_migration_head(Database(heads))
