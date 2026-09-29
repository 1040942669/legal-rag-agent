from __future__ import annotations

import json
from pathlib import Path

import pytest

from legal_rag.jobs.registry import JobRegistry, JobRegistryError


def _evaluation_config(tmp_path: Path) -> dict[str, object]:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    repository_root = Path(__file__).resolve().parents[1]
    return {
        "schema_version": 1,
        "artifact_root": str(artifact_root),
        "repository_root": str(repository_root),
        "evaluations": {
            "offline-smoke": {
                "scope_id": "scope-a",
                "profile_id": "a" * 64,
                "dataset_id": "synthetic-offline-v1",
                "experiment_root": "experiments",
                "total": 2,
            }
        },
        "ingestions": {},
    }


def test_registry_binds_entry_to_scope_profile_and_pinned_fingerprint(
    tmp_path: Path,
) -> None:
    config = _evaluation_config(tmp_path)
    config_path = tmp_path / "registry.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    registry = JobRegistry.from_json_file(config_path)
    entry = registry.evaluation(
        "offline-smoke", scope_id="scope-a", profile_id="a" * 64
    )
    assert entry is not None
    assert entry.total == 2
    assert entry.dataset_id == "synthetic-offline-v1"
    assert len(entry.fingerprint) == 64
    assert (
        registry.evaluation("offline-smoke", scope_id="scope-b", profile_id="a" * 64)
        is None
    )
    assert (
        registry.evaluation("offline-smoke", scope_id="scope-a", profile_id="b" * 64)
        is None
    )
    assert (
        registry.evaluation("unknown", scope_id="scope-a", profile_id="a" * 64) is None
    )
    assert registry.resolve("evaluation", "offline-smoke") == entry

    config["evaluations"]["offline-smoke"]["experiment_root"] = (
        "another-experiment-root"
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")
    changed = JobRegistry.from_json_file(config_path)
    changed_entry = changed.resolve("evaluation", "offline-smoke")
    assert changed_entry is not None
    assert changed_entry.fingerprint != entry.fingerprint


@pytest.mark.parametrize(
    "experiment_root",
    (
        "../elsewhere",
        "/absolute/path",
        "C:\\absolute\\path",
        "experiments/../../escape",
    ),
)
def test_registry_rejects_out_of_root_artifact_paths(
    tmp_path: Path, experiment_root: str
) -> None:
    config = _evaluation_config(tmp_path)
    config["evaluations"]["offline-smoke"]["experiment_root"] = experiment_root
    config_path = tmp_path / "registry.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(JobRegistryError):
        JobRegistry.from_json_file(config_path)


def test_registry_rejects_duplicate_json_keys_and_unknown_fields(
    tmp_path: Path,
) -> None:
    config = _evaluation_config(tmp_path)
    config["unexpected"] = "ignored-by-unsafe-parser"
    config_path = tmp_path / "registry.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(JobRegistryError):
        JobRegistry.from_json_file(config_path)

    config_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(JobRegistryError):
        JobRegistry.from_json_file(config_path)


def test_registry_rejects_symlink_escape(tmp_path: Path) -> None:
    config = _evaluation_config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "artifacts" / "experiments"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        config["evaluations"]["offline-smoke"]["experiment_root"] = (
            "experiments/../../outside"
        )
    config_path = tmp_path / "registry.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(JobRegistryError):
        JobRegistry.from_json_file(config_path)
