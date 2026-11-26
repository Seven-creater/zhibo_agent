"""成片包装（M8 第二步，docs/调研_剪辑包装.md）：把精剪好的横屏片段做成抖音竖屏成片。

整体是"模型出剪辑方案，程序按规则执行"（kingbootoshi/video-alchemy 的"剪辑方案即数据"，MIT）：
1. **剪辑方案**（GLM，只看文字）：基座每条都有——两行标题、整段字幕、最多 1 个高亮词、错别字修正、封面句、
   发布文案；额外手法（推近、去停顿、开场闪现 / 前置、音效）默认不用，模型要用必须写出具体理由，
   程序再按硬门槛筛（apply_gates），每条最多 1 个。BGM 只认配置里用户给的文件。模型只报句号和选项，不碰时间、
   不写滤镜；走账本，可回放。为什么这样克制见 docs/调研_剪辑克制.md（EVO-027）。
2. **时间线**（timeline.py）：开场钩子 + 去停顿，横屏剪好。
3. **竖屏合成**（一次 ffmpeg 编码）：布局（摄像头在上 / 模糊背景铺满）→ 推近镜头（zoompan）→ 标题 → 逐帧渲染的
   字幕轨（textlayer.py，带弹入）→ 音效 / BGM / 响度统一（sound.py）。
4. **封面、发布文案、质检**。

参数和做法的出处：
- 布局：clips-studio 游戏布局（https://github.com/ColinGPT9/clips-studio ，提交 bd39d8b，AGPL-3.0）、
  OpenShorts 模糊背景（MIT）；
- 字幕安全区（画面高度 62～72%）、推近要对上真实事件、开场钩子 1～3 秒、音效 ≤3 个、去停顿保护笑声、
  封面取最夸张的一刻、钩子兑付检查：hotclip（https://github.com/xixihhhh/hotclip ，提交 3543584，AGPL-3.0）。

需要项目 .venv 里的 Pillow。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core import media
from ..core.ledger import Ledger
from . import sound, textlayer, timeline
from .judge import GLMJudge

W, H = 1080, 1920
FPS = 30
PACKAGE_PROMPT_REV = "2026-11-26a"  # EVO-032：标题人名照笑点写
# 字幕里打码的脏话（平台对字幕里的脏话会限流）；可在配置 package.mask_words 里改
MASK_WORDS = ["他妈的", "他妈", "妈的", "傻逼", "傻B", "操你", "草泥马", "尼玛"]
ZOOM_TARGETS = ("face", "game", "center")
SFX_KINDS = ("ding", "boom", "pop")


# ---------------------------------------------------------------- 剪辑方案
# 基座每条都有（= EVO-025 用户认可的版本）：竖屏布局、两行标题、整段字幕、最多 1 个高亮词、脏话打码。
# 额外手法默认不用：模型要用，必须指出用在哪一句、这条片子里的具体理由；程序再按硬门槛筛一遍
# （apply_gates）。"一个都不用"是正常结果。依据见 docs/调研_剪辑克制.md：
# cut-as-code 的"Zero cues is valid"和逐条回答 avoid_when（MIT），hotclip coldopen.ts 的
# "笑点离片头太近不做钩子"门槛（提交 3543584，AGPL-3.0），DaVinci-AutoEdit-Agent 的"BGM 只在用户授权时加"（MIT）。

EXTRA_KINDS = ("zoom", "tighten", "flash", "cold_open", "sfx")


def plan_request(anchor: str, judge: Dict[str, Any], sentences: List[Dict[str, Any]], layout_kind: str,
                 clip_start: float, duration: float, cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "task": "package_plan", "prompt_rev": PACKAGE_PROMPT_REV, "anchor": anchor, "layout": layout_kind,
        "punchline": judge.get("punchline", ""), "judge_title": judge.get("title", ""),
        "key_quote": judge.get("key_quote", ""), "duration": round(duration, 1),
        "extras_max": int(cfg.get("extras_max", 1)), "sfx_enabled": bool(cfg.get("sfx_enabled", False)),
        "sentences": [{"n": i + 1, "text": s["text"], "laugh": bool(s.get("laugh")),
                       "at": round(max(0.0, s["start"] - clip_start), 1),
                       "gap_before": round(s.get("gap_before", 0.0), 1)} for i, s in enumerate(sentences)],
    }


EXAMPLE = {
    "focus": "Gus 气到想飙英语，把 angry 说成 hungry 自己没发现；观众要听清 hungry 这个词",
    "title": ["Gus 气到飙英语", "想说生气却喊成我好饿"],
    "highlight": [{"n": 5, "word": "HUNGRY"}],
    "fixes": [{"n": 4, "text": "还是 HUNGRY 呀"}],
    "extras": [],
    "cover_n": 5,
    "post": {"text": "气到想飙英语，结果一开口变成喊饿", "tags": ["#Gus", "#游戏翻车", "#口误"]},
}


def plan_prompt(req: Dict[str, Any]) -> str:
    lay = {"cam_top": "主播摄像头小窗放大放在上面、游戏画面放下面", "blur_fill": "原画面居中、上下模糊背景"}
    lines = [
        f"你是抖音搞笑切片的剪辑师。主播：{req['anchor']}。笑点：{req['punchline']}。笑点原话：{req['key_quote']}。",
        f"判定时起的标题：{req['judge_title']}。片子 {req['duration']} 秒，竖屏布局：{lay.get(req['layout'], req['layout'])}。",
        "片子里主播说的话（语音自动转写，可能有错字；（第 x 秒）是这句在片子里开始的时间；"
        "[停 x 秒] 是这句前面停了多久；（笑声）表示这句附近有笑声）：",
    ]
    for s in req["sentences"]:
        gap = f"[停 {s['gap_before']} 秒] " if s["gap_before"] >= 0.8 else ""
        lines.append(f"[{s['n']}]（第 {s['at']} 秒）{gap}{s['text']}" + ("（笑声）" if s["laugh"] else ""))
    lines += [
        "",
        "这条片子一定会有：竖屏布局、两行标题、整段字幕。用户看过这样的成片，认为已经很好。",
        "剪辑手法不是越多越好，要精准简洁：每多一个手法，都要有这条片子里具体的理由，说不出就不用。"
        "不加任何额外手法是正常结果，也是最常见的结果。",
        "按顺序想清楚，再输出：",
        "1. focus：一句话写这条片子好笑在哪、陌生观众要听清或看清什么才会笑。",
        "2. title：两行标题，每行不超过 12 个字。第一行交代是谁在干什么，第二行点出笑点或反转；必须是片子里真实发生的事，不夸大。"
        "人名照笑点里写的来：笑点没说是主播本人做的，就不要把主播的名字安上去（多人连麦时做事的常常是别人）。"
        "标题和字幕已经会把笑点说出来，不要再用别的手法把同一件事说第三遍。",
        "3. highlight：最多 1 个词，就是笑点本身那个词（说错的词、翻车的那句话的核心）；没有一个明确的词就给空列表。"
        "必须原样出现在那句（修正后的）话里；不选脏话。",
        "4. fixes：只修正明显的转写错别字，意思语气不能改，没把握就不改。",
        f"5. extras：额外手法，最多 {req['extras_max']} 个，通常是空列表。可选的手法和它们的适用条件：",
        "   - zoom（在某一句推近一次）：适合笑点在主播的表情或动作上、而画面里看不清；"
        "不适合笑点在说的话上（字幕已经交代了）。要写 target：face（主播的脸）或 game（游戏画面）。",
        "   - tighten（去掉句间的空档）：适合主播自己在磨叽、空档里什么都没发生；"
        "不适合空档里在打游戏、做动作、或者停一拍再抖包袱——搞笑片大多是后一种。",
        "   - flash（开头先闪现笑点那一刻不到 1 秒，再从头播）：只在笑点那句从第 6 秒以后才开始、而且那一刻的画面或喊声很炸时用；"
        "提前看到会剧透的不用。",
        "   - cold_open（把笑点那句的前 3 秒提前到开头）：只在笑点那句从第 10 秒以后才开始、而且那句话单独听也让人好奇时用；"
        "提前听到会剧透的不用。",
    ]
    if req.get("sfx_enabled"):
        lines.append("   - sfx（在某一句的开头或结尾加一个音效）：只在笑点落地那一下需要提示时用。"
                     "要写 sound：ding（叮）、boom（咚，翻车或反转）、pop（啵）；at：start 或 end。")
    lines += [
        "   每个手法写成 {\"kind\": 手法, \"n\": 句号, \"reason\": 理由}。理由要指出这条片子里的具体内容"
        "（例如\"第 3 句主播憋笑的表情在小窗里太小\"）；\"更有节奏\"\"更吸引人\"\"增加趣味\"这类话不算理由。",
        "6. cover_n：封面用哪一句的画面，选表情或画面最夸张的那一刻。",
        "7. post：发布文案 text（一句话，不超过 30 个字，不夸大），tags 3～5 个话题标签（以 # 开头，比如主播名、游戏名、笑点类型）。",
        "只输出一个 JSON 对象，不要其他文字。格式示例（这个例子里 extras 是空的，因为口误靠字幕和高亮词就够了）：",
        json.dumps(EXAMPLE, ensure_ascii=False),
    ]
    if req.get("feedback"):
        lines += ["", req["feedback"]]
    return "\n".join(lines)


class GLMPlanner(GLMJudge):
    """剪辑方案只看文字，复用判定的 GLM 客户端（密钥、重试、长度放宽都一样）。"""

    kind = "glm_package"

    @staticmethod
    def prompt(request: Dict[str, Any]) -> str:
        return plan_prompt(request)

    def body(self, request: Dict[str, Any], max_tokens: Optional[int] = None) -> Dict[str, Any]:
        return {"model": self.model, "max_tokens": max_tokens or self.max_tokens,
                "thinking": {"type": "enabled", "budget_tokens": self.thinking_budget},
                "messages": [{"role": "user", "content": [{"type": "text", "text": plan_prompt(request)}]}]}


class MockPlanner:
    """不调模型：标题拆两行，只出基座，不加额外手法。用来跑通流程。"""

    kind = "mock_package"

    def __init__(self, cfg: Dict[str, Any]):
        pass

    @staticmethod
    def respond(request: Dict[str, Any]) -> str:
        t = request.get("judge_title") or request.get("punchline") or "直播高光"
        parts = re.split(r"[：:，,！!？?]", t, maxsplit=1)
        title = [parts[0][:12], parts[1][:12]] if len(parts) > 1 and parts[1] else [t[:12], t[12:24]]
        laugh = [s["n"] for s in request["sentences"] if s["laugh"]]
        k = laugh[0] if laugh else (len(request["sentences"]) or 1)
        return json.dumps({"focus": request.get("punchline", ""), "title": [x for x in title if x], "highlight": [],
                           "fixes": [], "extras": [], "cover_n": k,
                           "post": {"text": t[:30], "tags": [f"#{request['anchor']}"]}}, ensure_ascii=False)


def validate_plan(text: Optional[str], req: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """格式检查 + 把模型的选择收紧：高亮词最多 1 个，额外手法最多 extras_max 个、每个要有理由。
    被丢掉的手法记在 plan["dropped"]，方便事后看模型想加什么、为什么没加。"""
    if not text:
        return None, ["没有输出"]
    m = re.search(r"\{.*\}", text, re.S)
    try:
        obj = json.loads(m.group(0) if m else text)
    except json.JSONDecodeError as e:
        return None, [f"不是合法 JSON：{e}"]
    errs: List[str] = []
    n_max = len(req["sentences"])

    def num(v) -> Optional[int]:
        try:
            v = int(v)
            return v if 1 <= v <= n_max else None
        except (TypeError, ValueError):
            return None

    title = obj.get("title")
    if isinstance(title, str):
        title = [title]
    if not (isinstance(title, list) and 1 <= len(title) <= 2 and all(isinstance(x, str) and x.strip() for x in title)):
        errs.append("title 要是 1～2 行的字符串列表")
    elif any(len(x) > 16 for x in title):
        errs.append("title 每行不超过 12 个字")
    fixes = {}
    for f in obj.get("fixes") or []:
        n = num((f or {}).get("n")) if isinstance(f, dict) else None
        if n and str(f.get("text", "")).strip():
            fixes[n] = str(f["text"]).strip()
    texts = {s["n"]: fixes.get(s["n"], s["text"]) for s in req["sentences"]}
    hl = {}
    for h in obj.get("highlight") or []:
        n = num((h or {}).get("n")) if isinstance(h, dict) else None
        w = str((h or {}).get("word", "")).strip() if isinstance(h, dict) else ""
        if n and w and w in texts[n] and not any(x in w for x in MASK_WORDS):
            hl[n] = w
            break  # 全片最多 1 个
    extras, dropped = [], []
    allowed = set(EXTRA_KINDS) - (set() if req.get("sfx_enabled") else {"sfx"})
    for x in obj.get("extras") or []:
        if not isinstance(x, dict):
            continue
        kind, n, reason = x.get("kind"), num(x.get("n")), str(x.get("reason", "")).strip()
        why = None
        if kind not in allowed:
            why = "不支持或没打开的手法"
        elif not n:
            why = "句号不对"
        elif len(reason) < 6:
            why = "没写具体理由"
        elif len(extras) >= int(req.get("extras_max", 1)):
            why = "超过额外手法上限"
        elif any(e["kind"] == kind or {e["kind"], kind} == {"flash", "cold_open"} for e in extras):
            why = "同类手法重复"
        if why:
            dropped.append({"kind": kind, "n": x.get("n"), "reason": reason, "dropped": why})
            continue
        e = {"kind": kind, "n": n, "reason": reason}
        if kind == "zoom":
            e["target"] = x.get("target") if x.get("target") in ZOOM_TARGETS else "face"
        if kind == "sfx":
            e["sound"] = x.get("sound") if x.get("sound") in SFX_KINDS else "ding"
            e["at"] = "start" if x.get("at") == "start" else "end"
        extras.append(e)
    post = obj.get("post") if isinstance(obj.get("post"), dict) else {}
    tags = [t if str(t).startswith("#") else f"#{t}" for t in (post.get("tags") or []) if str(t).strip()][:5]
    if errs:
        return None, errs
    return {"focus": str(obj.get("focus", "")).strip(), "title": [x.strip() for x in title], "highlight": hl,
            "fixes": fixes, "extras": extras, "dropped": dropped,
            "cover_n": num(obj.get("cover_n")) or next(iter(hl), None) or n_max,
            "post": {"text": str(post.get("text", "")).strip()[:40], "tags": tags}}, []


def apply_gates(plan: Dict[str, Any], rel: List[Dict[str, Any]], duration: float, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """程序的硬门槛，模型说了也不算（hotclip coldopen.ts：笑点离片头太近就不做钩子）。
    rel 是片段内时间的句子。返回执行用的 hook / tighten / zoom / sfx，并把被门槛挡掉的手法追加到 plan["dropped"]。"""
    by_n = {i + 1: s for i, s in enumerate(rel)}
    max_total = float(cfg.get("max_total_seconds", 20.0))
    out = {"hook": {"type": "none", "n": None}, "tighten": False, "zoom": [], "sfx": []}
    for e in plan["extras"]:
        s = by_n.get(e["n"])
        why = None
        if e["kind"] in ("flash", "cold_open"):
            lead = float(cfg.get("flash_min_lead" if e["kind"] == "flash" else "cold_open_min_lead",
                                 6.0 if e["kind"] == "flash" else 10.0))
            extra = float(cfg.get("flash_seconds", 0.9)) if e["kind"] == "flash" else float(cfg.get("cold_open_max", 3.0))
            if s["start"] < lead:
                why = f"笑点句从第 {s['start']:.1f} 秒开始，离片头不到 {lead:g} 秒，再提前放一遍是重复"
            elif duration + extra > max_total:
                why = f"加上钩子会到 {duration + extra:.1f} 秒，超过 {max_total:g} 秒"
            else:
                out["hook"] = {"type": e["kind"], "n": e["n"]}
        elif e["kind"] == "tighten":
            out["tighten"] = True
        elif e["kind"] == "zoom":
            out["zoom"].append({"n": e["n"], "target": e["target"]})
        elif e["kind"] == "sfx":
            if cfg.get("sfx_enabled"):
                out["sfx"].append({"n": e["n"], "at": e["at"], "kind": e["sound"]})
            else:
                why = "配置里没打开音效"
        if why:
            plan["dropped"].append(dict(e, dropped=why))
    return out


def make_plan(planner, ledger: Optional[Ledger], req: Dict[str, Any], max_revisions: int = 1) -> Dict[str, Any]:
    feedback, errs = None, ["没有输出"]
    for _ in range(max_revisions + 1):
        r = dict(req, feedback=feedback) if feedback else req
        if ledger is not None and getattr(planner, "kind", "").startswith("glm"):
            status, text = ledger.call(r, planner.respond)
            if status not in ("received", "replayed"):
                raise RuntimeError(f"剪辑方案：账本状态 {status}")
        else:
            text = planner.respond(r)
        plan, errs = validate_plan(text, req)
        if plan:
            return plan
        feedback = "上次输出有问题，请修正：" + "；".join(errs)
    raise RuntimeError("剪辑方案格式校验失败：" + "；".join(errs))


# ---------------------------------------------------------------- 字幕块

def _chunks(text: str, max_chars: int, keep: Optional[str] = None) -> List[str]:
    """一句话按字数切成一屏一块：优先在标点处断，再按字数硬断；英文单词和高亮词 keep 不拆开。"""
    parts = [p for p in re.split(r"(?<=[，。！？、,.!?；;])", text) if p.strip()]
    out: List[str] = []
    for p in parts:
        p = p.strip().rstrip("，。、,.")
        tokens = re.findall((re.escape(keep) + "|" if keep else "") + r"[A-Za-z0-9'*]+|\s+|.", p)
        cur = ""
        for t in tokens:
            if len(cur) + len(t) > max_chars and cur.strip():
                out.append(cur.strip())
                cur = ""
            cur += t
        if cur.strip():
            out.append(cur.strip())
    return out or [text]


def _chunks_timed(text: str, times: List[float], max_chars: int, keep: Optional[str] = None) -> List[Tuple[str, float]]:
    """有逐字时间时：在说话停顿最长的地方切屏（EVO-032：按字数硬切把"眼神"拆成了两屏）。
    返回 [(这一屏的字, 第一个字的时间)]。标点处、停顿处优先断；英文单词和高亮词 keep 不拆开。"""
    units = []  # (开始下标, 结束下标)：不能从中间断开的最小单位
    for m in re.finditer((re.escape(keep) + "|" if keep else "") + r"[A-Za-z0-9'*]+|\s+|.", text):
        units.append((m.start(), m.end()))
    out: List[Tuple[str, float]] = []
    i, min_len = 0, max(2, max_chars // 3)
    while i < len(units):
        a = units[i][0]
        if len(text) - a <= max_chars:
            j = len(units)
        else:
            best, j = None, i + 1
            for k in range(i + 1, len(units) + 1):
                end = units[k - 1][1]
                if end - a > max_chars:
                    break
                if end - a < min_len or k == len(units):
                    continue
                gap = times[units[k][0]] - times[end - 1]  # 断点后一个字和前一个字的时间差
                if text[end - 1] in "，。！？、,.!?；; ":
                    gap += 1.0
                if best is None or gap >= best:
                    best, j = gap, k
        piece = text[a:units[j - 1][1]]
        if piece.strip(" ，。、,."):
            out.append((piece.strip().rstrip("，。、,."), times[a]))
        i = j
    return out


def mask(text: str, words: List[str]) -> str:
    """把脏话换成同样长度的星号。"""
    for w in sorted(words, key=len, reverse=True):
        text = text.replace(w, "*" * len(w))
    return text


def caption_cues(sentences: List[Dict[str, Any]], plan: Dict[str, Any], max_chars: int,
                 mask_words: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """逐句稿（片段内时间）→ 字幕块（片段内时间）：每句按字数切块，句子时长按字数分摊。"""
    cues = []
    for i, s in enumerate(sentences):
        n = i + 1
        text = mask(plan["fixes"].get(n, s["text"]), MASK_WORDS if mask_words is None else mask_words)
        a, b = s["start"], s["end"]
        if b - a < 0.2:
            continue
        word = plan["highlight"].get(n)
        ts = s.get("ts")
        if ts and len(ts) == len(text) and "t0" in s:  # 有逐字时间（改字后长度不变也能用）：按停顿切、按说话时间出字
            timed = _chunks_timed(text, [s["t0"] + x for x in ts], max_chars, word)
            starts = [max(a, t) for _, t in timed] + [b]
            for (p, _), t, u in zip(timed, starts, starts[1:]):
                if u > t:
                    cues.append({"n": n, "start": t, "end": u, "text": p,
                                 "highlight": word if word and word in p else None})
            continue
        pieces = _chunks(text, max_chars, word)
        total = sum(len(p) for p in pieces) or 1
        t = a
        for p in pieces:
            d = (b - a) * len(p) / total
            cues.append({"n": n, "start": t, "end": t + d, "text": p, "highlight": word if word and word in p else None})
            t += d
    return cues


def _settle(cues: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
    """成片时间上：相邻字幕的小空档补齐，最后一块多停 0.5 秒。"""
    cues = sorted([c for c in cues if c["end"] - c["start"] > 0.05], key=lambda c: c["start"])
    for x, y in zip(cues, cues[1:]):
        if 0 < y["start"] - x["end"] < 0.6:
            x["end"] = y["start"]
        x["end"] = min(x["end"], y["start"])
    if cues:
        cues[-1]["end"] = min(cues[-1]["end"] + 0.5, duration)
    return cues


# ---------------------------------------------------------------- 竖屏合成

def _layout_filter(layout: Dict[str, Any], sw: int, sh: int,
                   blur_dup: bool = False) -> Tuple[str, int, int, Dict[str, Tuple[int, int, int, int]]]:
    """返回（底图滤镜，标题区底边 y，字幕中心 y，可推近的区域）。输出标签 [base]。
    blur_dup：游戏画面里原来那个小窗要不要打模糊。默认不打——用户看片后认为模糊色块比画面里有两个主播更显眼（EVO-027）。"""
    blur = (f"[0:v]split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"boxblur=luma_radius=40:luma_power=2,eq=brightness=-0.08[bg];")
    if layout.get("kind") == "cam_top" and layout.get("cam"):
        cx, cy, cw, ch = layout["cam"]
        x, y, w, h = int(cx * sw), int(cy * sh), int(cw * sw) // 2 * 2, int(ch * sh) // 2 * 2
        cam_h = int(round(W * h / w / 2)) * 2
        game_h = int(round(W * sh / sw / 2)) * 2
        top = int(layout.get("top", 300))
        if blur_dup:  # 游戏画面里原来那个小窗打上重度模糊
            f = (blur + f"[b]split=3[c][g][gc];[c]crop={w}:{h}:{x}:{y},scale={W}:{cam_h}[cam];"
                 f"[gc]crop={w}:{h}:{x}:{y},boxblur=luma_radius=25:luma_power=3[gcb];[g][gcb]overlay={x}:{y}[gm];"
                 f"[gm]scale={W}:{game_h}[game];")
        else:
            f = blur + f"[b]split=2[c][g];[c]crop={w}:{h}:{x}:{y},scale={W}:{cam_h}[cam];[g]scale={W}:{game_h}[game];"
        f += f"[bg][cam]overlay=0:{top}[t1];[t1][game]overlay=0:{top + cam_h}[base]"
        regions = {"face": (0, top, W, cam_h), "game": (0, top + cam_h, W, game_h), "center": (0, top, W, cam_h + game_h)}
        return f, top - 20, min(top + cam_h + game_h + 70, int(H * 0.86)), regions
    fg_h = int(round(W * sh / sw / 2)) * 2
    y = int((H - fg_h) / 2 - layout.get("raise", 120))
    f = blur + f"[b]scale={W}:{fg_h}[fg];[bg][fg]overlay=0:{y}[base]"
    reg = (0, y, W, fg_h)
    return f, y - 40, int(H * 0.66), {"face": reg, "game": reg, "center": reg}


def _zoom_filter(events: List[Tuple[float, float, float]], amount: float) -> str:
    """推近镜头：每个事件 (开始, 结束, 焦点 y 比例)，0.12 秒快速推到 1+amount 倍，结束前 0.25 秒退回。
    表达式放在单引号里，里面的逗号不用转义；it 是输入帧的时间。"""
    def z(a, b):
        return f"min(1,max(0,(it-{a:.3f})/0.12))*min(1,max(0,({b:.3f}-it)/0.25))"
    zexp = z(*events[0][:2])
    for a, b, _ in events[1:]:
        zexp = f"max({zexp},{z(a, b)})"
    fy = "0.5"
    for a, b, f in reversed(events):
        fy = f"if(between(it,{a - 0.2:.3f},{b + 0.3:.3f}),{f:.3f},{fy})"
    return (f"zoompan=z='1+{amount}*{zexp}':x='iw/2-iw/zoom/2':"
            f"y='max(0,min(ih-ih/zoom,{fy}*ih-ih/zoom/2))':d=1:s={W}x{H}:fps={FPS}")


def package_clip(clip: Path, out: Path, sentences: List[Dict[str, Any]], perception: Dict[int, Dict[str, Any]],
                 clip_start: float, plan: Dict[str, Any], layout: Dict[str, Any], cfg: Dict[str, Any],
                 bgm: Optional[Path] = None) -> Dict[str, Any]:
    """把一段横屏精剪片段做成竖屏成片。sentences 的时间是 epoch 秒；clip_start 是片段第 0 秒对应的 epoch 秒。"""
    p = cfg
    work = out.with_suffix("")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    info = media.probe(clip)
    dur = float(info["duration"])
    rel = [dict(s, start=max(0.0, s["start"] - clip_start), end=min(dur, s["end"] - clip_start),
                t0=s["start"] - clip_start) for s in sentences]  # t0：句子开头在片段里的时间（可能为负），逐字时间从这里算
    by_n = {i + 1: s for i, s in enumerate(rel)}
    ex = apply_gates(plan, rel, dur, p)  # 模型选的额外手法过一遍硬门槛
    punch = set(plan["highlight"]) | {e["n"] for e in plan["extras"]}
    for n in punch:  # 笑点句前后的停顿是节奏，不删
        if n in by_n:
            by_n[n]["punch"] = True

    # 1. 时间线：开场钩子 + 去停顿（默认都不做）
    laugh_secs = [t - clip_start for t, r in perception.items()
                  if r.get("laugh", 0) >= 0.3 and 0 <= t - clip_start <= dur]
    body = timeline.tighten(rel, dur, laugh_secs, float(p.get("tighten_min_gap", 1.5)),
                            float(p.get("tighten_pad", 0.2))) if ex["tighten"] else [(0.0, dur)]
    hook = timeline.hook_piece(ex["hook"]["type"], by_n.get(ex["hook"]["n"]), dur,
                               float(p.get("flash_seconds", 0.9)), float(p.get("cold_open_max", 3.0)))
    tl = timeline.Timeline(body, hook)
    edit = work / "edit.mp4"
    timeline.render(clip, tl, edit, FPS)
    total = tl.duration

    # 2. 字幕（成片时间）
    cues = []
    for c in caption_cues(rel, plan, int(p.get("caption_max_chars", 12)), p.get("mask_words")):
        a, b = tl.map_span(c["start"], c["end"])
        cues.append(dict(c, start=a, end=b))
    if hook and ex["hook"]["type"] == "cold_open":  # 提前到开头的那句也配字幕
        for c in caption_cues(rel, plan, int(p.get("caption_max_chars", 12)), p.get("mask_words")):
            if c["n"] == ex["hook"]["n"] and hook[0] <= c["start"] < hook[1]:
                cues.append(dict(c, start=c["start"] - hook[0], end=min(c["end"] - hook[0], tl.hook_len)))
    cues = _settle(cues, total)

    sw, sh = [int(x) for x in subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0",
         str(edit)], capture_output=True, text=True).stdout.strip().split(",")]
    base, title_bottom, cap_y, regions = _layout_filter(layout, sw, sh, bool(p.get("blur_dup_cam", False)))

    # 3. 推近镜头：对上句子（成片时间），焦点取对应区域的中心
    zoom_events = []
    for z in ex["zoom"]:
        s = by_n.get(z["n"])
        if not s:
            continue
        a, b = tl.map_span(s["start"], s["end"])
        b = min(b + 0.4, a + float(p.get("zoom_max_seconds", 2.0)))
        rx, ry, rw, rh = regions[z["target"]]
        zoom_events.append((a, b, (ry + rh / 2) / H))
    chain = [base]
    vlabel = "base"
    if zoom_events:
        chain.append(f"[base]{_zoom_filter(zoom_events, float(p.get('zoom_amount', 0.18)))}[zb]")
        vlabel = "zb"

    # 4. 标题和字幕轨
    tw, th = textlayer.title_png(plan["title"], p, work / "title.png")
    band_h = int(p.get("caption_band", 240))
    pattern = textlayer.caption_track(cues, total, FPS, band_h, p, work / "captions")
    chain.append(f"[{vlabel}][1:v]overlay={(W - tw) // 2}:{max(40, title_bottom - th)}[vt]")
    chain.append(f"[vt][2:v]overlay=0:{cap_y - band_h // 2}:eof_action=pass,format=yuv420p[vout]")

    # 5. 声音：音效（含钩子切回正片的"嗖"）、BGM、响度统一
    cues_sfx = []
    for x in ex["sfx"]:
        s = by_n.get(x["n"])
        if s:
            cues_sfx.append((tl.map_time(s["start"] if x["at"] == "start" else s["end"]), x["kind"]))
    if hook and p.get("sfx_enabled"):
        cues_sfx.append((max(0.0, tl.hook_len - 0.2), "whoosh"))
    cues_sfx = sound.thin_cues(cues_sfx, int(p.get("sfx_max", 3)), float(p.get("sfx_min_gap", 1.5)))
    sfx = [(t, sound.ensure_sfx(k, work, p.get("sfx_dir"))) for t, k in cues_sfx]
    use_bgm = bgm  # BGM 只认配置里用户给的文件，模型不能自己打开
    a_args, a_fc = sound.audio_chain(3, sfx, use_bgm, total, p)

    script = work / "filter.txt"
    script.write_text(";".join(chain) + ";" + a_fc, encoding="utf-8")
    cmd = [media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(edit),
           "-loop", "1", "-framerate", str(FPS), "-i", str(work / "title.png"),
           "-framerate", str(FPS), "-i", pattern] + a_args
    cmd += media.filter_script_args(script) + ["-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}", "-r", str(FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(p.get("crf", 22)),
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"竖屏合成失败：{r.stderr[-600:]}")

    # 6. 封面：封面句中间那一刻（避开开场钩子）
    cs = by_n.get(plan["cover_n"]) or rel[-1]
    ct = max(tl.hook_len + 0.3, tl.map_time((cs["start"] + cs["end"]) / 2))
    cover = out.with_name(out.stem + "_cover.jpg")
    subprocess.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{min(ct, total - 0.1):.3f}",
                    "-i", str(out), "-frames:v", "1", "-q:v", "2", str(cover)], capture_output=True)
    post = out.with_name(out.stem + "_post.txt")
    post.write_text((plan["post"]["text"] + "\n" + " ".join(plan["post"]["tags"])).strip() + "\n", encoding="utf-8")

    # 7. 质检
    got = media.probe(out)
    problems = []
    if not got.get("duration") or abs(got["duration"] - total) > 0.6:
        problems.append(f"成片时长 {got.get('duration')} 与时间线 {total:.2f} 不符")
    if not got.get("audio") or not got.get("video"):
        problems.append("缺少音频或视频轨")
    if hook and ex["hook"]["n"] not in by_n:
        problems.append("开场钩子指向的句子不在片子里")
    missing_hl = [w for n, w in plan["highlight"].items() if not any(c.get("highlight") == w for c in cues)]
    if missing_hl:
        problems.append(f"高亮词没出现在字幕里：{missing_hl}")
    return {"path": str(out), "cover": str(cover), "post": str(post), "duration": got.get("duration"),
            "source_duration": dur, "layout": layout.get("kind", "blur_fill"), "title": plan["title"],
            "hook": ex["hook"]["type"] if hook else "none", "tighten_cut_seconds": round(dur - sum(b - a for a, b in body), 2),
            "zooms": len(zoom_events), "sfx": [k for _, k in cues_sfx], "bgm": bool(use_bgm),
            "captions": len(cues), "highlights": sum(1 for c in cues if c["highlight"]),
            "check_ok": not problems, "problems": problems}
