"""每秒引擎：一个直播间一个，实时采集和离线回放共用（需求 8.3 节"两种模式共用同一套业务代码"）。

每秒做的事：把这一秒收到的消息聚合成一行信号 → 热度检测 → 报告"刚开始了一次异常"或"刚确认结束了一次异常"。
归因、保留录像、判定、出片由调用方处理：实时采集和离线回放在这之后的动作不一样。

以后声音、画面等感知通道也从这里喂进来（重构方案第 3、4 阶段）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .core.models import HeatEvent, RawMessage, SignalPoint
from .detect.episode import HeatDetector
from .perceive.chat import SignalAggregator


@dataclass
class Step:
    point: SignalPoint
    z: float  # 这一秒各通道里最大的 z 值
    opened: Optional[float]  # 这一秒刚开了一次异常：它的开始时刻
    event: Optional[HeatEvent]  # 这一秒刚确认结束的异常


class RoomEngine:
    def __init__(self, room_id: str, cfg: Dict[str, Any]):
        self.room_id = room_id
        self.agg = SignalAggregator(room_id, cfg)
        self.det = HeatDetector(room_id, cfg)

    def feed(self, msg: RawMessage) -> None:
        self.agg.feed(msg)

    def step(self, t: float) -> Step:
        pt = self.agg.close(t)
        z, ev = self.det.update(pt)
        return Step(point=pt, z=z, opened=self.det.pop_opened(), event=ev)

    def flush(self, now: float) -> Optional[HeatEvent]:
        """数据结束时把还开着的异常收尾。"""
        return self.det.flush(now)

    @property
    def z_smooth(self) -> float:
        """给调度用的平滑热度。"""
        return self.det.z_smooth
