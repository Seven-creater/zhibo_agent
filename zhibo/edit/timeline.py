"""成片时间线：开场钩子 + 去停顿跳剪，把"片段内时间"换算成"成片时间"。

- 开场钩子（hotclip `src/core/coldopen.ts` 的两种做法，https://github.com/xixihhhh/hotclip ，提交 3543584，AGPL-3.0）：
  - flash：把笑点那句开头的 0.6～1 秒画面先闪一下，再从头播（"结局闪现"）；
  - cold_open：把笑点那一句（最长 3 秒）整句提前到开头，再从头播（"高潮前置"）。
  钩子超过 3～4 秒就开始掉人，所以都有上限。
- 去停顿：两句之间停顿超过 min_gap 秒时，只留两边各 pad 秒；**笑声附近不删**（hotclip 调研："情绪/笑声事件前后
  1 秒禁删，保住包袱前的停顿"）；片头的预留（为了看清动作，EVO-023）和最后一句之后都不动。
- 所有时间都是"片段内时间"（片段第 0 秒 = 0），由 map_time() 换成成片时间。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core import media

Piece = Tuple[float, float]


def tighten(sentences: List[Dict[str, Any]], duration: float, laugh_secs: List[float],
            min_gap: float = 0.7, pad: float = 0.2, protect: float = 1.0) -> List[Piece]:
    """去掉句间长停顿，返回保留的片段（片段内时间）。sentences 已是片段内时间，按开始排序。"""
    cuts: List[Piece] = []
    for x, y in zip(sentences, sentences[1:]):
        a, b = x["end"] + pad, y["start"] - pad
        if y["start"] - x["end"] <= min_gap or b - a <= 0.1:
            continue
        if any(a - protect <= s <= b + protect for s in laugh_secs):  # 笑声附近不删
            continue
        if x.get("punch") or y.get("punch"):  # 笑点句前后的停顿是节奏，不删
            continue
        cuts.append((a, b))
    pieces, t = [], 0.0
    for a, b in cuts:
        if a > t:
            pieces.append((t, a))
        t = b
    pieces.append((t, duration))
    return [(round(a, 3), round(b, 3)) for a, b in pieces if b - a > 0.05]


def hook_piece(kind: str, sentence: Optional[Dict[str, Any]], duration: float, flash_len: float = 0.9,
               cold_max: float = 3.0) -> Optional[Piece]:
    if not sentence or kind not in ("flash", "cold_open"):
        return None
    a = max(0.0, sentence["start"] - 0.1)
    b = a + flash_len if kind == "flash" else min(sentence["end"] + 0.15, a + cold_max)
    b = min(b, duration)
    return (round(a, 3), round(b, 3)) if b - a >= 0.4 else None


class Timeline:
    def __init__(self, body: List[Piece], hook: Optional[Piece] = None):
        self.hook = hook
        self.body = body
        self.hook_len = (hook[1] - hook[0]) if hook else 0.0

    @property
    def pieces(self) -> List[Piece]:
        return ([self.hook] if self.hook else []) + self.body

    @property
    def duration(self) -> float:
        return round(sum(b - a for a, b in self.pieces), 3)

    def map_time(self, t: float) -> float:
        """片段内时间 → 成片时间（只映射正片部分；落在删掉的停顿里就取下一段的开头）。"""
        out = self.hook_len
        for a, b in self.body:
            if t < a:
                return round(out, 3)
            if t <= b:
                return round(out + (t - a), 3)
            out += b - a
        return round(out, 3)

    def map_span(self, a: float, b: float) -> Tuple[float, float]:
        return self.map_time(a), self.map_time(b)


def render(clip: Path, tl: Timeline, out: Path, fps: int = 30) -> None:
    """按时间线把横屏片段剪好拼好（重新编码，保证切点准确），作为竖屏合成的输入。"""
    parts, labels = [], []
    for i, (a, b) in enumerate(tl.pieces):
        parts.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS,fps={fps}[v{i}];"
                     f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    fc = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=1:a=1[v][a]"
    r = subprocess.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip),
                        "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "16", "-c:a", "aac", "-b:a", "192k", str(out)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"时间线剪辑失败：{r.stderr[-400:]}")
