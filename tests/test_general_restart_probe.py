"""Candidate database facts must match the actual checkout, not old M6 labels."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("general_restart_probe", _ROOT / "scripts/m3_restart_probe.py")
assert _SPEC is not None and _SPEC.loader is not None
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def scalar(self, statement):
        return "0.8.1" if "pg_extension" in str(statement) else "180001"


class _Engine:
    def connect(self):
        return _Connection()


def _revision(monkeypatch, value):
    class _Context:
        def get_current_revision(self):
            return value
    monkeypatch.setattr(probe.MigrationContext, "configure", lambda connection: _Context())


def test_restart_probe_accepts_actual_current_head_instead_of_old_constant(monkeypatch):
    _revision(monkeypatch, "0008_execution_money")
    assert probe._database_facts(_Engine()) == {
        "migration_revision": "0008_execution_money", "pgvector_version": "0.8.1", "postgres_major": 18,
    }


@pytest.mark.parametrize("revision", ["0007_m6_jobs_outbox", "unrecognized_head", None])
def test_restart_probe_rejects_actual_db_head_that_differs_from_candidate(monkeypatch, revision):
    _revision(monkeypatch, revision)
    with pytest.raises(RuntimeError, match="unexpected migration revision"):
        probe._database_facts(_Engine())
