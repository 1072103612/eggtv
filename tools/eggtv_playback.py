"""Weekly FongMi playback monitor. Standard library only; no AI runtime."""
import argparse
import copy
import datetime as dt
import html
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from .playback_android import Android, DetectionError, PREFIX
    from .playback_policy import signature, round_verdict, decide, widespread_failure
except ImportError:
    from playback_android import Android, DetectionError, PREFIX
    from playback_policy import signature, round_verdict, decide, widespread_failure

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".playback"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_json(url):
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with OPENER.open(req, timeout=30) as response:
        return json.load(response)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class ConfigServer:
    def __init__(self, android, payload):
        self.android, self.payload = android, payload
        self.content = b"{}"
        self.path = "/none"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != owner.path:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(owner.content)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        self.android.call("reverse", "tcp:" + str(self.port), "tcp:" + str(self.port))

    def select(self, site):
        marker = "检测·" + site["key"]
        data = copy.deepcopy(self.payload)
        candidate = copy.deepcopy(site)
        candidate.update(name=marker, changeable=0)
        data["sites"] = [candidate]
        data.pop("lives", None)
        self.path = "/" + signature(site)[:16] + ".json"
        self.content = json.dumps(data, ensure_ascii=False).encode()
        self.android.load_config(f"http://127.0.0.1:{self.port}{self.path}")
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            nodes = self.android.screen()
            title = self.android.find(nodes, field="title")
            if title is not None and title.get("text") == marker:
                return marker
            time.sleep(1)
        raise DetectionError("独立片源配置没有加载成功")

    def close(self):
        try:
            self.android.call("reverse", "--remove", "tcp:" + str(self.port))
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)


def movie_list(android, count):
    nodes = android.home()
    deadline = time.monotonic() + 25
    category = None
    while time.monotonic() < deadline:
        category = next((android.find(nodes, text=name) for name in ("电影", "电影片", "电影频道")
                         if android.find(nodes, text=name) is not None), None)
        if category is not None:
            break
        time.sleep(1)
        nodes = android.screen()
    if category is None:
        raise DetectionError("片源没有可辨认的电影分类")
    android.tap(category)
    films = []
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        nodes = android.screen()
        for n in nodes:
            if n.get("resource-id") == PREFIX + "name" and n.get("text") and n.get("text") not in films:
                films.append(n.get("text"))
        if len(films) >= count:
            return films[:count]
        time.sleep(1)
    raise DetectionError("没有取得足够的不同电影样本")


def sample(android, marker, movie, limits):
    android.home()
    nodes = android.screen()
    category = next((android.find(nodes, text=name) for name in ("电影", "电影片", "电影频道")
                     if android.find(nodes, text=name) is not None), None)
    if category is None:
        raise DetectionError("电影分类发生变化")
    android.tap(category)
    time.sleep(1)
    nodes = android.screen()
    target = android.find(nodes, text=movie, field="name")
    if target is None:
        raise DetectionError("电影列表已变化，无法确认样本")
    begun = time.monotonic()
    android.tap(target)
    timeout = limits["startup_timeout_seconds"]
    started = None
    previous_position = None
    samples = []
    identity = False
    seek_verified = False
    seek_seconds = None
    seek_timeout = False
    tool_error = None
    duration = None
    last = {}
    last_progress = None
    try:
        # The first window is fixed at eight seconds, including initial loading.
        while time.monotonic() - begun < timeout:
            if (LOCAL / "stop.flag").exists():
                raise DetectionError("用户停止了检测")
            try:
                media = android.request("media", _timeout=max(.05, min(.75, timeout-(time.monotonic()-begun))))
                elapsed = time.monotonic() - begun
                entry = {"stage": "startup", "seconds": round(elapsed, 2), **{k: media.get(k) for k in ("state", "position", "title")}}
                position = media.get("position")
                same = media.get("title") == movie
                moving = same and media.get("state") == 3 and isinstance(position, (float, int)) and previous_position is not None and position > previous_position
                if started is None and moving:
                    started = elapsed
                if moving:
                    last_progress = elapsed
                if same and isinstance(position, (float, int)):
                    previous_position = position
                    duration = media.get("duration")
                    last = media
                samples.append(entry)
            except Exception as exc:
                samples.append({"stage": "startup", "seconds": round(time.monotonic()-begun, 2), "error": type(exc).__name__})
            time.sleep(min(.25, max(0, timeout-(time.monotonic()-begun))))
        nodes = android.screen()
        source = android.find(nodes, field="site")
        identity = source is not None and source.get("text") == "站源：" + marker
        startup_timeout = started is None or started > timeout or last.get("state") in (1, 6)
        if last.get("state") == 3 and last_progress is not None and timeout-last_progress > 1:
            startup_timeout = True
        if not last:
            tool_error = "未取得这部影片的播放状态，无法区分工具异常和片源缓冲"
        if not startup_timeout and last.get("state") != 3:
            tool_error = "播放被暂停或中断，无法归因到片源"
        if not startup_timeout and not tool_error and identity:
            if not isinstance(duration, (int, float)) or duration <= 0:
                tool_error = "无法取得影片时长，不能验证拖到一半"
            else:
                seek_begin = android.seek_middle()
                target_ms = duration / 2
                tolerance = max(3000, duration * .01)
                previous_position = None
                seek_limit = limits["seek_timeout_seconds"]
                while time.monotonic()-seek_begin <= seek_limit:
                    if (LOCAL / "stop.flag").exists():
                        raise DetectionError("用户停止了检测")
                    try:
                        media = android.request("media", _timeout=max(.05, min(.75, seek_limit-(time.monotonic()-seek_begin))))
                        elapsed = time.monotonic()-seek_begin
                        position = media.get("position")
                        near = media.get("title") == movie and isinstance(position, (int, float)) and abs(position-target_ms) <= tolerance
                        seek_verified = seek_verified or near
                        samples.append({"stage": "seek", "seconds": round(elapsed, 2), **{k: media.get(k) for k in ("state", "position", "title")}})
                        if near and media.get("state") == 3 and previous_position is not None and position > previous_position and elapsed <= seek_limit:
                            seek_seconds = round(elapsed, 2)
                            break
                        previous_position = position if near else None
                    except Exception as exc:
                        samples.append({"stage": "seek", "seconds": round(time.monotonic()-seek_begin, 2), "error": type(exc).__name__})
                    time.sleep(min(.25, max(0, seek_limit-(time.monotonic()-seek_begin))))
                seek_timeout = seek_seconds is None and seek_verified
                if seek_timeout and media.get("state") == 2:
                    tool_error = "跳转后播放被暂停，无法归因到片源"
                if not seek_verified:
                    tool_error = "无法确认进度已跳到影片一半，不能把拖动失败算作片源失败"
    finally:
        android.home()
    return {"movie": movie, "identity_verified": identity,
            "startup_seconds": round(started, 2) if started is not None else None,
            "startup_timeout": startup_timeout,
            "seek_verified": seek_verified,
            "seek_seconds": seek_seconds,
            "seek_timeout": seek_timeout,
            "tool_error": tool_error,
            "read_error_ratio": sum("error" in s for s in samples) / max(1, len(samples)),
            "samples": samples}


def test_round(android, marker, limits):
    films = movie_list(android, limits["films_per_source"])
    trials = []
    for film in films:
        if (LOCAL / "stop.flag").exists():
            raise DetectionError("用户停止了检测")
        print("    试播：" + film, flush=True)
        try:
            trials.append(sample(android, marker, film, limits))
        except (DetectionError, OSError, ValueError, subprocess.SubprocessError) as exc:
            trials.append({"movie": film, "tool_error": str(exc)[:180]})
    return trials


def github(method, path, data=None):
    # Reuse local Git credential manager; do not store or print credentials.
    cred = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                          capture_output=True, text=True, timeout=30, check=True)
    values = dict(line.split("=", 1) for line in cred.stdout.splitlines() if "=" in line)
    token = values.get("password")
    if not token:
        raise DetectionError("没有可用的 GitHub 登录凭据")
    url = "https://api.github.com/repos/1072103612/eggtv/" + path
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "User-Agent": "eggtv-playback"}
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with OPENER.open(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def publish(state):
    import base64
    import urllib.error
    for attempt in range(3):
        current = github("GET", "contents/playback_health.json?ref=main")
        remote = json.loads(base64.b64decode(current["content"]))
        before = copy.deepcopy(remote)
        # Preserve other profiles and concurrent entries we did not test.
        for profile, entries in state.get("profiles", {}).items():
            target = remote.setdefault("profiles", {}).setdefault(profile, {})
            for key, value in entries.items():
                if value.get("checked_at", "") >= target.get(key, {}).get("checked_at", ""):
                    target[key] = value
        if remote == before:
            return False
        body = {"message": "chore(playback): update cinema playback health", "branch": "main",
                "sha": current["sha"], "content": base64.b64encode(json.dumps(remote, ensure_ascii=False, indent=2).encode()).decode()}
        try:
            github("PUT", "contents/playback_health.json", body)
            github("POST", "actions/workflows/sync-sources.yml/dispatches", {"ref": "main"})
            return True
        except urllib.error.HTTPError as exc:
            if exc.code not in (409, 422) or attempt == 2:
                raise


def write_report(report):
    atomic_json(LOCAL / "latest.json", report)
    rows = []
    labels = {"healthy": "正常", "unhealthy": "确认差源", "unknown": "未能判断"}
    rounds = {"good": "通过", "bad": "表现差", "unknown": "无法判断", None: "无需复测"}
    for item in report["sources"]:
        details = item.get("error") or f"首轮：{rounds.get(item.get('first_verdict'))}，复测：{rounds.get(item.get('retest_verdict'))}"
        for trial in item.get("trials", []):
            startup = trial.get("startup_seconds")
            seek = trial.get("seek_seconds")
            details += f"；{trial.get('movie', '')}：开播{startup if startup is not None else '8秒内未成功'}秒，跳到一半后恢复{seek if seek is not None else '未通过'}秒"
            if trial.get("tool_error"):
                details += "（" + trial["tool_error"] + "）"
            elif trial.get("startup_timeout"):
                details += "（第8秒未正常播放）"
            elif trial.get("seek_timeout"):
                details += "（跳转后等待超过10秒）"
        rows.append("<tr><td>" + html.escape(item["name"]) + "</td><td>" + labels.get(item.get("status"), "保持原状态") + "</td><td>" + html.escape(details) + "</td></tr>")
    page = """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>蛋壳影院播放检测</title><style>body{font:18px system-ui;max-width:1000px;margin:40px auto;padding:16px;background:#f4f6f8;color:#18202a}table{width:100%;border-collapse:collapse;background:white}td,th{padding:14px;text-align:left;border-bottom:1px solid #ddd}h1{font-size:28px}p{line-height:1.7}</style>
<h1>蛋壳影院播放检测</h1>"""
    page += "<p>检测时间：" + html.escape(report["time"]) + "<br>" + html.escape(report["message"]) + "</p>"
    page += "<p>结果依据雷电中影视的播放状态和进度。起播时间为确认进度推进的时间，不是精确首帧。不能保证电视画面、声音或一周内所有时段的表现。</p>"
    page += "<table><tr><th>片源</th><th>结果</th><th>说明</th></tr>" + "".join(rows) + "</table>"
    (LOCAL / "report.html").write_text(page, encoding="utf-8")


def run(args):
    limits = json.loads((ROOT / "playback_settings.json").read_text(encoding="utf-8"))
    LOCAL.mkdir(exist_ok=True)
    lock = LOCAL / "running.lock"
    lock_handle = lock.open("a+b")
    lock_handle.seek(0)
    if lock_handle.read(1) == b"":
        lock_handle.write(b"0")
        lock_handle.flush()
    lock_handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_handle.close()
        raise DetectionError("已有检测在运行，请等待完成或在工具中停止")
    (LOCAL / "stop.flag").unlink(missing_ok=True)
    android = Android(limits["adb"], limits.get("serial"))
    server = None
    original_url = limits["restore_url"]
    report = {"time": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(), "sources": [], "message": "检测中"}
    state_path = LOCAL / "health.json"
    try:
        try:
            android.connect()
        except DetectionError:
            if not args.start_emulator:
                raise
            subprocess.run([limits["emulator_console"], "launch", "--index", str(limits["emulator_index"])], timeout=30, check=True, capture_output=True)
            for _ in range(30):
                time.sleep(2)
                try:
                    android.connect()
                    break
                except DetectionError:
                    continue
            else:
                raise DetectionError("模拟器启动超时")
        android.home()
        android.click(field="setting")
        old = android.find(android.screen(), field="vodUrl")
        if old is not None and old.get("text", "").startswith(("http://", "https://")):
            original_url = old.get("text")
        android.home()
        try:
            payload = get_json(limits["candidate_url"])
        except Exception:
            candidate = ROOT / "playback_candidates_tvbox.json"
            if candidate.exists():
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            else:
                raise DetectionError("完整检测候选配置尚未发布，无法可靠检测已删除源")
        sites = payload.get("sites", [])
        if not sites:
            raise DetectionError("候选配置没有片源")
        if args.sources:
            sites = [s for s in sites if s.get("key") in args.sources.split(",")]
        if not sites:
            raise DetectionError("未找到指定片源")
        try:
            state = get_json("https://1072103612.github.io/eggtv/playback_health.json")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            state = json.loads((ROOT / "playback_health.json").read_text(encoding="utf-8"))
        # The latest remote state takes precedence over a cached local run.
        profile = state.setdefault("profiles", {}).setdefault(limits["profile"], {})
        server = ConfigServer(android, payload)
        server.start()
        deadline = time.monotonic() + limits["max_run_hours"] * 3600
        for index, site in enumerate(sites):
            if (LOCAL / "stop.flag").exists():
                raise DetectionError("用户停止了检测")
            if time.monotonic() >= deadline:
                report["message"] = "达到检测时长上限；未检测源保持原状态"
                break
            print(f"[{index+1}/{len(sites)}] 检测 {site['name']}", flush=True)
            result = {"key": site["key"], "name": site["name"], "signature": signature(site)}
            prior_entry = profile.get(site["key"], {})
            prior = prior_entry.get("status") if prior_entry.get("signature") == signature(site) else None
            try:
                marker = server.select(site)
                trials = test_round(android, marker, limits)
                first = round_verdict(trials, limits)
                result.update(trials=trials, first_verdict=first)
                retest = None
                if first == "bad" or (prior == "unhealthy" and first == "good"):
                    marker = server.select(site)
                    again = test_round(android, marker, limits)
                    retest = round_verdict(again, limits)
                    result.update(retest_trials=again, retest_verdict=retest)
                result["status"] = decide(first, retest, prior)
            except Exception as exc:
                result.update(error=str(exc)[:200], status=prior or "unknown", first_verdict="unknown")
            report["sources"].append(result)
            write_report(report)
        if (LOCAL / "stop.flag").exists():
            raise DetectionError("用户停止了检测")
        if widespread_failure(report["sources"]):
            report["message"] = "多个片源同时异常，可能是影院网络或运行环境问题；本轮不更新菜单"
        else:
            for result in report["sources"]:
                if result.get("first_verdict") == "unknown":
                    continue
                profile[result["key"]] = {"signature": result["signature"], "status": result["status"], "checked_at": report["time"]}
            atomic_json(state_path, state)
            if args.publish and limits["publish_health"]:
                changed = publish(state)
                report["message"] = "健康记录已提交，正在由云端更新影院菜单" if changed else "检测完成，没有可更新的健康记录；影院菜单保持原样"
            else:
                report["message"] = "检测完成；结果仅保存在本机，未更新影院菜单"
        write_report(report)
    except Exception as exc:
        report["message"] = "检测中止，保留现有菜单：" + str(exc)[:200]
        write_report(report)
        raise
    finally:
        if android.port:
            try:
                android.request("action", do="control", type="stop")
            except Exception:
                pass
            try:
                android.load_config(original_url, "影院原配置")
            except Exception as exc:
                report["message"] += "；原配置恢复失败，请在影视设置中重新选择影院配置"
                write_report(report)
                print("恢复提示：" + str(exc)[:150], flush=True)
            if server:
                server.close()
            android.close()
        lock_handle.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="蛋壳影院自动播放检测")
    p.add_argument("--sources", help="仅检测指定片源key，逗号分隔")
    p.add_argument("--publish", action="store_true", help="提交健康记录（还需设置启用）")
    p.add_argument("--start-emulator", action="store_true")
    args = p.parse_args()
    try:
        run(args)
    except Exception as exc:
        print("检测未完成：" + str(exc)[:200], flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
