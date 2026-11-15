import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('app_feed', Path(__file__).resolve().parents[1] / 'tools/publish_app_feed.py')
feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feed)


class AppFeedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'jar').mkdir()
        jar = self.root / 'jar/source.jar'
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.writestr('classes.dex', b'dex\n035\0fixture')
        self.config = {'spider': 'https://example.org/jar/source.jar;md5;' + hashlib.md5(jar.read_bytes()).hexdigest(),
                       'sites': [{'key': 'home', 'name': 'Original', 'api': 'csp_Home'},
                                 {'key': 'film', 'name': 'Films', 'api': 'csp_Film'}],
                       'lives': [{'name': 'not published'}]}
        self.save()

    def save(self):
        (self.root / 'tvbox_config.json').write_text(json.dumps(self.config))

    def test_independent_feed_does_not_change_original(self):
        original = (self.root / 'tvbox_config.json').read_bytes()
        feed.publish(self.root, self.root / 'app', self.root / 'assets')
        self.assertEqual(original, (self.root / 'tvbox_config.json').read_bytes())
        result = json.loads((self.root / 'app/tvbox_config.json').read_text())
        self.assertEqual(result['sites'][0]['name'], feed.WELCOME)
        self.assertEqual(result['sites'][1], self.config['sites'][1])
        self.assertNotIn('lives', result)
        self.assertIn('/main/app/jar/', result['spider'])
        self.assertEqual((self.root / 'assets/config.json').read_bytes(), (self.root / 'app/tvbox_config.json').read_bytes())
        self.assertEqual(hashlib.sha256((self.root / 'assets/spider.jar').read_bytes()).hexdigest(), result['eggSpiderSha256'])

    def test_bad_jar_keeps_last_good_feed(self):
        feed.publish(self.root, self.root / 'app')
        previous = (self.root / 'app/tvbox_config.json').read_bytes()
        (self.root / 'jar/source.jar').write_bytes(b'bad jar')
        with self.assertRaises(ValueError):
            feed.publish(self.root, self.root / 'app')
        self.assertEqual(previous, (self.root / 'app/tvbox_config.json').read_bytes())

    def test_only_app_feed_excludes_sources_requiring_extra_plugins(self):
        self.config['sites'] += [{'key': 'python', 'api': 'https://example.org/spider.py'},
                                 {'key': 'node', 'api': 'node:https://example.org/config.js'}]
        self.save()
        feed.publish(self.root, self.root / 'app')
        result = json.loads((self.root / 'app/tvbox_config.json').read_text())
        self.assertEqual([site['key'] for site in result['sites']], ['home', 'film'])
        self.assertEqual(len(json.loads((self.root / 'tvbox_config.json').read_text())['sites']), 4)

    def test_invalid_catalogue_not_published(self):
        for sites in [[], [{'name': 'bad'}], [self.config['sites'][0]] * 2]:
            with self.subTest(sites=sites):
                self.config['sites'] = sites
                self.save()
                with self.assertRaises(ValueError):
                    feed.publish(self.root, self.root / 'app')
        self.assertFalse((self.root / 'app/tvbox_config.json').exists())

    def test_jar_without_dex_not_published(self):
        jar = self.root / 'jar/source.jar'
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.writestr('README', b'not a spider')
        self.config['spider'] = 'https://example.org/jar/source.jar;md5;' + hashlib.md5(jar.read_bytes()).hexdigest()
        self.save()
        with self.assertRaises(ValueError):
            feed.publish(self.root, self.root / 'app')


if __name__ == '__main__':
    unittest.main()
