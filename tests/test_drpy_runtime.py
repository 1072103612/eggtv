import shutil
import subprocess
import unittest
from pathlib import Path


class DrpyRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for the ES module runtime check')
    def test_legacy_module_loading_and_parser_helpers_only(self):
        script = Path(__file__).with_name('drpy_runtime.cjs')
        result = subprocess.run(['node', '--experimental-vm-modules', str(script)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_default_homepage_does_not_use_unverified_legacy_js_engine(self):
        import json
        root = Path(__file__).resolve().parents[1]
        for name in ('tvbox_config.json', 'jsm_backup.json'):
            config = json.loads((root / name).read_text())
            settings = json.loads((root / 'eggtv_sync.json').read_text())
            profile = settings['profiles']['tvbox' if name == 'tvbox_config.json' else 'jsm']
            selected = next(site for site in config['sites'] if site['key'] == profile['homepage_fallback']['source_key'])
            self.assertEqual(config['sites'][0]['api'], selected['api'])
            self.assertEqual(config['sites'][0]['type'], selected['type'])
            self.assertTrue(selected['type'] == 1 or selected['api'].startswith('csp_'))
            self.assertEqual(config['sites'][0]['key'], 'drpy_js_豆瓣' if name == 'tvbox_config.json' else '新闪雷')
