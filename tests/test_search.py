import json
import unittest
import urllib.parse

from tools.eggtv_search import audit_search, parse_search, search_url


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.site = {"key": "movie", "name": "电影", "type": 1,
                     "api": "https://example.test/vod?token=keep&ac=list", "searchable": 1, "quickSearch": 1}
        self.options = {"keywords": ["流浪地球", "肖申克"]}

    def run_audit(self, fetch):
        sites, report = audit_search([self.site], self.options, fetch)
        return sites[0], report["sources"][0]["status"]

    def test_search_url_preserves_required_query_and_replaces_action(self):
        url = search_url(self.site["api"], "流浪地球")
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query),
                         {"token": ["keep"], "ac": ["list"], "pg": ["1"], "wd": ["流浪地球"]})

    def test_explicit_refusal_disables_search_but_keeps_browsing_and_recovers(self):
        updated, status = self.run_audit(lambda _: "暂不支持搜索".encode())
        self.assertEqual(status, "unsupported")
        self.assertEqual((updated["searchable"], updated["quickSearch"]), (0, 0))
        self.assertEqual(updated["api"], self.site["api"])

        def working(url):
            keyword = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["wd"][0]
            return json.dumps({"list": [{"vod_id": keyword, "vod_name": keyword}]}).encode()
        updated, status = self.run_audit(working)
        self.assertEqual(status, "working")
        self.assertEqual(updated["searchable"], 1)
        self.assertEqual(self.site["quickSearch"], 1)

    def test_network_errors_and_missing_sample_films_do_not_disable_source(self):
        def timeout(_):
            raise RuntimeError("timeout")
        for fetch, expected in [(timeout, "unavailable"), (lambda _: b'{"list":[]}', "no_matches"),
                                (lambda _: b'<html>blocked</html>', "unavailable")]:
            updated, status = self.run_audit(fetch)
            self.assertEqual(status, expected)
            self.assertEqual(updated, self.site)

    def test_repeated_unrelated_list_is_not_a_working_search(self):
        fetch = lambda _: b'{"list":[{"vod_id":1,"vod_name":"unrelated"}]}'
        updated, status = self.run_audit(fetch)
        self.assertEqual(status, "ignores_keyword")
        self.assertEqual(updated["searchable"], 0)

    def test_different_unmatched_lists_do_not_prove_keyword_is_ignored(self):
        responses = iter([b'{"list":[{"vod_id":1,"vod_name":"one"}]}',
                          b'{"list":[{"vod_id":2,"vod_name":"two"}]}'])
        updated, status = self.run_audit(lambda _: next(responses))
        self.assertEqual(status, "no_matches")
        self.assertEqual(updated, self.site)

    def test_only_one_refusal_is_not_enough_to_disable_source(self):
        responses = iter(["暂不支持搜索".encode(), b'{"list":[]}'])
        updated, status = self.run_audit(lambda _: next(responses))
        self.assertEqual(status, "unavailable")
        self.assertEqual(updated, self.site)

    def test_xml_search_parses_results_and_empty_lists(self):
        body = '<rss><list><video><id>1</id><name>流浪地球2</name></video></list></rss>'.encode()
        self.assertEqual(parse_search(body, "流浪地球")["matches"], ["流浪地球2"])
        self.assertEqual(parse_search(b"<rss><list/></rss>", "film")["count"], 0)

    def test_temporary_outage_does_not_reenable_previously_confirmed_bad_search(self):
        _, previous = audit_search([self.site], self.options, lambda _: "暂不支持搜索".encode())

        def unavailable(_):
            raise RuntimeError("timeout")
        sites, report = audit_search([self.site], self.options, unavailable, previous=previous)
        self.assertEqual(sites[0]["searchable"], 0)
        self.assertEqual(report["sources"][0]["status"], "unavailable")
        self.assertEqual(report["disabled_search"][0]["reason"], "unsupported")
        changed_api = dict(self.site, api="https://new.test/vod")
        sites, _ = audit_search([changed_api], self.options, unavailable, previous=previous)
        self.assertEqual(sites[0]["searchable"], 1)
        previous["sources"][0].pop("probe")
        sites, _ = audit_search([self.site], self.options, unavailable, previous=previous)
        self.assertEqual(sites[0]["searchable"], 1, "Do not inherit failure evidence from a different query method")

    def test_verified_recovery_clears_previous_pause(self):
        _, previous = audit_search([self.site], self.options, lambda _: "暂不支持搜索".encode())
        def working(url):
            keyword = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["wd"][0]
            return json.dumps({"list": [{"vod_id": keyword, "vod_name": keyword}]}).encode()
        sites, report = audit_search([self.site], self.options, working, previous=previous)
        self.assertEqual(sites[0]["searchable"], 1)
        self.assertEqual(report["disabled_search"], [])

    def test_javascript_sources_are_not_misclassified_by_http_probe(self):
        site = dict(self.site, type=3, api="csp_Demo")
        with self.assertRaises(ValueError):
            audit_search([self.site], {"keywords": ["one"]}, lambda _: b"unused")
        sites, report = audit_search([site], self.options, lambda _: self.fail("Do not execute spiders"))
        self.assertEqual(sites, [site])
        self.assertEqual(report["sources"], [])
