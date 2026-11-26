"""抖音侧的接口：解析直播间参数、按主播主页查开播状态、取指定清晰度的流地址。
所有对抖音的连接都不走本机代理（抖音要看到本机真实 IP）。"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any, Dict, Optional

PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def _env_no_proxy() -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in PROXY_VARS}


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def parse_room(arg: str) -> Dict[str, Optional[str]]:
    """支持三种写法：名字=直播间号、名字=live.douyin.com 链接、名字=主播主页链接（douyin.com/user/<sec_uid>）。
    主页链接适合还没开播的主播：开播后自动查到直播间号。"""
    name, _, val = arg.partition("=") if "=" in arg else ("", "", arg)
    m = re.search(r"/user/(MS4w[\w-]+)", val) or re.match(r"(MS4w[\w-]+)$", val.strip())
    if m:
        return {"room_id": None, "sec_uid": m.group(1), "anchor": name or m.group(1)[:12]}
    m = re.search(r"(\d{6,})", val)
    if not m:
        raise SystemExit(f"认不出直播间：{arg}（要直播间号、live.douyin.com 链接或主播主页链接）")
    return {"room_id": m.group(1), "sec_uid": None, "anchor": name or m.group(1)}


def live_state(sec_uid: str):
    """按主播 sec_uid 查开播状态（不需要登录）。返回 (状态, 直播间信息)，状态是：
    live（在播）、offline（没播：status_code=10011，或直播间 status 不是 2）、unknown（请求失败，不能下结论）。
    直播间信息里的 id_str 是这一场的内部房间号：主播下播再开播会换一个（EVO-031 推测，待验证）。"""
    url = ("https://webcast.amemv.com/webcast/room/reflow/info/?type_id=0&live_id=1&room_id=2"
           f"&sec_user_id={sec_uid}&version_code=99.99.99&app_id=1128")
    try:
        with NO_PROXY.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15) as r:
            d = json.loads(r.read())
    except (OSError, json.JSONDecodeError):
        return "unknown", {}
    room = (d.get("data") or {}).get("room") or {}
    if d.get("status_code") == 0 and room.get("status") == 2:
        return "live", room
    if d.get("status_code") == 10011 or (d.get("status_code") == 0 and room):
        return "offline", room
    return "unknown", {}


def _reflow(sec_uid: str) -> Dict[str, Any]:
    """在播时返回直播间信息，否则返回空字典。"""
    state, room = live_state(sec_uid)
    return room if state == "live" else {}


def resolve_live_room(sec_uid: str) -> Optional[str]:
    """在播返回直播间号（web_rid），没播返回 None。"""
    return (_reflow(sec_uid).get("owner") or {}).get("web_rid")


# 清晰度（接口 resolution_name 的叫法）：ORIGIN=原画，FULL_HD1=蓝光，HD1=超清，SD2=高清，SD1=标清。
# 原画不在 flv_pull_url 里，在 live_core_sdk_data.pull_data.stream_data 的 origin 项。
# 2026-11-24 实测（一个游戏直播间，各录 20 秒）：原画 1920x1080 60 帧约 6.2 Mbps（2.8 GB/时）；
# 超清 1280x720 60 帧约 2.9 GB/时；高清 1280x720 30 帧约 1.0 GB/时；纯音频（ao）约 0.09 GB/时。
# 码率随主播的推流设置变化，原画在别的直播间可能到 7～9 GB/时。
QUALITY_ORDER = ["ORIGIN", "FULL_HD1", "HD1", "SD2", "SD1"]  # 从高到低


def _sdk_streams(room: Dict[str, Any]) -> Dict[str, str]:
    """live_core_sdk_data 里各档的 flv 地址：origin、uhd、hd、sd、ld、md、ao（纯音频）。"""
    raw = (((room.get("stream_url") or {}).get("live_core_sdk_data") or {}).get("pull_data") or {}).get("stream_data")
    try:
        data = json.loads(raw)["data"] if raw else {}
    except (ValueError, KeyError, TypeError):
        return {}
    return {k: (v.get("main") or {}).get("flv") for k, v in data.items() if (v.get("main") or {}).get("flv")}


def quality_stream_url(sec_uid: str, quality: str) -> Optional[str]:
    """按指定清晰度取流；没有这一档就先往低找、再往高找。"""
    room = _reflow(sec_uid)
    flv = dict(((room.get("stream_url") or {}).get("flv_pull_url")) or {})
    origin = _sdk_streams(room).get("origin")
    if origin:
        flv["ORIGIN"] = origin
    i = QUALITY_ORDER.index(quality) if quality in QUALITY_ORDER else 0
    for q in QUALITY_ORDER[i:] + QUALITY_ORDER[:i][::-1]:
        if flv.get(q):
            return flv[q]
    return next(iter(flv.values()), None)
