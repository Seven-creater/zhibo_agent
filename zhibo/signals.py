"""信号聚合（M2 → SignalSeries）：把消息流按秒聚合，礼物去重计价（FR-2.2），识别复制粘贴的"串"。"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Optional, Tuple

from .models import RawMessage, SignalPoint

SPECIAL_TYPES = {"lucky_bag", "red_packet", "pk"}
_STRIP = re.compile(r"\[[^\[\]]{1,8}\]|（单击可串）|\(单击可串\)|[\s\W_]", re.UNICODE)


def normalize_text(text: str) -> str:
    """去掉 [捂脸] 这类表情占位、标点、空白和 emoji，用来判断两条弹幕是不是同一句。"""
    return _STRIP.sub("", text)


class SignalAggregator:
    def __init__(self, room_id: str, cfg: Dict[str, Any]):
        s = cfg["signals"]
        self.room_id = room_id
        self.laugh_words = tuple(s["laugh_words"])
        self.copy_min_len = int(s.get("copy_min_len", 10))
        self.copy_min_users = int(s.get("copy_min_users", 3))
        self.copy_window = float(s.get("copy_window_seconds", 60))
        self.lucky_hints = tuple(s.get("lucky_hints", ["福袋", "中一次", "口令", "抽奖"]))
        self._combo: Dict[Tuple[str, str, str], int] = {}  # (用户, 礼物, 分组) → 已计过的连击数
        self._seen: Dict[str, Dict[Any, float]] = {}  # 规范化文本 → {用户: 最近发送时刻}，用来识别"串"
        self._last_prune = 0.0
        self._online: Optional[int] = None
        self._reset()

    def _reset(self) -> None:
        self._chat = 0
        self._users: set = set()
        self._texts: Counter = Counter()
        self._reaction_texts: Counter = Counter()  # 不含"串"的弹幕，给归因和标题用
        self._laugh = 0
        self._laugh_users: set = set()
        self._copy = 0
        self._copy_lucky = 0
        self._gift_by_user: Counter = Counter()
        self._likes = 0
        self._enters = 0
        self._specials: list = []

    def _is_copypasta(self, key: str, user: Any, t: float) -> bool:
        """一句够长的话，在最近 copy_window 秒里被 copy_min_users 个以上不同的人发过，就当作复制粘贴的"串"或口令。
        "哈哈哈哈哈哈哈哈"这种只由一两个字重复组成的不算。"""
        senders = self._seen.setdefault(key, {})
        senders[user] = t
        if t - self._last_prune > 30:
            self._last_prune = t
            for k in list(self._seen):
                self._seen[k] = {u: ts for u, ts in self._seen[k].items() if t - ts <= self.copy_window}
                if not self._seen[k]:
                    del self._seen[k]
        if len(key) < self.copy_min_len or len(set(key)) <= 2:
            return False
        recent = sum(1 for ts in senders.values() if t - ts <= self.copy_window)
        return recent >= self.copy_min_users

    def feed(self, msg: RawMessage) -> None:
        p = msg.payload
        if msg.type == "chat":
            text = p.get("text", "") or ""
            user = p.get("user")
            self._chat += 1
            self._users.add(user)
            self._texts[text] += 1
            if self._is_copypasta(normalize_text(text), user, msg.recv_ts):
                self._copy += 1
                if any(w in text for w in self.lucky_hints):
                    self._copy_lucky += 1
                return  # 串不算观众的真实反应，不计入笑点
            self._reaction_texts[text] += 1
            if any(w in text for w in self.laugh_words):
                self._laugh += 1
                self._laugh_users.add(user)
        elif msg.type == "gift":
            # 同一次送礼会推多条：按 (用户, 礼物 ID, 分组 ID) 合并，只取连击数的增量
            key = (p.get("user"), str(p.get("gift_id")), str(p.get("group_id")))
            combo = int(p.get("combo") or 1)
            prev = self._combo.get(key, 0)
            if combo > prev:
                self._combo[key] = combo
                self._gift_by_user[p.get("user")] += (combo - prev) * float(p.get("diamond") or 0)
        elif msg.type == "like":
            self._likes += int(p.get("count") or 1)
        elif msg.type == "enter":
            self._enters += 1
        elif msg.type == "room_stats":
            if p.get("online") is not None:
                self._online = int(p["online"])
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
            laugh_users=len(self._laugh_users),
            copy=self._copy,
            gift_value=float(sum(self._gift_by_user.values())),
            likes=self._likes,
            enters=self._enters,
            online=self._online,
            specials=list(self._specials),
            detail={
                "users": set(self._users),
                "texts": Counter(self._texts),
                "reaction_texts": Counter(self._reaction_texts),
                "copy_lucky": self._copy_lucky,
                "gift_by_user": Counter(self._gift_by_user),
            },
        )
        self._reset()
        return pt
