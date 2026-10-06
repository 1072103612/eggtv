"""Conservative, deterministic playback decisions shared by local and cloud runs."""
import hashlib
import json


def signature(site):
    fields = {k: site.get(k) for k in ("key", "type", "api", "ext")}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def verdict(trial, limits):
    if not trial.get("identity_verified") or trial.get("tool_error"):
        return "unknown"
    if trial.get("read_error_ratio", 1) > .1:
        return "unknown"
    if trial.get("startup_timeout"):
        return "bad"
    startup = trial.get("startup_seconds")
    if startup is None:
        return "unknown"
    if startup > limits["startup_timeout_seconds"]:
        return "bad"
    if not trial.get("seek_verified"):
        return "unknown"
    if trial.get("seek_timeout"):
        return "bad"
    seek = trial.get("seek_seconds")
    if seek is None:
        return "unknown"
    if seek > limits["seek_timeout_seconds"]:
        return "bad"
    return "good"


def round_verdict(trials, limits):
    results = [verdict(t, limits) for t in trials]
    if len(results) < limits["films_per_source"]:
        return "unknown"
    if results.count("bad") >= 2:
        return "bad"
    if results.count("good") == limits["films_per_source"]:
        return "good"
    return "unknown"


def decide(first, retest, prior):
    if first == "bad" and retest == "bad":
        return "unhealthy"
    if first == "good" and (prior != "unhealthy" or retest == "good"):
        return "healthy"
    return prior or "unknown"


def widespread_failure(results):
    known = [r for r in results if r.get("first_verdict") in ("good", "bad")]
    return len(known) >= 3 and sum(r["first_verdict"] == "bad" for r in known) / len(known) >= .6


def apply_health(sites, state):
    kept, removed = [], []
    for site in sites:
        entry = state.get(site.get("key"), {})
        if entry.get("status") == "unhealthy" and entry.get("signature") == signature(site):
            removed.append(site)
        else:
            kept.append(site)
    return kept, removed


def is_healthy(site, state):
    entry = state.get(site.get("key"), {})
    return entry.get("status") == "healthy" and entry.get("signature") == signature(site)
