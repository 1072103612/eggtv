import shutil
import subprocess
import unittest
from pathlib import Path


class DrpyRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for the ES module runtime check')
    def test_full_hosted_drpy_and_parser_compatibility(self):
        script = Path(__file__).with_name('drpy_runtime.cjs')
        result = subprocess.run(['node', '--experimental-vm-modules', str(script)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
