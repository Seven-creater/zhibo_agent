"""声音感知总装：音量特征 + 说话检测/转写 + 声音分类，喂同一路 16 kHz 单声道样本，按秒合并输出。

实时采集（重构第 4 阶段接 PCM 管道）和离线补算（`python -m zhibo perceive`）共用这一个类。
输出两种记录，时间都是 epoch 秒：
- 每秒一行感知：t、db、rms、spike、burst、zcr、speech（说话占比）、laugh、scream、cheer、gun、music；
- 每句一行逐句稿：start、end、text。

需要项目 .venv（numpy、sherpa-onnx）和 models/ 里的模型。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .audio import AudioFeatures
from .speech import SAMPLE_RATE, SpeechTranscriber
from .tagger import SoundTagger


class AudioPerceiver:
    def __init__(self, cfg: Dict[str, Any], start_ts: float, models: Optional[Tuple[Any, Any]] = None):
        p = cfg.get("perceive", {})
        models_dir = Path(p.get("models_dir", "models"))
        self.start_ts = float(start_ts)
        self.features = AudioFeatures(int(p.get("spike_window", 30)))
        self.speech, self.tagger = models or (SpeechTranscriber(models_dir, p), SoundTagger(models_dir, p))
        self.speech.reset()
        self.tagger.reset()
        self.pending: Dict[int, Dict[str, Any]] = {}

    def feed(self, x: np.ndarray) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """喂入样本，返回（凑齐了的每秒感知，说完了的句子）。"""
        for r in self.features.feed(x):
            self._merge(r)
        for r in self.tagger.feed(x):
            self._merge(r)
        sentences = self.speech.feed(x)
        for sec, ratio in self.speech.pop_speech().items():
            self._merge({"sec": sec, "speech": ratio})
        return self._ready(), [self._abs(s) for s in sentences]

    def flush(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        sentences = self.speech.flush()
        for sec, ratio in self.speech.pop_speech().items():
            self._merge({"sec": sec, "speech": ratio})
        return self._ready(final=True), [self._abs(s) for s in sentences]

    def _merge(self, r: Dict[str, Any]) -> None:
        self.pending.setdefault(r["sec"], {}).update(r)

    def _ready(self, final: bool = False) -> List[Dict[str, Any]]:
        need = ("db", "laugh", "speech")
        done = sorted(k for k, v in self.pending.items() if final or all(n in v for n in need))
        rows = []
        for k in done:
            r = self.pending.pop(k)
            if "db" not in r:  # 收尾时不满 1 秒的零头
                continue
            r.setdefault("speech", 0.0)
            rows.append({"t": round(self.start_ts + r.pop("sec"), 3), **r})
        return rows

    def _abs(self, s: Dict[str, Any]) -> Dict[str, Any]:
        row = {"start": round(self.start_ts + s["start"], 3), "end": round(self.start_ts + s["end"], 3), "text": s["text"]}
        if s.get("ts"):  # 逐字时间，相对句子开头
            row["ts"] = s["ts"]
        return row


def _audio_sources(data: Path) -> Dict[str, List[Dict[str, Any]]]:
    """优先用整场音频（audio.json）；老数据集没有，就从录像（recordings.json）里取声音。"""
    for name in ("audio.json", "recordings.json"):
        f = data / name
        if f.exists():
            raw = json.loads(f.read_text(encoding="utf-8"))
            out = {rid: (v if isinstance(v, list) else [v]) for rid, v in raw.items()}
            if any(out.values()):
                return out
    return {}


def perceive_dataset(data: Path, cfg: Dict[str, Any], log=print) -> Dict[str, Any]:
    """给数据集补算声音感知，写 perception/<room_id>.jsonl 和 transcript/<room_id>.jsonl。"""
    data = Path(data)
    srcs = _audio_sources(data)
    if not srcs:
        raise SystemExit(f"{data} 里没有 audio.json 或 recordings.json，没有声音可分析")
    p = cfg.get("perceive", {})
    models_dir = Path(p.get("models_dir", "models"))
    t0 = time.time()
    models = (SpeechTranscriber(models_dir, p), SoundTagger(models_dir, p))
    log(f"[感知] 模型加载 {time.time() - t0:.1f} 秒")
    (data / "perception").mkdir(exist_ok=True)
    (data / "transcript").mkdir(exist_ok=True)
    summary = {}
    for rid, entries in srcs.items():
        work = 0.0
        n_rows = n_sent = 0
        with (data / "perception" / f"{rid}.jsonl").open("w", encoding="utf-8") as fp, \
                (data / "transcript" / f"{rid}.jsonl").open("w", encoding="utf-8") as ft:
            for e in sorted(entries, key=lambda e: e["start_ts"]):
                # 每段录制重新建一个（模型复用），时间从这段的第 0 秒算
                hp = AudioPerceiver(cfg, e["start_ts"], models=models)
                proc = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(data / e["path"]),
                                         "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
                                        stdout=subprocess.PIPE)
                t1 = time.time()
                while True:
                    chunk = proc.stdout.read(SAMPLE_RATE * 2)  # 每次 1 秒
                    if not chunk:
                        break
                    rows, sents = hp.feed(np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0)
                    for r in rows:
                        fp.write(json.dumps(r, ensure_ascii=False) + "\n")
                    for s in sents:
                        ft.write(json.dumps(s, ensure_ascii=False) + "\n")
                    n_rows += len(rows)
                    n_sent += len(sents)
                rows, sents = hp.flush()
                for r in rows:
                    fp.write(json.dumps(r, ensure_ascii=False) + "\n")
                for s in sents:
                    ft.write(json.dumps(s, ensure_ascii=False) + "\n")
                n_rows += len(rows)
                n_sent += len(sents)
                proc.wait()
                work += time.time() - t1
        summary[rid] = {"seconds": n_rows, "sentences": n_sent, "work_seconds": round(work, 1),
                        "realtime_x": round(n_rows / work, 1) if work else None}
        log(f"[感知] {rid}：{n_rows} 秒、{n_sent} 句，用时 {work:.1f} 秒（{summary[rid]['realtime_x']} 倍实时）")
    (data / "perception" / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
