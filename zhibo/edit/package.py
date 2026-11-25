"""成片包装（M8 第二步，docs/调研_剪辑包装.md 的 P0）：把精剪好的横屏片段做成抖音竖屏成片。

三件事：
1. **包装方案**：GLM 只看文字（片段里的逐句稿、判定给出的笑点和标题），给出两行标题、每句最多一个要高亮的词，
   以及明显的转写错别字修正。照"剪辑方案即数据"的思路（kingbootoshi/video-alchemy，MIT）：模型只出方案，
   不碰时间和滤镜，程序照方案渲染；用句号指代句子，和判定一致（FR-6.5）。走账本，可回放。
2. **文字**：用 Pillow 把标题、字幕画成透明 PNG（本机 ffmpeg 没有 libass/freetype，见 EVO-024）。
   中文字幕取"一屏一短句 + 关键词换色"，字幕基线放在画面高度约 65% 处（hotclip `subtitle.ts` 的 62～72% 安全带，
   https://github.com/xixihhhh/hotclip ，提交 3543584，AGPL-3.0）。
3. **竖屏合成**（ffmpeg，一次编码）：
   - cam_top：画面里有主播摄像头小窗时，小窗放大放上面、游戏画面放下面（clips-studio 游戏布局的思路，
     https://github.com/ColinGPT9/clips-studio ，提交 bd39d8b，AGPL-3.0）；小窗位置按主播在配置里写；
   - blur_fill：原画面按宽度放在中间，上下用同一画面的放大模糊版铺满（OpenShorts 的 GENERAL 模式，MIT）。

需要项目 .venv 里的 Pillow；模型调用复用 judge.py 的 GLM 客户端。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core import media
from ..core.ledger import Ledger
from .judge import GLMJudge

W, H = 1080, 1920
PACKAGE_PROMPT_REV = "2026-11-25b"
# 字幕里打码的脏话（平台对字幕里的脏话会限流）；可在配置 package.mask_words 里改
MASK_WORDS = ["他妈的", "他妈", "妈的", "傻逼", "傻B", "操你", "草泥马", "尼玛"]
LATIN = re.compile(r"[A-Za-z0-9']+")


# ---------------------------------------------------------------- 包装方案

def plan_request(anchor: str, judge: Dict[str, Any], sentences: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "task": "package_plan", "prompt_rev": PACKAGE_PROMPT_REV, "anchor": anchor,
        "punchline": judge.get("punchline", ""), "judge_title": judge.get("title", ""),
        "sentences": [{"n": i + 1, "text": s["text"], "laugh": bool(s.get("laugh"))} for i, s in enumerate(sentences)],
    }


def plan_prompt(req: Dict[str, Any]) -> str:
    lines = [
        f"你在给一条抖音搞笑切片做包装。主播：{req['anchor']}。笑点：{req['punchline']}。",
        f"判定时起的标题：{req['judge_title']}",
        "片子里主播说的话（语音自动转写，可能有错字；（笑声）表示这句附近有笑声）：",
    ]
    lines += [f"[{s['n']}] {s['text']}" + ("（笑声）" if s["laugh"] else "") for s in req["sentences"]]
    lines += [
        "",
        "请给出：",
        "1. title：放在视频上方的两行标题，每行不超过 12 个字。第一行交代是谁、在干什么，第二行点出笑点或反转；"
        "要让刷到的人想看下去，但必须是片子里真的发生的事，不许夸大或编造。",
        "2. highlight：要在字幕里换色强调的词，最多 3 个、每句最多 1 个，只选笑点关键词（口误的词、反转的词），"
        "必须是那句话里（按修正后的文字）原样出现的字；不要选脏话（脏话在字幕里会被打码）。",
        "3. fixes：只修正明显的转写错别字（同音字、英文单词拼错），意思和语气不能改，没把握就不改；不需要就给空列表。",
        "只输出一个 JSON 对象，不要其他文字。格式示例：",
        json.dumps({"title": ["Gus 气到飙英语", "想说生气却喊成我好饿"],
                    "highlight": [{"n": 5, "word": "HUNGRY"}],
                    "fixes": [{"n": 4, "text": "还是 HUNGRY 呀"}]}, ensure_ascii=False),
    ]
    if req.get("feedback"):
        lines += ["", req["feedback"]]
    return "\n".join(lines)


class GLMPlanner(GLMJudge):
    """包装方案只用文字，复用判定的 GLM 客户端（密钥、重试、长度放宽都一样）。"""

    kind = "glm_package"

    @staticmethod
    def prompt(request: Dict[str, Any]) -> str:
        return plan_prompt(request)

    def body(self, request: Dict[str, Any], max_tokens: Optional[int] = None) -> Dict[str, Any]:
        return {"model": self.model, "max_tokens": max_tokens or self.max_tokens,
                "thinking": {"type": "enabled", "budget_tokens": self.thinking_budget},
                "messages": [{"role": "user", "content": [{"type": "text", "text": plan_prompt(request)}]}]}


class MockPlanner:
    """不调模型：标题拆成两行，不高亮、不修正。用来跑通流程。"""

    kind = "mock_package"

    def __init__(self, cfg: Dict[str, Any]):
        pass

    @staticmethod
    def respond(request: Dict[str, Any]) -> str:
        t = request.get("judge_title") or request.get("punchline") or "直播高光"
        parts = re.split(r"[：:，,！!？?]", t, maxsplit=1)
        title = [parts[0][:12], parts[1][:12]] if len(parts) > 1 and parts[1] else [t[:12], t[12:24]]
        return json.dumps({"title": [x for x in title if x], "highlight": [], "fixes": []}, ensure_ascii=False)


def validate_plan(text: Optional[str], req: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    if not text:
        return None, ["没有输出"]
    m = re.search(r"\{.*\}", text, re.S)
    try:
        obj = json.loads(m.group(0) if m else text)
    except json.JSONDecodeError as e:
        return None, [f"不是合法 JSON：{e}"]
    errs = []
    n_max = len(req["sentences"])
    title = obj.get("title")
    if isinstance(title, str):
        title = [title]
    if not (isinstance(title, list) and 1 <= len(title) <= 2 and all(isinstance(x, str) and x.strip() for x in title)):
        errs.append("title 要是 1～2 行的字符串列表")
    elif any(len(x) > 16 for x in title):
        errs.append("title 每行不超过 12 个字")
    fixes = {}
    for f in obj.get("fixes") or []:
        try:
            n = int(f["n"])
            if 1 <= n <= n_max and str(f["text"]).strip():
                fixes[n] = str(f["text"]).strip()
        except (TypeError, KeyError, ValueError):
            errs.append("fixes 每项要有 n 和 text")
    texts = {s["n"]: fixes.get(s["n"], s["text"]) for s in req["sentences"]}
    hl = {}
    for h in obj.get("highlight") or []:
        try:
            n, w = int(h["n"]), str(h["word"]).strip()
        except (TypeError, KeyError, ValueError):
            errs.append("highlight 每项要有 n 和 word")
            continue
        if n in texts and w and w in texts[n] and n not in hl:
            hl[n] = w
    if errs:
        return None, errs
    return {"title": [x.strip() for x in title], "highlight": hl, "fixes": fixes}, []


def make_plan(planner, ledger: Optional[Ledger], req: Dict[str, Any], max_revisions: int = 1) -> Dict[str, Any]:
    feedback = None
    for _ in range(max_revisions + 1):
        r = dict(req, feedback=feedback) if feedback else req
        if ledger is not None and getattr(planner, "kind", "").startswith("glm"):
            status, text = ledger.call(r, planner.respond)
            if status not in ("received", "replayed"):
                raise RuntimeError(f"包装方案：账本状态 {status}")
        else:
            text = planner.respond(r)
        plan, errs = validate_plan(text, req)
        if plan:
            return plan
        feedback = "上次输出有问题，请修正：" + "；".join(errs)
    raise RuntimeError("包装方案格式校验失败：" + "；".join(errs))


# ---------------------------------------------------------------- 文字图片

def _font(cfg: Dict[str, Any], size: int):
    from PIL import ImageFont

    return ImageFont.truetype(cfg.get("font", "/System/Library/Fonts/Hiragino Sans GB.ttc"), size,
                              index=int(cfg.get("font_index", 2)))


def _text_png(segments: List[Tuple[str, str]], font, out: Path, stroke: int, max_w: int) -> Tuple[int, int]:
    """把一行（若干段不同颜色的文字）画成透明 PNG：白字黑描边，高亮段换色。返回图片宽高。"""
    from PIL import Image, ImageDraw

    pad = stroke + 6
    widths = [font.getbbox(t, stroke_width=stroke)[2] for t, _ in segments]
    asc, desc = font.getmetrics()
    w, h = min(sum(widths), max_w) + 2 * pad, asc + desc + 2 * pad
    img = Image.new("RGBA", (sum(widths) + 2 * pad, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = pad
    for (t, color), tw in zip(segments, widths):
        d.text((x, pad), t, font=font, fill=color, stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        x += tw
    if img.width > w:  # 太宽就等比缩小（极少发生：已经按字数断行）
        img = img.resize((w, int(h * w / img.width)), Image.LANCZOS)
    img.save(out)
    return img.width, img.height


def _chunks(text: str, max_chars: int) -> List[str]:
    """一句话按字数切成一屏一块：优先在标点处断，再按字数硬断；英文单词不拆开。"""
    parts = [p for p in re.split(r"(?<=[，。！？、,.!?；;])", text) if p.strip()]
    out: List[str] = []
    for p in parts:
        p = p.strip().rstrip("，。、,.")
        tokens = re.findall(r"[A-Za-z0-9']+|\s+|.", p)
        cur = ""
        for t in tokens:
            if len(cur) + len(t) > max_chars and cur.strip():
                out.append(cur.strip())
                cur = ""
            cur += t
        if cur.strip():
            out.append(cur.strip())
    return out or [text]


def mask(text: str, words: List[str]) -> str:
    """把脏话换成同样长度的星号。"""
    for w in sorted(words, key=len, reverse=True):
        text = text.replace(w, "*" * len(w))
    return text


def caption_cues(sentences: List[Dict[str, Any]], plan: Dict[str, Any], clip_start: float, clip_end: float,
                 max_chars: int, mask_words: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """逐句稿 → 字幕块：每句按字数切块，句子的时长按各块字数分摊；时间换成相对片段开头。"""
    cues = []
    for i, s in enumerate(sentences):
        n = i + 1
        text = mask(plan["fixes"].get(n, s["text"]), MASK_WORDS if mask_words is None else mask_words)
        a, b = max(s["start"], clip_start), min(s["end"], clip_end)
        if b - a < 0.2:
            continue
        pieces = _chunks(text, max_chars)
        total = sum(len(p) for p in pieces) or 1
        t = a
        for p in pieces:
            d = (b - a) * len(p) / total
            word = plan["highlight"].get(n)
            cues.append({"start": round(t - clip_start, 3), "end": round(t + d - clip_start, 3), "text": p,
                         "highlight": word if word and word in p else None})
            t += d
    # 相邻字幕之间的小空档补齐，避免闪烁；最后一块多停 0.5 秒
    for x, y in zip(cues, cues[1:]):
        if 0 < y["start"] - x["end"] < 0.6:
            x["end"] = y["start"]
    if cues:
        cues[-1]["end"] = min(cues[-1]["end"] + 0.5, clip_end - clip_start)
    return cues


def _split_highlight(text: str, word: Optional[str], color: str) -> List[Tuple[str, str]]:
    white = "#FFFFFF"
    if not word or word not in text:
        return [(text, white)]
    i = text.index(word)
    return [x for x in [(text[:i], white), (word, color), (text[i + len(word):], white)] if x[0]]


# ---------------------------------------------------------------- 合成

def _layout_filter(layout: Dict[str, Any], src_w: int, src_h: int) -> Tuple[str, int, int]:
    """返回（底图滤镜，标题区底边 y，字幕基线 y）。输出标签 [base]。"""
    blur = (f"[0:v]split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"boxblur=luma_radius=40:luma_power=2,eq=brightness=-0.08[bg];")
    if layout.get("kind") == "cam_top" and layout.get("cam"):
        cx, cy, cw, ch = layout["cam"]
        x, y, w, h = int(cx * src_w), int(cy * src_h), int(cw * src_w), int(ch * src_h)
        cam_h = int(round(W * h / w / 2)) * 2
        game_h = int(round(W * src_h / src_w / 2)) * 2
        top = int(layout.get("top", 300))
        f = (blur + f"[b]split=2[c][g];[c]crop={w}:{h}:{x}:{y},scale={W}:{cam_h}[cam];[g]scale={W}:{game_h}[game];"
             f"[bg][cam]overlay=0:{top}[t1];[t1][game]overlay=0:{top + cam_h}[base]")
        return f, top - 20, min(top + cam_h + game_h + 40, int(H * 0.86))
    fg_h = int(round(W * src_h / src_w / 2)) * 2
    y = int((H - fg_h) / 2 - layout.get("raise", 120))
    f = blur + f"[b]scale={W}:{fg_h}[fg];[bg][fg]overlay=0:{y}[base]"
    return f, y - 40, int(H * 0.66)


def package_clip(clip: Path, out: Path, sentences: List[Dict[str, Any]], clip_start: float, clip_end: float,
                 plan: Dict[str, Any], layout: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把一段横屏精剪片段做成竖屏成片。sentences 的时间是 epoch 秒；clip_start 是片段第 0 秒对应的 epoch 秒。"""
    p = cfg
    work = out.with_suffix("")
    work.mkdir(parents=True, exist_ok=True)
    info = media.probe(clip)
    src = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(clip)], capture_output=True, text=True).stdout.strip().split(",")
    sw, sh = int(src[0]), int(src[1])
    base, title_bottom, cap_y = _layout_filter(layout, sw, sh)

    hl_color = p.get("highlight_color", "#FFE400")
    inputs, overlays = [], []
    # 标题：两行，叠在画面上方，整段都在
    tfont = _font(p, int(p.get("title_size", 76)))
    lines = plan["title"]
    sizes = []
    for i, line in enumerate(lines):
        f = work / f"title{i}.png"
        color = hl_color if (i == len(lines) - 1 and len(lines) > 1) else "#FFFFFF"
        sizes.append(_text_png([(line, color)], tfont, f, int(p.get("title_stroke", 7)), W - 60))
        inputs.append(f)
    y = title_bottom - sum(h for _, h in sizes)
    for (w, h) in sizes:
        overlays.append({"x": (W - w) // 2, "y": max(40, y), "enable": None})
        y += h
    # 字幕：一屏一块，关键词换色
    cfont = _font(p, int(p.get("caption_size", 64)))
    cues = caption_cues(sentences, plan, clip_start, clip_end, int(p.get("caption_max_chars", 12)),
                        p.get("mask_words"))
    for k, c in enumerate(cues):
        f = work / f"cap{k:03d}.png"
        w, h = _text_png(_split_highlight(c["text"], c["highlight"], hl_color), cfont, f,
                         int(p.get("caption_stroke", 6)), W - 80)
        inputs.append(f)
        overlays.append({"x": (W - w) // 2, "y": cap_y - h // 2, "enable": (c["start"], c["end"])})

    chain, last = [base], "base"
    for i, o in enumerate(overlays):
        tag = f"o{i}"
        en = f":enable='between(t,{o['enable'][0]:.3f},{o['enable'][1]:.3f})'" if o["enable"] else ""
        chain.append(f"[{last}][{i + 1}:v]overlay={o['x']}:{o['y']}{en}[{tag}]")
        last = tag
    fc = ";".join(chain)
    script = work / "filter.txt"
    script.write_text(fc, encoding="utf-8")  # 滤镜串可能很长，写进文件
    cmd = [media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip)]
    for f in inputs:
        cmd += ["-loop", "1", "-i", str(f)]
    # ffmpeg 7 起用 "-/选项 文件" 从文件读选项值（ffmpeg 9 已去掉旧的 -filter_complex_script）
    cmd += ["-/filter_complex", str(script), "-map", f"[{last}]", "-map", "0:a?", "-shortest",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(p.get("crf", 22)), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"竖屏合成失败：{r.stderr[-500:]}")
    got = media.probe(out)
    return {"path": str(out), "duration": got.get("duration"), "source_duration": info.get("duration"),
            "layout": layout.get("kind", "blur_fill"), "title": lines, "captions": len(cues),
            "highlights": sum(1 for c in cues if c["highlight"])}
