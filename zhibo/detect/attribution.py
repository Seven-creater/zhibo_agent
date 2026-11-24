"""热度归因（M5 后半）：判断一次异常由什么引起，带证据链（FR-5.3、FR-5.7）。

demo 版是规则：能从消息直接看出来的原因（福袋、PK、开播、单人刷礼物）先排除，
剩下的再看弹幕语义判断是不是内容驱动。以后可以加 GLM 辅助归因（FR-5.6）。
"""
from __future__ import annotations

from typing import Any, Dict

from ..core.models import Attribution, HeatEvent, Room

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

    # 游客态收不到福袋消息（2026-11-24 实测），只能从"很多人发同一句带福袋口令的话"推断
    if s.get("copy_lucky", 0) >= int(a.get("lucky_copy_min", 5)):
        ev_lines.append(f"{s['copy_lucky']} 条复制粘贴的口令弹幕带福袋提示词，例如「{(s.get('top_copies') or [''])[0][:30]}」")
        return Attribution(ev.event_id, "lucky_bag", 0.75, ev_lines)

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

    # 合并后的长异常里有一段笑点爆发：笑点信号本身过了进入阈值，前后 30 秒笑点占比也够（2026-11-24 实测）
    lb = s.get("laugh_burst") or {}
    burst = (lb.get("z") is not None and lb["z"] >= float(cfg["heat"]["z_enter"])
             and lb.get("ratio", 0) >= float(a["content_laugh_ratio"]))
    if burst and s["laugh_ratio"] < float(a["content_laugh_ratio"]) and ev.channel != "laugh_users":
        ev_lines.append(f"整段笑点占比只有 {s['laugh_ratio']:.0%}，但 +{lb['t'] - room.start_ts:.0f}s 前后有一段笑点爆发"
                        f"（笑点信号 z={lb['z']}，前后 30 秒笑点占比 {lb['ratio']:.0%}），其余是其他弹幕高峰")
        ev_lines.append("排除：异常前后没有福袋/红包/PK 消息，不在开播期，礼物不集中在一人")
        return Attribution(ev.event_id, "content", round(min(0.9, 0.4 + lb["ratio"]), 2), ev_lines)

    no_laugh = s["laugh_ratio"] < float(a["content_laugh_ratio"])
    if s.get("copy_ratio", 0) >= float(a.get("spam_copy_ratio", 0.5)) and no_laugh:
        ev_lines.append(f"复制粘贴的串占弹幕 {s['copy_ratio']:.0%}，例如「{(s.get('top_copies') or [''])[0][:30]}」")
        return Attribution(ev.event_id, "chat_spam", 0.7, ev_lines)
    if s["text_unique_ratio"] < float(a["spam_unique_ratio"]) and no_laugh:
        ev_lines.append(f"弹幕大量重复（不同文本占比 {s['text_unique_ratio']}），没有笑点词")
        return Attribution(ev.event_id, "chat_spam", 0.6, ev_lines)

    # 大直播间弹幕多，笑点占比天然偏低；只要笑点信号本身是这次异常的主导信号，也算内容驱动（2026-11-24 实测）
    laugh_led = ev.channel == "laugh_users" and s["laugh_ratio"] >= float(a.get("laugh_led_min_ratio", 0.05))
    if s["laugh_ratio"] >= float(a["content_laugh_ratio"]) or laugh_led:
        if laugh_led and s["laugh_ratio"] < float(a["content_laugh_ratio"]):
            ev_lines.append(f"笑点信号是这次异常的主导信号（z={ev.peak_z}），虽然笑点占比只有 {s['laugh_ratio']:.0%}")
        ev_lines.append(f"{s['chat_users']} 位不同用户发弹幕，笑点/惊讶词占比 {s['laugh_ratio']:.0%}"
                        f"（已排除复制粘贴的串，串占 {s.get('copy_ratio', 0):.0%}）")
        ev_lines.append("排除：异常前后没有福袋/红包/PK 消息，不在开播期，礼物不集中在一人")
        conf = min(0.95, 0.5 + s["laugh_ratio"])
        return Attribution(ev.event_id, "content", round(conf, 2), ev_lines)

    ev_lines.append("没有命中任何规则")
    return Attribution(ev.event_id, "unknown", 0.3, ev_lines)
