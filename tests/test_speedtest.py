import argparse
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import eggtv_speedtest as speed
from tools import eggtv_sync as sync


class SpeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.payload = {"spider": "https://example.test/tool.jar", "sites": [
            {"key": "a", "name": "电影接口", "type": 1, "api": "https://example.test/api?token=x"},
            {"key": "b", "name": "另一个名称", "type": 1, "api": "https://example.test/api?token=x"},
            {"key": "c", "name": "需要电视测试", "type": 3, "api": "csp_Example"},
            {"key": "d", "name": "脚本片源", "type": 3, "api": "https://example.test/script.js",
             "ext": "https://example.test/rule.json"},
        ]}
        self.config = {"repo": {"raw_base": "https://example.test/base",
                      "mirrors": {"cdns": ["https://mirror.test/base", "https://example.test/base"]}},
                      "profiles": {"main": {"description": "主配置", "publish_output": "config.json",
                                            "upstream_url": "https://upstream.test/config.json"}}}
        sync.save_json(self.root / "config.json", self.payload)
        sync.save_json(self.root / "sync.json", self.config)
        self.target = {"group": "catalog", "url": "https://example.test/api", "profiles": ["main"], "names": ["电影"]}

    def response(self, ms):
        return {"reachable": True, "time_starttransfer_ms": ms, "time_total_ms": ms + 100,
                "download_kbps": 500, "download_bytes": 4096, "connection_mode": "direct"}

    def test_collect_deduplicates_and_keeps_unmeasurable_sites_separate(self):
        targets, skipped = speed.collect_targets(self.root, self.config, ["main"])
        catalogs = [target for target in targets if target["group"] == "catalog"]
        self.assertEqual(len(catalogs), 1)
        self.assertEqual(catalogs[0]["names"], ["电影接口", "另一个名称"])
        self.assertIn("token=x", catalogs[0]["url"])
        self.assertIn("ac=list", catalogs[0]["url"])
        self.assertEqual(len([t for t in targets if t["group"] == "config"]), 2)
        self.assertEqual([item["key"] for item in skipped], ["c"])

    def test_median_limits_effect_of_outlier(self):
        with patch.object(sync, "check_url_health", side_effect=[self.response(100), self.response(10000), self.response(200)]):
            result = speed.measure_target(self.target, 3, 5, {})
        self.assertEqual(result["median_response_ms"], 200)
        self.assertEqual(result["status"], "正常")

    def test_failures_are_recorded_and_partial_is_not_full_success(self):
        failed = {"reachable": False, "error": "timeout"}
        with patch.object(sync, "check_url_health", side_effect=[failed, self.response(200)]):
            result = speed.measure_target(self.target, 2, 5, {})
        self.assertEqual(result["status"], "偶发失败")
        self.assertEqual(result["success_rate"], 0.5)
        self.assertEqual(result["median_response_ms"], 200)
        with patch.object(sync, "check_url_health", return_value=failed):
            result = speed.measure_target(self.target, 2, 5, {})
        self.assertEqual(result["status"], "无法读取")
        self.assertIsNone(result["median_response_ms"])

    def test_recommendations_prefer_consistently_successful_lines(self):
        common = {"group": "config", "profiles": ["main"]}
        rows = [dict(common, url="https://slow.test", success_rate=1, median_response_ms=800),
                dict(common, url="https://unreliable.test", success_rate=0.5, median_response_ms=1),
                dict(common, url="https://fast.test", success_rate=1, median_response_ms=200)]
        self.assertEqual(speed.recommendations(rows, ["main"]), {"main": "https://fast.test"})
        self.assertEqual(speed.recommendations(rows[1:2], ["main"]), {})

    def args(self, **updates):
        values = dict(repo_root=str(self.root), config="sync.json", profiles=[], samples=2,
                      timeout=5, workers=2, location="测试网络", no_proxy=True, proxy=None)
        values.update(updates)
        return argparse.Namespace(**values)

    def test_command_writes_readable_report_without_changing_published_config(self):
        before = (self.root / "config.json").read_bytes()
        with patch.object(sync, "check_url_health", return_value=self.response(100)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(speed.run_speedtest(self.args()), 0)
        report = sync.load_json(self.root / "speed_report.json")
        self.assertEqual(report["location"], "测试网络")
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertEqual((self.root / "config.json").read_bytes(), before)
        page = (self.root / "speed_report.html").read_text(encoding="utf-8")
        self.assertIn("不是电影缓冲速度", page)
        self.assertIn("需要在电视上实测", page)

    def test_report_escapes_upstream_names_and_errors(self):
        malicious = '<img src=x onerror="alert(1)">'
        self.payload["sites"][0]["name"] = malicious
        sync.save_json(self.root / "config.json", self.payload)
        with patch.object(sync, "check_url_health", return_value={"reachable": False, "error": malicious}), \
                contextlib.redirect_stdout(io.StringIO()):
            speed.run_speedtest(self.args())
        page = (self.root / "speed_report.html").read_text(encoding="utf-8")
        self.assertNotIn(malicious, page)
        self.assertIn("&lt;img", page)

    def test_invalid_limits_or_unknown_profile_rejected(self):
        for updates in [{"samples": 0}, {"timeout": 100}, {"workers": 10}, {"profiles": ["missing"]}]:
            with self.subTest(updates=updates), self.assertRaises(sync.SyncError):
                speed.run_speedtest(self.args(**updates))
        self.assertFalse((self.root / "speed_report.json").exists())


if __name__ == "__main__":
    unittest.main()
