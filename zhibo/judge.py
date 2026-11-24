"""GLM 理解与片段判定（M7）。

判定器只需要实现 respond(request) -> 模型原文（JSON 字符串）。
- MockJudge：按证据包里的规则模拟模型输出，用来跑通流程；
- AcceptAllJudge：全部接受（基线用，不走账本）；
- GLMJudge：占位，以后接 agentic-video 的 opencode_provider（GLM-5.3-flash 视觉调用）。
输出格式固定并校验，格式错误时把具体字段反馈回去让模型改，有次数上限（FR-7.2）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .ledger import Ledger

OUTPUT_SPEC = {
    "event": "一句话说发生了什么",
    "worth_clipping": "true/false",
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


class GLMJudge:
    kind = "glm"
    uses_ledger = True

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

    def respond(self, request: Dict[str, Any]) -> str:
        # TODO 接 agentic-video 的 opencode_provider：把 request["evidence"]["frames"] 的图片、
        # 弹幕摘要、信号曲线和 OUTPUT_SPEC 拼成提示词，返回模型原文。
        raise NotImplementedError("GLM 判定器还没接入")


JUDGES = {"mock": MockJudge, "accept_all": AcceptAllJudge, "glm": GLMJudge}


def make_judge(cfg: Dict[str, Any]):
    return JUDGES[cfg["kind"]](cfg)


def validate(text: Optional[str], segment_ids: List[str]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    if text is None:
        return None, ["没有输出"]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        return None, [f"不是合法 JSON：{e}"]
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
