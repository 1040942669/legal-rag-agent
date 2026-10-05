"""Producer identity contracts only, not a published M5 fault receipt."""
import json
from contextlib import contextmanager

import pytest

from integration_tests import m5_support as producer


class Database:
    def __init__(self, heads):
        self.heads, self.queries = heads, []
    @contextmanager
    def connect(self):
        yield self
    def execute(self, statement):
        self.queries.append(str(statement))
        return self
    def scalars(self):
        return self
    def all(self):
        return self.heads


@pytest.mark.parametrize("head", ["0006_m5_harness_recovery", "0008_execution_money"])
def test_new_fault_receipt_metadata_observes_actual_unique_head_without_writing_receipt(tmp_path, head):
    path = tmp_path / "not-created-fixture.json"
    database = Database([head])
    payload = producer._load_or_create_receipt(path, "a" * 40, engine=database)
    assert payload["database"]["migration_head"] == head
    assert not path.exists() and "alembic_version" in database.queries[0]


def test_same_candidate_cannot_mix_existing_receipt_with_different_database_head(tmp_path):
    path = tmp_path / "partial-fixture.json"
    path.write_text(json.dumps({"candidate_sha": "a" * 40,
        "database": {"migration_head": "0006_m5_harness_recovery"}, "scenarios": {}}), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(AssertionError, match="database migration head changed"):
        producer._load_or_create_receipt(path, "a" * 40, engine=Database(["0008_execution_money"]))
    assert path.read_bytes() == original


@pytest.mark.parametrize("heads", [[], ["one", "two"], [None], ["private\nhead"]])
def test_nonunique_or_invalid_fault_receipt_database_identity_is_rejected(heads):
    with pytest.raises(AssertionError, match="unique database migration head"):
        producer._database_migration_head(Database(heads))
