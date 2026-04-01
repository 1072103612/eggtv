#!/usr/bin/env python3
"""Sync curated TVBox-style source files from upstream into this repo.

The workflow this script supports is:
1. Fetch the latest upstream JSON from a URL or a local seed file.
2. Save the raw upstream snapshot into the repo.
3. Derive the current curation rules from the previous upstream snapshot
   plus the current published file in the repo.
4. Apply those rules to the newly fetched upstream snapshot.
5. Rewrite the spider jar to a GitHub Raw URL with an md5 suffix.
6. Optionally commit and push the result.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


JsonValue = Any


class SyncError(RuntimeError):
    """Raised when a profile cannot be synced safely."""


URL_PATTERN = re.compile(r"https?://[^\s,'\"<>]+", re.IGNORECASE)


def load_json(path: Path) -> JsonValue:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, data: JsonValue) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    if previous == serialized:
        return False
    path.write_text(serialized, encoding="utf-8")
    return True


def is_http_url(value: str) -> bool:
    scheme = urllib.parse.urlparse(value).scheme
    return scheme in {"http", "https"}


def is_file_url(value: str) -> bool:
    return urllib.parse.urlparse(value).scheme == "file"


def normalize_source_url(source: str) -> str:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme not in {"http", "https"}:
        return source

    if parsed.netloc != "github.com":
        return source

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 5 and parts[2] == "blob":
        owner, repo, _, branch = parts[:4]
        remainder = "/".join(parts[4:])
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{remainder}"

    return source


def resolve_file_url(source: str) -> Path:
    parsed = urllib.parse.urlparse(source)
    return Path(urllib.parse.unquote(parsed.path)).expanduser()


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
    errors: List[str] = []
    for mode, proxy_url in build_fetch_attempts(network):
        command = [
            "curl",
            "-fsSL",
            "-A",
            "eggtv-sync/1.0 (+https://github.com/1072103612/eggtv)",
            "--retry",
            "2",
            "--retry-delay",
            "2",
            "--connect-timeout",
            str(min(timeout, 20)),
            "--max-time",
            str(timeout),
        ]
        if proxy_url:
            command.extend(["--proxy", proxy_url])
        command.append(source)
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode == 0:
            return result.stdout
        stderr = result.stderr.decode("utf-8", errors="replace").strip() or "curl failed"
        label = f"{mode}({proxy_url})" if proxy_url else mode
        errors.append(f"{label}: {stderr}")

    raise SyncError(f"failed to fetch {source}: {' | '.join(errors)}")


def read_bytes_from_source(source: str, timeout: int = 30, network: Optional[Dict[str, Any]] = None) -> bytes:
    source = normalize_source_url(source)
    if is_http_url(source):
        return read_http_bytes(source, timeout=timeout, network=network)
    if is_file_url(source):
        path = resolve_file_url(source)
        if not path.exists():
            raise SyncError(f"source does not exist: {path}")
        return path.read_bytes()

    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise SyncError(f"source does not exist: {path}")
    return path.read_bytes()


def load_json_from_source(source: str, timeout: int = 30, network: Optional[Dict[str, Any]] = None) -> JsonValue:
    payload = read_bytes_from_source(source, timeout=timeout, network=network)
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SyncError(f"invalid JSON from {source}: {exc}") from exc


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_valid_jar_bytes(data: bytes) -> bool:
    return data[:4] in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}


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
        capture_output=True,
        text=True,
        check=True,
    )
    branch = result.stdout.strip()
    return branch or "main"


def infer_github_repo(repo_root: Path) -> Optional[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
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
    explicit = repo_config.get("raw_base")
    if explicit:
        return explicit.rstrip("/")

    github_repo = repo_config.get("github_repo") or infer_github_repo(repo_root)
    if not github_repo:
        raise SyncError("cannot infer GitHub repo; set repo.github_repo or repo.raw_base")

    branch = repo_config.get("branch") or infer_default_branch(repo_root)
    return f"https://raw.githubusercontent.com/{github_repo}/{branch}"


def normalize_json_path(path: str) -> List[str]:
    return [segment for segment in path.split(".") if segment]


def get_in(data: JsonValue, path: str) -> JsonValue:
    current = data
    for segment in normalize_json_path(path):
        if not isinstance(current, dict) or segment not in current:
            raise KeyError(path)
        current = current[segment]
    return current


def set_in(data: JsonValue, path: str, value: JsonValue) -> None:
    segments = normalize_json_path(path)
    if not segments:
        raise SyncError("cannot set an empty path")

    current = data
    for segment in segments[:-1]:
        next_value = current.get(segment)
        if not isinstance(next_value, dict):
            next_value = {}
            current[segment] = next_value
        current = next_value
    current[segments[-1]] = value


def deep_patch(base: JsonValue, patch: JsonValue) -> JsonValue:
    if isinstance(base, dict) and isinstance(patch, dict):
        merged = copy.deepcopy(base)
        for key, value in patch.items():
            if key in merged:
                merged[key] = deep_patch(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged

    return copy.deepcopy(patch)


def deep_diff(base: JsonValue, target: JsonValue) -> Optional[JsonValue]:
    if base == target:
        return None

    if isinstance(base, dict) and isinstance(target, dict):
        diff: Dict[str, JsonValue] = {}
        keys = set(base) | set(target)
        for key in sorted(keys):
            if key not in target:
                continue
            if key not in base:
                diff[key] = copy.deepcopy(target[key])
                continue
            child = deep_diff(base[key], target[key])
            if child is not None:
                diff[key] = child
        return diff or None

    return copy.deepcopy(target)


def strip_spider_suffix(spider_value: str) -> str:
    return spider_value.split(";", 1)[0].strip()


def resolve_relative_reference(base_source: str, reference: str) -> str:
    parsed = urllib.parse.urlparse(reference)
    if parsed.scheme in {"http", "https", "file"}:
        return reference

    if base_source and (is_http_url(base_source) or is_file_url(base_source)):
        return urllib.parse.urljoin(base_source, reference)

    base_path = Path(base_source).expanduser()
    if not base_path.is_absolute():
        base_path = Path.cwd() / base_path
    return str((base_path.parent / reference).resolve())


def index_items(items: Iterable[Dict[str, Any]], key_field: str) -> Dict[str, Dict[str, Any]]:
    indexed: Dict[str, Dict[str, Any]] = {}
    for item in items:
        item_key = item.get(key_field)
        if item_key is None or item_key in indexed:
            continue
        indexed[item_key] = item
    return indexed


def group_items(items: Iterable[Dict[str, Any]], key_field: str) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        item_key = item.get(key_field)
        if item_key is None:
            continue
        grouped.setdefault(item_key, []).append(item)
    return grouped


def stringify_compact(value: JsonValue) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def collect_item_fields(item: Dict[str, Any], fields: Iterable[str]) -> Dict[str, str]:
    collected: Dict[str, str] = {}
    for field in fields:
        collected[field] = stringify_compact(item.get(field))
    return collected


def contains_any(haystack: str, needles: Iterable[str]) -> bool:
    lowered = haystack.casefold()
    for needle in needles:
        if needle.casefold() in lowered:
            return True
    return False


def item_matches_filter(item: Dict[str, Any], filter_config: Dict[str, Any]) -> bool:
    searchable_fields = filter_config.get("search_fields", ["key", "name", "api", "ext"])
    field_map = collect_item_fields(item, searchable_fields)
    combined = " ".join(value for value in field_map.values() if value).strip()

    retain_keywords = filter_config.get("retain_keywords", [])
    if retain_keywords and contains_any(combined, retain_keywords):
        return False

    drop_keywords = filter_config.get("drop_keywords", [])
    if drop_keywords and contains_any(combined, drop_keywords):
        return True

    drop_field_keywords = filter_config.get("drop_field_keywords", {})
    for field_name, keywords in drop_field_keywords.items():
        if contains_any(stringify_compact(item.get(field_name)), keywords):
            return True

    return False


def filter_items_by_rules(
    items: List[Dict[str, Any]],
    filter_config: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not filter_config:
        return copy.deepcopy(items)

    filtered: List[Dict[str, Any]] = []
    for item in items:
        if item_matches_filter(item, filter_config):
            continue
        filtered.append(copy.deepcopy(item))
    return filtered


def extract_urls_from_text(value: str) -> List[str]:
    return [match.rstrip(").") for match in URL_PATTERN.findall(value)]


def collect_probe_urls(value: JsonValue) -> List[str]:
    urls: List[str] = []
    if isinstance(value, str):
        urls.extend(extract_urls_from_text(value))
    elif isinstance(value, list):
        for item in value:
            urls.extend(collect_probe_urls(item))
    elif isinstance(value, dict):
        for item in value.values():
            urls.extend(collect_probe_urls(item))
    return urls


def unique_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for item in items:
        if item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result


def extract_site_probe_urls(site: Dict[str, Any], probe_config: Dict[str, Any]) -> List[str]:
    probe_fields = probe_config.get("probe_fields", ["api", "ext"])
    urls: List[str] = []
    for field in probe_fields:
        urls.extend(collect_probe_urls(site.get(field)))
    return unique_preserve_order(urls)[: int(probe_config.get("max_urls_per_site", 1))]


def probe_url(
    url: str,
    timeout: int,
    connect_timeout: int,
    network: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    errors: List[str] = []
    for mode, proxy_url in build_fetch_attempts(network):
        command = [
            "curl",
            "-fsSL",
            "-A",
            "Mozilla/5.0",
            "--connect-timeout",
            str(connect_timeout),
            "--max-time",
            str(timeout),
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}\t%{time_starttransfer}\t%{time_total}\t%{size_download}\t%{speed_download}",
        ]
        if proxy_url:
            command.extend(["--proxy", proxy_url])
        command.append(url)
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            http_code, ttfb, total, size_download, speed_download = result.stdout.strip().split("\t")
            return {
                "url": url,
                "http_code": int(http_code),
                "time_starttransfer_ms": float(ttfb) * 1000.0,
                "time_total_ms": float(total) * 1000.0,
                "size_download": float(size_download),
                "speed_download": float(speed_download),
                "mode": mode,
            }
        stderr = result.stderr.strip() or "curl failed"
        label = f"{mode}({proxy_url})" if proxy_url else mode
        errors.append(f"{label}: {stderr}")

    return None


def is_probe_slow(probe: Dict[str, Any], probe_config: Dict[str, Any]) -> bool:
    max_ttfb_ms = float(probe_config.get("max_time_starttransfer_ms", 4000))
    min_speed = float(probe_config.get("min_speed_bytes_per_sec", 12000))
    min_size = float(probe_config.get("min_size_for_speed_check", 4096))
    if probe["time_starttransfer_ms"] > max_ttfb_ms:
        return True
    if probe["size_download"] >= min_size and probe["speed_download"] < min_speed:
        return True
    return False


def apply_site_probes(
    sites: List[Dict[str, Any]],
    probe_config: Optional[Dict[str, Any]],
    network: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not probe_config or not probe_config.get("enabled"):
        return copy.deepcopy(sites)

    timeout = int(probe_config.get("timeout", 6))
    connect_timeout = int(probe_config.get("connect_timeout", 3))
    kept_sites: List[Dict[str, Any]] = []

    for site in sites:
        probe_urls = extract_site_probe_urls(site, probe_config)
        if not probe_urls:
            kept_sites.append(copy.deepcopy(site))
            continue

        best_probe: Optional[Dict[str, Any]] = None
        for url in probe_urls:
            probe = probe_url(url, timeout=timeout, connect_timeout=connect_timeout, network=network)
            if probe is None:
                continue
            if best_probe is None or probe["time_starttransfer_ms"] < best_probe["time_starttransfer_ms"]:
                best_probe = probe

        if best_probe is None:
            print(f"[probe] removed {site.get('name') or site.get('key')}: no reachable endpoint")
            continue

        if is_probe_slow(best_probe, probe_config):
            print(
                f"[probe] removed {site.get('name') or site.get('key')}: "
                f"slow endpoint {int(best_probe['time_starttransfer_ms'])}ms"
            )
            continue

        kept_sites.append(copy.deepcopy(site))

    return kept_sites


def make_occurrence_ref(item_key: str, occurrence_index: int) -> str:
    return f"{item_key}#{occurrence_index}"


def split_occurrence_ref(reference: str) -> Tuple[str, int]:
    item_key, _, index_text = reference.rpartition("#")
    if not item_key or not index_text.isdigit():
        raise SyncError(f"invalid occurrence reference: {reference}")
    return item_key, int(index_text)


def derive_array_rules(
    previous_upstream: JsonValue,
    previous_publish: JsonValue,
    array_name: str,
    key_field: str,
) -> Dict[str, Any]:
    previous_upstream_items = previous_upstream.get(array_name, []) if isinstance(previous_upstream, dict) else []
    previous_publish_items = previous_publish.get(array_name, []) if isinstance(previous_publish, dict) else []
    if not isinstance(previous_upstream_items, list) or not isinstance(previous_publish_items, list):
        return {"selected_refs": [], "patches": {}, "extra_items": {}}

    upstream_groups = group_items(previous_upstream_items, key_field)
    seen_counts: Dict[str, int] = {}
    selected_refs: List[str] = []
    patches: Dict[str, Any] = {}
    extra_items: Dict[str, Dict[str, Any]] = {}

    for item in previous_publish_items:
        if not isinstance(item, dict):
            continue
        item_key = item.get(key_field)
        if item_key is None:
            continue
        occurrence_index = seen_counts.get(item_key, 0)
        seen_counts[item_key] = occurrence_index + 1
        reference = make_occurrence_ref(item_key, occurrence_index)
        selected_refs.append(reference)

        upstream_items_for_key = upstream_groups.get(item_key, [])
        if occurrence_index >= len(upstream_items_for_key):
            extra_items[reference] = copy.deepcopy(item)
            continue
        upstream_item = upstream_items_for_key[occurrence_index]
        patch = deep_diff(upstream_item, item)
        if patch:
            patches[reference] = patch

    return {
        "selected_refs": selected_refs,
        "patches": patches,
        "extra_items": extra_items,
    }


def apply_array_rules(
    upstream_items: List[Dict[str, Any]],
    key_field: str,
    strategy: str,
    rules: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if strategy == "upstream_all":
        return copy.deepcopy(upstream_items)

    if not rules:
        return copy.deepcopy(upstream_items)

    upstream_groups = group_items(upstream_items, key_field)
    selected_items: List[Dict[str, Any]] = []
    selected_refs: List[str] = rules.get("selected_refs", [])
    patches: Dict[str, Any] = rules.get("patches", {})
    extra_items: Dict[str, Any] = rules.get("extra_items", {})

    for reference in selected_refs:
        item_key, occurrence_index = split_occurrence_ref(reference)
        upstream_matches = upstream_groups.get(item_key, [])
        if occurrence_index < len(upstream_matches):
            item = copy.deepcopy(upstream_matches[occurrence_index])
            patch = patches.get(reference)
            if patch:
                item = deep_patch(item, patch)
            selected_items.append(item)
            continue

        if reference in extra_items:
            selected_items.append(copy.deepcopy(extra_items[reference]))

    return selected_items


def derive_top_level_overrides(
    previous_upstream: Dict[str, Any],
    previous_publish: Dict[str, Any],
    excluded_keys: Iterable[str],
) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    excluded = set(excluded_keys)
    keys = set(previous_publish) | set(previous_upstream)
    for key in sorted(keys):
        if key in excluded:
            continue
        if key not in previous_publish:
            continue
        upstream_value = previous_upstream.get(key)
        publish_value = previous_publish.get(key)
        patch = deep_diff(upstream_value, publish_value)
        if patch is not None:
            overrides[key] = patch
    return overrides


def prepare_publish_payload(
    fetched_upstream: Dict[str, Any],
    previous_upstream: Optional[Dict[str, Any]],
    previous_publish: Optional[Dict[str, Any]],
    profile_config: Dict[str, Any],
) -> Dict[str, Any]:
    publish = copy.deepcopy(fetched_upstream)
    arrays = profile_config.get("arrays", {})
    explicit_filters = profile_config.get("explicit_filters", {})

    if previous_upstream is None or previous_publish is None:
        for array_name, array_config in arrays.items():
            strategy = array_config.get("strategy", "upstream_all")
            items = publish.get(array_name, [])
            if isinstance(items, list):
                items = filter_items_by_rules(items, explicit_filters.get(array_name))
                if strategy != "upstream_all":
                    publish[array_name] = items
                    continue
                publish[array_name] = apply_array_rules(
                    items,
                    array_config["item_key"],
                    strategy,
                    None,
                )
        publish = deep_patch(publish, profile_config.get("overrides", {}))
        return publish

    excluded_keys = set(arrays) | {"spider"}
    top_level_overrides = derive_top_level_overrides(previous_upstream, previous_publish, excluded_keys)
    if top_level_overrides:
        publish = deep_patch(publish, top_level_overrides)

    for array_name, array_config in arrays.items():
        upstream_items = publish.get(array_name, [])
        if not isinstance(upstream_items, list):
            continue
        upstream_items = filter_items_by_rules(upstream_items, explicit_filters.get(array_name))
        strategy = array_config.get("strategy", "upstream_all")
        rules = None
        if strategy == "published_selection":
            rules = derive_array_rules(
                previous_upstream,
                previous_publish,
                array_name,
                array_config["item_key"],
            )
        publish[array_name] = apply_array_rules(
            upstream_items,
            array_config["item_key"],
            strategy,
            rules,
        )
        publish[array_name] = filter_items_by_rules(
            publish[array_name],
            explicit_filters.get(array_name),
        )

    publish = deep_patch(publish, profile_config.get("overrides", {}))
    return publish


def update_spider_field(
    repo_root: Path,
    repo_config: Dict[str, Any],
    profile_config: Dict[str, Any],
    publish_payload: Dict[str, Any],
    upstream_source: str,
    network: Optional[Dict[str, Any]] = None,
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
    target_path = ensure_relative_to_repo(repo_root, spider_config["download_to"])
    target_path.parent.mkdir(parents=True, exist_ok=True)
    previous_content = target_path.read_bytes() if target_path.exists() else None
    new_content = read_bytes_from_source(resolved_source, timeout=spider_timeout, network=network)
    if not is_valid_jar_bytes(new_content):
        if previous_content is None:
            raise SyncError(f"invalid spider file from {resolved_source}: not a jar/zip payload")
        print(f"[spider] invalid payload from {resolved_source}, keeping existing {target_path.relative_to(repo_root)}")
        new_content = previous_content
    file_changed = previous_content != new_content
    if file_changed:
        target_path.write_bytes(new_content)

    digest = md5_file(target_path)
    raw_base = compute_raw_base(repo_root, repo_config)
    publish_path = spider_config.get("publish_path", spider_config["download_to"]).lstrip("/")
    publish_payload["spider"] = f"{raw_base}/{publish_path};md5;{digest}"
    return target_path if file_changed else None


def resolve_upstream_sources(repo_root: Path, profile_config: Dict[str, Any], cli_override: Optional[str]) -> List[str]:
    candidates: List[str] = []
    if cli_override:
        candidates.append(normalize_source_url(cli_override))
    else:
        primary = profile_config.get("upstream_url")
        if primary:
            candidates.append(normalize_source_url(primary))
        for fallback in profile_config.get("upstream_fallback_urls", []):
            candidates.append(normalize_source_url(fallback))

    if candidates:
        deduped: List[str] = []
        seen = set()
        for item in candidates:
            if item in seen:
                continue
            deduped.append(item)
            seen.add(item)
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
    errors: List[str] = []
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

    joined = "\n".join(errors)
    raise SyncError(joined or "no upstream source available")


def sync_profile(
    repo_root: Path,
    repo_config: Dict[str, Any],
    profile_name: str,
    profile_config: Dict[str, Any],
    upstream_override: Optional[str] = None,
    network: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    upstream_output = ensure_relative_to_repo(repo_root, profile_config["upstream_output"])
    publish_output = ensure_relative_to_repo(repo_root, profile_config["publish_output"])

    previous_upstream = load_json(upstream_output) if upstream_output.exists() else None
    previous_publish = load_json(publish_output) if publish_output.exists() else None
    if previous_upstream is not None and not isinstance(previous_upstream, dict):
        raise SyncError(f"{upstream_output} must contain a JSON object")
    if previous_publish is not None and not isinstance(previous_publish, dict):
        raise SyncError(f"{publish_output} must contain a JSON object")

    upstream_sources = resolve_upstream_sources(repo_root, profile_config, upstream_override)
    fetch_timeout = int(profile_config.get("fetch_timeout", 60))
    upstream_source, fetched_upstream = fetch_upstream_json(
        upstream_sources,
        timeout=fetch_timeout,
        network=network,
    )

    publish_payload = prepare_publish_payload(
        fetched_upstream,
        previous_upstream,
        previous_publish,
        profile_config,
    )
    if isinstance(publish_payload.get("sites"), list):
        publish_payload["sites"] = apply_site_probes(
            publish_payload["sites"],
            profile_config.get("site_probes"),
            network,
        )

    changed_files: List[Path] = []
    if save_json(upstream_output, fetched_upstream):
        changed_files.append(upstream_output)

    spider_file = update_spider_field(
        repo_root,
        repo_config,
        profile_config,
        publish_payload,
        upstream_source,
        network=network,
    )
    if spider_file is not None:
        changed_files.append(spider_file)

    if save_json(publish_output, publish_payload):
        changed_files.append(publish_output)

    return {
        "profile": profile_name,
        "source": upstream_source,
        "changed_files": changed_files,
    }


def reconcile_spider_fields(
    repo_root: Path,
    repo_config: Dict[str, Any],
    profiles: Dict[str, Any],
) -> List[Path]:
    raw_base = compute_raw_base(repo_root, repo_config)
    changed_files: List[Path] = []

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


def run_git(repo_root: Path, args: List[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def git_commit_and_push(repo_root: Path, files: List[Path], commit_message: str) -> None:
    if not files:
        return

    relative_files = [str(path.relative_to(repo_root)) for path in files]
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


def load_config(repo_root: Path, config_path: Path) -> Dict[str, Any]:
    config = load_json(config_path)
    if not isinstance(config, dict):
        raise SyncError("config file must contain a JSON object")
    config.setdefault("repo", {})
    config.setdefault("rule_sets", {})
    config.setdefault("profiles", {})
    if not isinstance(config["rule_sets"], dict):
        raise SyncError("config.rule_sets must be an object")
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


def resolve_profile_config(config: Dict[str, Any], profile_name: str) -> Dict[str, Any]:
    profile = copy.deepcopy(config["profiles"].get(profile_name))
    if profile is None:
        raise SyncError(f"unknown profile: {profile_name}")

    rule_set_name = profile.get("rule_set")
    if not rule_set_name:
        return profile

    rule_set = config["rule_sets"].get(rule_set_name)
    if rule_set is None:
        raise SyncError(f"profile {profile_name} references missing rule_set: {rule_set_name}")

    merged = copy.deepcopy(rule_set)
    merged.update(profile)
    if "arrays" in rule_set or "arrays" in profile:
        arrays = copy.deepcopy(rule_set.get("arrays", {}))
        arrays.update(profile.get("arrays", {}))
        merged["arrays"] = arrays
    if "overrides" in rule_set or "overrides" in profile:
        overrides = copy.deepcopy(rule_set.get("overrides", {}))
        overrides = deep_patch(overrides, profile.get("overrides", {}))
        merged["overrides"] = overrides
    if "explicit_filters" in rule_set or "explicit_filters" in profile:
        explicit_filters = copy.deepcopy(rule_set.get("explicit_filters", {}))
        explicit_filters = deep_patch(explicit_filters, profile.get("explicit_filters", {}))
        merged["explicit_filters"] = explicit_filters
    return merged


def cmd_list(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config_path = (repo_root / args.config).resolve()
    config = load_config(repo_root, config_path)
    network = resolve_network_config(config, args)
    proxy_text = network.get("proxy_url") if network.get("proxy_mode") != "off" else "(disabled)"
    print(f"proxy\t{network.get('proxy_mode', 'prefer')}\t{proxy_text}")
    for name in sorted(config["profiles"]):
        profile = resolve_profile_config(config, name)
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

    if args.profile:
        names = [args.profile]
    else:
        names = sorted(config["profiles"])

    print(f"- 代理模式: {network.get('proxy_mode', 'prefer')}")
    print(f"- 代理地址: {network.get('proxy_url') or '(未设置)'}")
    print()

    for name in names:
        profile = resolve_profile_config(config, name)
        print(f"[{name}] {profile.get('description', '')}".strip())
        print(f"- 上游链接: {profile.get('upstream_url') or '(未设置)'}")
        fallback_urls = profile.get("upstream_fallback_urls") or []
        if fallback_urls:
            print(f"- 候补链接: {', '.join(fallback_urls)}")
        print(f"- 上游留底: {profile.get('upstream_output')}")
        print(f"- 对外发布: {profile.get('publish_output')}")
        print(f"- 规则集: {profile.get('rule_set') or '内联规则'}")
        explicit_filters = profile.get("explicit_filters", {}).get("sites", {})
        if explicit_filters:
            retain_keywords = explicit_filters.get("retain_keywords", [])
            drop_keywords = explicit_filters.get("drop_keywords", [])
            if retain_keywords:
                print(f"- 站点显式保留关键词: {', '.join(retain_keywords)}")
            if drop_keywords:
                print(f"- 站点显式删除关键词: {', '.join(drop_keywords)}")
        site_probes = profile.get("site_probes", {})
        if site_probes.get("enabled"):
            print(
                "- 站点测速剔除: "
                f"开启, 超时 {site_probes.get('timeout', 6)}s, "
                f"首包阈值 {site_probes.get('max_time_starttransfer_ms', 4000)}ms"
            )
        print("- 当前清洗规则:")
        print("  1. 抓取上游原始 JSON，保存为留底文件")
        print("  2. `sites` 先按显式关键词规则过滤，再按当前发布文件的保留项、顺序和改名规则生成")
        print("  3. 最终发布结果会再执行一次显式规则过滤，避免旧条目被继承回来")
        print("  4. 对最终保留的 `sites` 做可达性和基础速度探测，剔除失效或明显过慢的源")
        print("  5. `lives` 直接跟随上游最新内容")
        print("  6. 其他顶层字段默认沿用你当前发布文件里已经形成的人工改动")
        print("  7. `spider.jar` 下载到仓库，再改写成 GitHub Raw 地址和最新 MD5")
        print("  8. 如果上游返回的不是有效 jar，而是网页或报错页，就保留仓库里现有的 jar")
        print("  9. 因为主/副配置共用同一个 `jar/spider.jar`，两个文件的 spider MD5 会自动保持一致")
        print()
    return 0


def collect_target_profiles(config: Dict[str, Any], args: argparse.Namespace) -> List[str]:
    if args.all:
        return sorted(config["profiles"])
    if args.profiles:
        missing = [name for name in args.profiles if name not in config["profiles"]]
        if missing:
            raise SyncError(f"unknown profile(s): {', '.join(missing)}")
        return args.profiles
    raise SyncError("select at least one profile or pass --all")


def cmd_sync(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config_path = (repo_root / args.config).resolve()
    config = load_config(repo_root, config_path)
    repo_config = config["repo"]
    network = resolve_network_config(config, args)
    target_profiles = collect_target_profiles(config, args)
    changed_files: List[Path] = []

    for name in target_profiles:
        upstream_override = args.upstream_url if len(target_profiles) == 1 else None
        profile_config = resolve_profile_config(config, name)
        result = sync_profile(
            repo_root,
            repo_config,
            name,
            profile_config,
            upstream_override=upstream_override,
            network=network,
        )
        changed_files.extend(result["changed_files"])
        changed_summary = ", ".join(str(path.relative_to(repo_root)) for path in result["changed_files"]) or "no file changes"
        print(f"[{name}] {result['source']} -> {changed_summary}")

    resolved_profiles = {name: resolve_profile_config(config, name) for name in config["profiles"]}
    changed_files.extend(reconcile_spider_fields(repo_root, repo_config, resolved_profiles))

    if args.push:
        unique_files = sorted(set(changed_files), key=lambda path: str(path))
        commit_message = args.commit_message or f"chore(sync): refresh {'/'.join(target_profiles)}"
        git_commit_and_push(repo_root, unique_files, commit_message)
        print("git push completed")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync curated upstream TVBox source files")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="repository root containing eggtv_sync.json (default: current directory)",
    )
    parser.add_argument(
        "--config",
        default="eggtv_sync.json",
        help="path to the config file relative to repo root",
    )
    parser.add_argument(
        "--proxy",
        help="proxy URL, for example http://127.0.0.1:7890",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="disable proxy for this run",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list configured profiles")
    list_parser.set_defaults(func=cmd_list)

    set_url_parser = subparsers.add_parser("set-url", help="set the upstream URL for a profile")
    set_url_parser.add_argument("profile", help="profile name")
    set_url_parser.add_argument("url", help="upstream JSON URL")
    set_url_parser.set_defaults(func=cmd_set_url)

    show_rules_parser = subparsers.add_parser("show-rules", help="show the current cleaning rules")
    show_rules_parser.add_argument("profile", nargs="?", help="optional profile name")
    show_rules_parser.set_defaults(func=cmd_show_rules)

    sync_parser = subparsers.add_parser("sync", help="sync one or more profiles")
    sync_parser.add_argument("profiles", nargs="*", help="profile names to sync")
    sync_parser.add_argument("--all", action="store_true", help="sync every configured profile")
    sync_parser.add_argument(
        "--upstream-url",
        help="override the upstream URL for a single sync run",
    )
    sync_parser.add_argument(
        "--push",
        action="store_true",
        help="commit and push changed files after syncing",
    )
    sync_parser.add_argument(
        "--commit-message",
        help="git commit message used with --push",
    )
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
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or str(exc), file=sys.stderr)
        return exc.returncode or 1


if __name__ == "__main__":
    sys.exit(main())
