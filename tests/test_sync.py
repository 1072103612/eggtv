import argparse
import contextlib
import copy
import hashlib
import io
import json
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tools import eggtv_sync as sync


def make_jar(*classes):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name in classes:
            archive.writestr(f"com/github/catvod/spider/{name}.class", b"test class")
    return output.getvalue()


def payload(api="csp_Demo"):
    return {"spider": "./jar/tool.jar", "sites": [
        {"key": "demo", "name": "电影站", "api": api, "type": 3}
    ]}


class SyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.responses = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = cls.responses.get(self.path)
                self.send_response(200 if data is not None else 404)
                self.end_headers()
                self.wfile.write(data if data is not None else b"not found")

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.responses.clear()
        self.responses["/source/config.json"] = json.dumps(payload()).encode()
        self.responses["/source/jar/tool.jar"] = make_jar("Demo")
        self.profile = {
            "upstream_url": self.base + "/source/config.json",
            "upstream_output": "snapshot.json",
            "publish_output": "config.json",
            "filter": {"block_keywords": ["儿童", "网盘", "FM"]},
            "spider": {"download_to": "jar/main.jar", "publish_path": "jar/main.jar", "timeout": 5},
        }
        self.repo = {"raw_base": self.base + "/published"}
        self.network = {"proxy_mode": "off"}

    def run_sync(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return sync.sync_profile(self.root, self.repo, "main", self.profile,
                                     network=self.network, **kwargs)

    def snapshots(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def test_recursive_references_keep_rules_and_selectors(self):
        original = {"api": "./lib/a.js", "ext": {"script": "../js/b.js", "rule": "/app/log"},
                    "sites": [{"api": "csp_Demo", "url": "/file.json"}]}
        result = sync.resolve_payload_references(original, self.base + "/source/config.json")
        self.assertEqual(result["api"], self.base + "/source/lib/a.js")
        self.assertEqual(result["ext"]["script"], self.base + "/js/b.js")
        self.assertEqual(result["ext"]["rule"], "/app/log")
        self.assertEqual(result["sites"][0]["api"], "csp_Demo")
        self.assertEqual(result["sites"][0]["url"], self.base + "/file.json")
        self.assertEqual(original["api"], "./lib/a.js")

    def test_filter_case_insensitive(self):
        sites = [{"name": name} for name in ["影院", "儿童节目", "云网盘", "蜻蜓fm"]]
        kept, removed = sync.filter_sites(sites, ["儿童", "网盘", "FM"])
        self.assertEqual([s["name"] for s in kept], ["影院"])
        self.assertEqual(len(removed), 3)

    def test_removed_site_stays_removed_and_selected_site_stays_first(self):
        original = payload()
        original["sites"] = [{"key": "slow", "name": "旧欢迎入口", "api": "csp_Demo"},
                             {"key": "other", "name": "其他影院", "api": "csp_Demo"},
                             {"key": "OleLive", "name": "欧乐影院", "api": "csp_Demo"}]
        self.responses["/source/config.json"] = json.dumps(original).encode()
        self.profile["filter"]["block_keys"] = ["slow"]
        self.profile["first_site_key"] = "OleLive"
        self.profile["rename_first"] = "欢迎来到蛋壳影院"
        self.run_sync()
        sites = sync.load_json(self.root / "config.json")["sites"]
        self.assertEqual(sites[0]["key"], "OleLive")
        self.assertEqual(sites[0]["name"], "欢迎来到蛋壳影院")
        self.assertNotIn("slow", [site["key"] for site in sites])
        self.run_sync()
        self.assertEqual(sync.load_json(self.root / "config.json")["sites"], sites)

    def test_jar_must_be_complete_and_match_sites(self):
        self.assertFalse(sync.is_valid_jar_bytes(b"PK\x03\x04broken"))
        self.assertFalse(sync.is_valid_jar_bytes(b"<html>success</html>"))
        self.assertTrue(sync.jar_supports_sites(make_jar("Demo"), payload()))
        self.assertFalse(sync.jar_supports_sites(make_jar("Other"), payload()))

    def test_empty_or_malformed_config_rejected(self):
        for invalid in [{}, {"sites": []}, [], {"sites": [{}], "spider": "x"}]:
            with self.subTest(invalid=invalid), self.assertRaises(sync.SyncError):
                sync.validate_source_payload(invalid)

    def test_duplicate_keys_removed(self):
        sites = payload()["sites"] * 2
        kept, removed = sync.deduplicate_sites(sites)
        self.assertEqual(len(kept), 1)
        self.assertEqual(removed, ["电影站"])

    def test_json_replace_existing_and_dry_run(self):
        path = self.root / "nested/config.json"
        sync.save_json(path, {"value": 1})
        sync.save_json(path, {"value": 2})
        self.assertEqual(sync.load_json(path), {"value": 2})
        dry_path = self.root / "absent/config.json"
        sync.save_json(dry_path, {}, dry_run=True)
        self.assertFalse(dry_path.parent.exists())

    def test_sync_rewrites_and_checks_script_dependencies(self):
        original = payload("./lib/script.js")
        original["sites"][0]["ext"] = "./rules/site.json"
        self.responses["/source/config.json"] = json.dumps(original).encode()
        self.responses["/source/lib/script.js"] = b"var rule = {};"
        self.responses["/source/rules/site.json"] = b"{}"
        self.run_sync()
        published = sync.load_json(self.root / "config.json")
        self.assertEqual(published["sites"][0]["api"], self.base + "/source/lib/script.js")
        self.assertEqual(published["sites"][0]["ext"], self.base + "/source/rules/site.json")
        jar_path = self.root / "jar/main.jar"
        self.assertTrue(published["spider"].endswith(";md5;" + sync.md5_file(jar_path)))

    def test_missing_script_keeps_all_existing_files(self):
        self.run_sync()
        before = self.snapshots()
        self.responses["/source/config.json"] = json.dumps(payload("./lib/missing.js")).encode()
        with self.assertRaises(sync.SyncError):
            self.run_sync()
        self.assertEqual(self.snapshots(), before)

    def test_invalid_payload_keeps_all_existing_files(self):
        self.run_sync()
        before = self.snapshots()
        self.responses["/source/config.json"] = b"{}"
        with self.assertRaises(sync.SyncError):
            self.run_sync()
        self.assertEqual(self.snapshots(), before)

    def test_wrong_jar_keeps_existing_files(self):
        self.run_sync()
        before = self.snapshots()
        self.responses["/source/config.json"] = json.dumps(payload("csp_New")).encode()
        self.responses["/source/jar/tool.jar"] = make_jar("Other")
        with self.assertRaises(sync.SyncError):
            self.run_sync()
        self.assertEqual(self.snapshots(), before)

    def test_bad_primary_tool_uses_complete_fallback(self):
        self.responses["/source/jar/tool.jar"] = b"<html>not a jar</html>"
        self.responses["/backup/config.json"] = json.dumps(payload("csp_Backup")).encode()
        self.responses["/backup/jar/tool.jar"] = make_jar("Backup")
        self.profile["upstream_fallback_urls"] = [self.base + "/backup/config.json"]
        result = self.run_sync()
        published = sync.load_json(self.root / "config.json")
        self.assertTrue(result["using_fallback"])
        self.assertEqual(published["sites"][0]["api"], "csp_Backup")
        self.assertTrue(sync.jar_supports_sites((self.root / "jar/main.jar").read_bytes(), published))

    def test_dry_run_does_not_create_files(self):
        result = self.run_sync(dry_run=True)
        self.assertTrue(result["changed_files"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_filter_before_branding_and_count_without_keywords(self):
        original = payload()
        original["lives"] = [{"url": "http://example.com/live"}]
        original["sites"].insert(0, {"key": "child", "name": "儿童节目", "api": "csp_Demo"})
        self.responses["/source/config.json"] = json.dumps(original).encode()
        self.profile["rename_first"] = "欢迎来到影院"
        self.profile["filter"]["drop_fields"] = ["lives"]
        result = self.run_sync()
        published = sync.load_json(self.root / "config.json")
        self.assertEqual(published["sites"][0]["key"], "demo")
        self.assertEqual(result["sites_kept"], 1)
        self.assertNotIn("lives", published)
        self.profile["filter"]["block_keywords"] = []
        self.assertEqual(self.run_sync()["sites_kept"], 2)

    def test_separate_profiles_do_not_overwrite_tools(self):
        self.run_sync()
        first = (self.root / "jar/main.jar").read_bytes()
        other = copy.deepcopy(self.profile)
        other.update({"publish_output": "backup.json", "upstream_output": "backup_snapshot.json"})
        other["spider"] = {"download_to": "jar/backup.jar", "publish_path": "jar/backup.jar"}
        self.responses["/source/config.json"] = json.dumps(payload("csp_Other")).encode()
        self.responses["/source/jar/tool.jar"] = make_jar("Other")
        with contextlib.redirect_stdout(io.StringIO()):
            sync.sync_profile(self.root, self.repo, "backup", other, network=self.network)
        self.assertEqual((self.root / "jar/main.jar").read_bytes(), first)
        self.assertNotEqual((self.root / "jar/backup.jar").read_bytes(), first)

    def test_http_200_html_is_not_healthy_config_or_tool(self):
        self.responses["/html"] = b"<!DOCTYPE html><html>error</html>"
        for kind in ["config", "jar", "resource"]:
            with self.subTest(kind=kind):
                self.assertFalse(sync.check_url_health(self.base + "/html", 5, self.network, kind=kind)["reachable"])

    def test_jar_checksum_and_stale_mirror_detected(self):
        self.assertFalse(sync.check_url_health(self.base + "/source/jar/tool.jar", 5,
                         self.network, kind="jar", expected_md5="0" * 32)["reachable"])
        self.assertFalse(sync.check_url_health(self.base + "/source/config.json", 5,
                         self.network, kind="config", expected_payload=payload("csp_Other"))["reachable"])

    def test_health_checks_upstream_tool(self):
        self.responses["/source/jar/tool.jar"] = b"<html>error</html>"
        self.assertFalse(sync.check_url_health(self.base + "/source/config.json", 5,
                         self.network, kind="upstream")["reachable"])

    def test_speed_catalog_validates_actual_list_response(self):
        self.responses["/catalog"] = b'{"list":[{"vod_name":"movie"}]}'
        self.assertTrue(sync.check_url_health(self.base + "/catalog", 5,
                        self.network, kind="catalog")["reachable"])
        self.responses["/catalog"] = b'{"error":"not authorized"}'
        self.assertFalse(sync.check_url_health(self.base + "/catalog", 5,
                         self.network, kind="catalog")["reachable"])
        self.responses["/catalog"] = b'<rss><list><video><name>movie</name></video></list></rss>'
        self.assertTrue(sync.check_url_health(self.base + "/catalog", 5,
                        self.network, kind="catalog")["reachable"])

    def test_speed_resource_rejects_html_with_bom(self):
        self.responses["/script.js"] = b'\xef\xbb\xbf<!DOCTYPE html><html>error</html>'
        self.assertFalse(sync.check_url_health(self.base + "/script.js", 5,
                         self.network, kind="resource")["reachable"])

    def test_partial_failure_allows_other_profile_to_update(self):
        args = argparse.Namespace(repo_root=str(self.root), config="sync.json", all=True,
                                  profiles=[], upstream_url=None, push=False, dry_run=False,
                                  diff=False, no_proxy=True, proxy=None)
        sync.save_json(self.root / "sync.json", {"repo": self.repo,
                      "profiles": {"bad": self.profile, "good": self.profile}})
        good = {"profile": "good", "source": "test", "changed_files": [], "sites_kept": 1}
        with patch.object(sync, "sync_profile", side_effect=[sync.SyncError("broken"), good]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.cmd_sync(args), 0)
        report = sync.load_json(self.root / "sync_report.json")
        self.assertEqual(report["profiles"][0]["status"], "kept_previous")
        self.assertEqual(report["profiles"][1]["status"], "updated")

    def test_all_failed_is_failure_and_has_report(self):
        args = argparse.Namespace(repo_root=str(self.root), config="sync.json", all=True,
                                  profiles=[], upstream_url=None, push=False, dry_run=False,
                                  diff=False, no_proxy=True, proxy=None)
        sync.save_json(self.root / "sync.json", {"repo": self.repo, "profiles": {"bad": self.profile}})
        with patch.object(sync, "sync_profile", side_effect=sync.SyncError("broken")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.cmd_sync(args), 1)
        self.assertEqual(sync.load_json(self.root / "sync_report.json")["profiles"][0]["status"], "kept_previous")


if __name__ == "__main__":
    unittest.main()
