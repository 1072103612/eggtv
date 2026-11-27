#!/usr/bin/env python3
"""List source keys and validate the two-field Eggshell TV control file."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_URL = "https://1072103612.github.io/eggtv/tvbox_config.json"


def validate(value):
    if not isinstance(value, dict) or set(value) != {"subscription_url", "home_source_key"}:
        raise ValueError("总控只能包含 subscription_url、home_source_key 两个字段")
    url, key = value["subscription_url"], value["home_source_key"]
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("订阅地址必须为 HTTPS 字符串")
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.fragment or "#" in url:
        raise ValueError("订阅地址必须为完整 HTTPS URL，不能带账号或片段")
    if any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in url.strip()):
        raise ValueError("订阅地址含非法空白或控制字符")
    if not isinstance(key, str) or len(key) > 256 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in key):
        raise ValueError("片源 key 必须为不超过 256 字的字符串，可为空")
    return {"subscription_url": url.strip(), "home_source_key": key}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def catalogue(path):
    sites = read_json(path).get("sites", [])
    if not sites or any(not isinstance(s, dict) or not isinstance(s.get("key"), str) or not s["key"] for s in sites):
        raise ValueError("片源文件没有有效 sites 列表")
    if len({s["key"] for s in sites}) != len(sites):
        raise ValueError("片源 key 重复")
    return sites


def check(control_path, catalogue_path=None):
    plan = validate(read_json(control_path))
    # A changed URL must never be validated against the old subscription's catalogue.
    path = catalogue_path or (ROOT / "tvbox_config.json" if plan["subscription_url"] == ORIGINAL_URL else None)
    if path is not None:
        sites = catalogue(path)
        key = plan["home_source_key"]
        if key and key not in {s["key"] for s in sites}:
            raise ValueError(f"默认片源不存在：{key}；请运行 sites 查看 key")
        return f"总控校验通过，片源 {len(sites)} 个"
    return "总控格式通过；订阅地址已变更，须用新配置配合 --catalog 检查默认片源，APP 仍会下载校验"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    checker = sub.add_parser("check", help="检查总控，不联网")
    checker.add_argument("--control", type=Path, default=ROOT / "app_control.json")
    checker.add_argument("--catalog", type=Path, help="新订阅的本地 JSON 文件")
    listing = sub.add_parser("sites", help="显示名称和对应 key")
    listing.add_argument("--catalog", type=Path, default=ROOT / "tvbox_config.json")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            print(check(args.control, args.catalog))
        else:
            for site in catalogue(args.catalog):
                print(f"{site.get('name', '')}\t{site['key']}")
        return 0
    except (ValueError, OSError, TypeError, AttributeError) as error:
        parser.exit(1, f"校验失败：{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
