import copy
import unittest
from tools.playback_policy import signature, verdict, round_verdict, decide, apply_health, widespread_failure
from tests import test_sync as fixtures
from tools import eggtv_sync as sync

LIMITS = {"films_per_source": 3, "startup_timeout_seconds": 8, "seek_timeout_seconds": 10}


def trial(**overrides):
    item = {"identity_verified": True, "read_error_ratio": 0, "startup_seconds": 4,
            "seek_verified": True, "seek_seconds": 2, "seek_timeout": False}
    item.update(overrides)
    return item


class PlaybackPolicyTests(unittest.TestCase):
    def test_tool_error_and_wrong_source_never_count_bad(self):
        self.assertEqual(verdict(trial(tool_error="timeout"), LIMITS), "unknown")
        self.assertEqual(verdict(trial(identity_verified=False, startup_timeout=True, startup_seconds=None), LIMITS), "unknown")

    def test_failed_seek_cannot_pass_or_count_as_bad_source(self):
        self.assertEqual(verdict(trial(seek_verified=False, seek_timeout=True), LIMITS), "unknown")

    def test_eight_second_startup_timeout_is_failure(self):
        self.assertEqual(verdict(trial(startup_timeout=True), LIMITS), "bad")

    def test_seek_ten_second_boundary(self):
        self.assertEqual(verdict(trial(seek_seconds=10), LIMITS), "good")
        self.assertEqual(verdict(trial(seek_seconds=10.01), LIMITS), "bad")
        self.assertEqual(verdict(trial(seek_seconds=None, seek_timeout=True), LIMITS), "bad")

    def test_two_bad_films_require_failed_retest(self):
        first = round_verdict([trial(startup_timeout=True), trial(seek_seconds=12), trial()], LIMITS)
        self.assertEqual(first, "bad")
        self.assertEqual(decide(first, "unknown", "healthy"), "healthy")
        self.assertEqual(decide(first, "bad", "healthy"), "unhealthy")

    def test_recovery_requires_two_clean_rounds(self):
        self.assertEqual(decide("good", None, "unhealthy"), "unhealthy")
        self.assertEqual(decide("good", "good", "unhealthy"), "healthy")

    def test_signature_ignores_brand_but_detects_changed_api(self):
        a = {"key": "a", "name": "欧乐", "api": "url1"}
        b = dict(a, name="欢迎")
        self.assertEqual(signature(a), signature(b))
        state = {"a": {"status": "unhealthy", "signature": signature(a)}}
        self.assertEqual(apply_health([b], state)[0], [])
        self.assertEqual(len(apply_health([dict(b, api="url2")], state)[0]), 1)

    def test_network_guard(self):
        self.assertTrue(widespread_failure([{"first_verdict": v} for v in ("bad", "bad", "good")]))
        self.assertFalse(widespread_failure([{"first_verdict": v} for v in ("bad", "unknown", "good")]))


class PlaybackSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SyncTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.SyncTests.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.SyncTests.setUp(self)

    # Reuse isolated source/JAR fixtures; new tests exercise real staging behavior.
    def test_health_filter_keeps_full_candidates(self):
        import json
        source = {"spider": "./jar/tool.jar", "sites": [
            {"key": "a", "name": "旧首页", "api": "csp_Demo", "type": 3},
            {"key": "b", "name": "健康电影", "api": "csp_Demo", "type": 3}]}
        self.responses["/source/config.json"] = json.dumps(source).encode()
        # Fingerprints of csp sites are unchanged by URL normalisation.
        health = {"version": 1, "profiles": {"main": {
            "a": {"status": "unhealthy", "signature": signature(source["sites"][0])},
            "b": {"status": "healthy", "signature": signature(source["sites"][1])}}}}
        sync.save_json(self.root / "health.json", health)
        self.profile.update(first_site_key="a", rename_first="欢迎", candidates_output="candidates.json")
        sync.sync_profile(self.root, {"raw_base": self.base, "playback_health": "health.json"}, "main", self.profile)
        published = sync.load_json(self.root / self.profile["publish_output"])
        candidates = sync.load_json(self.root / "candidates.json")
        self.assertEqual([s["key"] for s in published["sites"]], ["b"])
        self.assertEqual(published["sites"][0]["name"], "欢迎")
        self.assertEqual(len(candidates["sites"]), 2)
        self.assertEqual(candidates["spider"], published["spider"])

    def test_all_unhealthy_does_not_overwrite_old_menu(self):
        source = {"key": "demo", "name": "电影站", "api": "csp_Demo", "type": 3}
        sync.save_json(self.root / "health.json", {"version": 1, "profiles": {"main": {"demo": {
            "status": "unhealthy", "signature": signature(source)}}}})
        destination = self.root / self.profile["publish_output"]
        destination.write_text('{"keep":"old"}', encoding="utf-8")
        with self.assertRaises(sync.SyncError):
            sync.sync_profile(self.root, {"raw_base": self.base, "playback_health": "health.json"}, "main", self.profile)
        self.assertEqual(destination.read_text(), '{"keep":"old"}')
