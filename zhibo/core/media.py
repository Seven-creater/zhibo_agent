"""FFmpeg 封装：静音检测（句子边界）、裁切、抽帧、成片检查。"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List


def ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("找不到 ffmpeg，先运行：brew install ffmpeg")
    return path


_MAJOR: List[int] = []


def ffmpeg_major() -> int:
    """ffmpeg 主版本号（读 "ffmpeg version 6.1.1-..."），读不出来当作 7。"""
    if not _MAJOR:
        out = subprocess.run([ffmpeg(), "-version"], capture_output=True, text=True).stdout
        m = re.match(r"ffmpeg version n?(\d+)\.", out)
        _MAJOR.append(int(m.group(1)) if m else 7)
    return _MAJOR[0]


def filter_script_args(script: Path) -> List[str]:
    """从文件读 filter_complex 的参数：ffmpeg 7 起是 "-/filter_complex 文件"（ffmpeg 9 去掉了旧写法）；
    ffmpeg 6 及更早只认 "-filter_complex_script 文件"（Ubuntu 24.04 自带 6.1，EVO-030）。"""
    return ["-/filter_complex", str(script)] if ffmpeg_major() >= 7 else ["-filter_complex_script", str(script)]


def _run(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def silence_boundaries(path: Path, noise_db: float, min_silence: float) -> List[float]:
    """返回每段静音的中点（相对录像开头的秒数），当作句子之间的自然切点。"""
    r = _run([
        ffmpeg(), "-hide_banner", "-nostats", "-i", str(path), "-vn",
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-",
    ])
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return [round((a + b) / 2, 3) for a, b in zip(starts, ends)]


def cut(src: Path, start: float, duration: float, out: Path) -> None:
    """按秒精确裁切（重新编码，保证起止点准确）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    r = _run([
        ffmpeg(), "-hide_banner", "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-b:a", "64k",
        "-movflags", "+faststart", str(out),
    ])
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 裁切失败：{r.stderr[-500:]}")


def extract_frames(src: Path, times: List[float], out_dir: Path, width: int = 480) -> List[str]:
    """按时刻抽帧，缩到 width 宽（控制给模型的图片体积）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, t in enumerate(times):
        out = out_dir / f"frame_{i}.jpg"
        _run([ffmpeg(), "-hide_banner", "-y", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
              "-vf", f"scale={width}:-2", "-q:v", "5", str(out)])
        if out.exists():
            paths.append(str(out))
    return paths


def probe(path: Path) -> Dict[str, object]:
    """成片自检用：时长、有没有视频和音频轨。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"duration": None, "video": None, "audio": None}
    r = _run([ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)])
    try:
        info = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"duration": None, "video": False, "audio": False}
    kinds = {s.get("codec_type") for s in info.get("streams", [])}
    dur = info.get("format", {}).get("duration")
    return {"duration": float(dur) if dur else None, "video": "video" in kinds, "audio": "audio" in kinds}
