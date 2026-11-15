import json
import tempfile
import unittest
from pathlib import Path

from tools import eggtv_control as control


class ControlTests(unittest.TestCase):
    def test_current_settings_match_the_published_catalogue(self):
        self.assertIn("通过", control.check(control.ROOT / "app_control.json"))

    def test_bad_types_and_transport_are_rejected(self):
        for value in ([], {}, {"subscription_url": control.ORIGINAL_URL, "home_source_key": 7},
                      {"subscription_url": "http://example.com/a", "home_source_key": ""},
                      {"subscription_url": "https://u:p@example.com/a", "home_source_key": ""},
                      {"subscription_url": "https://example.com/a#", "home_source_key": ""},
                      {"subscription_url": "https://example.com/a b", "home_source_key": ""}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                control.validate(value)

    def test_unknown_fields_rejected_and_empty_key_supported(self):
        plan = {"subscription_url": control.ORIGINAL_URL, "home_source_key": ""}
        self.assertEqual("", control.validate(plan)["home_source_key"])
        with self.assertRaises(ValueError):
            control.validate(dict(plan, revision=1))

    def test_changed_url_does_not_use_old_catalogue(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "control.json"
            path.write_text(json.dumps({"subscription_url": "https://new.example/config.json", "home_source_key": "new-source"}))
            self.assertIn("须用新配置", control.check(path))
            with self.assertRaises(ValueError):
                control.check(path, control.ROOT / "tvbox_config.json")

    def test_missing_key_is_reported_before_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "control.json"
            path.write_text(json.dumps({"subscription_url": control.ORIGINAL_URL, "home_source_key": "missing"}))
            with self.assertRaisesRegex(ValueError, "默认片源不存在"):
                control.check(path)

    def test_new_catalogue_key_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            path, feed = Path(folder) / "control.json", Path(folder) / "feed.json"
            path.write_text(json.dumps({"subscription_url": "https://new.example/config.json", "home_source_key": "new"}))
            feed.write_text(json.dumps({"sites": [{"key": "new", "name": "New"}]}))
            self.assertIn("片源 1 个", control.check(path, feed))


if __name__ == "__main__":
    unittest.main()
