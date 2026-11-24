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

from .attribution import CAUSE_ZH
from .ledger import Ledger

OUTPUT_SPEC = {
    "event": "一句话说发生了什么",
    "worth_clipping": True,
    "reason": "值不值得切的理由",
    "structure": {"setup": ["段号"], "climax": ["段号"], "reaction": ["段号"]},
    "title": "建议标题",
}


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
        top = "、".join(ev["top_texts"][:3])
        out = {
            "event": f"观众集中刷「{top}」，{ev['chat_users']} 人参与",
            "worth_clipping": worth,
            "reason": (f"笑点/惊讶弹幕占 {ev['laugh_ratio']:.0%}，反应集中" if worth
                       else f"笑点弹幕只占 {ev['laugh_ratio']:.0%}，看不出内容亮点"),
            "structure": {"setup": setup, "climax": climax, "reaction": reaction},
            "title": f"【{request['room']['anchor']}】{ev['top_texts'][0] if ev['top_texts'] else '高光'}",
        }
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
        self.base_dir = Path(".")  # 由主流程设成本次运行的输出目录，截图路径相对于它

    @staticmethod
    def prompt(request: Dict[str, Any]) -> str:
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

    def body(self, request: Dict[str, Any]) -> Dict[str, Any]:
        blocks: List[Dict[str, Any]] = [{"type": "text", "text": self.prompt(request)}]
        for rel in request["evidence"].get("frames", []):
            data = base64.b64encode((self.base_dir / rel).read_bytes()).decode("ascii")
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            # glm-5.3-flash 是思考型模型，不封顶思考预算时可能把 max_tokens 吃光、正文为空（hunter 实测）
            "thinking": {"type": "enabled", "budget_tokens": self.thinking_budget},
            "messages": [{"role": "user", "content": blocks}],
        }

    def respond(self, request: Dict[str, Any]) -> str:
        if not self.key:
            raise RuntimeError("缺少 ZHIPU_API_KEY：在项目根目录的 .env 里写一行 ZHIPU_API_KEY=你的密钥")
        req = urllib.request.Request(
            f"{self.base}/v1/messages", data=json.dumps(self.body(request)).encode("utf-8"), method="POST",
            headers={"x-api-key": self.key, "Authorization": f"Bearer {self.key}",
                     "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GLM HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}")
        text = "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        if not text:
            raise RuntimeError(f"GLM 返回空正文（stop_reason={data.get('stop_reason')}）")
        return text


JUDGES = {"mock": MockJudge, "accept_all": AcceptAllJudge, "glm": GLMJudge}


def make_judge(cfg: Dict[str, Any]):
    return JUDGES[cfg["kind"]](cfg)


def validate(text: Optional[str], segment_ids: List[str]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
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
        obj, errs = validate(text, seg_ids)
        if not errs:
            return {"status": "ok", "result": obj, "raw": text, "attempts": attempt + 1, "ledger": trail}
        feedback = "上次输出有问题，请修正：" + "；".join(errs)
    return {"status": "failed", "error": "格式校验失败：" + "；".join(errs), "attempts": max_revisions + 1, "ledger": trail}
