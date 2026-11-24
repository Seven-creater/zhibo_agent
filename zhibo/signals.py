"""信号聚合（M2 → SignalSeries）：把消息流按秒聚合，礼物去重计价（FR-2.2）。"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Optional, Tuple

from .models import RawMessage, SignalPoint

SPECIAL_TYPES = {"lucky_bag", "red_packet", "pk"}


class SignalAggregator:
    def __init__(self, room_id: str, cfg: Dict[str, Any]):
        self.room_id = room_id
        self.laugh_words = tuple(cfg["signals"]["laugh_words"])
        self._combo: Dict[Tuple[str, str, str], int] = {}  # (用户, 礼物, 分组) → 已计过的连击数
        self._online: Optional[int] = None
        self._reset()

    def _reset(self) -> None:
        self._chat = 0
        self._users: set = set()
        self._texts: Counter = Counter()
        self._laugh = 0
        self._gift_by_user: Counter = Counter()
        self._likes = 0
        self._enters = 0
        self._specials: list = []

    def feed(self, msg: RawMessage) -> None:
        p = msg.payload
        if msg.type == "chat":
            text = p.get("text", "")
            self._chat += 1
            self._users.add(p.get("user"))
            self._texts[text] += 1
            if any(w in text for w in self.laugh_words):
                self._laugh += 1
        elif msg.type == "gift":
            # 同一次送礼会推多条：按 (用户, 礼物 ID, 分组 ID) 合并，只取连击数的增量
            key = (p.get("user"), str(p.get("gift_id")), str(p.get("group_id")))
            combo = int(p.get("combo", 1))
            prev = self._combo.get(key, 0)
            if combo > prev:
                self._combo[key] = combo
                self._gift_by_user[p.get("user")] += (combo - prev) * float(p.get("diamond", 0))
        elif msg.type == "like":
            self._likes += int(p.get("count", 1))
        elif msg.type == "enter":
            self._enters += 1
        elif msg.type == "room_stats":
            self._online = int(p.get("online", 0))
        elif msg.type in SPECIAL_TYPES:
            self._specials.append(msg.type)

    def close(self, t: float) -> SignalPoint:
        """结束一秒，返回这一秒的信号点。"""
        pt = SignalPoint(
            room_id=self.room_id,
            t=t,
            chat=self._chat,
            chat_users=len(self._users),
            laugh=self._laugh,
            gift_value=float(sum(self._gift_by_user.values())),
            likes=self._likes,
            enters=self._enters,
            online=self._online,
            specials=list(self._specials),
            detail={
                "users": set(self._users),
                "texts": Counter(self._texts),
                "gift_by_user": Counter(self._gift_by_user),
            },
        )
        self._reset()
        return pt
