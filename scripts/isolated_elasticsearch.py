"""Experiment-only, loopback Elasticsearch lifecycle. No download or installation.

The caller must supply a verified distribution with an exactly matching IK
plugin. Local logs/data are retained; this is not an outbound-network sandbox.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile

from scripts.benchmark_smartcn import terminate_process_tree

ROOT = Path(__file__).resolve().parents[1]
READY_TIMEOUT_SECONDS = 120
ARCHIVE_PINS = {
    "elasticsearch": ("sha512", "66f10e3e69dce8a1dc7374570d1ac7b24eaafee99f44bdfbd669c99c6afc77c63a3e0859516afd35b44f8880af19c2909d3c42fea82e2a2b39ae78ab0fe431a2"),
    "analysis-ik": ("sha256", "0bf5c3e809212dc4646e34c9e81169007c4bc400526675d37a4fd7d6b285e2db"),
}


class IsolatedElasticsearchError(RuntimeError):
    """Stable errors; raw server diagnostics remain in the owned local log."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise IsolatedElasticsearchError("unexpected_node_redirect")


def _digest(path, algorithm="sha256"):
    if path.is_symlink() or not path.is_file():
        raise IsolatedElasticsearchError("invalid_home_file")
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _properties(path):
    values = {}
    for line in path.read_text(encoding="utf-8-sig", errors="strict").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "!")):
            if "=" not in line:
                raise IsolatedElasticsearchError("invalid_home_properties")
            key, value = line.split("=", 1)
            if key.strip() in values:
                raise IsolatedElasticsearchError("duplicate_home_property")
            values[key.strip()] = value.strip()
    return values


def _dictionary_config(path):
    text = path.read_text(encoding="utf-8-sig", errors="strict")
    if "<!ENTITY" in text:
        raise IsolatedElasticsearchError("invalid_dictionary_config")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise IsolatedElasticsearchError("invalid_dictionary_config") from None
    if root.tag != "properties":
        raise IsolatedElasticsearchError("invalid_dictionary_config")
    entries = {}
    for entry in root.findall("entry"):
        key = entry.get("key")
        if key in entries:
            raise IsolatedElasticsearchError("duplicate_dictionary_entry")
        entries[key] = (entry.text or "").strip()
    # enable_remote_dict=false alone does not prevent initial remote loading.
    if any(entries.get(key) for key in
           ("ext_dict", "ext_stopwords", "remote_ext_dict", "remote_ext_stopwords")):
        raise IsolatedElasticsearchError("dictionary_extension_forbidden")


def verify_home(home: Path, expected_version="9.1.4") -> dict:
    """Read-only byte identity, not a substitute for archive authenticity checks."""
    home = Path(home)
    if home.is_symlink() or not home.is_dir():
        raise IsolatedElasticsearchError("home_directory_missing")
    home = home.resolve()
    if not re.fullmatch(r"\d+\.\d+\.\d+", expected_version):
        raise ValueError("invalid_expected_version")
    required = [home / f"lib/elasticsearch-{expected_version}.jar", home / "jdk/bin/java.exe",
                home / "jdk/release", home / "config/jvm.options", home / "config/log4j2.properties"]
    if any(not path.is_file() for path in required):
        raise IsolatedElasticsearchError("distribution_version_or_file_missing")
    plugin = home / "plugins/analysis-ik"
    descriptor = plugin / "plugin-descriptor.properties"
    if not descriptor.is_file():
        raise IsolatedElasticsearchError("ik_plugin_missing")
    props = _properties(descriptor)
    if props.get("elasticsearch.version") != expected_version or props.get("version") != expected_version:
        raise IsolatedElasticsearchError("plugin_version_mismatch")
    if props.get("name") != "analysis-ik" or not list(plugin.glob("*.jar")):
        raise IsolatedElasticsearchError("invalid_ik_plugin")
    configs = list(plugin.rglob("IKAnalyzer.cfg.xml"))
    override = home / "config/analysis-ik/IKAnalyzer.cfg.xml"
    if override.is_file():
        configs.append(override)
    if not configs:
        raise IsolatedElasticsearchError("dictionary_config_missing")
    for path in configs:
        _dictionary_config(path)
    release = _properties(home / "jdk/release")
    java_version = release.get("JAVA_VERSION", "").strip('"')
    if not re.fullmatch(r"\d+(?:\.\d+)*(?:[-+][A-Za-z0-9.-]+)?", java_version):
        raise IsolatedElasticsearchError("invalid_bundled_java_version")
    paths = set(required)
    for base in ("lib", "modules", "plugins"):
        paths.update((home / base).rglob("*.jar"))
    for base in (plugin, home / "config/analysis-ik"):
        if base.exists():
            paths.update(path for path in base.rglob("*") if path.is_file())
    for name in ("jdk/lib/modules", "bin/elasticsearch.bat", "bin/elasticsearch-env.bat", "bin/elasticsearch-cli.bat"):
        if (home / name).is_file():
            paths.add(home / name)
    paths.update((home / "jdk").rglob("*.dll"))
    return {"elasticsearch_version": expected_version, "plugin_version": props["version"],
            "java_version": java_version, "java_version_source": "bundled jdk/release",
            "files": {path.relative_to(home).as_posix(): _digest(path) for path in sorted(paths)},
            "dictionary_policy": "bundled dictionaries unchanged; no local/remote extensions",
            "network_boundary": "loopback listener and disabled configured downloaders; not an OS firewall"}


def verify_dependencies(home: Path, expected_archives: dict[str, Path]) -> dict:
    """Bind installed bytes to the frozen original archives, without extraction."""
    if set(expected_archives) != set(ARCHIVE_PINS):
        raise ValueError("invalid_expected_archives")
    archives = {key: Path(path) for key, path in expected_archives.items()}
    for key, path in archives.items():
        algorithm, expected = ARCHIVE_PINS[key]
        if _digest(path, algorithm) != expected:
            raise IsolatedElasticsearchError("archive_hash_mismatch")
    identity = verify_home(home)
    with zipfile.ZipFile(archives["elasticsearch"]) as elastic, zipfile.ZipFile(archives["analysis-ik"]) as ik:
        for archive in (elastic, ik):
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise IsolatedElasticsearchError("duplicate_archive_entry")
        for name, installed_hash in identity["files"].items():
            if name.startswith("plugins/analysis-ik/"):
                archive, entry = ik, name.removeprefix("plugins/analysis-ik/")
            elif name.startswith("config/analysis-ik/"):
                archive, entry = ik, "config/" + name.removeprefix("config/analysis-ik/")
            else:
                archive, entry = elastic, "elasticsearch-9.1.4/" + name
            try:
                digest = hashlib.sha256()
                with archive.open(entry) as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
            except KeyError:
                raise IsolatedElasticsearchError("installed_archive_file_missing") from None
            if digest.hexdigest() != installed_hash:
                raise IsolatedElasticsearchError("installed_archive_content_mismatch")
    return {"home": identity, "archive_hashes": {key: {"algorithm": algorithm, "digest": digest}
            for key, (algorithm, digest) in ARCHIVE_PINS.items()}, "installed_files_match_archives": True}


def _check_ports(*ports):
    # Do not accidentally connect to or reuse an existing local service.
    for port in ports:
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
        except OSError:
            raise IsolatedElasticsearchError("port_unavailable") from None


class IsolatedElasticsearch:
    def __init__(self, home: Path, work_dir: Path, http_port: int, transport_port: int,
                 expected_version="9.1.4"):
        if (any(type(port) is not int or not 1 <= port <= 65535 for port in (http_port, transport_port))
                or http_port == transport_port):
            raise ValueError("invalid_ports")
        if Path(home).is_symlink():
            raise IsolatedElasticsearchError("invalid_home_file")
        self.home = Path(home).resolve()
        work = Path(work_dir)
        if work.is_symlink():
            raise IsolatedElasticsearchError("invalid_work_directory")
        self.work_dir = work.resolve()
        if (self.work_dir == ROOT or self.work_dir in self.home.parents or self.work_dir == self.home
                or self.work_dir.is_relative_to(self.home)
                or (self.work_dir.is_relative_to(ROOT) and not any(self.work_dir.is_relative_to(base)
                    for base in (ROOT / ".tmp", ROOT / "artifacts/experiments")))):
            raise IsolatedElasticsearchError("invalid_work_directory")
        self.http_port, self.transport_port, self.expected_version = http_port, transport_port, expected_version
        self.cluster_name = "legal-rag-ik-" + uuid.uuid4().hex
        self._identity = verify_home(self.home, expected_version)
        self._process = None
        self._parent = None
        self._descendants = {}
        self._log = None
        self._owned_config = {}
        self._started = False
        self._closed = False

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.http_port}"

    @property
    def identity(self):
        return deepcopy(self._identity)

    def _environment(self):
        allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMPUTERNAME", "USERPROFILE", "TEMP", "TMP"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env.update({"ES_JAVA_HOME": str(self.home / "jdk"), "ES_PATH_CONF": str(self.work_dir / "config"),
                    "ES_TMPDIR": str(self.work_dir / "tmp"), "ES_HOME": str(self.home)})
        return env

    def _remember(self):
        import psutil
        try:
            if self._parent is None:
                self._parent = psutil.Process(self._process.pid)
                self._parent_created = self._parent.create_time()
            if self._parent.create_time() != self._parent_created:
                raise IsolatedElasticsearchError("owned_process_identity_changed")
            for child in self._parent.children(recursive=True):
                self._descendants[(child.pid, child.create_time())] = child
        except psutil.NoSuchProcess:
            pass

    def _request(self, path):
        # No proxy environment, redirects or caller-provided URLs.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(self.endpoint + path, timeout=1) as response:
            if response.geturl() != self.endpoint + path:
                raise IsolatedElasticsearchError("unexpected_node_redirect")
            payload = response.read(65537)
            if len(payload) > 65536:
                raise IsolatedElasticsearchError("invalid_readiness_response")
        try:
            value = json.loads(payload)
        except (ValueError, UnicodeError):
            raise IsolatedElasticsearchError("invalid_readiness_response") from None
        if not isinstance(value, dict):
            raise IsolatedElasticsearchError("invalid_readiness_response")
        return value

    def start(self):
        if self._started or self._closed:
            raise IsolatedElasticsearchError("node_not_restartable")
        if verify_home(self.home, self.expected_version) != self._identity:
            raise IsolatedElasticsearchError("home_identity_changed")
        if self.work_dir.exists():
            raise IsolatedElasticsearchError("work_directory_exists")
        _check_ports(self.http_port, self.transport_port)
        self.work_dir.mkdir(parents=True, exist_ok=False)
        conf = self.work_dir / "config"
        for directory in (conf, self.work_dir / "tmp", self.work_dir / "data", self.work_dir / "logs", conf / "jvm.options.d"):
            directory.mkdir()
        for name in ("jvm.options", "log4j2.properties"):
            shutil.copyfile(self.home / "config" / name, conf / name)
        # Explicit experiment heap, not automatic sizing of half the host RAM.
        (conf / "jvm.options.d/experiment.options").write_text("-Xms1g\n-Xmx1g\n", encoding="utf-8")
        settings = {"cluster.name": self.cluster_name, "node.name": self.cluster_name,
                    "network.host": "127.0.0.1", "http.host": "127.0.0.1", "transport.host": "127.0.0.1",
                    "http.port": self.http_port, "transport.port": self.transport_port, "discovery.type": "single-node",
                    "path.data": str(self.work_dir / "data"), "path.logs": str(self.work_dir / "logs"),
                    "xpack.security.enabled": False, "xpack.security.autoconfiguration.enabled": False,
                    "xpack.security.enrollment.enabled": False, "xpack.license.self_generated.type": "basic",
                    "ingest.geoip.downloader.enabled": False, "xpack.ml.enabled": False, "xpack.watcher.enabled": False}
        (conf / "elasticsearch.yml").write_text("".join(f"{key}: {json.dumps(value)}\n" for key, value in settings.items()), encoding="utf-8")
        if (self.home / "config/analysis-ik").exists():
            shutil.copytree(self.home / "config/analysis-ik", conf / "analysis-ik")
        self._owned_config = {path.relative_to(conf).as_posix(): _digest(path)
                              for path in sorted(conf.rglob("*")) if path.is_file()}
        # Copying must not silently redefine a drifted source as the baseline.
        for name, digest in self._owned_config.items():
            source_hash = self._identity["files"].get("config/" + name)
            if source_hash is not None and source_hash != digest:
                raise IsolatedElasticsearchError("copied_config_source_mismatch")
        self._identity["owned_config_files"] = deepcopy(self._owned_config)
        self._identity["owned_config_verification"] = {"checked": False, "stable": None}
        args = [str(self.home / "jdk/bin/java.exe"), "-Xms4m", "-Xmx64m", "-XX:+UseSerialGC",
                "-Dcli.name=server", "-Dcli.libs=lib/tools/server-cli", f"-Des.path.home={self.home}",
                f"-Des.path.conf={conf}", "-Des.distribution.type=zip", "-Des.java.type=bundled JDK",
                "-cp", str(self.home / "lib/*") + os.pathsep + str(self.home / "lib/cli-launcher/*"),
                "org.elasticsearch.launcher.CliToolLauncher"]
        self._log = (self.work_dir / "launcher.log").open("xb")
        try:
            try:
                self._process = subprocess.Popen(args, cwd=self.home, env=self._environment(), shell=False,
                    stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError:
                raise IsolatedElasticsearchError("node_start_failed") from None
            self._remember()
            deadline = time.monotonic() + READY_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                self._remember()
                if self._process.poll() is not None:
                    raise IsolatedElasticsearchError("node_exited_before_ready")
                try:
                    live = self._request("/")
                except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                    time.sleep(.1)
                    continue
                if (not isinstance(live.get("version"), dict)
                        or live["version"].get("number") != self.expected_version
                        or live.get("cluster_name") != self.cluster_name):
                    raise IsolatedElasticsearchError("unexpected_node_identity")
                if live.get("cluster_uuid") in (None, "", "_na_"):
                    time.sleep(.1)
                    continue
                self._identity["observed"] = {key: live.get(key) for key in ("version", "cluster_name", "cluster_uuid", "name")}
                self._identity["settings"] = settings
                self._identity["launcher"] = "bundled java CliToolLauncher/server-cli; shell=false"
                self._started = True
                return self
            raise IsolatedElasticsearchError("readiness_timeout")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        config_stable = True
        if self._owned_config:
            try:
                config_stable = all(_digest(self.work_dir / "config" / name) == digest
                                    for name, digest in self._owned_config.items())
            except (OSError, IsolatedElasticsearchError):
                config_stable = False
            self._identity["owned_config_verification"] = {"checked": True, "stable": config_stable}
        try:
            if self._process is not None:
                try:
                    self._remember()
                finally:
                    # A configuration failure must never skip owned cleanup.
                    terminate_process_tree(self._process, known_descendants=tuple(self._descendants.values()))
                    _check_ports(self.http_port, self.transport_port)
        finally:
            if self._log is not None:
                self._log.close()
            self._closed = True
        if not config_stable:
            raise IsolatedElasticsearchError("owned_config_identity_changed")

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()
