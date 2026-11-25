"""声音设计：音效打点、可选 BGM（人声出现时自动压低）、响度统一。

做法和参数来自 hotclip `src/core/sound-design.ts` 与 `docs/RESEARCH-2026-08-CLIP-QUALITY.md`
（https://github.com/xixihhhh/hotclip ，提交 3543584，AGPL-3.0）：
- 音效用 ffmpeg 本地合成（正弦衰减、噪声扫频），没有素材文件，也没有版权风险；想换真实音效时，
  在 package.sfx_dir 放同名 wav（ding.wav、boom.wav、pop.wav、whoosh.wav）即可覆盖；
- 每条最多 3 个音效，两两之间至少隔 1.5 秒，多了显得廉价；
- BGM 比人声低 15～20 dB，并以人声为触发做侧链压缩（人说话时 BGM 自动压低），结尾淡出；
- 成片响度统一到 -14 LUFS（loudnorm 单遍）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core import media

SFX_SYNTH = {
    # 笑点落地：清脆的"叮"
    "ding": "aevalsrc='0.55*sin(2*PI*1568*t)*exp(-6*t)+0.3*sin(2*PI*2093*t)*exp(-8*t)':d=0.7",
    # 反转、翻车：低沉的"咚"
    "boom": "aevalsrc='0.9*sin(2*PI*(120-70*t)*t)*exp(-4*t)':d=0.8",
    # 字幕或关键词弹出：短促的"啵"
    "pop": "aevalsrc='0.7*sin(2*PI*(1100-3500*t)*t)*exp(-35*t)':d=0.18",
    # 转场：从开场钩子切回正片时的"嗖"
    "whoosh": "anoisesrc=d=0.45:c=pink:a=0.5,bandpass=f=1800:w=1500,afade=t=in:d=0.18,afade=t=out:st=0.2:d=0.25",
}


def ensure_sfx(kind: str, work: Path, sfx_dir: Optional[str] = None) -> Path:
    if sfx_dir and (Path(sfx_dir) / f"{kind}.wav").exists():
        return Path(sfx_dir) / f"{kind}.wav"
    out = work / f"sfx_{kind}.wav"
    if not out.exists():
        r = subprocess.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", SFX_SYNTH[kind], "-ar", "48000", "-ac", "2", str(out)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"合成音效 {kind} 失败：{r.stderr[-200:]}")
    return out


def thin_cues(cues: List[Tuple[float, str]], max_n: int = 3, min_gap: float = 1.5) -> List[Tuple[float, str]]:
    """按时间排序，去掉离上一个太近的，最多留 max_n 个。"""
    kept: List[Tuple[float, str]] = []
    for t, k in sorted(cues):
        if kept and t - kept[-1][0] < min_gap:
            continue
        kept.append((t, k))
    return kept[:max_n]


def audio_chain(first_input: int, sfx: List[Tuple[float, Path]], bgm: Optional[Path], duration: float,
                cfg: Dict[str, Any]) -> Tuple[List[str], str]:
    """返回（额外的 -i 参数，音频滤镜串，输出标签 [aout]）。主音频是 0:a；音效和 BGM 从 first_input 号输入开始。"""
    args: List[str] = []
    parts: List[str] = ["[0:a]aformat=sample_rates=48000:channel_layouts=stereo[voice]"]
    mix = ["[voice]"]
    idx = first_input
    sfx_vol = float(cfg.get("sfx_volume", 0.8))
    for k, (t, path) in enumerate(sfx):
        args += ["-i", str(path)]
        ms = int(max(0.0, t) * 1000)
        parts.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={sfx_vol},"
                     f"adelay={ms}|{ms}[s{k}]")
        mix.append(f"[s{k}]")
        idx += 1
    if bgm is not None:
        args += ["-stream_loop", "-1", "-i", str(bgm)]
        db = float(cfg.get("bgm_db", -18))
        fade_st = max(0.0, duration - 1.5)
        parts.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={db}dB,"
                     f"atrim=0:{duration:.3f},afade=t=out:st={fade_st:.3f}:d=1.5[bgmraw]")
        # 人声做侧链：说话时 BGM 自动压低
        parts[0] = "[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[voice][key]"
        parts.append("[bgmraw][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[bgm]")
        mix.append("[bgm]")
    lufs = float(cfg.get("loudness_lufs", -14))
    if len(mix) > 1:
        parts.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0[mixed]")
        src = "[mixed]"
    else:
        src = "[voice]"
    parts.append(f"{src}loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample=48000[aout]")
    return args, ";".join(parts)
