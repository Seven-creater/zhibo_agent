"""剪辑出片（M8）：裁切 + 产出前自检（FR-8.1、FR-8.5）。

有逐句稿时同时出字幕：旁边放一个同名 .srt，再把字幕作为可开关的字幕轨封进 mp4（mov_text）。
本机 ffmpeg 没有 libass / 文字滤镜，暂时不能把字幕烧进画面；竖屏、封面以后加在这里。"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import media
from ..core.models import Candidate
from ..ingest.sources import Recording


def render_clip(cand: Candidate, rec: Recording, out_dir: Path, cfg: Dict[str, Any],
                transcript: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    out = out_dir / "clips" / f"{cand.cand_id}.mp4"
    want = cand.end - cand.start
    media.cut(rec.path, rec.rel(cand.start), want, out)
    subtitles = 0
    if transcript:
        from .evidence import write_srt

        srt = out.with_suffix(".srt")
        subtitles = write_srt(transcript, cand.start, cand.end, srt)
        if subtitles:
            tmp = out.with_suffix(".sub.mp4")
            r = subprocess.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(out), "-i", str(srt),
                                "-map", "0", "-map", "1", "-c", "copy", "-c:s", "mov_text",
                                "-metadata:s:s:0", "language=chi", str(tmp)], capture_output=True, text=True)
            if r.returncode == 0:
                tmp.replace(out)
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
    lo, hi = float(b["min_seconds"]), float(b["max_seconds"])
    if transcript is not None:  # 证据包第 2 版精剪，时长限制按 judge.clip_min/max_seconds
        lo = min(lo, float(cfg["judge"].get("clip_min_seconds", 10)))
    if not (lo - tol <= want <= hi + tol):
        problems.append(f"时长 {want:.1f}s 不在 {lo:g}～{hi:g} 秒内")
    res = {
        "path": str(out.relative_to(out_dir)),
        "duration": round(dur, 3) if dur else None,
        "check_ok": not problems,
        "problems": problems,
    }
    if transcript is not None:  # 只有证据包第 2 版才带这个字段，第 1 版的输出保持不变
        res["subtitles"] = subtitles
    return res
