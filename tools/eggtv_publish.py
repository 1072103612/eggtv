"""Publish generated files with bounded retries and without overwriting concurrent edits."""
import argparse
import subprocess
import time
from pathlib import Path


def run_git(root, args):
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True,
                          check=False, timeout=120)


def push_with_retry(root, branch="main", attempts=4, run=None, sleep=time.sleep):
    run = run or (lambda args: run_git(root, args))
    head = run(["rev-parse", "HEAD"])
    if head.returncode:
        raise RuntimeError(head.stderr.strip() or "Cannot determine local commit")
    head_sha = head.stdout.strip()
    baseline = set()
    for ref in (f"refs/remotes/origin/{branch}", "HEAD^"):
        result = run(["rev-parse", ref])
        if not result.returncode:
            baseline.add(result.stdout.strip())
    for attempt in range(attempts):
        try:
            result = run(["push", "origin", f"HEAD:refs/heads/{branch}"])
        except subprocess.TimeoutExpired:
            result = subprocess.CompletedProcess("push", 1, "", "git push timed out")
        if not result.returncode:
            return
        error = (result.stdout + "\n" + result.stderr).strip()
        # A lost response can follow a successful server-side push.
        try:
            remote = run(["ls-remote", "origin", f"refs/heads/{branch}"])
        except subprocess.TimeoutExpired:
            remote = subprocess.CompletedProcess("ls-remote", 1, "", "timed out")
        if not remote.returncode and remote.stdout.strip():
            remote_sha = remote.stdout.split()[0]
            if remote_sha == head_sha:
                return
            if remote_sha not in baseline:
                raise RuntimeError("远端分支已有其他新提交，请从新版本重跑同步；没有覆盖远端。\n" + error)
        lowered = error.lower()
        permanent = ("permission denied", "authentication failed", "403", "non-fast-forward",
                     "fetch first", "protected branch", "repository not found")
        transient = ("internal server error", "500", "502", "503", "504", "rpc failed",
                     "connection reset", "timed out", "timeout", "could not resolve", "remote end hung up")
        if (attempt + 1 == attempts or any(x in lowered for x in permanent)
                or not any(x in lowered for x in transient)):
            raise RuntimeError(error or "git push failed")
        delay = min(2 ** (attempt + 1), 20)
        print(f"GitHub 推送临时失败，{delay} 秒后重试（{attempt + 2}/{attempts}）", flush=True)
        sleep(delay)


def publish_changes(root, message, branch="main"):
    result = run_git(root, ["add", "-A"])
    if result.returncode:
        raise RuntimeError(result.stderr)
    result = run_git(root, ["diff", "--cached", "--quiet"])
    if not result.returncode:
        print("No changes to commit")
        return
    if result.returncode != 1:
        raise RuntimeError(result.stderr)
    result = run_git(root, ["commit", "-m", message])
    if result.returncode:
        raise RuntimeError(result.stderr)
    push_with_retry(root, branch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--message", default="chore(sync): refresh sources")
    args = parser.parse_args()
    try:
        publish_changes(Path(args.repo_root).resolve(), args.message, args.branch)
    except (RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
