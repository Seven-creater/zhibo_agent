"""算力与资源调度（M4）。demo 版只调度"录制路数"这一项预算。

- fixed：开头按在线人数选前 K 间，之后不动（固定名单基线）。
- dynamic：每隔几秒按 log(在线人数) × (1 + 权重 × 平滑热度) 打分，分数明显更高的直播间替换掉正在录的。
所有升降都记成 Decision（FR-4.2），同一份输入回放两次结果一致（FR-4.7）。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from ..core.models import Decision, Room


class Scheduler:
    def __init__(self, rooms: Dict[str, Room], cfg: Dict[str, Any]):
        s = cfg["scheduler"]
        self.cfg = s
        self.rooms = rooms
        self.budget = int(s["record_budget"])
        self.online: Dict[str, int] = {}
        self.heat: Dict[str, float] = {}
        self.recording: Dict[str, float] = {}  # 正在录的直播间 → 开始录的时刻
        self.intervals: Dict[str, List[Tuple[float, float]]] = {r: [] for r in rooms}
        self.decisions: List[Decision] = []
        self._last_tick: Optional[float] = None
        self._started = False

    def observe(self, room_id: str, t: float, online: Optional[int], heat: float) -> None:
        if online is not None:
            self.online[room_id] = online
        self.heat[room_id] = heat

    def score(self, room_id: str) -> float:
        base = math.log1p(self.online.get(room_id, 0))
        h = min(self.heat.get(room_id, 0.0), float(self.cfg["heat_cap"]))
        return base * (1 + float(self.cfg["heat_weight"]) * h)

    def _start(self, rid: str, t: float, reason: str) -> None:
        self.recording[rid] = t
        self.decisions.append(Decision(t, rid, "record_start", reason))

    def _stop(self, rid: str, t: float, reason: str) -> None:
        self.intervals[rid].append((self.recording.pop(rid), t))
        self.decisions.append(Decision(t, rid, "record_stop", reason))

    def tick(self, t: float) -> None:
        if self._last_tick is not None and t - self._last_tick < float(self.cfg["tick_seconds"]):
            return
        self._last_tick = t
        live = [r for r in self.rooms if self.rooms[r].is_live(t) and r in self.online]
        for rid in list(self.recording):
            if rid not in live:
                self._stop(rid, t, "下播或没有数据")
        if not live:
            return
        if not self._started:
            self._started = True
            for rid in sorted(live, key=lambda r: (-self.online[r], r))[: self.budget]:
                self._start(rid, t, f"初始分配：在线 {self.online[rid]}，排前 {self.budget}")
            return
        if self.cfg["policy"] == "fixed":
            return
        hyst = float(self.cfg["hysteresis"])
        while True:
            outside = sorted((r for r in live if r not in self.recording), key=lambda r: (-self.score(r), r))
            if not outside:
                break
            best = outside[0]
            if len(self.recording) < self.budget:
                self._start(best, t, f"有空余录制路数，分数 {self.score(best):.1f}")
                continue
            worst = min(self.recording, key=lambda r: (self.score(r), r))
            if self.score(best) > hyst * self.score(worst):
                reason = (
                    f"{best} 分数 {self.score(best):.1f}（热度 {self.heat.get(best, 0):.1f}）"
                    f" > {hyst}× {worst} 的 {self.score(worst):.1f}"
                )
                self._stop(worst, t, "被替换：" + reason)
                self._start(best, t, "升级录制：" + reason)
            else:
                break

    def finalize(self, t: float) -> None:
        for rid in list(self.recording):
            self._stop(rid, t, "回放结束")

    def covered(self, room_id: str, start: float, end: float, now: float) -> Tuple[bool, str]:
        """候选时段是否完整落在录像（环形缓冲）里。"""
        spans = list(self.intervals.get(room_id, []))
        if room_id in self.recording:
            spans.append((self.recording[room_id], now))
        ring = float(self.cfg["ring_buffer_seconds"])
        if start < now - ring:
            return False, f"起点已超出环形缓冲（{ring:.0f} 秒）"
        for a, b in spans:
            if a <= start and end <= b + 1e-6:
                return True, ""
        if not spans:
            return False, "这段时间没有在录这个直播间"
        rec_from = max((a for a, b in spans if a <= end), default=None)
        if rec_from is not None and rec_from > start:
            return False, f"录制比候选起点晚 {rec_from - start:.0f} 秒才开始，前因没录到"
        return False, "这段时间没有在录这个直播间"

    def recorded_at(self, room_id: str, t: float) -> bool:
        return any(a <= t < b for a, b in self.intervals.get(room_id, []))

    def recorded_seconds(self) -> float:
        return sum(b - a for spans in self.intervals.values() for a, b in spans)
