"""输入源（M2 采集、M3 录像）。

骨架只认两样东西：按接收时间排好序的 RawMessage 流，和"直播间 → 录像文件 + 录像起点时间"。
回放模式读冻结的数据集；实时模式以后换成 dyhub / DouyinBarrage + DouyinLiveRecorder 的适配层，
业务代码不用改（需求 8.3 节）。
"""
from __future__ import annotations

import heapq
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .models import RawMessage, Room


@dataclass
class Recording:
    room_id: str
    path: Path
    start_ts: float  # 录像第 0 秒对应的 epoch 时间（时钟对齐，FR-3.4）
    duration: float

    def rel(self, ts: float) -> float:
        return ts - self.start_ts


class ReplaySource:
    """离线回放：读取 data/<集>/ 下的冻结数据。"""

    def __init__(self, dataset_dir: Path):
        self.dir = Path(dataset_dir)
        rooms = json.loads((self.dir / "rooms.json").read_text(encoding="utf-8"))
        self.rooms: List[Room] = [Room(**r) for r in rooms]
        recs = json.loads((self.dir / "recordings.json").read_text(encoding="utf-8"))
        self.recordings: Dict[str, Recording] = {
            rid: Recording(rid, self.dir / r["path"], r["start_ts"], r["duration"]) for rid, r in recs.items()
        }

    def _room_stream(self, room_id: str) -> Iterator[RawMessage]:
        path = self.dir / "messages" / f"{room_id}.jsonl"
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield RawMessage(**json.loads(line))

    def messages(self) -> Iterator[RawMessage]:
        """所有直播间的消息按 recv_ts 归并成一条流。"""
        streams = [self._room_stream(r.room_id) for r in self.rooms]
        return heapq.merge(*streams, key=lambda m: (m.recv_ts, m.room_id))

    def recording(self, room_id: str) -> Optional[Recording]:
        return self.recordings.get(room_id)


class LiveSource:
    """实时模式占位。接 dyhub（WebSocket）或 DouyinBarrage 时在这里写适配层：
    1. 把原始消息转成 RawMessage（礼物字段未登录时标"不可用"，FR-2.3）；
    2. 观众 ID 加盐哈希（NFR-7）；
    3. 原始消息带接收时间落盘，供以后回放（FR-2.6）；
    4. 断线时段发 missing 标记（FR-2.7）。
    录像端接 DouyinLiveRecorder 的分段录制，并加环形缓冲（FR-3.2）。
    """

    def __init__(self, *_args, **_kwargs):
        raise NotImplementedError("实时采集还没接，先用 ReplaySource 跑离线回放")
