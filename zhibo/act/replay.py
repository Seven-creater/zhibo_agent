"""即时重放的保留策略（EVO-012）：异常一开始先占住录像，确认结束后按归因定稿或释放。

- 开始时：占住"开始时刻往前 replay_seconds 秒起、终点未定"的录像，免得等结束的这段时间里被环形缓冲删掉；
- 结束时：原因在 pin_causes 里（默认只有内容驱动），就把这段改成
  [min(确认时刻 − replay_seconds, 开始 − pin_pre_seconds), max(确认时刻, 结束 + pin_post_seconds)]；否则释放。
缓冲只要盖住 replay_seconds 加上检测延迟的余量，和异常持续多久无关。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..core.models import HeatEvent


class ReplayPolicy:
    def __init__(self, cfg: Dict[str, Any]):
        live = cfg.get("live", {})
        self.pin_causes = set(live.get("pin_causes", ["content"]))
        self.pin_pre = float(live.get("pin_pre_seconds", 60))
        self.pin_post = float(live.get("pin_post_seconds", 20))
        self.replay = float(live.get("replay_seconds", 300))

    def keep_seconds(self, full_record: bool) -> Optional[float]:
        """环形缓冲要保留多久；全程录制时返回 None。"""
        return None if full_record else self.replay + 60

    def hold_start(self, opened: float) -> float:
        """异常刚开始时，先占住的录像从哪一刻起。"""
        return opened - self.replay

    def keeps(self, cause: str) -> bool:
        return cause in self.pin_causes

    def window(self, ev: HeatEvent) -> Tuple[float, float]:
        """确认要保留时，最终保留的录像区间。"""
        return min(ev.closed_at - self.replay, ev.start - self.pin_pre), max(ev.closed_at, ev.end + self.pin_post)
