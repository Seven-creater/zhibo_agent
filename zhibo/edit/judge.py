"""GLM 理解与片段判定（M7）。

判定器只需要实现 respond(request) -> 模型原文（JSON 字符串）。
- MockJudge：按证据包里的规则模拟模型输出，用来跑通流程；
- AcceptAllJudge：全部接受（基线用，不走账本）；
- GLMJudge：占位，以后接 agentic-video 的 opencode_provider（GLM-5.3-flash 视觉调用）。
输出格式固定并校验，格式错误时把具体字段反馈回去让模型改，有次数上限（FR-7.2）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..detect.attribution import CAUSE_ZH
from ..core.ledger import Ledger

OUTPUT_SPEC = {
    "event": "一句话说发生了什么",
    "worth_clipping": True,
    "reason": "值不值得切的理由",
    "structure": {"setup": ["段号"], "climax": ["段号"], "reaction": ["段号"]},
    "title": "建议标题",
}
# 证据包第 2 版多一个 cut：最精简版本从第几句到第几句（句号见提示里主播原话前的 [n]），切点由程序按句子起止算
# 第 2 版提示的修订号：改了 prompt_v2() 的写法就改这里。它放进请求里，所以提示一改账本就会重新调用
PROMPT_REV_V2 = "2026-11-24d"
OUTPUT_SPEC_V2 = {"event": "一句话说发生了什么", "punchline": "笑点到底是什么（说不清就是没有）", "funny_score": 8,
                  "worth_clipping": True, "reason": "值不值得切的理由",
                  "structure": {"setup": ["段号"], "climax": ["段号"], "reaction": ["段号"]},
                  "cut": {"from": 12, "to": 18}, "key_quote": "笑点那句主播原话（从上面照抄）", "title": "建议标题"}


class MockJudge:
    kind = "mock"
    uses_ledger = True

    def __init__(self, cfg: Dict[str, Any]):
        self.min_laugh = float(cfg.get("mock_min_laugh_ratio", 0.15))

    def respond(self, request: Dict[str, Any]) -> str:
        ev = request["evidence"]
        segs = request["segments"]
        peak = ev["peak_rel"]
        sig_start = ev["signal_start_rel"]
        setup = [s["id"] for s in segs if s["end"] <= sig_start + 1] or [segs[0]["id"]]
        climax = [s["id"] for s in segs if s["id"] not in setup and s["start"] <= peak + 3] or [segs[-1]["id"]]
        reaction = [s["id"] for s in segs if s["id"] not in setup and s["id"] not in climax]
        worth = request["attribution"]["cause"] == "content" and ev["laugh_ratio"] >= self.min_laugh
        # 证据包第 2 版带声音证据：主播笑、观众也有笑点弹幕，也算（模拟判定器只用来跑通流程）
        laugh = ((ev.get("audio_peaks") or {}).get("laugh") or {}).get("value", 0)
        if request.get("version") == 2 and request["attribution"]["cause"] == "content" and laugh >= 0.4 \
                and ev["laugh_ratio"] >= 0.05:
            worth = True
        top = "、".join(ev["top_texts"][:3])
        cut = None
        if request.get("sentence_count"):  # 模拟精剪：取高潮那几段里的句子
            nums = [int(n) for s_ in segs if s_["id"] in climax for n in re.findall(r"\[(\d+)\]", s_.get("said", ""))]
            cut = {"from": min(nums), "to": max(nums)} if nums else {"from": 1, "to": 1}
        out = {
            "event": f"观众集中刷「{top}」，{ev['chat_users']} 人参与",
            "worth_clipping": worth,
            "reason": (f"笑点/惊讶弹幕占 {ev['laugh_ratio']:.0%}，反应集中" if worth
                       else f"笑点弹幕只占 {ev['laugh_ratio']:.0%}，看不出内容亮点"),
            "structure": {"setup": setup, "climax": climax, "reaction": reaction},
            "title": f"【{request['room']['anchor']}】{ev['top_texts'][0] if ev['top_texts'] else '高光'}",
        }
        if cut:
            out["cut"] = cut
        if request.get("sentence_count"):
            out["funny_score"] = 8 if worth else 3
            out["punchline"] = out["event"]
            out["key_quote"] = ""  # 模拟判定器不核对原话
        return json.dumps(out, ensure_ascii=False)


class AcceptAllJudge:
    kind = "accept_all"
    uses_ledger = False

    def __init__(self, cfg: Dict[str, Any]):
        pass

    def respond(self, request: Dict[str, Any]) -> str:
        ids = [s["id"] for s in request["segments"]]
        return json.dumps({
            "event": "基线：不做判定", "worth_clipping": True, "reason": "基线全部接受",
            "structure": {"setup": ids[:1], "climax": ids[1:2] or ids[:1], "reaction": ids[2:]}, "title": "基线切片",
        }, ensure_ascii=False)


def _load_env_file(path: Path = Path(".env")) -> Dict[str, str]:
    """读项目根目录的 .env（KEY=VALUE 每行一个）。.env 已在 .gitignore 里，不会提交。"""
    out: Dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class GLMJudge:
    """GLM-5.3-flash 视觉判定，走智谱 z.ai 的 Anthropic 兼容接口（做法同 douyin_template_hunter/hunter/glm.py）。

    请求里放的是截图的相对路径（便于记账和回放）；真正发送时才读图转 base64。
    密钥从环境变量或项目 .env 的 ZHIPU_API_KEY 读取。
    """

    kind = "glm"
    uses_ledger = True

    def __init__(self, cfg: Dict[str, Any]):
        env = {**_load_env_file(), **os.environ}
        self.key = env.get("ZHIPU_API_KEY", "").strip()
        self.base = env.get("GLM_ANTHROPIC_BASE", cfg.get("base", "https://api.z.ai/api/anthropic")).rstrip("/")
        self.model = env.get("GLM_MODEL", cfg.get("model", "glm-5.3-flash"))
        self.max_tokens = int(cfg.get("max_tokens", 4000))
        self.thinking_budget = int(cfg.get("thinking_budget", 1200))
        self.timeout = float(cfg.get("timeout", 180))
        self.retry_max_tokens = int(cfg.get("retry_max_tokens", 16000))
        self.base_dir = Path(".")  # 由主流程设成本次运行的输出目录，截图路径相对于它

    @staticmethod
    def prompt_v2(request: Dict[str, Any]) -> str:
        """证据包第 2 版：每段有主播原话、声音事件、弹幕；截图是九宫格。"""
        room = request["room"]
        ev = request["evidence"]
        n_sheets = len(ev.get("frames", []))
        lines = [
            f"你在帮一个抖音直播切片账号找搞笑片段。主播：{room['anchor']}，品类：{room['category']}。",
            "下面是直播里的一段（约几分钟），已经按主播说话的句子停顿切成编号小段 S1、S2……。每段给出：时间（秒，相对开头）、"
            "主播这段说了什么（语音自动转写，可能有错字）、声音里发生了什么、观众弹幕（已经按观众的反应延迟对齐到这一段）。",
            f"另附 {n_sheets} 张九宫格截图：每张 3×3，从左到右、从上到下依次对应 9 个段（第 1 张是 S1～S9，第 2 张是 S10～S18，以此类推），"
            "每格是那一段中间时刻的画面。",
            f"系统初步判断这次热度上升的原因是：{CAUSE_ZH.get(request['attribution']['cause'], request['attribution']['cause'])}"
            f"（{'；'.join(request['attribution']['evidence'])}）。",
            "",
        ]
        for seg in request["segments"]:
            parts = [f"{seg['id']}：{seg['start']:.0f}–{seg['end']:.0f} 秒"]
            parts.append(f"主播：{seg.get('said') or '（没说话）'}")
            if seg.get("events"):
                parts.append("声音：" + "、".join(seg["events"]))
            texts = "、".join(seg.get("chats") or [])
            parts.append(f"弹幕 {seg.get('chat_count', 0)} 条" + (f"：{texts}" if texts else ""))
            lines.append("；".join(parts))
        taste = request.get("taste") or []
        done = request.get("already_clipped") or []
        if done:
            lines.append("这个直播间刚才已经切过下面这些笑点。如果这段的笑点就是其中之一（包括观众还在继续刷那个梗），"
                         "worth_clipping 填 false：")
            lines += [f"- {d}" for d in done]
        lines += [
            "",
            "请判断：这段里有没有一个**陌生人不看直播上下文、刷到也会笑**的搞笑场面。要求很严：大多数直播片段都不合格。",
            "先在 punchline 里用一句话说清笑点到底是什么（谁、做了/说了什么、为什么好笑）；说不清就是没有。",
            "再给 funny_score 打 1～10 分：9～10 一看就笑、有明确的包袱；7～8 好笑但要一点上下文；6 分及以下都不要。"
            "弹幕在刷「哈哈」「？」只说明直播间的观众有反应，不等于片段本身好笑；正常的游戏交流、解说、聊天、玩梗互动都不算。",
        ]
        if taste:
            lines.append("这个账号的审核人看过之前的片段，口味如下，请照着判：")
            lines += [f"- {t}" for t in taste]
        lines += [
            "如果合格，在 structure 里指出铺垫、高潮、观众反应各是哪几段；",
            f"再在 cut 里给出最精简的版本：从第几句到第几句（主播原话前方括号里的句号）。cut 的目标是 {request.get('target_seconds', '15～30')} 秒："
            "只留让人看懂所需的一两句铺垫和笑点本身，宁短勿长；cut 的最后一句就是笑点那一句，"
            "不要为了留观众反应去多选后面的句子（程序会在最后一句后自动多留 2 秒）。"
            "key_quote 照抄 cut 范围里笑点那句主播原话（可以只抄关键的一部分）；如果笑点是笑声本身，写（笑声）。"
            "注意：弹幕比画面晚，后面几段弹幕还在刷的梗，笑点往往已经在前面说完了；cut 必须选到笑点真正说出来的那几句。"
            "主播原话后面标了（笑声）的，表示那句话期间或紧接着有笑声；如果好笑的只是笑声本身，cut 只选笑声前一句和带笑声的那句。"
            "程序会在最后一句之后把紧接着的笑声留完。",
            "如果只是福袋、抽奖、刷礼物、复制粘贴的弹幕刷屏、单纯聊天，或者说不清笑点，worth_clipping 填 false。",
            "只输出一个 JSON 对象，不要其他文字。worth_clipping 是布尔值 true 或 false（不加引号），funny_score 是整数。格式示例：",
            json.dumps(OUTPUT_SPEC_V2, ensure_ascii=False),
            "段号、句号都只能用上面列出的，不要输出时间戳。",
        ]
        if request.get("feedback"):
            lines += ["", request["feedback"]]
        return "\n".join(lines)

    @staticmethod
    def prompt(request: Dict[str, Any]) -> str:
        if request.get("version") == 2:
            return GLMJudge.prompt_v2(request)
        room = request["room"]
        lines = [
            f"你在帮一个抖音直播切片账号找搞笑片段。主播：{room['anchor']}，品类：{room['category']}。",
            "下面这段直播已经按说话停顿切成编号小段，每段给出时间范围（秒，相对本段开头）、这段时间里观众发的弹幕，"
            "并按段号顺序附上每段中间的一张截图（第 1 张图对应第 1 段，以此类推）。",
            "注意：观众的弹幕反应通常比画面上发生的事晚 5～15 秒，所以好笑的事往往发生在弹幕开始刷「哈哈」「？」的前一两段。",
            f"系统初步判断这次热度上升的原因是：{CAUSE_ZH.get(request['attribution']['cause'], request['attribution']['cause'])}"
            f"（{'；'.join(request['attribution']['evidence'])}）。",
            "",
        ]
        for seg in request["segments"]:
            texts = "、".join(seg.get("chats") or []) or "（没有弹幕）"
            lines.append(f"{seg['id']}：{seg['start']:.0f}–{seg['end']:.0f} 秒；弹幕：{texts}")
        lines += [
            "",
            "请判断：这段里有没有值得剪成独立短视频的搞笑场面（主播整活、翻车、游戏里很菜或很离谱的操作、意外等都算）。"
            "如果有，指出铺垫从哪段开始、高潮在哪几段、观众反应到哪段结束，让切出来的视频不看直播上下文也能看懂。"
            "如果只是福袋、抽奖、刷礼物、复制粘贴的弹幕刷屏，或者看不出好笑在哪，worth_clipping 填 false。",
            "只输出一个 JSON 对象，不要其他文字。worth_clipping 是布尔值 true 或 false（不加引号）。格式示例：",
            json.dumps(OUTPUT_SPEC, ensure_ascii=False),
            "段号只能用上面列出的，不要输出时间戳。",
        ]
        if request.get("feedback"):
            lines += ["", request["feedback"]]
        return "\n".join(lines)

    def body(self, request: Dict[str, Any], max_tokens: Optional[int] = None) -> Dict[str, Any]:
        blocks: List[Dict[str, Any]] = [{"type": "text", "text": self.prompt(request)}]
        for rel in request["evidence"].get("frames", []):
            data = base64.b64encode((self.base_dir / rel).read_bytes()).decode("ascii")
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
        return {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            # glm-5.3-flash 是思考型模型，不封顶思考预算时可能把 max_tokens 吃光、正文为空（hunter 实测）
            "thinking": {"type": "enabled", "budget_tokens": self.thinking_budget},
            "messages": [{"role": "user", "content": blocks}],
        }

    def respond(self, request: Dict[str, Any]) -> str:
        if not self.key:
            raise RuntimeError("缺少 ZHIPU_API_KEY：在项目根目录的 .env 里写一行 ZHIPU_API_KEY=你的密钥")
        data = self._send(request, self.max_tokens)
        text = self._text(data)
        if not text and data.get("stop_reason") == "max_tokens" and self.retry_max_tokens > self.max_tokens:
            # 思考型模型偶尔不守思考预算，把输出长度全用在思考上、正文为空（2026-11-24 实测：8000 也不够）。
            # 照 hotclip（detect.ts 的 MAX_TOKENS 4000 / RETRY_MAX_TOKENS 16000）放宽一次长度重试
            data = self._send(request, self.retry_max_tokens)
            text = self._text(data)
        if not text:
            raise RuntimeError(f"GLM 返回空正文（stop_reason={data.get('stop_reason')}）")
        return text

    @staticmethod
    def _text(data: Dict[str, Any]) -> str:
        return "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()

    def _send(self, request: Dict[str, Any], max_tokens: int) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base}/v1/messages", data=json.dumps(self.body(request, max_tokens)).encode("utf-8"), method="POST",
            headers={"x-api-key": self.key, "Authorization": f"Bearer {self.key}",
                     "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GLM HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}")


JUDGES = {"mock": MockJudge, "accept_all": AcceptAllJudge, "glm": GLMJudge}


def make_judge(cfg: Dict[str, Any]):
    return JUDGES[cfg["kind"]](cfg)


def validate(text: Optional[str], segment_ids: List[str], n_sentences: int = 0) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    if text is None:
        return None, ["没有输出"]
    m = re.search(r"\{.*\}", text, re.S)  # 模型常把 JSON 包在 ```json 代码块里
    try:
        obj = json.loads(m.group(0) if m else text)
    except json.JSONDecodeError as e:
        return None, [f"不是合法 JSON：{e}"]
    if not isinstance(obj, dict):
        return None, ["输出不是 JSON 对象"]
    if isinstance(obj.get("worth_clipping"), str) and obj["worth_clipping"].strip().lower() in ("true", "false"):
        obj["worth_clipping"] = obj["worth_clipping"].strip().lower() == "true"  # 宽容：字符串 "true" 也认
    errs = []
    for k, typ in (("event", str), ("worth_clipping", bool), ("reason", str), ("structure", dict), ("title", str)):
        if not isinstance(obj.get(k), typ):
            errs.append(f"字段 {k} 缺失或类型不对，应为 {typ.__name__}")
    if isinstance(obj.get("structure"), dict):
        for part in ("setup", "climax", "reaction"):
            for sid in obj["structure"].get(part, []):
                if sid not in segment_ids:
                    errs.append(f"structure.{part} 里的段号 {sid} 不存在")
    if n_sentences:  # 证据包第 2 版：要有好笑分（1～10 的整数）
        try:
            score = int(obj.get("funny_score"))
            if not 1 <= score <= 10:
                raise ValueError
            obj["funny_score"] = score
        except (TypeError, ValueError):
            errs.append("funny_score 要是 1～10 的整数")
    if n_sentences and obj.get("worth_clipping") is True and not isinstance(obj.get("key_quote"), str):
        errs.append("值得切时要有 key_quote（笑点那句主播原话，或（笑声））")
    if n_sentences and obj.get("worth_clipping") is True:  # 证据包第 2 版：值得切时要给精剪的句号范围
        cut = obj.get("cut")
        try:
            a, b = int(cut["from"]), int(cut["to"])
            if not (1 <= a <= b <= n_sentences):
                errs.append(f"cut 的句号要满足 1 ≤ from ≤ to ≤ {n_sentences}")
            else:
                obj["cut"] = {"from": a, "to": b}
        except (TypeError, KeyError, ValueError):
            errs.append("值得切时要有 cut，形如 {\"from\": 句号, \"to\": 句号}")
    return (obj if not errs else None), errs


def judge_candidate(judge, ledger: Ledger, request: Dict[str, Any], max_revisions: int) -> Dict[str, Any]:
    """返回 {status: ok/failed, result, attempts, ledger: [...]}。"""
    seg_ids = [s["id"] for s in request["segments"]]
    feedback: Optional[str] = None
    trail: List[str] = []
    errs: List[str] = []
    for attempt in range(max_revisions + 1):
        req = dict(request, feedback=feedback) if feedback else request
        if judge.uses_ledger:
            status, text = ledger.call(req, judge.respond)
        else:
            status, text = "direct", judge.respond(req)
        trail.append(status)
        if status not in ("received", "replayed", "direct"):
            return {"status": "failed", "error": f"账本状态 {status}", "attempts": attempt + 1, "ledger": trail}
        obj, errs = validate(text, seg_ids, int(request.get("sentence_count", 0)))
        if not errs:
            return {"status": "ok", "result": obj, "raw": text, "attempts": attempt + 1, "ledger": trail}
        feedback = "上次输出有问题，请修正：" + "；".join(errs)
    return {"status": "failed", "error": "格式校验失败：" + "；".join(errs), "attempts": max_revisions + 1, "ledger": trail}
