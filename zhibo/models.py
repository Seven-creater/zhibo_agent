"""核心数据对象（需求文档 8.2 节）。字段先够 demo 用，设计阶段再细化。

时间统一用 epoch 秒（float）。回放和实时都用消息的接收时间 recv_ts 作为时钟。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class Room:
    room_id: str
    anchor: str
    category: str
    source: str  # manual / hot_list / follow
    start_ts: float  # 开播时间
    end_ts: Optional[float] = None  # 下播时间，未知为 None
    stream_start_known: bool = True  # False = start_ts 只是开始采集的时刻，不是真实开播时间

    def is_live(self, t: float) -> bool:
        return t >= self.start_ts and (self.end_ts is None or t < self.end_ts)


@dataclass
class RawMessage:
    """采集适配层输出的消息。payload 保留原始内容，回放时逐条重放。"""

    room_id: str
    recv_ts: float
    type: str  # chat / gift / like / enter / follow / room_stats / lucky_bag / red_packet / pk / room_status
    payload: Dict[str, Any]


@dataclass
class SignalPoint:
    """一个直播间一秒的聚合信号。detail 只在内存里用（给归因看），不落盘。"""

    room_id: str
    t: float
    chat: int = 0
    chat_users: int = 0
    laugh: int = 0
    laugh_users: int = 0  # 发笑点弹幕的不同用户数（防同一人刷屏）
    gift_value: float = 0.0  # 去重后的钻石价值
    likes: int = 0
    enters: int = 0
    online: Optional[int] = None
    specials: List[str] = field(default_factory=list)  # 本秒出现的福袋、红包、PK 等
    missing: bool = False  # True = 这一秒没收到（断线），不是"没有消息"
    detail: Dict[str, Any] = field(default_factory=dict, repr=False)

    def to_row(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("detail", None)
        return d


@dataclass
class HeatEvent:
    event_id: str
    room_id: str
    start: float
    peak: float
    end: float
    peak_z: float
    channel: str  # 偏离最大的信号
    shape: str  # spike / sustained
    closed_at: float  # 系统"知道"这次异常结束的时刻
    stats: Dict[str, Any]


@dataclass
class Attribution:
    event_id: str
    cause: str  # content / lucky_bag / pk / campaign / single_user_gift / start_end / chat_spam / unknown
    confidence: float
    evidence: List[str]


@dataclass
class Decision:
    t: float
    room_id: str
    action: str  # record_start / record_stop
    reason: str


@dataclass
class Candidate:
    cand_id: str
    room_id: str
    event_id: str
    cause: str
    start: float
    end: float
    start_source: str  # 切点来自哪个程序计算的边界（FR-6.5）
    end_source: str
    created_at: float  # 回放时钟下的产出时刻，用来算端到端时延
    status: str  # accepted / rejected / lost_not_recording / duplicate / judge_failed / deferred
    note: str = ""
    judge: Optional[Dict[str, Any]] = None
    clip: Optional[Dict[str, Any]] = None


@dataclass
class GoldLabel:
    room_id: str
    start: float
    end: float
    reason: str
    has_signal_peak: bool = True
    source: str = "synthetic"


def write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            if hasattr(r, "to_row"):
                r = r.to_row()
            elif hasattr(r, "__dataclass_fields__"):
                r = asdict(r)
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
