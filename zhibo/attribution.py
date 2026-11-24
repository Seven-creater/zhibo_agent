"""热度归因（M5 后半）：判断一次异常由什么引起，带证据链（FR-5.3、FR-5.7）。

demo 版是规则：能从消息直接看出来的原因（福袋、PK、开播、单人刷礼物）先排除，
剩下的再看弹幕语义判断是不是内容驱动。以后可以加 GLM 辅助归因（FR-5.6）。
"""
from __future__ import annotations

from typing import Any, Dict

from .models import Attribution, HeatEvent, Room

CAUSE_ZH = {
    "content": "内容驱动",
    "lucky_bag": "福袋或红包",
    "pk": "PK 连麦",
    "campaign": "打榜或活动",
    "single_user_gift": "个别用户刷礼物",
    "start_end": "开播或下播效应",
    "chat_spam": "弹幕刷屏",
    "unknown": "未知",
}


def attribute(ev: HeatEvent, room: Room, cfg: Dict[str, Any]) -> Attribution:
    a = cfg["attribution"]
    s = ev.stats
    if not a.get("enabled", True):
        return Attribution(ev.event_id, "content", 0.0, ["归因已关闭（基线），所有异常都当成内容驱动"])

    ev_lines = [f"峰值信号 {ev.channel}，z={ev.peak_z}，持续 {s['duration']:.0f} 秒"]
    look = float(a["special_lookback_seconds"])
    specials = [x for x in s["specials"] if ev.start - look <= x["t"] <= ev.end]

    bags = [x for x in specials if x["type"] in ("lucky_bag", "red_packet")]
    if bags:
        ev_lines.append(f"异常开始前后出现 {len(bags)} 条福袋/红包消息（首条在 +{bags[0]['t'] - room.start_ts:.0f}s）")
        ev_lines.append(f"弹幕文本重复度高：不同文本占比 {s['text_unique_ratio']}")
        return Attribution(ev.event_id, "lucky_bag", 0.9, ev_lines)

    if any(x["type"] == "pk" for x in specials):
        ev_lines.append("异常时段内有 PK 消息")
        return Attribution(ev.event_id, "pk", 0.8, ev_lines)

    since_start = ev.start - room.start_ts
    if room.stream_start_known and since_start < float(a["start_effect_seconds"]):
        ev_lines.append(f"异常开始于开播后 {since_start:.0f} 秒内")
        return Attribution(ev.event_id, "start_end", 0.7, ev_lines)

    chat_z = s["peak_z_by_channel"].get("chat_users", 0.0)
    if (ev.channel == "gift" and s["gift_top1_share"] >= float(a["single_gift_top1_share"])
            and chat_z < float(cfg["heat"]["z_enter"])):
        ev_lines.append(f"礼物里最大一位用户占 {s['gift_top1_share']:.0%}，弹幕没有同步放量")
        return Attribution(ev.event_id, "single_user_gift", 0.85, ev_lines)

    if s["text_unique_ratio"] < float(a["spam_unique_ratio"]) and s["laugh_ratio"] < float(a["content_laugh_ratio"]):
        ev_lines.append(f"弹幕大量重复（不同文本占比 {s['text_unique_ratio']}），没有笑点词")
        return Attribution(ev.event_id, "chat_spam", 0.6, ev_lines)

    if s["laugh_ratio"] >= float(a["content_laugh_ratio"]):
        ev_lines.append(f"{s['chat_users']} 位不同用户发弹幕，笑点/惊讶词占比 {s['laugh_ratio']:.0%}")
        ev_lines.append("排除：异常前后没有福袋/红包/PK 消息，不在开播期，礼物不集中在一人")
        conf = min(0.95, 0.5 + s["laugh_ratio"])
        return Attribution(ev.event_id, "content", round(conf, 2), ev_lines)

    ev_lines.append("没有命中任何规则")
    return Attribution(ev.event_id, "unknown", 0.3, ev_lines)
