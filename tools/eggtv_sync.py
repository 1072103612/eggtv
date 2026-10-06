#!/usr/bin/env python3
"""Validate, clean and publish TVBox configs with independent playback tools."""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
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
    serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    if previous == serialized:
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写临时文件，再 rename
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(serialized, encoding="utf-8")
        tmp_path.replace(path)
    return True


def is_http_url(value: str) -> bool:
    return urllib.parse.urlparse(value).scheme in {"http", "https"}


def clamp_timeout(value: int, min_val: int = 5, max_val: int = 300) -> int:
    """确保超时值在合理范围内，防止零超时或极长等待。"""
    return max(min_val, min(max_val, value))


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
        else:
            cmd.extend(["--noproxy", "*"])
        cmd.append(source)
        # curl --retry 2 最多 3 次尝试，加 buffer 防止 subprocess 僵死
        process_timeout = timeout * 3 + 60
        try:
            result = subprocess.run(cmd, capture_output=True, check=False, timeout=process_timeout)
        except subprocess.TimeoutExpired:
            errors.append(f"{mode}: subprocess timed out after {process_timeout}s")
            continue
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
        return json.loads(read_bytes_from_source(source, timeout=timeout, network=network).decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise SyncError(f"invalid JSON from {source}: {exc}") from exc


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_valid_jar_bytes(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            return (
                any(name.endswith((".dex", ".class")) for name in names)
                and archive.testzip() is None
            )
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError):
        return False


def jar_supports_sites(data: bytes, payload: Dict[str, Any]) -> bool:
    if not is_valid_jar_bytes(data):
        return False
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        dex = b"".join(archive.read(name) for name in names if name.endswith(".dex"))
        for site in payload["sites"]:
            api = site["api"]
            if not api.startswith("csp_"):
                continue
            class_path = "com/github/catvod/spider/" + api[4:]
            if class_path + ".class" not in names and ("L" + class_path + ";").encode() not in dex:
                return False
    return True


def validate_source_payload(payload: JsonValue) -> None:
    """Reject incomplete upstream responses before replacing a working config."""
    if not isinstance(payload, dict):
        raise SyncError("配置必须是 JSON 对象")
    sites = payload.get("sites")
    if not isinstance(sites, list) or not sites:
        raise SyncError("片源站点为空，保留原配置")
    if not isinstance(payload.get("spider"), str) or not payload["spider"].strip():
        raise SyncError("缺少播放工具地址，保留原配置")
    for site in sites:
        if not isinstance(site, dict) or not all(
            isinstance(site.get(field), str) and site[field].strip()
            for field in ("key", "name", "api")
        ):
            raise SyncError("站点缺少名称、标识或接口，保留原配置")


def resolve_payload_references(value: JsonValue, upstream_source: str, field: str = "") -> JsonValue:
    """Keep upstream-relative scripts and nested resources usable after publishing."""
    if isinstance(value, dict):
        return {key: resolve_payload_references(item, upstream_source, key) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_payload_references(item, upstream_source, field) for item in value]
    if isinstance(value, str) and is_http_url(upstream_source):
        if value.startswith(("./", "../", "//")) or (
            value.startswith("/") and field in {"api", "ext", "url", "spider", "wallpaper", "logo", "epg"}
        ):
            return urllib.parse.urljoin(upstream_source, value)
    return value


def all_strings(value: JsonValue) -> List[str]:
    if isinstance(value, dict):
        return [text for item in value.values() for text in all_strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in all_strings(item)]
    return [value] if isinstance(value, str) else []


def resource_urls(payload: JsonValue) -> List[str]:
    return sorted({value for value in all_strings(payload) if is_http_url(value)
                   and urllib.parse.urlparse(value).path.lower().endswith((".js", ".py", ".json"))})


def validate_relative_resources(original: Dict[str, Any], published: Dict[str, Any],
                                source: str, network: Optional[Dict[str, Any]]) -> None:
    if not is_http_url(source):
        return
    published_urls = set(resource_urls(published))
    urls = sorted({urllib.parse.urljoin(source, value) for value in all_strings(original)
                   if value.startswith(("./", "../", "/"))
                   and urllib.parse.urljoin(source, value) in published_urls})
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda url: check_url_health(url, 15, network, kind="resource"), urls))
    failures = [result["url"] for result in results if not result["reachable"]]
    if failures:
        raise SyncError("配套文件无法读取，保留原配置: " + ", ".join(failures))


def deduplicate_sites(sites: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    kept, removed, seen = [], [], set()
    for site in sites:
        if site["key"] in seen:
            removed.append(site["name"])
        else:
            seen.add(site["key"])
            kept.append(site)
    return kept, removed


def filter_sites(sites: List[Dict[str, Any]], block_keywords: List[str],
                 block_keys: Optional[List[str]] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """按站点名字过滤，命中关键字则删除，返回 (保留下来的, 被移除的)"""
    if not block_keywords and not block_keys:
        return sites, []
    kept = []
    removed = []
    for site in sites:
        name = site.get("name", "")
        blocked = site.get("key") in (block_keys or []) or any(kw.casefold() in name.casefold() for kw in block_keywords)
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
    spider_timeout = clamp_timeout(int(spider_config.get("timeout", 120)))

    # Build list of sources to try
    spider_sources = [resolved_source]
    for fallback in spider_config.get("fallback_sources", []):
        fallback_normalized = normalize_source_url(fallback)
        if fallback_normalized not in spider_sources:
            spider_sources.append(fallback_normalized)

    target_path = ensure_relative_to_repo(repo_root, spider_config["download_to"])
    if not dry_run:
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
        keep_spider = profile_config.get("keep_upstream_spider")
        if keep_spider:
            print(f"[spider] keep_upstream_spider: preserving original spider URL, jar backed up locally")
        elif new_spider_url != publish_payload.get("spider"):
            print(f"[spider] dry-run: would update spider URL")
        return None

    # Try sources
    new_content = None
    success_source = None
    errors = []

    for source in spider_sources:
        content, errs = download_spider_with_fallback([source], spider_timeout, network)
        if content is not None and jar_supports_sites(content, publish_payload):
            new_content = content
            success_source = source
            break
        else:
            errors.extend(errs)
            if content is not None:
                errors.append(f"{source}: 播放工具不包含此配置需要的站点功能")

    if new_content is None:
        if previous_content is None or not jar_supports_sites(previous_content, publish_payload):
            raise SyncError(f"invalid spider from all sources: {'; '.join(errors[:3])}")
        print(f"[spider] WARNING: all spider sources failed, keeping existing {target_path.relative_to(repo_root)}")
        new_content = previous_content

    if success_source and success_source != resolved_source:
        print(f"[spider] primary source failed, used fallback: {success_source}")

    file_changed = previous_content != new_content
    if file_changed:
        tmp_path = target_path.with_suffix(".jar.tmp")
        tmp_path.write_bytes(new_content)
        tmp_path.replace(target_path)

    digest = md5_file(target_path)
    if not profile_config.get("keep_upstream_spider"):
        raw_base = compute_raw_base(repo_root, repo_config).rstrip("/")
        publish_path = spider_config.get("publish_path", spider_config["download_to"]).strip("/")
        publish_payload["spider"] = f"{raw_base}/{publish_path};md5;{digest}"
    else:
        print(f"[spider] keep_upstream_spider: preserving original spider URL")
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
        if not spider_config or profile_config.get("keep_upstream_spider"):
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
            validate_source_payload(payload)
        except SyncError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(payload, dict):
            errors.append(f"upstream {source} must be a JSON object")
            continue
        return source, payload

    raise SyncError(" | ".join(errors) or "no upstream source available")


def _sync_profile(
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
    fetch_timeout = clamp_timeout(int(profile_config.get("fetch_timeout", 60)))
    upstream_source, fetched_upstream = fetch_upstream_json(
        upstream_sources,
        timeout=fetch_timeout,
        network=network,
    )

    # Use upstream directly as publish payload
    publish_payload = resolve_payload_references(copy.deepcopy(fetched_upstream), upstream_source)

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

    # 清洗站点：过滤掉不需要的分类
    block_keywords = profile_config.get("filter", {}).get("block_keywords", [])
    if "sites" in publish_payload and isinstance(publish_payload["sites"], list):
        original_count = len(publish_payload["sites"])
        publish_payload["sites"], removed_names = filter_sites(
            publish_payload["sites"], block_keywords,
            profile_config.get("filter", {}).get("block_keys", []))
        publish_payload["sites"], duplicate_names = deduplicate_sites(publish_payload["sites"])
        removed_names.extend(duplicate_names)
        removed_count = original_count - len(publish_payload["sites"])
        sync_info["sites_kept"] = len(publish_payload["sites"])
        sync_info["sites_removed"] = removed_count
        sync_info["removed_sites"] = removed_names
        if removed_count > 0:
            print(f"[{profile_name}] filter: 移除 {removed_count} 个站点")
            for name in removed_names:
                print(f"  - {name}")

    for field in profile_config.get("filter", {}).get("drop_fields", []):
        publish_payload.pop(field, None)
    validate_source_payload(publish_payload)
    validate_relative_resources(fetched_upstream, publish_payload, upstream_source, network)

    # 先过滤再改名，避免欢迎语掩盖原站点类别。
    first_key = profile_config.get("first_site_key")
    if first_key:
        preferred = next((site for site in publish_payload["sites"] if site["key"] == first_key), None)
        if preferred is None:
            raise SyncError(f"上游缺少指定首页站点 {first_key}，尝试候补或保留原配置")
        publish_payload["sites"].remove(preferred)
        publish_payload["sites"].insert(0, preferred)
    rename_first = profile_config.get("rename_first")
    if rename_first:
        old_name = publish_payload["sites"][0]["name"]
        publish_payload["sites"][0]["name"] = rename_first
        sync_info["renamed"] = {"from": old_name, "to": rename_first}

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


def sync_profile(
    repo_root: Path,
    repo_config: Dict[str, Any],
    profile_name: str,
    profile_config: Dict[str, Any],
    upstream_override: Optional[str] = None,
    network: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    # 下载、清洗及校验都成功后，才更新正式文件。
    errors = []
    for upstream_source in resolve_upstream_sources(repo_root, profile_config, upstream_override):
        try:
            result = stage_profile(repo_root, repo_config, profile_name, profile_config,
                                   upstream_source, network, dry_run=dry_run)
            result["using_fallback"] = upstream_source != normalize_source_url(profile_config.get("upstream_url", ""))
            return result
        except (SyncError, OSError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
            print(f"[{profile_name}] 此来源不完整，尝试候补: {upstream_source}")
    raise SyncError(" | ".join(errors))


def stage_profile(repo_root: Path, repo_config: Dict[str, Any], profile_name: str,
                  profile_config: Dict[str, Any], upstream_source: str,
                  network: Optional[Dict[str, Any]], dry_run: bool = False) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="eggtv-sync-") as stage_dir:
        stage_root = Path(stage_dir)
        staged_config = copy.deepcopy(profile_config)
        staged_config["upstream_url"] = upstream_source
        staged_config["upstream_fallback_urls"] = []
        if not is_http_url(staged_config["upstream_url"]):
            staged_config["upstream_url"] = str(Path(staged_config["upstream_url"]).resolve())
        seed_paths = [profile_config["publish_output"], profile_config["upstream_output"]]
        if profile_config.get("spider"):
            seed_paths.append(profile_config["spider"]["download_to"])
        for relative_path in seed_paths:
            source = ensure_relative_to_repo(repo_root, relative_path)
            target = ensure_relative_to_repo(stage_root, relative_path)
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        result = _sync_profile(stage_root, repo_config, profile_name, staged_config,
                               network=network)
        staged_payload = load_json(stage_root / profile_config["publish_output"])
        validate_source_payload(staged_payload)
        changed_files = []
        for staged_path in result["changed_files"]:
            target = ensure_relative_to_repo(repo_root, staged_path.relative_to(stage_root).as_posix())
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = target.with_suffix(target.suffix + ".tmp")
                shutil.copyfile(staged_path, tmp_path)
                tmp_path.replace(target)
            changed_files.append(target)
        result["changed_files"] = changed_files
        return result


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
    print("  2. 按名称清洗、去重，并修正配套文件地址")
    print("  3. 两套配置分别保存播放工具，检查工具与站点是否匹配")
    print("  4. spider 多源兜底，失败时自动切换")
    print("  5. mirrors.json 提供多 CDN 出口")
    print()
    return 0


def check_url_health(url: str, timeout: int, network: Optional[Dict[str, Any]],
                     kind: Optional[str] = None, expected_md5: Optional[str] = None,
                     expected_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    timeout = clamp_timeout(timeout)
    errors = []
    for mode, proxy_url in build_fetch_attempts(network):
        with tempfile.TemporaryDirectory(prefix="eggtv-health-") as health_dir:
            body_path = Path(health_dir) / "response"
            result = _check_url_attempt(url, timeout, proxy_url, body_path)
            if result.returncode == 0:
                parts = result.stdout.strip().split("\t")
                try:
                    if len(parts) != 3 or not 200 <= int(parts[0]) < 300:
                        raise SyncError("未返回有效内容")
                    body = body_path.read_bytes()
                    if not body:
                        raise SyncError("返回了空文件")
                    if kind in {"config", "upstream"}:
                        payload = json.loads(body.decode("utf-8-sig"))
                        validate_source_payload(payload)
                        if expected_payload is not None and payload != expected_payload:
                            raise SyncError("镜像尚未更新到当前版本")
                        if kind == "upstream":
                            tool_url = resolve_relative_reference(url, strip_spider_suffix(payload["spider"]))
                            tool_result = check_url_health(tool_url, timeout, network, kind="jar", expected_payload=payload)
                            if not tool_result["reachable"]:
                                raise SyncError("上游播放工具不可用: " + tool_result["error"])
                    if kind == "jar":
                        if not is_valid_jar_bytes(body):
                            raise SyncError("返回的内容不是有效播放工具")
                        if expected_md5 and hashlib.md5(body).hexdigest() != expected_md5:
                            raise SyncError("播放工具与菜单校验值不一致")
                        if expected_payload is not None and not jar_supports_sites(body, expected_payload):
                            raise SyncError("播放工具与站点不匹配")
                    if kind == "resource" and body[:1024].decode("utf-8-sig", errors="replace").lstrip().lower().startswith(("<!doctype html", "<html")):
                        raise SyncError("配套文件地址返回了网页")
                    if kind == "catalog":
                        validate_catalog(body)
                    return {"url": url, "http_code": int(parts[0]), "reachable": True,
                            "time_starttransfer_ms": float(parts[1]) * 1000,
                            "time_total_ms": float(parts[2]) * 1000,
                            "download_bytes": len(body), "connection_mode": mode,
                            "download_kbps": len(body) / max(float(parts[2]), 0.001) / 1024}
                except (SyncError, ValueError, UnicodeError, OSError) as exc:
                    errors.append(f"{mode}: {exc}")
                    continue
            errors.append(f"{mode}: {result.stderr.strip() or 'curl failed'}")
    return {"url": url, "reachable": False, "error": " | ".join(errors)}


def validate_catalog(body: bytes) -> None:
    """Accept actual JSON/XML catalog responses, rather than HTTP-200 error pages."""
    text = body.decode("utf-8-sig").strip()
    if text.startswith(("{", "[")):
        payload = json.loads(text)
        if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
            return
        if isinstance(payload, dict):
            data = payload.get("data", payload)
            if isinstance(data, dict) and isinstance(data.get("list"), list):
                return
            if isinstance(data, list):
                return
        raise SyncError("接口没有返回影片列表")
    try:
        root = ET.fromstring(text)
        if root.tag in {"rss", "list"} and (root.tag == "list" or root.find("list") is not None):
            return
    except ET.ParseError:
        pass
    raise SyncError("接口返回的内容不是影片列表")


def cmd_speedtest(args: argparse.Namespace) -> int:
    if __package__:
        from .eggtv_speedtest import run_speedtest
    else:
        from eggtv_speedtest import run_speedtest
    return run_speedtest(args)


def _check_url_attempt(url: str, timeout: int, proxy_url: Optional[str],
                       body_path: Path) -> subprocess.CompletedProcess:
    cmd = [
        "curl", "-fsSL",
        "-A", "eggtv-healthcheck/1.0",
        "--connect-timeout", str(min(timeout, 20)),
        "--max-time", str(timeout),
        "-o", str(body_path),
        "-w", "%{http_code}\t%{time_starttransfer}\t%{time_total}",
    ]
    if proxy_url:
        cmd.extend(["--proxy", proxy_url])
    else:
        cmd.extend(["--noproxy", "*"])
    cmd.append(url)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="检查超时")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=str(exc))


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

        result = check_url_health(upstream_url, timeout, network, kind="upstream")
        if result["reachable"]:
            print(f"[{name}] 上游: OK -> HTTP {result['http_code']}, {result['time_starttransfer_ms']:.0f}ms")
        else:
            print(f"[{name}] 上游: FAIL -> {result['error'][:80]}")
            all_ok = False

        for fallback in profile.get("upstream_fallback_urls", []):
            fb_result = check_url_health(fallback, timeout, network, kind="upstream")
            if fb_result["reachable"]:
                print(f"  └─ 候补: OK ({fb_result['time_starttransfer_ms']:.0f}ms)")
            else:
                print(f"  └─ 候补: FAIL")

    print()

    # Check spider JAR
    print("--- Spider JAR ---")
    for name, profile in config["profiles"].items():
        spider = profile.get("spider", {})
        spider_file = ensure_relative_to_repo(repo_root, spider.get("download_to", "jar/spider.jar"))
        if spider_file.exists() and is_valid_jar_bytes(spider_file.read_bytes()):
            print(f"[{name}] {spider_file.name}: 文件有效")
        else:
            print(f"[{name}] {spider_file.name}: 缺失或损坏")
            all_ok = False

    print()

    # Check CDN mirrors
    mirrors_config = config.get("repo", {}).get("mirrors", {})
    if mirrors_config.get("enabled"):
        cdns = mirrors_config.get("cdns", [])
        print("--- CDN 镜像 ---")
        for cdn in cdns:
            for name, profile in config["profiles"].items():
                url = cdn.rstrip("/") + "/" + profile["publish_output"]
                local_file = repo_root / profile["publish_output"]
                expected = load_json(local_file) if local_file.exists() else None
                result = check_url_health(url, timeout, network, kind="config", expected_payload=expected)
                print(f"[{name}] 镜像: {'OK' if result['reachable'] else 'FAIL'} -> {url}")
                if not result["reachable"]:
                    all_ok = False
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
                validate_source_payload(payload)
                spider = profile.get("spider", {})
                jar_file = ensure_relative_to_repo(repo_root, spider.get("download_to", "jar/spider.jar"))
                if not jar_file.exists() or not jar_supports_sites(jar_file.read_bytes(), payload):
                    raise SyncError("播放工具与站点不匹配")
                parts = payload["spider"].split(";md5;", 1)
                if len(parts) != 2 or parts[1] != md5_file(jar_file):
                    raise SyncError("播放工具校验值不一致")
                tool_result = check_url_health(parts[0], timeout, network, kind="jar", expected_md5=parts[1])
                if not tool_result["reachable"]:
                    raise SyncError("已发布的播放工具不可用: " + tool_result["error"])
                dependencies = resource_urls(payload)
                with ThreadPoolExecutor(max_workers=6) as executor:
                    dependency_results = list(executor.map(
                        lambda url: check_url_health(url, timeout, network, kind="resource"), dependencies))
                for dependency in dependency_results:
                    if not dependency["reachable"]:
                        print(f"  配套文件 FAIL: {dependency['url']}")
                        all_ok = False
                sites_count = len(payload.get("sites", [])) if isinstance(payload, dict) else 0
                print(f"[{name}] {publish_path.name}: OK ({size_kb} KB, {sites_count} sites)")
            except Exception as e:
                print(f"[{name}] {publish_path.name}: 解析失败 ({e})")
                all_ok = False
        else:
            print(f"[{name}] {publish_path.name}: 缺失!")
            all_ok = False

    print()
    print("检查范围: 配置、镜像、配套文件与播放工具；实际搜索及播放需在电视上试播。")
    if all_ok:
        print("状态: 上述文件检查通过，尚未验证实际播放")
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
        report["profiles"][-1].update({"status": r.get("status", "updated"),
                                      "using_fallback": r.get("using_fallback", False),
                                      "error": r.get("error")})

    # 生成 Markdown 报告
    md_lines = []
    md_lines.append("# 🥚 蛋壳影院 - 同步报告")
    md_lines.append("")
    md_lines.append(f"**同步时间**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    md_lines.append("")
    md_lines.append("---")
    md_lines.append("")

    for r in results:
        profile = r["profile"]
        kept = r.get("sites_kept", 0)
        removed = r.get("sites_removed", 0)
        renamed = r.get("renamed")

        md_lines.append(f"## 📺 {profile.upper()} 配置")
        md_lines.append("")
        md_lines.append(f"- **来源**: {r['source']}")
        if r.get("error"):
            md_lines.append(f"- **状态**: 更新失败，保留原配置。原因: {r['error']}")
        elif r.get("using_fallback"):
            md_lines.append("- **状态**: 首选来源不完整，已使用整套候补来源")
        md_lines.append(f"- **保留站点**: {kept} 个 ✅")
        md_lines.append(f"- **移除站点**: {removed} 个 🗑️")

        if renamed:
            md_lines.append(f"- **重命名**: {renamed['from']} → {renamed['to']}")

        if removed > 0 and removed <= 20:
            md_lines.append("")
            md_lines.append("**移除的站点**:")
            for name in r.get('removed_sites', []):
                md_lines.append(f"- {name}")
        elif removed > 20:
            md_lines.append("")
            md_lines.append(f"**移除的站点** (共 {removed} 个):")
            for name in r.get('removed_sites', [])[:20]:
                md_lines.append(f"- {name}")
            md_lines.append(f"- ... 还有 {removed - 20} 个")

        md_lines.append("")
        md_lines.append("---")
        md_lines.append("")

    md_lines.append("*此报告由 eggtv-sync 自动生成*")

    report_path = repo_root / "sync_report.md"
    report_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"[report] 报告已生成: sync_report.md")
    save_json(repo_root / "sync_report.json", report)
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
    failed_profiles = []

    for name in target_profiles:
        upstream_override = args.upstream_url if len(target_profiles) == 1 else None
        profile_config = config["profiles"].get(name, {})
        try:
            result = sync_profile(
                repo_root, repo_config, name, profile_config,
                upstream_override=upstream_override, network=network, dry_run=dry_run,
            )
        except SyncError as exc:
            failed_profiles.append(name)
            print(f"[{name}] 更新失败，保留原配置: {exc}")
            sync_results.append({"profile": name, "source": profile_config.get("upstream_url", ""),
                                 "status": "kept_previous", "error": str(exc), "changed_files": []})
            continue
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
        return 1 if failed_profiles else 0

    # 生成同步报告
    report_path = generate_sync_report(sync_results, repo_root)

    if args.push:
        unique_files = sorted(set(changed_files), key=lambda p: str(p))
        unique_files.append(report_path)  # 报告也提交
        unique_files.append(repo_root / "sync_report.json")
        commit_message = args.commit_message or f"chore(sync): refresh {'/'.join(target_profiles)}"
        git_commit_and_push(repo_root, unique_files, commit_message)
        print("git push completed")

    # 控制台摘要（好看版）
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║              🥚 蛋壳影院 - 同步报告                     ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    for r in sync_results:
        profile = r['profile']
        kept = r.get('sites_kept', 0)
        removed = r.get('sites_removed', 0)
        renamed = r.get('renamed')

        # 图标
        emoji = "✅" if removed == 0 else "🔄"

        print(f"  📺 {profile.upper()} 配置")
        print(f"  ─────────────────────────────────────────")

        if renamed:
            print(f"  ✏️  重命名: {renamed['from']}")
            print(f"     → {renamed['to']}")

        print(f"  📊 站点统计:")
        print(f"     保留: {kept} 个 ✅")
        if removed > 0:
            print(f"     移除: {removed} 个 🗑️")

        if removed > 0 and removed <= 10:
            print(f"  🗑️  移除的站点:")
            for name in r.get('removed_sites', [])[:10]:
                print(f"     - {name}")
            if removed > 10:
                print(f"     ... 还有 {removed - 10} 个")

        print()

    print("  ─────────────────────────────────────────")
    print(f"  ⏰ 同步时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  📁 报告文件: sync_report.md")
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║                    同步处理完成                        ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    if failed_profiles:
        print("部分来源更新失败，原配置已保留: " + ", ".join(failed_profiles))
    return 1 if len(failed_profiles) == len(target_profiles) else 0


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
    relative_files = [str(p.relative_to(repo_root)) for p in files
                      if run_git(repo_root, ["check-ignore", "-q", "--", str(p.relative_to(repo_root))]).returncode != 0]
    if not relative_files:
        return
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

    speed_parser = subparsers.add_parser("speedtest", help="measure source and mirror response speed")
    speed_parser.add_argument("profiles", nargs="*", help="profile names (default: all)")
    speed_parser.add_argument("--samples", type=int, help="requests per target, 1 to 5")
    speed_parser.add_argument("--timeout", type=int, help="seconds per request, 5 to 30")
    speed_parser.add_argument("--workers", type=int, help="concurrent targets, 1 to 8")
    speed_parser.add_argument("--location", help="description of the network used")
    speed_parser.set_defaults(func=cmd_speedtest)

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
    # Windows 终端也应能显示站点名中的中文和符号。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
