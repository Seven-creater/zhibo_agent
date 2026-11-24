"""每秒引擎：一个直播间一个，实时采集和离线回放共用（需求 8.3 节"两种模式共用同一套业务代码"）。

每秒做的事：把这一秒收到的弹幕聚合成一行信号，挂上这一秒的声音感知 → 热度检测 → 报告"刚开始了一次异常"
或"刚确认结束了一次异常"。归因、保留录像、判定、出片由调用方处理：实时采集和离线回放在这之后的动作不一样。

检测器由 heat.kind 选：
- chat_z（默认）：只看弹幕类信号，多路取最大 z 值（detect/episode.py）；
- fused：弹幕和声音多路融合（detect/fuse.py）。只有这一种会用到声音感知。

声音比弹幕早（主播先笑、观众后刷），所以声音感知按 fuse.audio_lead_seconds 往后挪再和弹幕对齐：
第 t 秒的信号挂的是第 t − lead 秒的声音。异常的时间因此是"弹幕时间"，回溯找边界时再按信号滞后往前推。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .core.models import HeatEvent, RawMessage, SignalPoint
from .detect.episode import HeatDetector
from .detect.fuse import FusedDetector
from .perceive.chat import SignalAggregator


@dataclass
class Step:
    point: SignalPoint
    z: float  # 这一秒的检测分：chat_z 是各通道最大 z 值，fused 是融合分
    opened: Optional[float]  # 这一秒刚开了一次异常：它的开始时刻
    event: Optional[HeatEvent]  # 这一秒刚确认结束的异常


class RoomEngine:
    def __init__(self, room_id: str, cfg: Dict[str, Any]):
        self.room_id = room_id
        self.agg = SignalAggregator(room_id, cfg)
        self.kind = cfg["heat"].get("kind", "chat_z")
        self.det = FusedDetector(room_id, cfg) if self.kind == "fused" else HeatDetector(room_id, cfg)
        self.lead = int(cfg.get("fuse", {}).get("audio_lead_seconds", 0))
        self.audio: Dict[int, Dict[str, Any]] = {}

    def feed(self, msg: RawMessage) -> None:
        self.agg.feed(msg)

    def feed_audio(self, row: Optional[Dict[str, Any]]) -> None:
        """喂一秒声音感知（perceive/hearing.py 的输出行，t 为 epoch 秒）。"""
        if row and self.kind == "fused":
            self.audio[int(row["t"])] = row

    def step(self, t: float) -> Step:
        pt = self.agg.close(t)
        if self.kind == "fused":
            src = int(t) - self.lead
            row = self.audio.pop(src, None)
            for k in [k for k in self.audio if k < src]:
                del self.audio[k]
            if row:
                pt.audio = {k: v for k, v in row.items() if k != "t"}
        z, ev = self.det.update(pt)
        return Step(point=pt, z=z, opened=self.det.pop_opened(), event=ev)

    def flush(self, now: float) -> Optional[HeatEvent]:
        """数据结束时把还开着的异常收尾。"""
        return self.det.flush(now)

    @property
    def z_smooth(self) -> float:
        """给调度用的平滑热度。"""
        return self.det.z_smooth
