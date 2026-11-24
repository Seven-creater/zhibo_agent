"""热度异常检测（M5 前半）：每个直播间维护自己的基线，按相对自身的偏离判断（FR-5.1）。

做法（demo 版）：几路信号各取最近 N 秒的滚动和，和自己的 EWMA 基线比 z 值；
z 连续几秒超过进入阈值就开一段异常，连续几秒低于退出阈值就结束。
"""
from __future__ import annotations

import math
from collections import Counter, deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from .models import HeatEvent, SignalPoint

CHANNEL_VALUE = {
    "chat_users": lambda pts: float(sum(p.chat_users for p in pts)),
    "laugh_users": lambda pts: float(sum(p.laugh_users for p in pts)),  # 搞笑方向的主信号
    "likes": lambda pts: math.log1p(sum(p.likes for p in pts)),  # 点赞是成批来的，取对数压一压
    "gift": lambda pts: math.log1p(sum(p.gift_value for p in pts)),
}


class HeatDetector:
    def __init__(self, room_id: str, cfg: Dict[str, Any]):
        h = cfg["heat"]
        self.room_id = room_id
        self.cfg = h
        self.channels: List[str] = list(h["channels"])
        self.window: Deque[SignalPoint] = deque(maxlen=int(h["rolling_seconds"]))
        self.history: Deque[SignalPoint] = deque(maxlen=int(h["max_seconds"]) + 120)
        self.mean: Dict[str, float] = {}
        self.var: Dict[str, float] = {}
        self.n = 0
        self.above = 0
        self.below = 0
        self.ep: Optional[Dict[str, Any]] = None
        self.z_smooth = 0.0  # 给调度用的平滑热度
        self.smooth_alpha = float(cfg["scheduler"]["heat_smooth_alpha"])
        self._seq = 0

    def _update_baseline(self, xs: Dict[str, float]) -> None:
        a = max(1.0 / self.n, float(self.cfg["ewma_alpha"]))
        for c, x in xs.items():
            if c not in self.mean:
                self.mean[c], self.var[c] = x, 0.0
                continue
            d = x - self.mean[c]
            self.mean[c] += a * d
            self.var[c] = (1 - a) * (self.var[c] + a * d * d)

    def _z(self, xs: Dict[str, float]) -> Dict[str, float]:
        floor = self.cfg["std_floor"]
        return {
            c: (x - self.mean[c]) / max(math.sqrt(self.var[c]), float(floor.get(c, 1.0))) for c, x in xs.items()
        }

    def update(self, p: SignalPoint) -> Tuple[float, Optional[HeatEvent]]:
        self.window.append(p)
        self.history.append(p)
        self.n += 1
        xs = {c: CHANNEL_VALUE[c](self.window) for c in self.channels}
        if self.n <= int(self.cfg["warmup_seconds"]):
            self._update_baseline(xs)
            return 0.0, None

        zs = self._z(xs)
        ch, zmax = max(zs.items(), key=lambda kv: kv[1])
        self.z_smooth = (1 - self.smooth_alpha) * self.z_smooth + self.smooth_alpha * max(zmax, 0.0)

        if self.ep is None:
            if zmax >= self.cfg["z_enter"]:
                self.above += 1
            else:
                self.above = 0
                if zmax < self.cfg["baseline_update_max_z"]:
                    self._update_baseline(xs)
            if self.above >= int(self.cfg["min_seconds"]):
                start = p.t - (self.above - 1)
                self.ep = {"start": start, "peak": p.t, "peak_z": zmax, "channel": ch, "peaks": dict(zs)}
                self.above = 0
            return zmax, None

        ep = self.ep
        if zmax > ep["peak_z"]:
            ep.update(peak=p.t, peak_z=zmax, channel=ch)
        for c, z in zs.items():
            ep["peaks"][c] = max(ep["peaks"].get(c, z), z)
        # 收尾只看主导信号：几路信号取最大值的话，任一路的噪声都会让异常拖着不结束
        self.below = self.below + 1 if zs[ep["channel"]] < self.cfg["z_exit"] else 0
        too_long = p.t - ep["start"] >= int(self.cfg["max_seconds"])
        if self.below >= int(self.cfg["exit_seconds"]) or too_long:
            end = p.t - self.below + 1 if not too_long else p.t
            return zmax, self._close(end, p.t)
        return zmax, None

    def flush(self, now: float) -> Optional[HeatEvent]:
        """直播结束时把还开着的异常收尾。"""
        if self.ep is None:
            return None
        return self._close(now, now)

    def _close(self, end: float, now: float) -> HeatEvent:
        ep = self.ep
        self.ep = None
        self.below = 0
        self._seq += 1
        pts = [q for q in self.history if ep["start"] - 30 <= q.t <= end]
        inside = [q for q in pts if q.t >= ep["start"]]
        users: set = set()
        texts: Counter = Counter()
        reactions: Counter = Counter()
        gifts: Counter = Counter()
        for q in inside:
            users |= q.detail.get("users", set())
            texts.update(q.detail.get("texts", {}))
            reactions.update(q.detail.get("reaction_texts", q.detail.get("texts", {})))
            gifts.update(q.detail.get("gift_by_user", {}))
        chat = sum(q.chat for q in inside)
        copies = sum(q.copy for q in inside)
        gift_total = float(sum(gifts.values()))
        top_user_value = max(gifts.values()) if gifts else 0.0
        specials = sorted({(s, q.t) for q in pts for s in q.specials}, key=lambda x: x[1])
        duration = end - ep["start"]
        stats = {
            "duration": duration,
            "chat": chat,
            "chat_users": len(users),
            "text_unique_ratio": round(len(texts) / chat, 3) if chat else 0.0,
            "laugh_ratio": round(sum(q.laugh for q in inside) / chat, 3) if chat else 0.0,
            "top_texts": [t for t, _ in reactions.most_common(5)],  # 不含复制粘贴的串
            "copy_ratio": round(copies / chat, 3) if chat else 0.0,
            "copy_lucky": sum(q.detail.get("copy_lucky", 0) for q in pts),
            "top_copies": [t for t, _ in (texts - reactions).most_common(3)],
            "gift_total": gift_total,
            "gift_top1_share": round(top_user_value / gift_total, 3) if gift_total else 0.0,
            "specials": [{"type": s, "t": t} for s, t in specials],
            "peak_z_by_channel": {c: round(z, 2) for c, z in ep["peaks"].items()},
        }
        return HeatEvent(
            event_id=f"{self.room_id}-ev{self._seq:03d}",
            room_id=self.room_id,
            start=ep["start"],
            peak=ep["peak"],
            end=end,
            peak_z=round(ep["peak_z"], 2),
            channel=ep["channel"],
            shape="sustained" if duration >= 45 else "spike",
            closed_at=now,
            stats=stats,
        )
