"""每秒声音特征：音量、相对突变、短促爆发密度、噪声度。纯信号处理，不用模型。

改写自 clips-studio `analysis/audio_features.py`
（https://github.com/ColinGPT9/clips-studio ，提交 bd39d8b，AGPL-3.0）。改动：
- 原版一次处理整段视频；这里改成流式，每喂满 1 秒就出一行，实时采集和离线回放共用；
- 突变原版用以当前秒为中心的 30 秒中位数（要看到未来）；这里只用**之前** 30 秒的中位数，实时可用；
- "听得见"的门槛原版用整段平均音量的 30%；这里用到目前为止的平均音量的 30%；
- 另外输出 dBFS，方便人看和设门槛（frigate `frigate/events/audio.py` 的 `calculate_audio_levels` 也是这个算法，MIT）。

每行：sec（从流开始的第几秒）、db（dBFS）、rms、spike（本秒音量 / 之前 30 秒中位数，截到 0～5）、
burst（本秒内能量比上一帧跳到 2 倍以上的 50 毫秒帧数，笑声、掌声这类声音多）、zcr（过零率，越嘈杂越高，听不见时记 0）。
"""
from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List

import numpy as np

SAMPLE_RATE = 16000
FRAME = SAMPLE_RATE // 20  # 50 毫秒一帧
FRAMES_PER_SEC = SAMPLE_RATE // FRAME


class AudioFeatures:
    def __init__(self, spike_window: int = 30):
        self.buf = np.zeros(0, dtype=np.float32)
        self.sec = 0
        self.prev_frame_rms = 0.0
        self.history: Deque[float] = deque(maxlen=int(spike_window))
        self.loud_sum = 0.0

    def feed(self, x: np.ndarray) -> List[Dict[str, Any]]:
        """喂入 16 kHz 单声道 float32（-1～1）样本，返回这次凑满的每一秒。"""
        self.buf = np.concatenate([self.buf, x.astype(np.float32, copy=False)])
        rows = []
        while self.buf.size >= SAMPLE_RATE:
            sec, self.buf = self.buf[:SAMPLE_RATE], self.buf[SAMPLE_RATE:]
            rows.append(self._second(sec))
        return rows

    def _second(self, sec: np.ndarray) -> Dict[str, Any]:
        frames = sec.reshape(FRAMES_PER_SEC, FRAME)
        frame_rms = np.sqrt((frames ** 2).mean(axis=1))
        loud = float(frame_rms.mean())
        median = float(np.median(self.history)) if self.history else loud
        spike = float(np.clip(loud / max(median, 1e-6), 0, 5))
        prev = np.concatenate([[self.prev_frame_rms], frame_rms[:-1]])
        burst = int((frame_rms > 2.0 * np.maximum(prev, 1e-6)).sum())
        signs = np.signbit(frames)
        zcr = float((signs[:, 1:] != signs[:, :-1]).mean())
        self.loud_sum += loud
        audible = loud > max(self.loud_sum / (self.sec + 1) * 0.3, 1e-6)
        rms = float(np.sqrt((sec ** 2).mean()))
        row = {"sec": self.sec, "db": round(20 * np.log10(max(rms, 1e-6)), 1), "rms": round(rms, 5),
               "spike": round(spike, 3), "burst": burst, "zcr": round(zcr if audible else 0.0, 4)}
        self.history.append(loud)
        self.prev_frame_rms = float(frame_rms[-1])
        self.sec += 1
        return row
