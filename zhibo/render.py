"""剪辑出片（M8）：裁切 + 产出前自检（FR-8.1、FR-8.5）。字幕、竖屏、封面以后加在这里。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from . import media
from .models import Candidate
from .sources import Recording


def render_clip(cand: Candidate, rec: Recording, out_dir: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = out_dir / "clips" / f"{cand.cand_id}.mp4"
    want = cand.end - cand.start
    media.cut(rec.path, rec.rel(cand.start), want, out)
    info = media.probe(out)
    problems = []
    dur = info["duration"]
    tol = float(cfg["render"]["duration_tolerance"])
    if dur is None:
        problems.append("读不出时长")
    elif abs(dur - want) > tol:
        problems.append(f"时长 {dur:.2f}s 与计划 {want:.2f}s 相差超过 {tol}s")
    if info["video"] is False or info["audio"] is False:
        problems.append("缺少视频或音频轨")
    b = cfg["backtrack"]
    if not (float(b["min_seconds"]) - tol <= want <= float(b["max_seconds"]) + tol):
        problems.append(f"时长 {want:.1f}s 不在 {b['min_seconds']}～{b['max_seconds']} 秒内")
    return {
        "path": str(out.relative_to(out_dir)),
        "duration": round(dur, 3) if dur else None,
        "check_ok": not problems,
        "problems": problems,
    }
