"""声音分类：每秒用最近 2 秒的声音过一遍 AudioSet 527 类分类模型，按组取最高概率（笑声、喊叫、欢呼、枪炮、音乐）。

- 模型：sherpa-onnx 发布的 zipformer-small-audio-tagging-2024-04-15（k2-fsa 训练，Apache-2.0）；
  调用方式参考 sherpa-onnx 示例 `python-api-examples/audio-tagging-from-a-file.py`（Apache-2.0）。
- "音量过门槛才分类、只听关心的类别、按类别设阈值"的做法来自 frigate `frigate/events/audio.py`
  （https://github.com/blakeblackshear/frigate ，提交 d20d6dc，MIT）：太安静的窗口直接记 0，省算力，也少误报。
- 认的是直播流里的混音，主要是主播这一侧的笑声、喊叫，以及游戏音效；观众的笑只体现在弹幕里。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import numpy as np

SAMPLE_RATE = 16000
TAGGER_DIR = "sherpa-onnx-zipformer-small-audio-tagging-2024-04-15"

# AudioSet 类名（见模型目录的 class_labels_indices.csv）→ 我们关心的组
GROUPS: Dict[str, List[str]] = {
    "laugh": ["Laughter", "Baby laughter", "Giggle", "Snicker", "Belly laugh", "Chuckle, chortle"],
    "scream": ["Shout", "Whoop", "Yell", "Battle cry", "Children shouting", "Screaming"],
    "cheer": ["Clapping", "Cheering", "Applause", "Crowd"],
    "gun": ["Explosion", "Gunshot, gunfire", "Machine gun", "Fusillade", "Artillery fire", "Cap gun", "Boom", "Bang"],
    "music": ["Music"],
}


class SoundTagger:
    def __init__(self, models_dir: Path, cfg: Dict[str, Any]):
        import sherpa_onnx  # 只在用到声音感知时才需要（项目 .venv）

        d = Path(models_dir) / cfg.get("tagger_dir", TAGGER_DIR)
        self.at = sherpa_onnx.AudioTagging(sherpa_onnx.AudioTaggingConfig(
            model=sherpa_onnx.AudioTaggingModelConfig(
                zipformer=sherpa_onnx.OfflineZipformerAudioTaggingModelConfig(model=str(d / "model.int8.onnx")),
                num_threads=int(cfg.get("tagger_threads", 1))),
            labels=str(d / "class_labels_indices.csv"), top_k=int(cfg.get("tagger_top_k", 60))))
        self.window = int(float(cfg.get("tag_window_seconds", 2)) * SAMPLE_RATE)
        self.min_db = float(cfg.get("tag_min_db", -50))
        self.label_group = {name: g for g, names in GROUPS.items() for name in names}
        self.reset()

    def reset(self) -> None:
        """换一路新的样本流：缓冲和秒数清零，模型不重新加载。"""
        self.buf = np.zeros(0, dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)
        self.sec = 0

    def feed(self, x: np.ndarray) -> List[Dict[str, Any]]:
        """喂入 16 kHz 单声道 float32 样本，每凑满 1 秒，用"截至这一秒末的最近 2 秒"分类一次。"""
        self.pending = np.concatenate([self.pending, x.astype(np.float32, copy=False)])
        rows = []
        while self.pending.size >= SAMPLE_RATE:
            one, self.pending = self.pending[:SAMPLE_RATE], self.pending[SAMPLE_RATE:]
            self.buf = np.concatenate([self.buf, one])[-self.window:]
            rows.append(self._tag())
            self.sec += 1
        return rows

    def _tag(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {"sec": self.sec, **{g: 0.0 for g in GROUPS}}
        rms = float(np.sqrt((self.buf ** 2).mean())) if self.buf.size else 0.0
        if 20 * np.log10(max(rms, 1e-6)) < self.min_db:
            return row
        s = self.at.create_stream()
        s.accept_waveform(SAMPLE_RATE, self.buf)
        for e in self.at.compute(s):
            g = self.label_group.get(e.name)
            if g and e.prob > row[g]:
                row[g] = round(float(e.prob), 3)
        return row
