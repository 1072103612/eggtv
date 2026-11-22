import hashlib
import tempfile
import unittest
from pathlib import Path

from tools import eggtv_sync as sync
from tools import verify_published as published


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.base = "https://cinema.test/site/"
        script = b"var rule={};"
        digest = hashlib.sha256(script).hexdigest()
        self.resource = self.root / f"deps/{digest}/movie.js"
        self.resource.parent.mkdir(parents=True)
        self.resource.write_bytes(script)
        self.tool = self.root / "jar/tool.jar"
        self.tool.parent.mkdir()
        self.tool.write_bytes(b"verified-tool")
        self.config = {"repo": {"raw_base": self.base.rstrip("/"), "publish_dependency_transport": "pages"},
                       "profiles": {"main": {"publish_output": "config.json"}}}
        self.payload = {"spider": self.base + "jar/tool.jar;md5;" + sync.md5_file(self.tool),
                        "sites": [{"key": "film", "type": 3, "name": "电影", "api": self.base + self.resource.relative_to(self.root).as_posix()}]}
        sync.save_json(self.root / "config.json", self.payload)
        sync.save_json(self.root / "config_proxy.json", self.payload)

    def fetch(self, url):
        self.assertNotIn("?", url, "Verify the actual original address, not a new subscription")
        return (self.root / url.removeprefix(self.base)).read_bytes()

    def test_original_configs_alias_and_all_dependencies_are_verified(self):
        targets = published.publication_targets(self.root, self.config)
        self.assertEqual(len(targets), 4)
        self.assertEqual(published.verify_once(targets, self.fetch), [])
        errors = published.verify_once(targets, lambda _: b"stale cached bytes")
        self.assertEqual(len(errors), 4)

    def test_corrupt_local_assets_block_publication_before_network(self):
        self.resource.write_bytes(b"corrupt")
        with self.assertRaises(sync.SyncError):
            published.publication_targets(self.root, self.config)

    def test_stale_legacy_subscription_cannot_be_published(self):
        sync.save_json(self.root / "config_proxy.json", {**self.payload, "wallpaper": "stale"})
        with self.assertRaisesRegex(sync.SyncError, "旧订阅入口"):
            published.publication_targets(self.root, self.config)

    def test_stale_deployment_is_retried_and_report_records_verified_state(self):
        count = [0]

        def fetch(url):
            count[0] += 1
            return b"old" if count[0] <= 4 else self.fetch(url)
        delays = []
        result = published.wait_for_publication(self.root, self.config, fetch=fetch,
                                               clock=lambda: 0, sleep=delays.append)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(delays), 1)
        self.assertTrue(sync.load_json(self.root / "publication_report.json")["original_urls"])

    def test_unavailable_original_url_fails_the_check_and_saves_diagnostics(self):
        ticks = iter([0, 2])
        with self.assertRaisesRegex(sync.SyncError, "发布后验证失败"):
            published.wait_for_publication(self.root, self.config, timeout=1,
                                          fetch=lambda _: b"wrong", clock=lambda: next(ticks))
        self.assertEqual(sync.load_json(self.root / "publication_report.json")["status"], "failed")
