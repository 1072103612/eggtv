#!/usr/bin/env python3
"""Sync TVBox sources from upstream to GitHub.

Simple workflow:
1. Fetch upstream JSON
2. Save raw upstream snapshot
3. Update spider.jar URLs to GitHub Raw
4. Generate mirrors.json for CDN redundancy
5. Save as publish file (same as upstream, just with updated spider URLs)
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


JsonValue = Any


class SyncError(RuntimeError):
    """Raised when a sync fails."""


def load_json(path: Path) -> JsonValue:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: JsonValue, dry_run: bool = False) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    if previous == serialized:
        return False
    if not dry_run:
        path.write_text(serialized, encoding="utf-8")
    return True


def is_http_url(value: str) -> bool:
    return urllib.parse.urlparse(value).scheme in {"http", "https"}


def normalize_source_url(source: str) -> str:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme not in {"http", "https"}:
        return source
    if parsed.netloc != "github.com":
        return source
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 5 and parts[2] == "blob":
        owner, repo, _, branch = parts[:4]
        remainder = "/".join(parts[4:])
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{remainder}"
    return source


def build_fetch_attempts(network: Optional[Dict[str, Any]]) -> List[Tuple[str, Optional[str]]]:
    network = network or {}
    proxy_url = network.get("proxy_url")
    proxy_mode = network.get("proxy_mode", "prefer")
    if not proxy_url or proxy_mode == "off":
        return [("direct", None)]
    if proxy_mode == "only":
        return [("proxy", proxy_url)]
    if proxy_mode == "fallback":
        return [("direct", None), ("proxy", proxy_url)]
    return [("proxy", proxy_url), ("direct", None)]


def read_http_bytes(source: str, timeout: int = 30, network: Optional[Dict[str, Any]] = None) -> bytes:
    errors = []
    for mode, proxy_url in build_fetch_attempts(network):
        cmd = [
            "curl", "-fsSL",
            "-A", "eggtv-sync/1.0 (+https://github.com/1072103612/eggtv)",
            "--retry", "2", "--retry-delay", "2",
            "--connect-timeout", str(min(timeout, 20)),
            "--max-time", str(timeout),
        ]
        if proxy_url:
            cmd.extend(["--proxy", proxy_url])
        cmd.append(source)
        result = subprocess.run(cmd, capture_output=True, check=False)
        if result.returncode == 0:
            return result.stdout
        errors.append(f"{mode}: {result.stderr.decode('utf-8', errors='replace').strip() or 'curl failed'}")
    raise SyncError(f"failed to fetch {source}: {' | '.join(errors)}")


def read_bytes_from_source(source: str, timeout: int = 30, network: Optional[Dict[str, Any]] = None) -> bytes:
    source = normalize_source_url(source)
    if is_http_url(source):
        return read_http_bytes(source, timeout=timeout, network=network)
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise SyncError(f"source does not exist: {path}")
    return path.read_bytes()


def load_json_from_source(source: str, timeout: int = 30, network: Optional[Dict[str, Any]] = None) -> JsonValue:
    try:
        return json.loads(read_bytes_from_source(source, timeout=timeout, network=network).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SyncError(f"invalid JSON from {source}: {exc}") from exc


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_valid_jar_bytes(data: bytes) -> bool:
    return data[:4] in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}


def filter_sites(sites: List[Dict[str, Any]], block_keywords: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """按站点名字过滤，命中关键字则删除，返回 (保留下来的, 被移除的)"""
    if not block_keywords:
        return sites, []
    kept = []
    removed = []
    for site in sites:
        name = site.get("name", "")
        blocked = any(kw in name for kw in block_keywords)
        if blocked:
            removed.append(name)
            continue
        kept.append(site)
    return kept, removed


def ensure_relative_to_repo(repo_root: Path, relative_path: str) -> Path:
    candidate = (repo_root / relative_path).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise SyncError(f"path escapes repository root: {relative_path}") from exc
    return candidate


def infer_default_branch(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip() or "main"


def infer_github_repo(repo_root: Path) -> Optional[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    remote = result.stdout.strip()
    if remote.startswith("git@github.com:"):
        remote = remote[len("git@github.com:") :]
    elif remote.startswith("https://github.com/"):
        remote = remote[len("https://github.com/") :]
    else:
        return None
    if remote.endswith(".git"):
        remote = remote[:-4]
    return remote or None


def compute_raw_base(repo_root: Path, repo_config: Dict[str, Any]) -> str:
    if repo_config.get("raw_base"):
        return repo_config["raw_base"].rstrip("/")
    github_repo = repo_config.get("github_repo") or infer_github_repo(repo_root)
    if not github_repo:
        raise SyncError("cannot infer GitHub repo; set repo.github_repo or repo.raw_base")
    branch = repo_config.get("branch") or infer_default_branch(repo_root)
    return f"https://raw.githubusercontent.com/{github_repo}/{branch}"


def strip_spider_suffix(spider_value: str) -> str:
    return spider_value.split(";", 1)[0].strip()


def resolve_relative_reference(base_source: str, reference: str) -> str:
    parsed = urllib.parse.urlparse(reference)
    if parsed.scheme in {"http", "https", "file"}:
        return reference
    if base_source and is_http_url(base_source):
        return urllib.parse.urljoin(base_source, reference)
    return reference


def run_git(repo_root: Path, args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True, text=True, check=False,
    )


# --- Spider handling ---

def download_spider_with_fallback(
    sources: List[str],
    spider_timeout: int,
    network: Optional[Dict[str, Any]],
) -> Tuple[Optional[bytes], List[str]]:
    """Try to download spider from multiple sources."""
    errors = []
    for source in sources:
        try:
            content = read_bytes_from_source(source, timeout=spider_timeout, network=network)
            if is_valid_jar_bytes(content):
                return content, sources
        except SyncError as exc:
            errors.append(f"{source}: {exc}")
        except Exception as exc:
            errors.append(f"{source}: {exc}")
    return None, errors


def update_spider_field(
    repo_root: Path,
    repo_config: Dict[str, Any],
    profile_config: Dict[str, Any],
    publish_payload: Dict[str, Any],
    upstream_source: str,
    network: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> Optional[Path]:
    spider_config = profile_config.get("spider")
    if not spider_config:
        return None

    spider_value = publish_payload.get("spider")
    if not isinstance(spider_value, str) or not spider_value.strip():
        return None

    spider_source = strip_spider_suffix(spider_value)
    resolved_source = resolve_relative_reference(upstream_source, spider_source)
    spider_timeout = int(spider_config.get("timeout", 120))

    # Build list of sources to try
    spider_sources = [resolved_source]
    for fallback in spider_config.get("fallback_sources", []):
        fallback_normalized = normalize_source_url(fallback)
        if fallback_normalized not in spider_sources:
            spider_sources.append(fallback_normalized)

    target_path = ensure_relative_to_repo(repo_root, spider_config["download_to"])
    target_path.parent.mkdir(parents=True, exist_ok=True)
    previous_content = target_path.read_bytes() if target_path.exists() else None

    if dry_run:
        if previous_content is None:
            print(f"[spider] dry-run: {target_path.relative_to(repo_root)} does not exist, would try {len(spider_sources)} sources")
            return None
        digest = md5_file(target_path)
        raw_base = compute_raw_base(repo_root, repo_config)
        publish_path = spider_config.get("publish_path", spider_config["download_to"]).lstrip("/")
        new_spider_url = f"{raw_base}/{publish_path};md5;{digest}"
        if new_spider_url != publish_payload.get("spider"):
            print(f"[spider] dry-run: would update spider URL")
        return None

    # Try sources
    new_content = None
    success_source = None
    errors = []

    for source in spider_sources:
        content, errs = download_spider_with_fallback([source], spider_timeout, network)
        if content is not None and is_valid_jar_bytes(content):
            new_content = content
            success_source = source
            break
        else:
            errors.extend(errs)

    if new_content is None:
        if previous_content is None:
            raise SyncError(f"invalid spider from all sources: {'; '.join(errors[:3])}")
        print(f"[spider] WARNING: all spider sources failed, keeping existing {target_path.relative_to(repo_root)}")
        new_content = previous_content

    if success_source and success_source != resolved_source:
        print(f"[spider] primary source failed, used fallback: {success_source}")

    file_changed = previous_content != new_content
    if file_changed:
        target_path.write_bytes(new_content)

    digest = md5_file(target_path)
    raw_base = compute_raw_base(repo_root, repo_config)
    publish_path = spider_config.get("publish_path", spider_config["download_to"]).lstrip("/")
    publish_payload["spider"] = f"{raw_base}/{publish_path};md5;{digest}"
    return target_path if file_changed else None


# --- Mirrors ---

def generate_mirrors_config(
    repo_root: Path,
    repo_config: Dict[str, Any],
    profiles: Dict[str, Any],
) -> Optional[Path]:
    mirrors_config = repo_config.get("mirrors")
    if not mirrors_config or not mirrors_config.get("enabled"):
        return None

    cdns = mirrors_config.get("cdns", [])
    if not cdns:
        return None

    mirrors_payload: Dict[str, Any] = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "profiles": {},
    }

    raw_base = compute_raw_base(repo_root, repo_config)
    for profile_name, profile_config in profiles.items():
        publish_output = ensure_relative_to_repo(repo_root, profile_config["publish_output"])
        if not publish_output.exists():
            continue

        profile_cdns = []
        for cdn in cdns:
            cdn_base = cdn.rstrip("/")
            profile_cdns.append(f"{cdn_base}/{publish_output.relative_to(repo_root)}")

        mirrors_payload["profiles"][profile_name] = {
            "config_url": f"{raw_base}/{publish_output.relative_to(repo_root)}",
            "mirrors": profile_cdns,
        }

    mirrors_path = repo_root / "mirrors.json"
    if save_json(mirrors_path, mirrors_payload):
        print(f"[mirrors] generated mirrors.json with {len(cdns)} CDN endpoints")
        return mirrors_path
    return None


def reconcile_spider_fields(
    repo_root: Path,
    repo_config: Dict[str, Any],
    profiles: Dict[str, Any],
) -> List[Path]:
    raw_base = compute_raw_base(repo_root, repo_config)
    changed_files = []

    for profile_name, profile_config in profiles.items():
        spider_config = profile_config.get("spider")
        if not spider_config:
            continue

        spider_file = ensure_relative_to_repo(repo_root, spider_config["download_to"])
        publish_output = ensure_relative_to_repo(repo_root, profile_config["publish_output"])
        if not spider_file.exists() or not publish_output.exists():
            continue

        payload = load_json(publish_output)
        if not isinstance(payload, dict):
            continue

        publish_path = spider_config.get("publish_path", spider_config["download_to"]).lstrip("/")
        expected_spider = f"{raw_base}/{publish_path};md5;{md5_file(spider_file)}"
        if payload.get("spider") == expected_spider:
            continue

        payload["spider"] = expected_spider
        if save_json(publish_output, payload):
            changed_files.append(publish_output)
            print(f"[spider] aligned {profile_name} -> {publish_output.relative_to(repo_root)}")

    return changed_files


# --- Sync core ---

def resolve_upstream_sources(repo_root: Path, profile_config: Dict[str, Any], cli_override: Optional[str]) -> List[str]:
    candidates = []
    if cli_override:
        candidates.append(normalize_source_url(cli_override))
    else:
        primary = profile_config.get("upstream_url")
        if primary:
            candidates.append(normalize_source_url(primary))
        for fallback in profile_config.get("upstream_fallback_urls", []):
            candidates.append(normalize_source_url(fallback))

    if candidates:
        seen = set()
        deduped = []
        for item in candidates:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped

    seed = profile_config.get("upstream_seed") or profile_config.get("upstream_output")
    if not seed:
        raise SyncError("no upstream_url or upstream_seed configured")
    return [str(ensure_relative_to_repo(repo_root, seed))]


def fetch_upstream_json(
    sources: List[str],
    timeout: int,
    network: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    errors = []
    for source in sources:
        try:
            payload = load_json_from_source(source, timeout=timeout, network=network)
        except SyncError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(payload, dict):
            errors.append(f"upstream {source} must be a JSON object")
            continue
        return source, payload

    raise SyncError(" | ".join(errors) or "no upstream source available")


def sync_profile(
    repo_root: Path,
    repo_config: Dict[str, Any],
    profile_name: str,
    profile_config: Dict[str, Any],
    upstream_override: Optional[str] = None,
    network: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    upstream_output = ensure_relative_to_repo(repo_root, profile_config["upstream_output"])
    publish_output = ensure_relative_to_repo(repo_root, profile_config["publish_output"])

    upstream_sources = resolve_upstream_sources(repo_root, profile_config, upstream_override)
    fetch_timeout = int(profile_config.get("fetch_timeout", 60))
    upstream_source, fetched_upstream = fetch_upstream_json(
        upstream_sources,
        timeout=fetch_timeout,
        network=network,
    )

    # Use upstream directly as publish payload
    publish_payload = copy.deepcopy(fetched_upstream)

    # Track sync info for report
    sync_info = {
        "profile": profile_name,
        "source": upstream_source,
        "changed_files": [],
        "sites_kept": 0,
        "sites_removed": 0,
        "removed_sites": [],
        "renamed": None,
    }

    # 重命名第一个站点
    rename_first = profile_config.get("rename_first")
    if rename_first and "sites" in publish_payload and isinstance(publish_payload["sites"], list) and len(publish_payload["sites"]) > 0:
        old_name = publish_payload["sites"][0].get("name", "")
        publish_payload["sites"][0]["name"] = rename_first
        sync_info["renamed"] = {"from": old_name, "to": rename_first}
        print(f"[{profile_name}] rename: {old_name} -> {rename_first}")

    # 清洗站点：过滤掉不需要的分类
    block_keywords = profile_config.get("filter", {}).get("block_keywords", [])
    if block_keywords and "sites" in publish_payload and isinstance(publish_payload["sites"], list):
        original_count = len(publish_payload["sites"])
        publish_payload["sites"], removed_names = filter_sites(publish_payload["sites"], block_keywords)
        removed_count = original_count - len(publish_payload["sites"])
        sync_info["sites_kept"] = len(publish_payload["sites"])
        sync_info["sites_removed"] = removed_count
        sync_info["removed_sites"] = removed_names
        if removed_count > 0:
            print(f"[{profile_name}] filter: 移除 {removed_count} 个站点")
            for name in removed_names:
                print(f"  - {name}")

    changed_files = []

    # Save upstream snapshot
    if save_json(upstream_output, fetched_upstream, dry_run=dry_run):
        changed_files.append(upstream_output)

    # Update spider and save publish
    spider_file = update_spider_field(
        repo_root,
        repo_config,
        profile_config,
        publish_payload,
        upstream_source,
        network=network,
        dry_run=dry_run,
    )
    if spider_file is not None:
        changed_files.append(spider_file)

    if save_json(publish_output, publish_payload, dry_run=dry_run):
        changed_files.append(publish_output)

    sync_info["changed_files"] = changed_files
    return sync_info


# --- Config ---

def load_config(repo_root: Path, config_path: Path) -> Dict[str, Any]:
    config = load_json(config_path)
    if not isinstance(config, dict):
        raise SyncError("config file must contain a JSON object")
    config.setdefault("repo", {})
    config.setdefault("profiles", {})
    if not isinstance(config["profiles"], dict):
        raise SyncError("config.profiles must be an object")
    return config


def save_config(config_path: Path, config: Dict[str, Any]) -> None:
    save_json(config_path, config)


def resolve_network_config(config: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    network = copy.deepcopy(config.get("network", {}))
    if getattr(args, "proxy", None):
        network["proxy_url"] = args.proxy
    if getattr(args, "no_proxy", False):
        network["proxy_mode"] = "off"
        network.pop("proxy_url", None)
    network.setdefault("proxy_mode", "prefer")
    return network


# --- Commands ---

def cmd_list(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config_path = (repo_root / args.config).resolve()
    config = load_config(repo_root, config_path)
    network = resolve_network_config(config, args)
    proxy_text = network.get("proxy_url") if network.get("proxy_mode") != "off" else "(disabled)"
    print(f"proxy\t{network.get('proxy_mode', 'prefer')}\t{proxy_text}")
    for name in sorted(config["profiles"]):
        profile = config["profiles"].get(name, {})
        upstream_url = profile.get("upstream_url") or "(unset)"
        print(f"{name}\t{profile.get('publish_output')}\t{upstream_url}")
    return 0


def cmd_set_url(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config_path = (repo_root / args.config).resolve()
    config = load_config(repo_root, config_path)
    profile = config["profiles"].get(args.profile)
    if profile is None:
        raise SyncError(f"unknown profile: {args.profile}")
    normalized_url = normalize_source_url(args.url)
    profile["upstream_url"] = normalized_url
    save_config(config_path, config)
    print(f"{args.profile}: upstream_url -> {normalized_url}")
    return 0


def cmd_show_rules(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config_path = (repo_root / args.config).resolve()
    config = load_config(repo_root, config_path)
    network = resolve_network_config(config, args)

    print(f"- 同步逻辑: 极简主义 - 上游增就增，上游删就删，上游变就变")
    print(f"- 代理模式: {network.get('proxy_mode', 'prefer')}")
    print(f"- 代理地址: {network.get('proxy_url') or '(未设置)'}")
    print()

    for name in sorted(config["profiles"]):
        profile = config["profiles"].get(name, {})
        print(f"[{name}] {profile.get('description', '')}".strip())
        print(f"- 上游链接: {profile.get('upstream_url') or '(未设置)'}")
        fallback_urls = profile.get("upstream_fallback_urls") or []
        if fallback_urls:
            print(f"- 候补链接: {', '.join(fallback_urls)}")
        print(f"- 上游留底: {profile.get('upstream_output')}")
        print(f"- 对外发布: {profile.get('publish_output')}")
        print()

    print("工作方式:")
    print("  1. 抓取上游原始 JSON，保存为留底文件")
    print("  2. 直接使用上游内容作为发布文件（不做过滤、不做测速）")
    print("  3. spider.jar 下载到仓库，改写成 GitHub Raw 地址 + MD5")
    print("  4. spider 多源兜底，失败时自动切换")
    print("  5. mirrors.json 提供多 CDN 出口")
    print()
    return 0


def check_url_health(url: str, timeout: int, network: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    errors = []
    for mode, proxy_url in build_fetch_attempts(network):
        cmd = [
            "curl", "-fsSL",
            "-A", "eggtv-healthcheck/1.0",
            "--connect-timeout", str(min(timeout, 20)),
            "--max-time", str(timeout),
            "-o", "/dev/null",
            "-w", "%{http_code}\t%{time_starttransfer}\t%{time_total}",
        ]
        if proxy_url:
            cmd.extend(["--proxy", proxy_url])
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            parts = result.stdout.strip().split("\t")
            if len(parts) >= 3:
                http_code, ttfb, total = parts
                return {
                    "url": url,
                    "http_code": int(http_code),
                    "time_starttransfer_ms": float(ttfb) * 1000.0,
                    "time_total_ms": float(total) * 1000.0,
                    "reachable": True,
                }
        errors.append(f"{mode}: {result.stderr.strip() or 'curl failed'}")
    return {
        "url": url,
        "reachable": False,
        "error": " | ".join(errors),
    }


def cmd_health(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config_path = (repo_root / args.config).resolve()
    config = load_config(repo_root, config_path)
    network = resolve_network_config(config, args)
    timeout = int(getattr(args, "timeout", 15))

    print(f"=== 蛋壳影院片源健康检查 ===")
    print(f"检查时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print()

    all_ok = True

    # Check upstream sources
    print("--- 上游片源 ---")
    for name in sorted(config["profiles"]):
        profile = config["profiles"].get(name, {})
        upstream_url = profile.get("upstream_url")
        if not upstream_url:
            print(f"[{name}] 上游: (未配置)")
            continue

        result = check_url_health(upstream_url, timeout, network)
        if result["reachable"]:
            print(f"[{name}] 上游: OK -> HTTP {result['http_code']}, {result['time_starttransfer_ms']:.0f}ms")
        else:
            print(f"[{name}] 上游: FAIL -> {result['error'][:80]}")
            all_ok = False

        for fallback in profile.get("upstream_fallback_urls", []):
            fb_result = check_url_health(fallback, timeout, network)
            if fb_result["reachable"]:
                print(f"  └─ 候补: OK ({fb_result['time_starttransfer_ms']:.0f}ms)")
            else:
                print(f"  └─ 候补: FAIL")

    print()

    # Check spider JAR
    print("--- Spider JAR ---")
    spider_file = repo_root / "jar" / "spider.jar"
    if spider_file.exists():
        digest = md5_file(spider_file)
        size_kb = spider_file.stat().st_size // 1024
        print(f"spider.jar: OK ({size_kb} KB, md5: {digest[:12]}...)")
    else:
        print(f"spider.jar: 缺失!")
        all_ok = False

    print()

    # Check CDN mirrors
    mirrors_config = config.get("repo", {}).get("mirrors", {})
    if mirrors_config.get("enabled"):
        cdns = mirrors_config.get("cdns", [])
        print("--- CDN 镜像 ---")
        for cdn in cdns:
            print(f"CDN: {cdn}")
        print()

    # Check publish files
    print("--- 发布文件 ---")
    for name in sorted(config["profiles"]):
        profile = config["profiles"].get(name, {})
        publish_path = repo_root / profile.get("publish_output", "")
        if publish_path.exists():
            size_kb = publish_path.stat().st_size // 1024
            try:
                payload = load_json(publish_path)
                sites_count = len(payload.get("sites", [])) if isinstance(payload, dict) else 0
                print(f"[{name}] {publish_path.name}: OK ({size_kb} KB, {sites_count} sites)")
            except Exception as e:
                print(f"[{name}] {publish_path.name}: 解析失败 ({e})")
                all_ok = False
        else:
            print(f"[{name}] {publish_path.name}: 缺失!")
            all_ok = False

    print()
    if all_ok:
        print("状态: 全部正常")
        return 0
    else:
        print("状态: 存在问题，请检查上述 FAIL 项")
        return 1


def collect_target_profiles(config: Dict[str, Any], args: argparse.Namespace) -> List[str]:
    if args.all:
        return sorted(config["profiles"])
    if args.profiles:
        missing = [name for name in args.profiles if name not in config["profiles"]]
        if missing:
            raise SyncError(f"unknown profile(s): {', '.join(missing)}")
        return args.profiles
    raise SyncError("select at least one profile or pass --all")


def generate_sync_report(results: List[Dict[str, Any]], repo_root: Path) -> Path:
    """生成同步报告"""
    report = {
        "sync_time": datetime.now(timezone.utc).isoformat(),
        "profiles": []
    }
    for r in results:
        report["profiles"].append({
            "name": r["profile"],
            "source": r["source"],
            "sites_kept": r.get("sites_kept", 0),
            "sites_removed": r.get("sites_removed", 0),
            "removed_sites": r.get("removed_sites", []),
            "renamed": r.get("renamed"),
            "changed_files": [str(p.relative_to(repo_root)) for p in r.get("changed_files", [])]
        })
    report_path = repo_root / "sync_report.json"
    save_json(report_path, report)
    print(f"[report] 报告已生成: sync_report.json")
    return report_path


def cmd_sync(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config_path = (repo_root / args.config).resolve()
    config = load_config(repo_root, config_path)
    repo_config = config["repo"]
    network = resolve_network_config(config, args)
    target_profiles = collect_target_profiles(config, args)
    changed_files = []
    sync_results = []
    dry_run = getattr(args, "dry_run", False)
    show_diff = getattr(args, "diff", False)

    for name in target_profiles:
        upstream_override = args.upstream_url if len(target_profiles) == 1 else None
        profile_config = config["profiles"].get(name, {})
        result = sync_profile(
            repo_root,
            repo_config,
            name,
            profile_config,
            upstream_override=upstream_override,
            network=network,
            dry_run=dry_run,
        )
        changed_files.extend(result["changed_files"])
        sync_results.append(result)
        changed_summary = ", ".join(str(p.relative_to(repo_root)) for p in result["changed_files"]) or "no file changes"
        action = "[dry-run] would change" if dry_run else "->"
        print(f"[{name}] {result['source']} {action} {changed_summary}")

        if show_diff and result["changed_files"]:
            for changed_path in result["changed_files"]:
                _show_file_diff(repo_root, changed_path)

    resolved_profiles = {name: config["profiles"].get(name, {}) for name in config["profiles"]}
    if not dry_run:
        changed_files.extend(reconcile_spider_fields(repo_root, repo_config, resolved_profiles))
        mirror_file = generate_mirrors_config(repo_root, repo_config, resolved_profiles)
        if mirror_file:
            changed_files.append(mirror_file)

    if dry_run:
        print("[dry-run] no files were written")
        return 0

    # 生成同步报告
    report_path = generate_sync_report(sync_results, repo_root)

    if args.push:
        unique_files = sorted(set(changed_files), key=lambda p: str(p))
        unique_files.append(report_path)  # 报告也提交
        commit_message = args.commit_message or f"chore(sync): refresh {'/'.join(target_profiles)}"
        git_commit_and_push(repo_root, unique_files, commit_message)
        print("git push completed")

    # 控制台摘要
    print("\n" + "=" * 50)
    print("同步摘要")
    print("=" * 50)
    for r in sync_results:
        print(f"\n[{r['profile']}]")
        print(f"  来源: {r['source']}")
        print(f"  保留站点: {r.get('sites_kept', 0)} 个")
        print(f"  移除站点: {r.get('sites_removed', 0)} 个")
        if r.get('renamed'):
            print(f"  重命名: {r['renamed']['from']} -> {r['renamed']['to']}")
    print("\n" + "=" * 50)

    return 0


def _show_file_diff(repo_root: Path, file_path: Path) -> None:
    try:
        result = run_git(repo_root, ["diff", "HEAD", "--", str(file_path.relative_to(repo_root))])
        if result.stdout.strip():
            print(f"--- {file_path.relative_to(repo_root)}")
            print(result.stdout)
    except Exception:
        pass


def git_commit_and_push(repo_root: Path, files: List[Path], commit_message: str) -> None:
    if not files:
        return
    relative_files = [str(p.relative_to(repo_root)) for p in files]
    add_result = run_git(repo_root, ["add", *relative_files])
    if add_result.returncode != 0:
        raise SyncError(add_result.stderr.strip() or "git add failed")
    diff_result = run_git(repo_root, ["diff", "--cached", "--quiet"])
    if diff_result.returncode == 0:
        return
    if diff_result.returncode not in {0, 1}:
        raise SyncError(diff_result.stderr.strip() or "git diff --cached failed")
    commit_result = run_git(repo_root, ["commit", "-m", commit_message])
    if commit_result.returncode != 0:
        raise SyncError(commit_result.stderr.strip() or "git commit failed")
    push_result = run_git(repo_root, ["push", "origin", "HEAD"])
    if push_result.returncode != 0:
        raise SyncError(push_result.stderr.strip() or "git push failed")


# --- CLI ---

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync TVBox sources from upstream")
    parser.add_argument("--repo-root", default=".", help="repository root")
    parser.add_argument("--config", default="eggtv_sync.json", help="config file path")
    parser.add_argument("--proxy", help="proxy URL")
    parser.add_argument("--no-proxy", action="store_true", help="disable proxy")

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list profiles")
    list_parser.set_defaults(func=cmd_list)

    set_url_parser = subparsers.add_parser("set-url", help="set upstream URL")
    set_url_parser.add_argument("profile", help="profile name")
    set_url_parser.add_argument("url", help="upstream URL")
    set_url_parser.set_defaults(func=cmd_set_url)

    show_rules_parser = subparsers.add_parser("show-rules", help="show sync rules")
    show_rules_parser.add_argument("profile", nargs="?", help="profile name")
    show_rules_parser.set_defaults(func=cmd_show_rules)

    health_parser = subparsers.add_parser("health", help="check health")
    health_parser.add_argument("--timeout", type=int, default=15)
    health_parser.set_defaults(func=cmd_health)

    sync_parser = subparsers.add_parser("sync", help="sync sources")
    sync_parser.add_argument("profiles", nargs="*", help="profile names")
    sync_parser.add_argument("--all", action="store_true", help="sync all")
    sync_parser.add_argument("--upstream-url", help="override upstream URL")
    sync_parser.add_argument("--push", action="store_true", help="commit and push")
    sync_parser.add_argument("--commit-message", help="commit message")
    sync_parser.add_argument("--dry-run", action="store_true", help="preview only")
    sync_parser.add_argument("--diff", action="store_true", help="show diff")
    sync_parser.set_defaults(func=cmd_sync)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
