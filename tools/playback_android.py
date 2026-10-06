"""ADB and FongMi UI adapter. No desktop coordinates or AI calls."""
import json
import re
import shlex
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PACKAGE = "com.fongmi.android.tv"
PREFIX = PACKAGE + ":id/"


class DetectionError(RuntimeError):
    pass


class Android:
    def __init__(self, adb, serial=None):
        self.adb = str(adb)
        self.serial = serial
        self.port = None
        self.component = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, *args):
        command = [self.adb]
        if self.serial:
            command += ["-s", self.serial]
        result = subprocess.run(command + list(args), capture_output=True, timeout=25)
        if result.returncode:
            raise DetectionError("模拟器操作失败：" + result.stderr.decode("utf-8", errors="replace")[:180])
        return result.stdout.decode("utf-8", errors="replace")

    def connect(self):
        if not self.serial:
            devices = self.call("devices").splitlines()[1:]
            online = [line.split()[0] for line in devices if len(line.split()) == 2 and line.split()[1] == "device"]
            if len(online) != 1:
                raise DetectionError("需要恰好一个在线模拟器，或在设置中指定设备编号")
            self.serial = online[0]
        if self.call("get-state").strip() != "device":
            raise DetectionError("模拟器尚未就绪")
        packages = self.call("shell", "pm", "list", "packages", PACKAGE)
        if "package:" + PACKAGE not in packages:
            raise DetectionError("模拟器未安装影视应用")
        component = self.call("shell", "cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", PACKAGE).strip().splitlines()[-1]
        if not component.startswith(PACKAGE + "/"):
            raise DetectionError("无法定位影视启动入口")
        self.call("shell", "am", "start", "-n", component)
        self.component = component
        self.port = int(self.call("forward", "tcp:0", "tcp:9978").strip())
        time.sleep(2)

    def request(self, path, **params):
        url = f"http://127.0.0.1:{self.port}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with self.opener.open(url, timeout=4) as response:
            raw = response.read(1024 * 1024)
        return json.loads(raw) if path == "media" else raw

    def screen(self):
        for attempt in range(3):
            try:
                self.call("shell", "uiautomator", "dump", "/sdcard/eggtv-check.xml")
                break
            except DetectionError:
                if attempt == 2:
                    raise
                time.sleep(1)
        text = self.call("shell", "cat", "/sdcard/eggtv-check.xml")
        return list(ET.fromstring(text).iter("node"))

    def tap(self, node):
        if node.get("package") != PACKAGE or node.get("enabled") != "true":
            raise DetectionError("控件不属于影视应用或不可用")
        bounds = list(map(int, re.findall(r"\d+", node.get("bounds", ""))))
        if len(bounds) != 4 or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            raise DetectionError("控件位置无效")
        self.call("shell", "input", "tap", str((bounds[0]+bounds[2])//2), str((bounds[1]+bounds[3])//2))

    def find(self, nodes, text=None, field=None):
        found = [n for n in nodes if (text is None or n.get("text") == text)
                 and (field is None or n.get("resource-id") == PREFIX + field)]
        return found[0] if len(found) == 1 else None

    def click(self, text=None, field=None):
        node = self.find(self.screen(), text, field)
        if node is None:
            raise DetectionError("找不到唯一控件：" + str(text or field))
        self.tap(node)

    def back(self):
        self.call("shell", "input", "keyevent", "4")

    def home(self):
        for _ in range(7):
            nodes = self.screen()
            if self.find(nodes, field="title") is not None and self.find(nodes, field="setting") is not None:
                return nodes
            tab = next((n for n in nodes if n.get("package") == PACKAGE and n.get("content-desc") == "点播"
                        and n.get("clickable") == "true"), None)
            if tab is not None:
                self.tap(tab)
                time.sleep(.5)
                continue
            if not any(n.get("package") == PACKAGE for n in nodes) and self.component:
                self.call("shell", "am", "start", "-n", self.component)
                time.sleep(1)
                continue
            self.back()
            time.sleep(.5)
        raise DetectionError("无法返回影视主页")

    def load_config(self, url, name="蛋壳自动检测"):
        self.home()
        self.click(field="setting")
        nodes = self.screen()
        entries = [n for n in nodes if n.get("resource-id") == PREFIX + "vod"
                   and n.get("clickable") == "true" and int(re.findall(r"\d+", n.get("bounds", ""))[1]) < 500]
        if len(entries) != 1:
            raise DetectionError("无法找到点播配置入口")
        self.tap(entries[0])
        time.sleep(.5)
        self.request("action", do="setting", text=url, name=name)
        time.sleep(.5)
        nodes = self.screen()
        edit = self.find(nodes, field="url")
        if edit is not None and edit.get("text") != url:
            self.tap(edit)
            self.call("shell", "input", "keycombination", "113", "29")
            self.call("shell", "input", "keyevent", "67")
            # FongMi auto-completes a lone 'h' to 'http://'. Prefixing a
            # harmless character avoids duplicating the scheme during typing.
            self.call("shell", "input", "text", shlex.quote("x" + url))
            self.call("shell", "input", "keyevent", "122", "112", "123")
            nodes = self.screen()
            edit = self.find(nodes, field="url")
            if edit is None or edit.get("text") != url:
                raise DetectionError("配置地址没有正确填写；不加载错误配置")
            self.back()  # Dismiss the input keyboard before confirming.
            nodes = self.screen()
        button = self.find(nodes, field="positive")
        if button is None:
            button = self.find(nodes, text="确定")
        if button is None:
            raise DetectionError("配置地址已填写，但找不到确认按钮")
        self.tap(button)
        time.sleep(2)
        self.home()

    def scroll(self, node):
        bounds = list(map(int, re.findall(r"\d+", node.get("bounds", ""))))
        if len(bounds) != 4:
            raise DetectionError("无法滚动列表")
        x = (bounds[0]+bounds[2])//2
        self.call("shell", "input", "swipe", str(x), str(bounds[3]-50), str(x), str(bounds[1]+50), "450")
        time.sleep(.5)

    def close(self):
        if self.port:
            self.call("forward", "--remove", "tcp:" + str(self.port))
            self.port = None
