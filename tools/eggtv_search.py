"""Check real catalog searches without confusing an empty result with a broken source."""
import copy
import json
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor


def search_url(api, keyword):
    parsed = urllib.parse.urlsplit(api)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("ac", "list")
    query.update(wd=keyword, pg="1")
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def normalize(text):
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def parse_search(body, keyword):
    text = body.decode("utf-8-sig").strip()
    if len(text) <= 256 and any(message in text.casefold() for message in
                              ("不支持搜索", "搜索已关闭", "搜索功能已关闭", "search disabled")):
        return {"status": "unsupported", "message": text}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("data", data)
        items = data.get("list") if isinstance(data, dict) else data
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError("接口未返回影片列表")
        entries = [(str(item.get("vod_id", item.get("id", ""))),
                    str(item.get("vod_name", item.get("name", "")))) for item in items]
    except (json.JSONDecodeError, AttributeError):
        root = ET.fromstring(text)
        if root.tag not in {"rss", "list"}:
            raise ValueError("搜索接口返回网页或无效数据")
        entries = [(item.findtext("id", ""), item.findtext("name", ""))
                   for item in root.findall(".//video")]
        if root.tag == "rss" and root.find("list") is None:
            raise ValueError("XML 搜索接口未返回影片列表")
    names = [name for _, name in entries]
    return {"status": "ok", "count": len(entries), "identity": entries,
            "matches": [name for name in names if normalize(keyword) in normalize(name)],
            "sample": names[:5]}


def classify(samples):
    if samples and all(sample["status"] == "unsupported" for sample in samples):
        return "unsupported"
    if not samples or any(sample["status"] != "ok" for sample in samples):
        return "unavailable"
    if (len(samples) >= 2 and all(sample["identity"] for sample in samples)
            and not any(sample["matches"] for sample in samples)
            and all(sample["identity"] == samples[0]["identity"] for sample in samples[1:])):
        return "ignores_keyword"
    if any(sample["matches"] for sample in samples):
        return "working"
    return "no_matches"


def audit_search(sites, options, fetch, previous=None):
    keywords = list(dict.fromkeys(options.get("keywords", ["流浪地球", "肖申克"])))
    if not 2 <= len(keywords) <= 4 or any(not isinstance(k, str) or not k.strip() for k in keywords):
        raise ValueError("搜索检查需要 2 至 4 个不同的非空关键词")

    previous_rows = {row["key"]: row for row in (previous or {}).get("sources", [])}

    def check(site):
        row = {"key": site["key"], "name": site["name"], "api": site["api"],
               "type": site["type"], "probe": "catalog-list-v1", "samples": []}
        if site.get("searchable") == 0:
            return dict(row, status="upstream_disabled")
        for keyword in keywords:
            url = search_url(site["api"], keyword)
            try:
                sample = parse_search(fetch(url), keyword)
            except (ValueError, UnicodeError, ET.ParseError, OSError, RuntimeError) as exc:
                sample = {"status": "error", "message": str(exc)[:500]}
            row["samples"].append(dict(sample, keyword=keyword))
        row["status"] = classify(row["samples"])
        if row["status"] in {"unsupported", "ignores_keyword"}:
            row["disabled_reason"] = row["status"]
        elif row["status"] != "working":
            prior = previous_rows.get(site["key"], {})
            if (prior.get("api") == site["api"] and prior.get("type") == site["type"]
                    and prior.get("probe") == row["probe"] and prior.get("disabled_reason")):
                row["disabled_reason"] = prior["disabled_reason"]
        for sample in row["samples"]:
            sample.pop("identity", None)
        return row

    candidates = [site for site in sites if site.get("type") in (0, 1)]
    with ThreadPoolExecutor(max_workers=max(1, min(6, int(options.get("workers", 5))))) as pool:
        rows = list(pool.map(check, candidates))
    # Only an explicit refusal or repeatable evidence of ignoring keywords changes participation.
    # Network failures and missing sample films cannot establish source failure in the cinema.
    disabled = {row["key"] for row in rows if row.get("disabled_reason")}
    updated = copy.deepcopy(sites)
    for site in updated:
        if site["key"] in disabled:
            site["searchable"] = 0
            site["quickSearch"] = 0
    return updated, {"summary": dict(Counter(row["status"] for row in rows)),
                     "disabled_search": [dict(key=row["key"], name=row["name"], reason=row["disabled_reason"])
                                         for row in rows if row["key"] in disabled],
                     "sources": rows}
