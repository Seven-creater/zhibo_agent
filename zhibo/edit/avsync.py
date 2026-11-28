"""画面和逐句稿对时（EVO-033）：切片前量一下"同一句话在画面录像里比逐句稿晚多少秒"，按这个差去切画面。

为什么要量：逐句稿来自纯音频那一路，画面录像是另一路流。两路的第 0 秒都是按"第一个字节到达的时刻"估的，
直播流开头会先灌进几秒缓存，两路的延迟也各不相同，而且录得越久差得越多。
2026-11-25 服务器第一晚实测，画面比逐句稿晚 0.8～2.5 秒，陈泽那段大约 1.5 秒，所以字幕会比人说话早出来。

怎么量：取画面录像里这一段的原声，用同一个识别模型重新识别，按文字相似度和逐句稿一句句对上，
取起点差的中位数。至少要对上 min_matches 句才采用，否则返回 None（照旧按录像起点时间切）。
需要项目 .venv（sherpa-onnx、numpy）和 models/ 里的模型；没有就跳过。
"""
from __future__ import annotations

import difflib
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import media

_TRANSCRIBER: Dict[str, Any] = {}


def _transcriber(cfg: Dict[str, Any]):
    key = str(cfg.get("perceive", {}).get("models_dir", "models"))
    if key not in _TRANSCRIBER:
        try:
            from ..perceive.speech import SpeechTranscriber

            _TRANSCRIBER[key] = SpeechTranscriber(Path(key), cfg.get("perceive", {}))
        except Exception:  # noqa: BLE001  没装 sherpa-onnx 或没有模型：不对时
            _TRANSCRIBER[key] = None
    return _TRANSCRIBER[key]


def _pcm(path: Path, ss: float, dur: float):
    import numpy as np

    raw = subprocess.run([media.ffmpeg(), "-v", "error", "-ss", f"{max(0.0, ss):.3f}", "-i", str(path), "-t", f"{dur:.3f}",
                          "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


def match_offsets(heard: List[Dict[str, Any]], rows: List[Dict[str, Any]], t0: float,
                  max_offset: float, min_chars: int, min_ratio: float) -> List[float]:
    """heard：重新识别的句子（start 是相对 t0 的秒数）；rows：逐句稿（epoch 秒）。
    返回每一句对上的"画面比逐句稿晚多少秒"。"""
    out = []
    for h in heard:
        if len(h["text"]) < min_chars:
            continue
        t = t0 + h["start"]
        near = [r for r in rows if abs(r["start"] - t) <= max_offset]
        if not near:
            continue
        best = max(near, key=lambda r: difflib.SequenceMatcher(None, r["text"], h["text"]).ratio())
        if difflib.SequenceMatcher(None, best["text"], h["text"]).ratio() >= min_ratio:
            out.append(round(t - best["start"], 3))
    return out


def measure(rec_path: Path, rec_start_ts: float, center: float, rows: List[Dict[str, Any]],
            cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """在 center（epoch 秒，按录像起点时间算）前后取一段画面原声，返回 {offset, matches, offsets}；量不出来返回 None。"""
    s = cfg.get("sync", {})
    if not s.get("enabled") or not rows:
        return None
    tr = _transcriber(cfg)
    if tr is None:
        return None
    win = float(s.get("window_seconds", 60))
    t0 = center - win / 2
    x = _pcm(rec_path, t0 - rec_start_ts, win)
    if x.size < 16000:
        return None
    tr.reset()
    heard = tr.feed(x) + tr.flush()
    offs = match_offsets(heard, rows, t0, float(s.get("max_offset_seconds", 6)), int(s.get("min_chars", 6)),
                         float(s.get("min_ratio", 0.6)))
    if len(offs) < int(s.get("min_matches", 2)):
        return None
    srt = sorted(offs)
    mid = srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2
    return {"offset": round(mid, 3), "matches": len(offs), "offsets": offs}
