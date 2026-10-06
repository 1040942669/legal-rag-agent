"""Synthetic isolated-node contracts; never start Java or a search service."""
from pathlib import Path
import hashlib
import urllib.error
import zipfile

import yaml

import pytest

from scripts import isolated_elasticsearch as node


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "elasticsearch"
    for folder in ("jdk/bin", "lib/cli-launcher", "lib/tools/server-cli", "config", "plugins/analysis-ik/config"):
        (home / folder).mkdir(parents=True, exist_ok=True)
    for name in ("jdk/bin/java.exe", "lib/elasticsearch-9.1.4.jar", "lib/cli-launcher/launcher.jar",
                 "lib/tools/server-cli/server.jar", "plugins/analysis-ik/ik.jar"):
        (home / name).write_bytes(b"fictional-binary")
    (home / "jdk/release").write_text('JAVA_VERSION="24.0.2"\n', encoding="utf-8")
    (home / "plugins/analysis-ik/plugin-descriptor.properties").write_text(
        "name=analysis-ik\nversion=9.1.4\nelasticsearch.version=9.1.4\njava.version=17\n", encoding="utf-8")
    (home / "plugins/analysis-ik/config/IKAnalyzer.cfg.xml").write_text(
        '<properties><entry key="ext_dict"></entry><entry key="ext_stopwords"></entry></properties>', encoding="utf-8")
    (home / "plugins/analysis-ik/config/main.dic").write_text("合成\n", encoding="utf-8")
    for name in ("jvm.options", "log4j2.properties"):
        (home / "config" / name).write_text("# fictional default\n", encoding="utf-8")
    return home


def test_home_identity_freezes_java_jars_dictionary(home):
    before = node.verify_home(home)
    assert before["elasticsearch_version"] == "9.1.4"
    assert before["java_version"] == "24.0.2"
    assert "plugins/analysis-ik/config/main.dic" in before["files"]
    (home / "plugins/analysis-ik/config/main.dic").write_text("不同\n", encoding="utf-8")
    assert node.verify_home(home) != before


@pytest.mark.parametrize("key", ["ext_dict", "ext_stopwords", "remote_ext_dict", "remote_ext_stopwords"])
def test_no_custom_or_remote_dictionary(home, key):
    (home / "plugins/analysis-ik/config/IKAnalyzer.cfg.xml").write_text(
        f'<properties><entry key="{key}">https://invalid.example/dict</entry></properties>', encoding="utf-8")
    with pytest.raises(node.IsolatedElasticsearchError, match="dictionary_extension_forbidden"):
        node.verify_home(home)


def test_plugin_version_must_match(home):
    descriptor = home / "plugins/analysis-ik/plugin-descriptor.properties"
    descriptor.write_text("name=analysis-ik\nversion=9.1.3\nelasticsearch.version=9.1.3\n", encoding="utf-8")
    with pytest.raises(node.IsolatedElasticsearchError, match="plugin_version_mismatch"):
        node.verify_home(home)


class FakeProcess:
    pid = 765432
    def poll(self):
        return None


@pytest.fixture
def launch(home, tmp_path, monkeypatch):
    calls, stopped = [], []
    monkeypatch.setattr(node.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)) or FakeProcess())
    monkeypatch.setattr(node.IsolatedElasticsearch, "_remember", lambda self: None)
    monkeypatch.setattr(node, "terminate_process_tree", lambda process, **kw: stopped.append((process, kw)))
    monkeypatch.setattr(node, "_check_ports", lambda *args: None)
    instance = node.IsolatedElasticsearch(home, tmp_path / "run", 33491, 33492)
    monkeypatch.setattr(instance, "_request", lambda path: {
        "version": {"number": "9.1.4"}, "cluster_name": instance.cluster_name,
        "cluster_uuid": "synthetic-uuid", "name": "synthetic-node"})
    return instance, calls, stopped


def test_launch_is_bundled_loopback_no_shell_or_injected_env(launch, monkeypatch):
    instance, calls, stopped = launch
    for key in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "ES_JAVA_OPTS", "PRIVATE_KEY", "HTTPS_PROXY"):
        monkeypatch.setenv(key, "should-not-be-forwarded")
    assert instance.start() is instance
    args, kwargs = calls[0]
    assert Path(args[0]) == instance.home / "jdk/bin/java.exe"
    assert kwargs["shell"] is False
    assert not any(key in kwargs["env"] for key in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "PRIVATE_KEY", "HTTPS_PROXY"))
    config = (instance.work_dir / "config/elasticsearch.yml").read_text(encoding="utf-8")
    settings = yaml.safe_load(config)
    assert settings["network.host"] == "127.0.0.1"
    assert settings["discovery.type"] == "single-node"
    assert settings["ingest.geoip.downloader.enabled"] is False
    assert settings["xpack.security.autoconfiguration.enabled"] is False
    assert settings["xpack.license.self_generated.type"] == "basic"
    assert instance.endpoint == "http://127.0.0.1:33491"
    assert instance.identity["observed"]["cluster_uuid"] == "synthetic-uuid"
    instance.close()
    instance.close()
    assert len(stopped) == 1


def test_wrong_live_identity_fails_and_closes(launch, monkeypatch):
    instance, _, stopped = launch
    monkeypatch.setattr(instance, "_request", lambda path: {"version": {"number": "9.1.4"}, "cluster_name": "other", "cluster_uuid": "x"})
    with pytest.raises(node.IsolatedElasticsearchError, match="unexpected_node_identity"):
        instance.start()
    assert len(stopped) == 1


def test_home_drift_before_start_rejected(launch):
    instance, calls, _ = launch
    (instance.home / "plugins/analysis-ik/ik.jar").write_bytes(b"changed")
    with pytest.raises(node.IsolatedElasticsearchError, match="home_identity_changed"):
        instance.start()
    assert calls == []


def test_existing_work_never_overwritten(launch):
    instance, calls, _ = launch
    instance.work_dir.mkdir()
    existing = instance.work_dir / "kept.txt"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(node.IsolatedElasticsearchError, match="work_directory_exists"):
        instance.start()
    assert existing.read_text() == "keep" and calls == []


@pytest.mark.parametrize("http,transport", [(True, 33492), (0, 33492), (33491, 33491), (65536, 33492)])
def test_invalid_ports(home, tmp_path, http, transport):
    with pytest.raises(ValueError, match="invalid_ports"):
        node.IsolatedElasticsearch(home, tmp_path / "run", http, transport)


def test_readiness_timeout_closes(launch, monkeypatch):
    instance, _, stopped = launch
    monkeypatch.setattr(node, "READY_TIMEOUT_SECONDS", 0)
    with pytest.raises(node.IsolatedElasticsearchError, match="readiness_timeout"):
        instance.start()
    assert len(stopped) == 1


def test_config_override_dictionary_rejected(home):
    override = home / "config/analysis-ik"
    override.mkdir()
    (override / "IKAnalyzer.cfg.xml").write_text('<properties><entry key="remote_ext_dict">https://invalid.example/</entry></properties>', encoding="utf-8")
    with pytest.raises(node.IsolatedElasticsearchError, match="dictionary_extension_forbidden"):
        node.verify_home(home)


def test_redirect_is_rejected_before_following():
    with pytest.raises(node.IsolatedElasticsearchError, match="unexpected_node_redirect"):
        node._NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://invalid.example/")


def test_failed_popen_closes_log(launch, monkeypatch):
    instance, _, stopped = launch
    def fail(*args, **kwargs):
        raise OSError("fictional start failure")
    monkeypatch.setattr(node.subprocess, "Popen", fail)
    with pytest.raises(node.IsolatedElasticsearchError, match="node_start_failed"):
        instance.start()
    assert instance._log.closed
    assert stopped == []


@pytest.fixture
def archives(home, tmp_path, monkeypatch):
    elastic, ik = tmp_path / "elastic.zip", tmp_path / "ik.zip"
    files = node.verify_home(home)["files"]
    with zipfile.ZipFile(elastic, "w") as ez, zipfile.ZipFile(ik, "w") as iz:
        for name in files:
            if name.startswith("plugins/analysis-ik/"):
                iz.write(home / name, name.removeprefix("plugins/analysis-ik/"))
            else:
                ez.write(home / name, "elasticsearch-9.1.4/" + name)
    monkeypatch.setattr(node, "ARCHIVE_PINS", {
        "elasticsearch": ("sha512", hashlib.sha512(elastic.read_bytes()).hexdigest()),
        "analysis-ik": ("sha256", hashlib.sha256(ik.read_bytes()).hexdigest())})
    return {"elasticsearch": elastic, "analysis-ik": ik}


def test_installed_identity_matches_original_archives(home, archives):
    result = node.verify_dependencies(home, archives)
    assert result["installed_files_match_archives"] is True
    assert result["home"] == node.verify_home(home)


def test_dictionary_mutation_cannot_claim_original(home, archives):
    (home / "plugins/analysis-ik/config/main.dic").write_text("modified\n", encoding="utf-8")
    with pytest.raises(node.IsolatedElasticsearchError, match="installed_archive_content_mismatch"):
        node.verify_dependencies(home, archives)


def test_archive_digest_checked_before_accepting(home, archives):
    archives["analysis-ik"].write_bytes(b"changed")
    with pytest.raises(node.IsolatedElasticsearchError, match="archive_hash_mismatch"):
        node.verify_dependencies(home, archives)


def test_close_checks_ports_after_owned_cleanup(launch, monkeypatch):
    instance, _, stopped = launch
    instance.start()
    checked = []
    monkeypatch.setattr(node, "_check_ports", lambda *ports: checked.append(ports))
    instance.close()
    assert len(stopped) == 1
    assert checked == [(33491, 33492)]


@pytest.mark.parametrize("name", ["elasticsearch.yml", "jvm.options", "log4j2.properties", "jvm.options.d/experiment.options"])
def test_active_config_drift_fails_but_always_cleans(launch, monkeypatch, name):
    instance, _, stopped = launch
    instance.start()
    baseline = instance.identity["owned_config_files"]
    assert name in baseline
    (instance.work_dir / "config" / name).write_bytes(b"changed")
    checked = []
    monkeypatch.setattr(node, "_check_ports", lambda *ports: checked.append(ports))
    with pytest.raises(node.IsolatedElasticsearchError, match="owned_config_identity_changed"):
        instance.close()
    assert len(stopped) == 1 and checked == [(33491, 33492)]
    assert instance._log.closed
    assert instance.identity["owned_config_verification"] == {"checked": True, "stable": False}
    assert instance.identity["owned_config_files"] == baseline


def test_active_config_deletion_also_fails_after_cleanup(launch):
    instance, _, stopped = launch
    instance.start()
    (instance.work_dir / "config/elasticsearch.yml").unlink()
    with pytest.raises(node.IsolatedElasticsearchError, match="owned_config_identity_changed"):
        instance.close()
    assert len(stopped) == 1


def test_server_generated_keystore_not_read_or_false_drift(launch):
    instance, _, stopped = launch
    instance.start()
    (instance.work_dir / "config/elasticsearch.keystore").write_bytes(b"fictional-generated-operator-file")
    instance.close()
    assert len(stopped) == 1
    assert "elasticsearch.keystore" not in instance.identity["owned_config_files"]
    assert instance.identity["owned_config_verification"] == {"checked": True, "stable": True}


def test_copied_ik_dictionary_is_actual_config_identity(launch, tmp_path, monkeypatch):
    original, _, stopped = launch
    source = original.home / "config/analysis-ik"
    source.mkdir()
    (source / "IKAnalyzer.cfg.xml").write_text("<properties/>", encoding="utf-8")
    (source / "main.dic").write_text("合成\n", encoding="utf-8")
    instance = node.IsolatedElasticsearch(original.home, tmp_path / "copied-run", 33491, 33492)
    monkeypatch.setattr(instance, "_request", lambda path: {
        "version": {"number": "9.1.4"}, "cluster_name": instance.cluster_name, "cluster_uuid": "synthetic"})
    instance.start()
    assert "analysis-ik/main.dic" in instance.identity["owned_config_files"]
    (instance.work_dir / "config/analysis-ik/main.dic").write_text("changed\n", encoding="utf-8")
    with pytest.raises(node.IsolatedElasticsearchError, match="owned_config_identity_changed"):
        instance.close()
    assert len(stopped) == 1


def test_changed_copy_not_silently_frozen_as_original(launch, monkeypatch):
    instance, calls, _ = launch
    def changed_copy(source, destination):
        Path(destination).write_text("not the verified source", encoding="utf-8")
    monkeypatch.setattr(node.shutil, "copyfile", changed_copy)
    with pytest.raises(node.IsolatedElasticsearchError, match="copied_config_source_mismatch"):
        instance.start()
    assert calls == []
