"""文字层：标题图片 + 逐帧渲染的字幕轨（透明 PNG 序列）。

本机 ffmpeg 没有 libass/freetype（EVO-024），所以文字都用 Pillow 画。字幕整轨按帧画成透明图片序列，
作为一路视频输入叠加，这样能做"弹入"动画（hotclip 调研里中文主流是"一屏一短句 + 关键词换色"，
弹入用阻尼回弹：先小一点、略放大、再回到原大小，约 0.15 秒；https://github.com/xixihhhh/hotclip ，
提交 3543584，AGPL-3.0）。每块字幕只画一次，弹入的几帧用缩放版本，其余帧直接复用。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

W = 1080
WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)


def _hex(c: str) -> Tuple[int, int, int, int]:
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255)


def font(cfg: Dict[str, Any], size: int):
    from PIL import ImageFont

    return ImageFont.truetype(cfg.get("font", "/System/Library/Fonts/Hiragino Sans GB.ttc"), size,
                              index=int(cfg.get("font_index", 2)))


def text_img(segments: List[Tuple[str, Tuple[int, int, int, int]]], fnt, stroke: int, max_w: int):
    """一行文字（若干段不同颜色）画成透明图：白字黑描边，太宽就等比缩小。"""
    from PIL import Image, ImageDraw

    pad = stroke + 6
    widths = [fnt.getbbox(t, stroke_width=stroke)[2] for t, _ in segments]
    asc, desc = fnt.getmetrics()
    img = Image.new("RGBA", (sum(widths) + 2 * pad, asc + desc + 2 * pad), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = pad
    for (t, color), tw in zip(segments, widths):
        d.text((x, pad), t, font=fnt, fill=color, stroke_width=stroke, stroke_fill=BLACK)
        x += tw
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)), Image.LANCZOS)
    return img


def title_png(lines: List[str], cfg: Dict[str, Any], out: Path) -> Tuple[int, int]:
    """两行标题叠成一张图，第二行用高亮色。返回宽高。"""
    from PIL import Image

    fnt = font(cfg, int(cfg.get("title_size", 76)))
    hl = _hex(cfg.get("highlight_color", "#FFE400"))
    imgs = [text_img([(ln, hl if (i == len(lines) - 1 and len(lines) > 1) else WHITE)], fnt,
                     int(cfg.get("title_stroke", 7)), W - 60) for i, ln in enumerate(lines)]
    canvas = Image.new("RGBA", (max(i.width for i in imgs), sum(i.height for i in imgs)), (0, 0, 0, 0))
    y = 0
    for im in imgs:
        canvas.paste(im, ((canvas.width - im.width) // 2, y), im)
        y += im.height
    canvas.save(out)
    return canvas.width, canvas.height


def _segments(text: str, word: Optional[str], hl) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    if not word or word not in text:
        return [(text, WHITE)]
    i = text.index(word)
    return [x for x in [(text[:i], WHITE), (word, hl), (text[i + len(word):], WHITE)] if x[0]]


# 弹入动画：(距离出现的秒数上限, 缩放比例)
POP = [(0.05, 0.86), (0.10, 1.07), (0.15, 1.02)]


def caption_track(cues: List[Dict[str, Any]], duration: float, fps: int, band_h: int, cfg: Dict[str, Any],
                  out_dir: Path) -> str:
    """把字幕块渲染成透明 PNG 序列（宽 1080、高 band_h，字幕在条带中间），返回 ffmpeg 读取用的文件名模式。
    cues 的时间已经是成片时间。"""
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    fnt = font(cfg, int(cfg.get("caption_size", 64)))
    hl = _hex(cfg.get("highlight_color", "#FFE400"))
    stroke = int(cfg.get("caption_stroke", 6))
    pop = bool(cfg.get("caption_pop", False))
    base = [text_img(_segments(c["text"], c.get("highlight"), hl), fnt, stroke, W - 80) for c in cues]
    cache: Dict[Tuple[int, float], Any] = {}
    blank = Image.new("RGBA", (W, band_h), (0, 0, 0, 0))
    n = int(duration * fps) + 1
    k = 0
    for f in range(n):
        t = f / fps
        while k < len(cues) and cues[k]["end"] <= t:
            k += 1
        frame = blank
        if k < len(cues) and cues[k]["start"] <= t < cues[k]["end"]:
            s = 1.0
            if pop:
                dt = t - cues[k]["start"]
                s = next((sc for lim, sc in POP if dt < lim), 1.0)
            key = (k, s)
            if key not in cache:
                im = base[k]
                if s != 1.0:
                    im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
                cache[key] = im
            im = cache[key]
            frame = blank.copy()
            frame.paste(im, ((W - im.width) // 2, (band_h - im.height) // 2), im)
        frame.save(out_dir / f"c{f:05d}.png", compress_level=1)
    return str(out_dir / "c%05d.png")
