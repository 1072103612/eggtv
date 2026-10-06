"""Local, manual-invocation playback experiment. Never edits published configs."""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="雷电影视播放验证（不会删除片源）")
    p.add_argument("--adb", default=r"C:\leidian\LDPlayer14\adb.exe")
    p.add_argument("--serial", default="emulator-5554")
    p.add_argument("--action", choices=["inspect", "back", "tap", "observe", "trial"], default="inspect")
    p.add_argument("--target", help="屏幕上已存在的文字或控件完整编号")
    p.add_argument("--seconds", type=int, default=30)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    if not 1 <= args.seconds <= 300:
        p.error("观察时长必须在 1 到 300 秒之间")
    tapped_at = None
    expected_source = None
    observed_source = None

    def adb(*items):
        r = subprocess.run([args.adb, "-s", args.serial, *items], capture_output=True,
                           timeout=20, check=True)
        return r.stdout.decode("utf-8", errors="replace")

    def nodes():
        adb("shell", "uiautomator", "dump", "/sdcard/eggtv-window.xml")
        root = ET.fromstring(adb("shell", "cat", "/sdcard/eggtv-window.xml"))
        return list(root.iter("node"))

    if args.action == "back":
        adb("shell", "input", "keyevent", "4")
        time.sleep(1)
    elif args.action in ("tap", "trial"):
        screen = nodes()
        if args.action == "trial" and not any(n.get("resource-id") == "com.fongmi.android.tv:id/title" for n in screen):
            raise SystemExit("自动试播须从影片列表页面开始；没有点击")
        if args.action == "trial":
            expected_source = next(n.get("text") for n in screen if n.get("resource-id") == "com.fongmi.android.tv:id/title")
        matches = [n for n in screen if args.target and args.target in
                   (n.get("text"), n.get("resource-id"), n.get("content-desc"))]
        if len(matches) != 1:
            raise SystemExit(f"目标必须唯一，找到 {len(matches)} 个；没有点击")
        n = matches[0]
        if n.get("package") != "com.fongmi.android.tv":
            raise SystemExit("目标不是影视应用；没有点击")
        bounds = list(map(int, re.findall(r"\d+", n.get("bounds", ""))))
        if len(bounds) != 4:
            raise SystemExit("无法定位控件")
        if args.action == "trial" and n.get("resource-id") != "com.fongmi.android.tv:id/name":
            raise SystemExit("自动试播目标必须是影片名称；没有点击")
        tapped_at = time.monotonic()
        adb("shell", "input", "tap", str((bounds[0]+bounds[2])//2), str((bounds[1]+bounds[3])//2))
        if args.action == "tap":
            time.sleep(1)
    if args.action in ("observe", "trial"):
        adb("forward", "tcp:19878", "tcp:9978")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        started = tapped_at or time.monotonic()
        samples = []
        try:
            while time.monotonic() - started < args.seconds:
                stamp = round(time.monotonic() - started, 2)
                try:
                    with opener.open("http://127.0.0.1:19878/media", timeout=3) as response:
                        media = json.load(response)
                    # Do not persist expiring signed playback URLs or private headers.
                    samples.append({"elapsed_seconds": stamp, **{k: media.get(k) for k in
                                    ("state", "position", "duration", "title")}})
                except Exception as exc:
                    samples.append({"elapsed_seconds": stamp, "error": type(exc).__name__})
                time.sleep(1)
        finally:
            adb("forward", "--remove", "tcp:19878")
            if args.action == "trial":
                try:
                    detail_nodes = nodes()
                    observed_source = next((n.get("text", "").removeprefix("站源：") for n in detail_nodes
                                           if n.get("resource-id") == "com.fongmi.android.tv:id/site"), None)
                finally:
                    adb("shell", "input", "keyevent", "4")
        playing = [s for s in samples if s.get("state") == 3
                   and (args.action != "trial" or s.get("title") == args.target)
                   and isinstance(s.get("position"), (int, float))]
        advancing = any(b["position"] > a["position"] for a, b in zip(playing, playing[1:]))
        first_progress = next((b["elapsed_seconds"] for a, b in zip(playing, playing[1:]) if b["position"] > a["position"]), None)
        report = {"note": "单次播放验证；不是片源整体评分。依据播放器状态和进度，不证明每一帧画面正常。",
                  "target": args.target if args.action == "trial" else None,
                  "expected_source": expected_source,
                  "observed_source": observed_source,
                  "source_verified": observed_source == expected_source if expected_source else None,
                  "first_progress_after_click_seconds": first_progress if tapped_at else None,
                  "observed_seconds": round(time.monotonic()-started, 2),
                  "playback_progress_verified": advancing,
                  "buffering_samples": sum(s.get("state") == 6 for s in samples),
                  "buffering_samples_after_first_progress": sum(s.get("state") == 6 and s["elapsed_seconds"] >= first_progress for s in samples) if first_progress is not None else None,
                  "read_error_samples": sum("error" in s for s in samples),
                  "samples": samples}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "samples"}, ensure_ascii=False, indent=2))
        return
    for n in nodes():
        if n.get("text") or n.get("content-desc") or n.get("clickable") == "true":
            print(json.dumps({k: n.get(k) for k in ("text", "resource-id", "content-desc", "bounds", "clickable")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
