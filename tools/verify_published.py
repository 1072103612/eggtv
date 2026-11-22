"""Verify original subscription URLs and their exact dependency bytes after deployment."""
import argparse
import hashlib
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from . import eggtv_sync as sync
else:
    import eggtv_sync as sync


def publication_targets(root, config):
    base = sync.compute_raw_base(root, config["repo"]).rstrip("/") + "/"
    targets = {}

    def add_local(url, path):
        if not path.is_file():
            raise sync.SyncError(f"发布文件缺失: {path}")
        targets[url] = hashlib.sha256(path.read_bytes()).hexdigest()

    for profile in config["profiles"].values():
        output = sync.ensure_relative_to_repo(root, profile["publish_output"])
        payload = sync.load_json(output)
        sync.validate_source_payload(payload)
        if config["repo"].get("publish_dependency_transport") == "pages":
            sync.validate_pages_dependencies(root, config["repo"], payload)
        configs = [output]
        for suffix in ("proxy", "cdn"):
            alias = output.with_name(output.stem + f"_{suffix}" + output.suffix)
            if alias.is_file():
                if config["repo"].get("publish_dependency_transport") == "pages" and sync.load_json(alias) != payload:
                    raise sync.SyncError(f"旧订阅入口与主配置不一致: {alias}")
                configs.append(alias)
        for path in configs:
            add_local(base + path.relative_to(root).as_posix(), path)
        dependencies = [sync.strip_spider_suffix(payload["spider"]), *sync.resource_urls(payload)]
        for url in dependencies:
            if url.startswith(base):
                relative = urllib.parse.unquote(urllib.parse.urlsplit(url).path[len(urllib.parse.urlsplit(base).path):])
                add_local(url, sync.ensure_relative_to_repo(root, relative))
            else:
                targets.setdefault(url, None)
    return targets


def read_url(url, timeout=15):
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "eggtv-publication-check/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(16 * 1024 * 1024 + 1)
        if len(body) > 16 * 1024 * 1024:
            raise ValueError("发布文件超过检查大小限制")
        return body


def verify_once(targets, fetch=read_url):
    def check(item):
        url, expected = item
        try:
            body = fetch(url)
            if expected is not None and hashlib.sha256(body).hexdigest() != expected:
                raise ValueError("线上内容仍是旧版本或校验不一致")
            if not body:
                raise ValueError("线上文件为空")
            return None
        except (OSError, ValueError, RuntimeError) as exc:
            return {"url": url, "error": str(exc)[:500]}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return [error for error in pool.map(check, targets.items()) if error is not None]


def wait_for_publication(root, config, timeout=300, interval=10, fetch=read_url,
                         sleep=time.sleep, clock=time.monotonic):
    targets = publication_targets(root, config)
    deadline = clock() + timeout
    attempts = 0
    while True:
        attempts += 1
        errors = verify_once(targets, fetch)
        report = {"version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
                  "status": "failed" if errors else "verified", "original_urls": True,
                  "targets": len(targets), "attempts": attempts, "errors": errors}
        sync.save_json(root / "publication_report.json", report)
        if not errors:
            print(f"原订阅地址及全部 {len(targets)} 个配置/配套文件已核验，内容与本次发布一致。", flush=True)
            return report
        if clock() >= deadline:
            raise sync.SyncError(f"发布后验证失败，尚有 {len(errors)} 个原地址未取得正确文件: {errors[:3]}")
        print(f"等待 GitHub Pages 更新原地址：{len(errors)} 个文件尚未通过核验。", flush=True)
        sleep(min(interval, max(0, deadline - clock())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--local-only", action="store_true", help="validate prepared files without contacting the published site")
    args = parser.parse_args()
    if args.timeout < 0 or args.interval <= 0:
        parser.error("timeout must be nonnegative; interval must be positive")
    root = Path(args.repo_root).resolve()
    try:
        config = sync.load_config(root, root / "eggtv_sync.json")
        if args.local_only:
            targets = publication_targets(root, config)
            print(f"发布前校验通过：{len(targets)} 个配置和配套文件。")
        else:
            wait_for_publication(root, config, timeout=args.timeout, interval=args.interval)
    except (sync.SyncError, ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
