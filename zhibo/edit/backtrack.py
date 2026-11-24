"""事后回溯找边界（M6）。

1. 信号滞后于内容：起点 = 异常开始 - 信号滞后 - 铺垫（FR-6.1，滞后量以后从真实数据统计）；
2. 起止点吸附到程序算出来的自然边界上（demo 只有静音边界；以后加语音转写句子边界、镜头切换），
   每个切点都记下来源（FR-6.5）；
3. 时长约束（FR-6.4）；
4. 把片段按边界切成几小段 S1..Sn，交给模型只选"哪几段是铺垫/高潮/反应"，不让模型报时间戳。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from ..core.models import HeatEvent
from ..ingest.sources import Recording


@dataclass
class Plan:
    start: float
    end: float
    start_source: str
    end_source: str
    segments: List[Dict[str, Any]]


def _snap(desired: float, bounds: List[float], lo: float, hi: float) -> Tuple[float, str]:
    near = [b for b in bounds if desired - lo <= b <= desired + hi]
    if not near:
        return desired, "signal_estimate"  # 附近没有自然边界，退回信号估计值
    return min(near, key=lambda b: (abs(b - desired), b)), "silence"


def _segments(start: float, end: float, bounds: List[float], min_len: float = 8.0) -> List[Dict[str, Any]]:
    cuts = [start]
    for b in bounds:
        if cuts[-1] + min_len <= b <= end - min_len:
            cuts.append(b)
    cuts.append(end)
    return [
        {"id": f"S{i + 1}", "start": round(a, 3), "end": round(b, 3)} for i, (a, b) in enumerate(zip(cuts, cuts[1:]))
    ]


def plan_window(a: float, b: float, rec: Recording, bounds: List[float], sentence: List[float],
                cfg: Dict[str, Any]) -> Plan:
    """证据包第 2 版的宽窗口：[a, b] 由即时重放策略给出（act/replay.py），起点往前、终点往后吸附到最近的边界，
    再按边界切成至少 segment_min_seconds 秒的小段。sentence 里的边界来源记为 sentence，其余记为 silence。"""
    snap = float(cfg["judge"].get("window_snap_seconds", 5))
    sent = set(sentence)

    def pick(want: float, before: bool) -> Tuple[float, str]:
        lo, hi = (want - snap, want) if before else (want, want + snap)
        near = [x for x in bounds if lo <= x <= hi]
        if not near:
            return want, "replay_window"
        x = max(near) if before else min(near)
        return x, "sentence" if x in sent else "silence"

    start, s_src = pick(a, True)
    end, e_src = pick(b, False)
    rec_end = rec.start_ts + rec.duration
    if start < rec.start_ts:
        start, s_src = rec.start_ts, "recording_start"
    if end > rec_end:
        end, e_src = rec_end, "recording_end"
    segs = _segments(start, end, bounds, float(cfg["judge"].get("segment_min_seconds", 8)))
    return Plan(round(start, 3), round(end, 3), s_src, e_src, segs)


def plan(ev: HeatEvent, rec: Recording, bounds: List[float], cfg: Dict[str, Any]) -> Plan:
    b = cfg["backtrack"]
    lo, hi = float(b["snap_before"]), float(b["snap_after"])
    want_start = ev.start - float(b["signal_lag_seconds"]) - float(b["preroll_seconds"])
    want_end = ev.end + float(b["postroll_seconds"])
    start, s_src = _snap(want_start, bounds, lo, hi)  # 起点宁早勿晚
    end, e_src = _snap(want_end, bounds, hi, lo)  # 终点宁晚勿早

    mn, mx = float(b["min_seconds"]), float(b["max_seconds"])
    if end - start > mx:
        # 长事件：保留高潮附近，高潮前占 35%（以后可以拆成多个片段，FR-6.6）
        around_peak = ev.peak - float(b["signal_lag_seconds"]) - mx * 0.35
        if around_peak > start:
            start, s_src = _snap(around_peak, bounds, lo, hi)
        inside = [x for x in bounds if start + mx - 8 <= x <= start + mx]
        end, e_src = (max(inside), "silence(时长上限)") if inside else (start + mx, "max_duration")
    if end - start < mn:
        inside = [x for x in bounds if start + mn <= x <= start + mn + 8]
        end, e_src = (min(inside), "silence(时长下限)") if inside else (start + mn, "min_duration")

    rec_end = rec.start_ts + rec.duration
    if start < rec.start_ts:
        start, s_src = rec.start_ts, "recording_start"
    if end > rec_end:
        end, e_src = rec_end, "recording_end"
    return Plan(round(start, 3), round(end, 3), s_src, e_src, _segments(start, end, bounds))
