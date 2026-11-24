"""Chrome CDP launcher that opens a visible interactive window."""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[2]


def probe(port):
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
            data = json.load(response)
        if "Chrome/" in data.get("Browser", "") and data.get("webSocketDebuggerUrl"):
            return data
    except (OSError, URLError, ValueError):
        pass
    return None


def find_chrome(explicit=None):
    candidates = [Path(explicit)] if explicit else []
    if not explicit:
        for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if os.environ.get(name):
                candidates.append(Path(os.environ[name]) / "Google/Chrome/Application/chrome.exe")
        if found := shutil.which("chrome"):
            candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise RuntimeError("未找到 Google Chrome，请使用 --chrome 指定 chrome.exe 路径。")


def show_window(data, url, new_window=True):
    if "HeadlessChrome" in data.get("User-Agent", ""):
        raise RuntimeError("该端口运行的是无界面 Chrome，无法显示窗口。请先关闭对应实例后重试。")
    try:
        with connect(data["webSocketDebuggerUrl"], open_timeout=3, proxy=None) as ws:
            sequence = 0

            def call(method, params=None):
                nonlocal sequence
                sequence += 1
                ws.send(json.dumps({"id": sequence, "method": method, "params": params or {}}))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    reply = json.loads(ws.recv(timeout=max(.01, deadline - time.monotonic())))
                    if reply.get("id") == sequence:
                        if "error" in reply:
                            raise RuntimeError(f"Chrome 窗口操作失败：{reply['error'].get('message')}")
                        return reply.get("result", {})
                raise RuntimeError("Chrome 窗口操作超时")

            target_id = None
            if not new_window:
                targets = call("Target.getTargets").get("targetInfos", [])
                target_id = next((t["targetId"] for t in targets if t["type"] == "page"), None)
            if not target_id:
                target_id = call("Target.createTarget", {"url": url, "newWindow": True,
                                                         "background": False})["targetId"]
            window = call("Browser.getWindowForTarget", {"targetId": target_id})
            call("Browser.setWindowBounds", {"windowId": window["windowId"],
                                             "bounds": {"windowState": "normal"}})
            call("Target.activateTarget", {"targetId": target_id})
            return {"target_id": target_id, "window_id": window["windowId"]}
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"CDP 可用，但显示窗口失败（{type(exc).__name__}）。请检查 Chrome 窗口。") from exc


def start_chrome(port=9333, url="about:blank", chrome=None):
    if not 1024 <= port <= 65535:
        raise ValueError("端口必须在 1024–65535 之间")
    if url != "about:blank" and urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("启动地址只支持 http/https 或 about:blank")
    endpoint = f"http://127.0.0.1:{port}"
    if data := probe(port):
        window = show_window(data, url)
        return {"status": "window_opened", "endpoint": endpoint, "browser": data["Browser"], **window}
    # Fail clearly if some other process owns the port. Never terminate existing browsers.
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"端口 {port} 已占用，且不是可用的 Chrome CDP 服务。") from exc
    executable = find_chrome(chrome)
    profile = ROOT / "data" / f"chrome-profile-{port}"
    profile.mkdir(parents=True, exist_ok=True)
    args = [str(executable), f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", url]
    with (ROOT / "data" / f"chrome-{port}.log").open("ab") as log:
        # Visible interactive browser is intentional: the user needs to scan the login QR code.
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if data := probe(port):
            window = show_window(data, url, new_window=False)
            return {"status": "started", "endpoint": endpoint,
                    "browser": data["Browser"], "profile": str(profile), **window}
        time.sleep(.25)
    raise RuntimeError(f"Chrome 未在 15 秒内开放 {port}。请检查 data/chrome-{port}.log；"
                       "若该专用目录已被浏览器占用，请先手动关闭对应浏览器后重试。")


def main():
    for output in (sys.stdout, sys.stderr):
        if hasattr(output, "reconfigure"):
            output.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="启动带 CDP 的专用 Google Chrome")
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--url", default="about:blank")
    parser.add_argument("--chrome", help="chrome.exe 的完整路径")
    args = parser.parse_args()
    try:
        result = start_chrome(args.port, args.url, args.chrome)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"启动失败：{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Chrome CDP 已就绪。请在专用浏览器中登录并打开直播中控台。")


if __name__ == "__main__":
    main()
