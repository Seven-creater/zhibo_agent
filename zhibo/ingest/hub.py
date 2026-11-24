"""dyhub（third_party/dyhub，MIT，浏览器内核）的客户端：建游客账号、连直播间、取播放器地址、收 SSE 事件。"""
from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from .douyin import NO_PROXY, _env_no_proxy

HUB_DIR = Path(__file__).resolve().parent.parent.parent / "third_party" / "dyhub"


class Hub:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _req(self, method: str, path: str, body: Optional[dict] = None, timeout: float = 120) -> Dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with NO_PROXY.open(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read() or b"{}")
            except json.JSONDecodeError:
                return {"error": f"HTTP {e.code}"}

    def alive(self) -> bool:
        try:
            self._req("GET", "/api/stats", timeout=3)
            return True
        except OSError:
            return False

    def ensure_account(self) -> None:
        if not self._req("GET", "/api/accounts").get("accounts"):
            self._req("POST", "/api/accounts", {"name": "游客"})

    def connect(self, room_id: str) -> Dict[str, Any]:
        d = self._req("POST", "/api/rooms/connect", {"roomId": room_id})
        return d.get("room") or d

    def room(self, room_id: str) -> Dict[str, Any]:
        return self._req("GET", f"/api/rooms/{room_id}").get("room") or {}

    def stream_url(self, room_id: str) -> Optional[str]:
        return self._req("GET", f"/api/rooms/{room_id}/stream").get("url")

    def events(self, stop: threading.Event):
        """SSE 事件流；断了自动重连。"""
        while not stop.is_set():
            try:
                with NO_PROXY.open(self.base + "/api/events", timeout=40) as r:
                    for raw in r:
                        if stop.is_set():
                            return
                        line = raw.decode("utf-8", "replace").rstrip("\n")
                        if line.startswith("data: "):
                            try:
                                yield json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
            except OSError:
                if not stop.wait(2):
                    continue


def start_hub(port: int) -> subprocess.Popen:
    if not (HUB_DIR / "dist" / "index.js").exists():
        raise SystemExit(f"没找到 dyhub：{HUB_DIR}，先按 README 安装")
    env = _env_no_proxy() | {"DYHUB_HOST": "127.0.0.1", "DYHUB_PORT": str(port)}
    log = (HUB_DIR / "dyhub.log").open("a")
    return subprocess.Popen(["node", "dist/index.js"], cwd=HUB_DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
