"""Bounded HTTP speed measurements; does not execute spiders or claim playback success."""

import html
import os
import re
import statistics
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__:
    from . import eggtv_sync as sync
else:
    import eggtv_sync as sync


GROUPS = {"config": "配置线路", "upstream": "上游片源与工具", "tool": "播放工具", "catalog": "影片列表接口",
          "entry": "站点入口", "resource": "配套脚本"}
KINDS = {"config": "config", "upstream": "upstream", "tool": "jar", "catalog": "catalog", "entry": None, "resource": "resource"}


def catalog_url(url):
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["ac"] = "list"
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def collect_targets(root, config, profile_names):
    targets, skipped = {}, []

    def add(profile, name, group, url):
        if not isinstance(url, str) or not sync.is_http_url(url):
            return False
        parsed = urllib.parse.urlparse(url)
        if not parsed.hostname or "{" in url or "}" in url:
            return False
        identity = (group, url)
        target = targets.setdefault(identity, {"group": group, "url": url, "names": [], "profiles": []})
        if name not in target["names"]:
            target["names"].append(name)
        if profile not in target["profiles"]:
            target["profiles"].append(profile)
        return True

    raw_base = sync.compute_raw_base(root, config["repo"])
    cdns = config["repo"].get("mirrors", {}).get("cdns", [])
    for profile_name in profile_names:
        profile = config["profiles"][profile_name]
        file_path = sync.ensure_relative_to_repo(root, profile["publish_output"])
        payload = sync.load_json(file_path)
        sync.validate_source_payload(payload)
        upstreams = [profile.get("upstream_url"), *profile.get("upstream_fallback_urls", [])]
        for index, url in enumerate(upstreams):
            label = "首选上游" if index == 0 else "候补上游"
            add(profile_name, profile.get("description", profile_name) + " · " + label,
                "upstream", sync.normalize_source_url(url) if isinstance(url, str) else url)
        for cdn in dict.fromkeys([raw_base, *cdns]):
            url = cdn.rstrip("/") + "/" + file_path.relative_to(root).as_posix()
            add(profile_name, profile.get("description", profile_name), "config", url)
        add(profile_name, profile.get("description", profile_name), "tool", sync.strip_spider_suffix(payload["spider"]))
        for site in payload["sites"]:
            measured = False
            api = site["api"]
            if sync.is_http_url(api):
                group = "catalog" if site.get("type") in (0, 1) else "resource"
                measured = add(profile_name, site["name"], group, catalog_url(api) if group == "catalog" else api)
            ext = site.get("ext")
            if isinstance(ext, str) and sync.is_http_url(ext):
                # 部分站点把多个备用域名写在同一个 ext 中，分别测量。
                for ext_url in re.split(r",(?=https?://)", ext):
                    path = urllib.parse.urlparse(ext_url).path.lower()
                    group = "resource" if path.endswith((".js", ".py", ".json")) else "entry"
                    measured = add(profile_name, site["name"], group, ext_url) or measured
            if not measured:
                skipped.append({"profile": profile_name, "name": site["name"], "key": site["key"],
                                "reason": "通过客户端播放工具访问，需要在电视上实测"})
    return list(targets.values()), skipped


def measure_target(target, samples, timeout, network):
    attempts = []
    for _ in range(samples):
        try:
            result = sync.check_url_health(target["url"], timeout, network, kind=KINDS[target["group"]])
        except (OSError, ValueError, sync.SyncError) as exc:
            result = {"reachable": False, "error": str(exc)}
        attempts.append(result)
    successful = [attempt for attempt in attempts if attempt["reachable"]]
    row = dict(target, attempts=attempts, successful_samples=len(successful), total_samples=samples,
               success_rate=round(len(successful) / samples, 3), median_response_ms=None,
               median_total_ms=None, median_download_kbps=None)
    if successful:
        for output, source in [("median_response_ms", "time_starttransfer_ms"),
                               ("median_total_ms", "time_total_ms"),
                               ("median_download_kbps", "download_kbps")]:
            values = [attempt[source] for attempt in successful if source in attempt]
            row[output] = round(statistics.median(values), 1) if values else None
        row["status"] = "正常" if len(successful) == samples else "偶发失败"
    else:
        row["status"] = "无法读取"
    return row


def ranking_key(row):
    return (-row["success_rate"], row["median_response_ms"] if row["median_response_ms"] is not None else float("inf"), row["url"])


def recommendations(results, profile_names):
    best = {}
    for name in profile_names:
        candidates = [row for row in results if row["group"] == "config"
                      and name in row["profiles"] and row["success_rate"] == 1]
        if candidates:
            best[name] = min(candidates, key=ranking_key)["url"]
    return best


def render_html(report):
    escape = html.escape
    labels = report.get("profile_labels", {})
    rows = []
    for group in GROUPS:
        for row in sorted((r for r in report["results"] if r["group"] == group), key=ranking_key):
            response = "—" if row["median_response_ms"] is None else f"{row['median_response_ms']:.0f} 毫秒"
            download = f"{row['median_download_kbps']:.0f} KB/秒" if group == "tool" and row["median_download_kbps"] is not None else "—"
            color = "ok" if row["status"] == "正常" else "warn" if row["status"] == "偶发失败" else "bad"
            failures = list(dict.fromkeys(a.get("error", "检查失败") for a in row["attempts"] if not a["reachable"]))
            detail = " · ".join(dict.fromkeys(readable_error(error) for error in failures))
            name = " / ".join(row["names"])
            rows.append(f'<tr><td>{escape(GROUPS[group])}</td><td><strong>{escape(name)}</strong>'
                        f'<div class="small">{escape(" / ".join(labels.get(p, p) for p in row["profiles"]))}</div>'
                        f'<a href="{escape(row["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{escape(row["url"])}</a>'
                        f'<div class="small">{escape(detail)}</div></td><td class="{color}">{escape(row["status"])}</td>'
                        f'<td>{response}</td><td>{row["successful_samples"]}/{row["total_samples"]}</td><td>{download}</td></tr>')
    best = "".join(f'<li>{escape(labels.get(name, name))}：<a href="{escape(url, quote=True)}">{escape(url)}</a></li>'
                   for name, url in report["recommended_config_urls"].items()) or "<li>此次没有全部测量成功的配置线路，请稍后重测。</li>"
    skipped = "".join(f'<li>{escape(labels.get(item["profile"], item["profile"]))} · {escape(item["name"])}</li>' for item in report["unmeasured_sites"])
    clock = datetime.fromisoformat(report["measured_at"]).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>蛋壳影院 · 片源测速</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f5f6fa;color:#172033;font:15px/1.6 system-ui,sans-serif}}
main{{max-width:1250px;margin:32px auto;padding:0 20px}}h1{{margin:0;font-size:30px}}h2{{font-size:20px}}
.card{{background:white;border:1px solid #e2e7ef;border-radius:14px;padding:22px;margin:20px 0}}
.stats{{display:flex;gap:30px;flex-wrap:wrap}}.stats strong{{display:block;font-size:30px}}
.small{{font-size:12px;color:#667085;margin:3px 0}}a{{color:#2455bc;overflow-wrap:anywhere}}
.table{{overflow:auto}}table{{border-collapse:collapse;width:100%;min-width:800px}}th,td{{text-align:left;padding:13px 10px;border-bottom:1px solid #e7ebf1;vertical-align:top}}th{{background:#f8f9fc;white-space:nowrap}}td:nth-child(2){{max-width:480px}}td:nth-child(n+3){{white-space:nowrap}}
.ok{{color:#166534}}.warn{{color:#92400e}}.bad{{color:#b91c1c}}input{{width:100%;padding:12px;border:1px solid #cbd5e1;border-radius:8px;font:inherit}}ul{{padding-left:22px}}summary{{cursor:pointer}}
</style></head><body><main>
<h1>蛋壳影院 · 片源测速</h1><p>查看地址能否读取，以及等待第一份响应需要多久。响应时间越小，等待越短。</p>
<div class="small">测量时间：{clock}（北京时间） · 测量位置：{escape(report['location'])} · 每个地址测 {report['samples']} 次</div>
<section class="card"><div class="stats"><div><strong>{report['summary']['targets']}</strong>测量地址</div>
<div class="ok"><strong>{report['summary']['normal']}</strong>全部成功</div><div class="warn"><strong>{report['summary']['intermittent']}</strong>偶发失败</div>
<div class="bad"><strong>{report['summary']['failed']}</strong>无法读取</div></div>
<p>这里测的是配置、接口和配套文件的响应。工具文件的下载速度也会显示。<strong>这些结果不能证明电影能播放，也不是电影缓冲速度。</strong></p>
<p>{escape(report['network_note'])} 云端结果仅供参考，在影院网络测出的结果更贴近客人的体验。</p></section>
<section class="card"><h2>此次响应最快的配置线路</h2><p>从此次每次都成功的线路中选择，供切换地址时参考。</p><ul>{best}</ul></section>
<section class="card"><h2>测速明细</h2><label for="filter">查找片源或地址</label><input id="filter" placeholder="输入名称或地址">
<div class="table"><table><thead><tr><th>检查内容</th><th>名称与地址</th><th>状态</th><th>响应时间（中位数）</th><th>成功次数</th><th>工具下载速度</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
<details class="card"><summary>另有 {len(report['unmeasured_sites'])} 个菜单入口需要在电视上实测</summary>
<p>这些入口没有可直接测量的地址，由电视客户端的播放工具读取；本报告不把它们记为失败。</p><ul>{skipped}</ul></details>
</main><script>document.getElementById('filter').addEventListener('input',function(){{const q=this.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(row=>row.hidden=!row.textContent.toLowerCase().includes(q));}});</script></body></html>'''


def readable_error(error):
    lowered = error.lower()
    if "不是有效播放工具" in error or "播放工具与站点不匹配" in error:
        return "来源没有提供可用的配套播放工具"
    if "影片列表" in error:
        return "地址没有返回可用的影片列表"
    if "404" in error:
        return "地址不存在"
    if "403" in error:
        return "对方拒绝访问，请在影院网络复测"
    if "timed out" in lowered or "timeout" in lowered or "超时" in error or "522" in error:
        return "等待超时，请稍后重测"
    if "resolve host" in lowered:
        return "域名暂时无法访问"
    if "ssl" in lowered or "handshake" in lowered:
        return "连接建立失败，请稍后重测"
    return "此次无法读取，请稍后重测或在影院网络复测"


def run_speedtest(args):
    root = Path(args.repo_root).resolve()
    config = sync.load_config(root, (root / args.config).resolve())
    options = config.get("speedtest", {})
    names = args.profiles or sorted(config["profiles"])
    unknown = [name for name in names if name not in config["profiles"]]
    if unknown:
        raise sync.SyncError("未知配置: " + ", ".join(unknown))
    samples = args.samples if args.samples is not None else options.get("samples", 2)
    timeout = args.timeout if args.timeout is not None else options.get("timeout", 8)
    workers = args.workers if args.workers is not None else options.get("workers", 6)
    if not (1 <= samples <= 5 and 5 <= timeout <= 30 and 1 <= workers <= 8):
        raise sync.SyncError("测量次数需为 1–5，每次超时 5–30 秒，同时测量数 1–8")
    network = sync.resolve_network_config(config, args)
    targets, skipped = collect_targets(root, config, names)
    if not targets:
        raise sync.SyncError("没有可测量的地址")
    location = args.location or ("GitHub 云端运行机器" if os.environ.get("GITHUB_ACTIONS") == "true" else "当前电脑所在网络")
    print(f"开始测速：{len(targets)} 个地址，每个 {samples} 次；位置：{location}", flush=True)
    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(measure_target, target, samples, timeout, network) for target in targets]
        for future in as_completed(futures):
            row = future.result()
            results.append(row)
            ms = f"{row['median_response_ms']:.0f}ms" if row["median_response_ms"] is not None else "—"
            print(f"[{len(results)}/{len(targets)}] {row['status']} · {ms} · {row['names'][0]}", flush=True)
    results.sort(key=lambda row: (list(GROUPS).index(row["group"]), ranking_key(row)))
    report = {"version": 1, "measured_at": datetime.now(timezone.utc).isoformat(), "location": location,
              "profile_labels": {name: config["profiles"][name].get("description", name) for name in names},
              "samples": samples, "timeout_seconds": timeout, "elapsed_seconds": round(time.monotonic() - started, 1),
              "network_note": "此次使用代理优先或回退模式。" if network.get("proxy_url") and network.get("proxy_mode") != "off" else "此次按直连模式测量。",
              "scope": "HTTP response and supporting-file download; actual playback is untested",
              "summary": {"targets": len(results), "normal": sum(r["status"] == "正常" for r in results),
                          "intermittent": sum(r["status"] == "偶发失败" for r in results),
                          "failed": sum(r["status"] == "无法读取" for r in results)},
              "recommended_config_urls": recommendations(results, names), "results": results,
              "unmeasured_sites": skipped}
    sync.save_json(root / "speed_report.json", report)
    html_path = root / "speed_report.html"
    temp = html_path.with_suffix(".html.tmp")
    temp.write_text(render_html(report), encoding="utf-8")
    temp.replace(html_path)
    print("测速完成：speed_report.html（可直接打开），speed_report.json（详细数据）", flush=True)
    return 0
