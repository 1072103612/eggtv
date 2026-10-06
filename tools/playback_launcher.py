"""Small local window and quiet scheduled launcher; no extra dependencies."""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".playback"


def start(sources=None, publish=False):
    DATA.mkdir(exist_ok=True)
    python = str(Path(sys.executable).with_name("python.exe"))
    cmd = [python, str(ROOT / "tools" / "eggtv_playback.py"), "--start-emulator"]
    if sources:
        cmd += ["--sources", sources]
    if publish:
        cmd.append("--publish")
    filename = DATA / ("run-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".log")
    with filename.open("w", encoding="utf-8") as stream:
        return subprocess.Popen(cmd, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def gui():
    import tkinter as tk
    from tkinter import messagebox
    import webbrowser
    window = tk.Tk()
    window.title("蛋壳影院自动测速")
    window.geometry("560x390")
    window.resizable(False, False)
    status = tk.StringVar(value="就绪。每周一凌晨4点自动检测。")
    process = None

    def launch(sources=None, publish=False):
        nonlocal process
        if process and process.poll() is None:
            messagebox.showinfo("检测进行中", "请等待当前检测完成。")
            return
        process = start(sources, publish)
        status.set("正在检测，请不要操作雷电里的影视。")

    def report():
        path = DATA / "report.html"
        if path.exists():
            webbrowser.open(path.as_uri())
        else:
            messagebox.showinfo("暂无结果", "先点击试测，完成后就可以查看报告。")

    def stop():
        DATA.mkdir(exist_ok=True)
        (DATA / "stop.flag").write_text("stop", encoding="utf-8")
        status.set("已请求停止，工具会退出试播并恢复影院配置。")

    def refresh():
        nonlocal process
        if process is not None and process.poll() is not None:
            status.set("检测已结束，请查看检测报告。" if process.returncode == 0 else "检测未完成，请查看报告中的原因。")
            process = None
        window.after(1500, refresh)

    tk.Label(window, text="蛋壳影院自动测速", font=("Microsoft YaHei", 20, "bold")).pack(pady=(20, 10))
    tk.Label(window, text="在雷电里真实试播电影，日常运行不消耗 AI TOKEN。\n完整检测可能需要数小时，检测期间请不要操作模拟器。", font=("Microsoft YaHei", 10), justify="left").pack(pady=5)
    tk.Button(window, text="先试测欧乐（不更新片源菜单）", command=lambda: launch("OleLive"), width=40, height=2).pack(pady=5)
    tk.Button(window, text="检测全部片源并更新菜单", command=lambda: launch(publish=True), width=40, height=2).pack(pady=5)
    frame = tk.Frame(window)
    frame.pack(pady=7)
    tk.Button(frame, text="查看检测报告", command=report, width=18).pack(side="left", padx=5)
    tk.Button(frame, text="停止检测", command=stop, width=18).pack(side="left", padx=5)
    tk.Label(window, textvariable=status, wraplength=510, font=("Microsoft YaHei", 10)).pack(pady=10)
    refresh()
    window.mainloop()


if __name__ == "__main__":
    if "--scheduled" in sys.argv:
        child = start(publish=True)
        raise SystemExit(child.wait())
    gui()
