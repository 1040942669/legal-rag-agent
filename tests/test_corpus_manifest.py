import hashlib
import json
import unittest
from pathlib import Path
from uuid import uuid4

from legal_rag.cli import main
from legal_rag.corpus_manifest import (
    DEFAULT_MANIFEST_PATH,
    CorpusManifestError,
    build_corpus_manifest,
    fetch_corpus,
    load_corpus_manifest,
    parse_corpus_manifest,
    verify_corpus,
)

REVISION = "a" * 40
SOURCE = {
    "provider": "huggingface",
    "repo_type": "dataset",
    "repo_id": "owner/laws",
    "revision": REVISION,
}
FILES = {
    "中华人民共和国甲法.txt": "《中华人民共和国甲法》第一条规定，为了测试，制定本法。\n",
    "中华人民共和国乙法.txt": "第一条\u3000为了测试，制定本法。\n",
}


def scratch_dir() -> Path:
    path = Path(".tmp") / "tests" / uuid4().hex
    path.mkdir(parents=True)
    return path


def write_corpus(root: Path, files: dict[str, str] = FILES) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8", newline="\n")
    return root


def write_manifest(root: Path, data_dir: Path) -> Path:
    payload = build_corpus_manifest(data_dir, corpus_id="laws@aaaaaaa", source=SOURCE)
    path = root / "corpus.manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class CorpusManifestTest(unittest.TestCase):
    def test_committed_manifest_pins_one_revision(self) -> None:
        manifest = load_corpus_manifest(Path(__file__).parents[1] / DEFAULT_MANIFEST_PATH)
        self.assertEqual(manifest.repo_id, "Kuugo/Chinese_Law")
        self.assertEqual(manifest.revision, "a829f47d34938fd177c5ba92d7fcdc45433dc60a")
        self.assertEqual(len(manifest.files), 236)
        self.assertIn("中华人民共和国刑法.txt", {item.path for item in manifest.files})
        self.assertEqual(
            manifest.download_url("中华人民共和国刑法.txt", "https://hf-mirror.com/"),
            "https://hf-mirror.com/datasets/Kuugo/Chinese_Law/resolve/"
            "a829f47d34938fd177c5ba92d7fcdc45433dc60a/"
            "%E4%B8%AD%E5%8D%8E%E4%BA%BA%E6%B0%91%E5%85%B1%E5%92%8C%E5%9B%BD"
            "%E5%88%91%E6%B3%95.txt",
        )

    def test_verify_reports_missing_modified_and_extra_files(self) -> None:
        root = scratch_dir()
        corpus = write_corpus(root / "corpus")
        manifest = load_corpus_manifest(write_manifest(root, corpus))
        self.assertTrue(verify_corpus(corpus, manifest).ok)

        (corpus / "中华人民共和国甲法.txt").write_text("改过的文本\n", encoding="utf-8")
        (corpus / "中华人民共和国乙法.txt").unlink()
        (corpus / "工伤保险条例.txt").write_text("网上找来的\n", encoding="utf-8")
        (corpus / "README.md").write_text("not a law file\n", encoding="utf-8")
        result = verify_corpus(corpus, manifest)
        self.assertFalse(result.ok)
        self.assertEqual(result.modified, ("中华人民共和国甲法.txt",))
        self.assertEqual(result.missing, ("中华人民共和国乙法.txt",))
        self.assertEqual(result.extra, ("工伤保险条例.txt",))
        self.assertEqual(verify_corpus(root / "absent", manifest).missing, tuple(sorted(FILES)))

    def test_parse_rejects_unsafe_or_inconsistent_manifests(self) -> None:
        base = build_corpus_manifest(
            write_corpus(scratch_dir() / "corpus"), corpus_id="laws@aaaaaaa", source=SOURCE
        )
        broken_cases = {
            "short revision": {**base, "source": {**SOURCE, "revision": "abc"}},
            "path traversal": {
                **base,
                "files": [{**base["files"][0], "path": "../甲法.txt"}],
                "file_count": 1,
            },
            "duplicate path": {**base, "files": [base["files"][0]] * 2, "file_count": 2},
            "wrong count": {**base, "file_count": 5},
            "bad digest": {
                **base,
                "files": [{**base["files"][0], "sha256": "XYZ"}],
                "file_count": 1,
            },
            "schema": {**base, "schema_version": 2},
        }
        for label, payload in broken_cases.items():
            with self.subTest(label=label):
                with self.assertRaises(CorpusManifestError):
                    parse_corpus_manifest(payload)

    def test_fetch_downloads_verifies_and_never_silently_overwrites(self) -> None:
        root = scratch_dir()
        source_dir = write_corpus(root / "source")
        manifest = load_corpus_manifest(write_manifest(root, source_dir))
        served = {
            manifest.download_url(name, "https://mirror.test"): text.encode("utf-8")
            for name, text in FILES.items()
        }
        requested: list[str] = []

        def fake_fetch(url: str) -> bytes:
            requested.append(url)
            return served[url]

        target = root / "target"
        result = fetch_corpus(manifest, target, endpoint="https://mirror.test", fetch=fake_fetch)
        self.assertTrue(result.ok)
        self.assertEqual(len(requested), 2)
        self.assertEqual(list(target.glob("*.part")), [])
        # A second fetch downloads nothing.
        fetch_corpus(manifest, target, endpoint="https://mirror.test", fetch=fake_fetch)
        self.assertEqual(len(requested), 2)

        (target / "中华人民共和国甲法.txt").write_text("改过的文本\n", encoding="utf-8")
        with self.assertRaises(CorpusManifestError):
            fetch_corpus(manifest, target, endpoint="https://mirror.test", fetch=fake_fetch)
        self.assertTrue(
            fetch_corpus(
                manifest, target, endpoint="https://mirror.test", fetch=fake_fetch, overwrite=True
            ).ok
        )

        (target / "中华人民共和国乙法.txt").unlink()
        with self.assertRaises(CorpusManifestError):
            fetch_corpus(
                manifest, target, endpoint="https://mirror.test", fetch=lambda url: b"tampered"
            )
        self.assertFalse((target / "中华人民共和国乙法.txt").exists())
        self.assertEqual(list(target.glob("*.part")), [])

    def test_verify_corpus_cli_exit_codes(self) -> None:
        root = scratch_dir()
        corpus = write_corpus(root / "corpus")
        manifest_path = write_manifest(root, corpus)
        args = ["verify-corpus", "--data-dir", str(corpus), "--manifest", str(manifest_path)]
        self.assertEqual(main(args), 0)
        (corpus / "额外.txt").write_text("x\n", encoding="utf-8")
        self.assertEqual(main(args), 1)

    def test_manifest_hashes_are_sha256_of_file_bytes(self) -> None:
        corpus = write_corpus(scratch_dir() / "corpus")
        payload = build_corpus_manifest(corpus, corpus_id="laws@aaaaaaa", source=SOURCE)
        for entry in payload["files"]:
            data = (corpus / entry["path"]).read_bytes()
            self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(entry["size"], len(data))


if __name__ == "__main__":
    unittest.main()
