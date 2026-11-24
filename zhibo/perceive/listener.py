"""实时声音感知线程：接收各直播间录制进程交来的 PCM（每次 1 秒），逐秒算声音感知，交给检测并落盘。

- 只有一个线程，各直播间轮流处理，所以识别模型和分类模型各只加载一份（约 600 MB），不会被多个线程同时调用；
  每个直播间、每段录制各有一套自己的状态（AudioPerceiver，模型用 clone() 共用）。
- sink() 由录制线程调用，**只放不等**：队列满了就丢掉这一秒并计数，绝不让录制线程卡住（卡住会连累画面录制）。
- 结果写进数据集的 perception/<room_id>.jsonl、transcript/<room_id>.jsonl，格式和离线 perceive 命令一样，
  所以事后离线回放能重现实时的检测。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np

from .hearing import AudioPerceiver
from .speech import SpeechTranscriber
from .tagger import SoundTagger


class LiveListener(threading.Thread):
    def __init__(self, cfg: Dict[str, Any], out: Path, stop: threading.Event, log,
                 on_row: Callable[[str, Dict[str, Any]], None], on_sentence: Callable[[str, Dict[str, Any]], None]):
        super().__init__(daemon=True)
        self.cfg, self.out, self.stop, self.log = cfg, Path(out), stop, log
        self.on_row, self.on_sentence = on_row, on_sentence
        p = cfg.get("perceive", {})
        self.q: "queue.Queue[Tuple[str, float, Optional[bytes]]]" = queue.Queue(maxsize=int(p.get("live_queue_seconds", 180)))
        models_dir = Path(p.get("models_dir", "models"))
        self.base = (SpeechTranscriber(models_dir, p), SoundTagger(models_dir, p))  # 在主线程加载，出错早暴露
        self.active: Dict[str, Tuple[float, AudioPerceiver]] = {}
        self.files: Dict[str, Tuple[Any, Any]] = {}
        self.dropped: Dict[str, int] = {}
        self.done: Dict[str, int] = {}
        self.work = 0.0
        (self.out / "perception").mkdir(parents=True, exist_ok=True)
        (self.out / "transcript").mkdir(parents=True, exist_ok=True)

    def sink(self, room_id: str, start: float, chunk: Optional[bytes]) -> None:
        try:
            self.q.put_nowait((room_id, start, chunk))
        except queue.Full:
            if chunk is not None:
                self.dropped[room_id] = self.dropped.get(room_id, 0) + 1

    def stats(self) -> Dict[str, Any]:
        return {"backlog_seconds": self.q.qsize(), "done": dict(self.done), "dropped": dict(self.dropped),
                "work_seconds": round(self.work, 1)}

    def _files(self, rid: str):
        if rid not in self.files:
            self.files[rid] = ((self.out / "perception" / f"{rid}.jsonl").open("a", encoding="utf-8"),
                               (self.out / "transcript" / f"{rid}.jsonl").open("a", encoding="utf-8"))
        return self.files[rid]

    def _emit(self, rid: str, rows, sents) -> None:
        fp, ft = self._files(rid)
        for r in rows:
            fp.write(json.dumps(r, ensure_ascii=False) + "\n")
            self.on_row(rid, r)
        for s in sents:
            ft.write(json.dumps(s, ensure_ascii=False) + "\n")
            self.on_sentence(rid, s)
        if rows or sents:
            fp.flush()
            ft.flush()
        self.done[rid] = self.done.get(rid, 0) + len(rows)

    def _finish(self, rid: str) -> None:
        if rid in self.active:
            _, hp = self.active.pop(rid)
            self._emit(rid, *hp.flush())

    def run(self) -> None:
        while not (self.stop.is_set() and self.q.empty()):
            try:
                rid, start, chunk = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            t0 = time.time()
            try:
                cur = self.active.get(rid)
                if chunk is None:
                    if cur and cur[0] == start:
                        self._finish(rid)
                    continue
                if cur is None or cur[0] != start:  # 新的一段录制：上一段收尾，换一套状态
                    self._finish(rid)
                    sp, tg = self.base
                    self.active[rid] = (start, AudioPerceiver(self.cfg, start, models=(sp.clone(), tg.clone())))
                x = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                self._emit(rid, *self.active[rid][1].feed(x))
            except Exception as e:  # noqa: BLE001  一秒算错不能让整个感知线程退出
                self.log(f"[感知] {rid} 处理出错：{e}")
            finally:
                self.work += time.time() - t0
        for rid in list(self.active):
            self._finish(rid)
        for fp, ft in self.files.values():
            fp.close()
            ft.close()
