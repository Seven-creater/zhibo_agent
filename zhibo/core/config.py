"""配置加载（FR-12.3）：JSON 文件，支持 "extends" 继承和命令行 --set 覆盖。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: Path, overrides: List[str] = ()) -> Dict[str, Any]:
    path = Path(path)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    parent = cfg.pop("extends", None)
    if parent:
        cfg = _merge(load_config(path.parent / parent), cfg)
    for item in overrides:
        key, _, raw = item.partition("=")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        node = cfg
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return cfg
