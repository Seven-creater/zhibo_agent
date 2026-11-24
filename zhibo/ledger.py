"""请求账本（M11，FR-11.1 / FR-11.2）：模型请求先记账再发出。

- 已收到结果的请求：直接回放，不重复调用模型；
- 记了"已发出"但没有结果的请求：结果未知，不自动重发；
- replay_only=True：只允许回放，账本里没有的请求直接报缺失（离线调参用）。
以后可以换成 agentic-video 的 state.py，接口保持 call(request, fn) 不变。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple


def request_key(request: Dict[str, Any]) -> str:
    raw = json.dumps(request, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class Ledger:
    def __init__(self, path: Path, replay_only: bool = False):
        self.path = Path(path)
        self.replay_only = replay_only
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state: Dict[str, Dict[str, Any]] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.state.setdefault(row["key"], {}).update(row)

    def _append(self, row: Dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.state.setdefault(row["key"], {}).update(row)

    def call(self, request: Dict[str, Any], fn: Callable[[Dict[str, Any]], str]) -> Tuple[str, Optional[str]]:
        """返回 (状态, 模型原文)。状态：received / replayed / unknown / missing_in_replay / failed。"""
        key = request_key(request)
        prev = self.state.get(key)
        if prev and prev.get("status") == "received":
            return "replayed", prev["response"]
        if prev and prev.get("status") == "sent":
            return "unknown", None
        if self.replay_only:
            return "missing_in_replay", None
        self._append({"key": key, "status": "sent", "wall_ts": time.time(), "request": request})
        try:
            response = fn(request)
        except Exception as e:  # 失败要能统计，不能静默当成"不值得切"（FR-7.4）
            self._append({"key": key, "status": "failed", "error": f"{type(e).__name__}: {e}"})
            return "failed", None
        self._append({"key": key, "status": "received", "wall_ts": time.time(), "response": response})
        return "received", response
