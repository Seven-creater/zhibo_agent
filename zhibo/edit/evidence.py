"""判定证据包第 2 版（重构第 5 阶段）：句子边界切段、每段的主播原话和事件、九宫格截图、字幕。

和第 1 版（只有静音边界、每段一张图、只有弹幕）的区别：
- 切段优先用逐句稿的句子边界：游戏声音吵时静音边界很少（EVO-009 实测整段只切出 1 段），句子边界总是有；
- 每段除了弹幕，还写上主播这段说了什么、声音里发生了什么（笑声、音量突增、欢呼）、弹幕多少人——
  把信号写成文字给模型看，是 clips-studio `ARCHITECTURE.md` 4.4 节"事件时间线"的做法
  （https://github.com/ColinGPT9/clips-studio ，提交 bd39d8b，AGPL-3.0）；
- 截图拼成 3×3 九宫格，一张图看 9 段，调用里的图片数降一个量级。hotclip `src/core/highlight/vision.ts` 的做法
  （https://github.com/xixihhhh/hotclip ，提交 3543584，AGPL-3.0）。本机 ffmpeg 没有文字滤镜，格子里不写编号，
  按"从左到右、从上到下"的顺序对应段号，提示词里写明；
- 宽窗口和实时即时重放保存的区间一致（act/replay.py 的 window），离线判定看到的就是实时留下的那段。
"""
from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..core import media


def sentence_bounds(transcript: List[Dict[str, Any]], a: float, b: float) -> List[float]:
    """相邻两句之间停顿的中点，作为切点候选（epoch 秒）。"""
    sents = sorted((s for s in transcript if s["end"] >= a and s["start"] <= b), key=lambda s: s["start"])
    return [round((x["end"] + y["start"]) / 2, 3) for x, y in zip(sents, sents[1:]) if y["start"] >= x["end"]]


LAUGH_MARK = 0.3  # 句子期间或紧接着 2 秒内，笑声概率到这个值就在原话后标（笑声）


def sentence_laugh(sent: Dict[str, Any], perception: Dict[int, Dict[str, Any]]) -> float:
    """一句话期间到说完后 2 秒内，声音分类的最大笑声概率。"""
    return max((perception[t].get("laugh", 0.0) for t in range(int(sent["start"]), int(sent["end"] + 2) + 1)
                if t in perception), default=0.0)


def laugh_end(after: float, perception: Dict[int, Dict[str, Any]], limit: float, gap: int = 2) -> float:
    """从 after 开始，笑声（概率 ≥ LAUGH_MARK）一直持续到哪一秒结束；中间断开不超过 gap 秒算连续，最多到 limit。
    没有紧接着的笑声就返回 after。"""
    end, quiet, t = after, 0, int(after)
    while t < limit and quiet <= gap:
        if perception.get(t, {}).get("laugh", 0.0) >= LAUGH_MARK:
            end, quiet = t + 1.0, 0
        else:
            quiet += 1
        t += 1
    return min(max(end, after), limit)


def window_sentences(transcript: List[Dict[str, Any]], a: float, b: float) -> List[Dict[str, Any]]:
    """宽窗口里的句子，从 1 开始编号。模型精剪时只报句号，切点由程序按句子的起止时间算（FR-6.5）。"""
    sents = sorted((x for x in transcript if a <= (x["start"] + x["end"]) / 2 < b), key=lambda x: x["start"])
    return [{"n": i + 1, **x} for i, x in enumerate(sents)]


def segment_details(seg: Dict[str, Any], sentences: List[Dict[str, Any]], perception: Dict[int, Dict[str, Any]],
                    chats: List[Tuple[float, str]], lead: float) -> Dict[str, Any]:
    """一段的文字证据。seg 的时间是录像时间（= 声音时间）；弹幕比声音晚 lead 秒，所以弹幕按 [起+lead, 止+lead) 取。
    sentences 是 window_sentences() 编过号的句子，主播原话写成"[句号]原文"。"""
    s, e = seg["start"], seg["end"]
    said = " ".join(f"[{x['n']}]{x['text']}" + ("（笑声）" if sentence_laugh(x, perception) >= LAUGH_MARK else "")
                    for x in sentences if s <= (x["start"] + x["end"]) / 2 < e)
    rows = [perception[t] for t in range(int(s), int(e) + 1) if t in perception]
    events = []
    if rows:
        laugh = max(r.get("laugh", 0) for r in rows)
        spike = max(r.get("spike", 0) for r in rows)
        cheer = max(r.get("cheer", 0) for r in rows)
        if laugh >= 0.3:
            events.append(f"声音里有笑声（{laugh:.2f}）")
        if spike >= 3:
            events.append(f"音量突然变大（约 {spike:.1f} 倍）")
        if cheer >= 0.3:
            events.append(f"欢呼或掌声（{cheer:.2f}）")
    seg_chats = [x for ts, x in chats if s + lead <= ts < e + lead and x]
    return {"said": said[:200], "events": events, "chat_count": len(seg_chats),
            "chats": [t[:20] for t, _ in Counter(seg_chats).most_common(8)]}


def contact_sheets(src: Path, times: List[float], out_dir: Path, per_sheet: int = 9, width: int = 320) -> List[str]:
    """每个时刻截一帧，每 9 帧拼成一张 3×3 九宫格（从左到右、从上到下）。返回九宫格路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    sheets = []
    for k in range(0, len(times), per_sheet):
        chunk = times[k:k + per_sheet]
        tmp = out_dir / f"sheet{k // per_sheet + 1}_frames"
        media.extract_frames(src, chunk, tmp, width=width)
        for i in range(len(chunk)):  # tile 滤镜按文件名序号读；某帧截不出来就放灰图占位，保证格子和段号对得上
            f = tmp / f"frame_{i}.jpg"
            if not f.exists():
                subprocess.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                                "-i", f"color=gray:s={width}x{width * 9 // 16}", "-frames:v", "1", str(f)],
                               capture_output=True)
            f.rename(tmp / f"f{i + 1:02d}.jpg")
        sheet = out_dir / f"sheet{k // per_sheet + 1}.jpg"
        r = subprocess.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1",
                            "-i", str(tmp / "f%02d.jpg"), "-vf", "tile=3x3:padding=4:color=white",
                            "-frames:v", "1", "-q:v", "4", str(sheet)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"拼九宫格失败：{r.stderr[-300:]}")
        sheets.append(str(sheet))
    return sheets


def write_srt(transcript: List[Dict[str, Any]], start: float, end: float, out: Path) -> int:
    """把片段时间范围内的逐句稿写成 SRT 字幕（时间相对片段开头）。返回写了几句。"""
    def ts(x: float) -> str:
        x = max(0.0, x)
        h, rem = divmod(int(x * 1000), 3600_000)
        m, rem = divmod(rem, 60_000)
        s_, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s_:02d},{ms:03d}"

    lines, n = [], 0
    for x in transcript:
        if x["end"] <= start or x["start"] >= end:
            continue
        n += 1
        lines += [str(n), f"{ts(x['start'] - start)} --> {ts(min(x['end'], end) - start)}", x["text"], ""]
    if n:
        out.write_text("\n".join(lines), encoding="utf-8")
    return n
