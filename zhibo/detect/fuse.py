"""融合检测（重构第 3 阶段）：弹幕和声音多路信号合成一个分，再判断异常的开始和结束。

每秒：
1. 每路信号先变成 0～1 的"激活度"：
   - 弹幕类和音量类（z 通道）：和这个直播间自己的 EWMA 基线比 z 值，z 在 z_low～z_high 之间线性映射到 0～1；
   - 声音分类的概率（概率通道，如笑声）：最近 rolling_seconds 秒的最大概率，在 p_low～p_high 之间映射到 0～1。
2. 按品类权重加权求和。权重的思路来自 hotclip `src/core/highlight/moments.ts` 的 `MOMENT_WEIGHTS.reaction`
   （https://github.com/xixihhhh/hotclip ，提交 3543584，AGPL-3.0）：游戏、户外、聊天这类"反应型"直播，
   语气、笑声、弹幕权重高，画面权重低。
3. 共振加成：同一秒激活的通道每多一路，总分乘 (1 + agreement_bonus)。hotclip 的说法是"只有音量高可能是 BGM，
   只有弹幕高可能是刷屏，几路一起亮才是真爆点"（`MOMENT_AGREEMENT_BONUS = 0.25`）。
4. 饱和降权：某一路在最近 saturation_window 秒里激活的时间超过 saturation_ratio，权重乘 saturation_discount。
   hotclip 的 `MOMENT_SATURATION = 0.55`：一路信号全程都亮，就不再有区分度（实测一乐店长直播间音乐 438/594 秒都在响）。
5. 融合分连续 min_seconds 秒 ≥ enter 就开始一次异常；之后连续 exit_seconds 秒 < exit 才结束（沿用 EVO-012 的
   "宁晚勿早"），最长 max_seconds。

各参数都在配置 fuse 一节，现在的数值是起步值，要用真实数据调（【待验证】）。
"""
from __future__ import annotations

import copy

from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

from ..core.models import HeatEvent, SignalPoint
from .episode import CHANNEL_VALUE, HeatDetector

# 音量类声音通道：取最近 rolling_seconds 秒的值，和基线比 z
AUDIO_Z_VALUE = {
    "a_spike": lambda pts: sum(p.audio.get("spike", 1.0) for p in pts) / max(len(pts), 1),  # 相对之前 30 秒变响多少
    "a_burst": lambda pts: float(sum(p.audio.get("burst", 0) for p in pts)),  # 短促爆发（笑声、掌声多）
}
# 声音分类通道：取最近 rolling_seconds 秒里的最大概率
AUDIO_PROB_KEY = {"a_laugh": "laugh", "a_cheer": "cheer", "a_gun": "gun", "a_music": "music"}


def _clip01(x: float) -> float:
    return 0.0 if x <= 0 else 1.0 if x >= 1 else x


class FusedDetector(HeatDetector):
    def __init__(self, room_id: str, cfg: Dict[str, Any]):
        f = cfg["fuse"]
        heat = copy.deepcopy(cfg["heat"])
        heat["std_floor"] = {**heat.get("std_floor", {}), **f.get("std_floor", {})}
        super().__init__(room_id, {**cfg, "heat": heat})
        self.f = f
        self.weights: Dict[str, float] = {c: float(w) for c, w in f["weights"].items() if float(w) > 0}
        self.z_channels = [c for c in self.weights if c in CHANNEL_VALUE or c in AUDIO_Z_VALUE]
        self.prob_channels = [c for c in self.weights if c in AUDIO_PROB_KEY]
        unknown = set(self.weights) - set(self.z_channels) - set(self.prob_channels)
        if unknown:
            raise ValueError(f"fuse.weights 里有不认识的通道：{sorted(unknown)}")
        self.channels = self.z_channels  # 基线和 z 值只算 z 通道
        self.lit: Dict[str, Deque[bool]] = {c: deque(maxlen=int(f["saturation_window"])) for c in self.weights}
        self.held: Dict[str, Deque[float]] = {c: deque(maxlen=max(1, int(f.get("hold_seconds", 1))))
                                              for c in self.weights}
        self.last: Dict[str, Any] = {}  # 最近一秒的激活度和贡献，调试和报告用

    def _values(self) -> Dict[str, float]:
        out = {}
        for c in self.z_channels:
            fn = CHANNEL_VALUE.get(c) or AUDIO_Z_VALUE[c]
            out[c] = fn(self.window)
        return out

    def _activation(self, zs: Dict[str, float]) -> Dict[str, float]:
        f = self.f
        zl, zh = float(f["z_low"]), float(f["z_high"])
        pl, ph = float(f["p_low"]), float(f["p_high"])
        act = {c: _clip01((zs[c] - zl) / (zh - zl)) for c in self.z_channels}
        for c in self.prob_channels:
            p = max((q.audio.get(AUDIO_PROB_KEY[c], 0.0) for q in self.window), default=0.0)
            act[c] = _clip01((p - pl) / (ph - pl))
        # 保持最近 hold_seconds 秒的最大激活度：不同通道的峰不会正好落在同一秒
        # （ring_pin 实测：主播笑在先，观众的笑点弹幕晚了约 10 秒），不保持就叠不起来
        for c, a in act.items():
            self.held[c].append(a)
        return {c: max(self.held[c]) for c in act}

    def _score(self, act: Dict[str, float]) -> Tuple[float, str, Dict[str, float]]:
        f = self.f
        on = float(f["on"])
        contrib = {}
        for c, w in self.weights.items():
            hist = self.lit[c]
            hist.append(act[c] >= on)
            saturated = len(hist) >= 60 and sum(hist) / len(hist) > float(f["saturation_ratio"])
            contrib[c] = w * (float(f["saturation_discount"]) if saturated else 1.0) * act[c]
        k = sum(1 for c in contrib if act[c] >= on)
        score = sum(contrib.values()) * (1 + float(f["agreement_bonus"]) * max(0, k - 1))
        ch = max(contrib, key=lambda c: contrib[c])
        return score, ch, contrib

    def update(self, p: SignalPoint) -> Tuple[float, Optional[HeatEvent]]:
        self.window.append(p)
        self.history.append(p)
        self.n += 1
        xs = self._values()
        if self.n <= int(self.cfg["warmup_seconds"]):
            self._update_baseline(xs)
            return 0.0, None
        zs = self._z(xs)
        act = self._activation(zs)
        score, ch, contrib = self._score(act)
        self.last = {"score": round(score, 3), "act": {c: round(a, 3) for c, a in act.items() if a > 0},
                     "contrib": {c: round(v, 3) for c, v in contrib.items() if v > 0}}
        self.z_smooth = (1 - self.smooth_alpha) * self.z_smooth + self.smooth_alpha * score

        if self.ep is None:
            if score >= float(self.f["enter"]):
                self.above += 1
            else:
                self.above = 0
                if score < float(self.f["baseline_update_max"]):
                    self._update_baseline(xs)
            if self.above >= int(self.f["min_seconds"]):
                start = p.t - (self.above - 1)
                self.ep = {"start": start, "peak": p.t, "peak_z": score, "channel": ch, "peaks": dict(zs),
                           "laugh_best": (zs.get("laugh_users", float("-inf")), p.t), "act_peaks": dict(act)}
                self.above = 0
                self._opened = start
            return score, None

        ep = self.ep
        if score > ep["peak_z"]:
            ep.update(peak=p.t, peak_z=score, channel=ch)
        for c, z in zs.items():
            ep["peaks"][c] = max(ep["peaks"].get(c, z), z)
        for c, a in act.items():
            ep["act_peaks"][c] = max(ep["act_peaks"].get(c, a), a)
        if zs.get("laugh_users", float("-inf")) > ep["laugh_best"][0]:
            ep["laugh_best"] = (zs["laugh_users"], p.t)
        self.below = self.below + 1 if score < float(self.f["exit"]) else 0
        too_long = p.t - ep["start"] >= int(self.cfg["max_seconds"])
        if self.below >= int(self.cfg["exit_seconds"]) or too_long:
            end = p.t - self.below + 1 if not too_long else p.t
            acts = ep["act_peaks"]
            ev = self._close(end, p.t)
            ev.stats["act_peaks"] = {c: round(a, 3) for c, a in acts.items() if a > 0}
            return score, ev
        return score, None

    def flush(self, now: float) -> Optional[HeatEvent]:
        if self.ep is None:
            return None
        acts = self.ep["act_peaks"]
        ev = self._close(now, now)
        ev.stats["act_peaks"] = {c: round(a, 3) for c, a in acts.items() if a > 0}
        return ev
