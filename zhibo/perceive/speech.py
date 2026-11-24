"""主播说的话：说话检测（silero VAD）切出一句一句，每句交给 SenseVoice 转写，得到带时间的逐句稿。

做法来自 sherpa-onnx 的示例 `python-api-examples/simulate-streaming-sense-voice-microphone.py`
（https://github.com/k2-fsa/sherpa-onnx ，Apache-2.0，Copyright (c) 2025 Xiaomi Corporation）：
VAD 每次吃一个窗口（512 个样本），一句话结束就取出整句送进非流式的 SenseVoice。改动：
- 输入从麦克风换成任意来源的样本流（直播 PCM 管道或离线解码），时间用样本数换算，和墙上时钟无关；
- 参数按直播调整：句间停顿 0.3 秒、单句最长 15 秒（示例是 0.1 秒、8 秒，适合对话交互）；
- 只要文字。SenseVoice 的情绪和事件标签实测没有区分度（EVO-013：355 句全是中性/说话），不用。

模型文件见 scripts/fetch_models.sh；第一次调用时加载（约 240 MB 内存）。
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

SAMPLE_RATE = 16000
SENSE_VOICE_DIR = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
VAD_FILE = "silero_vad_v5.onnx"


class SpeechTranscriber:
    def __init__(self, models_dir: Path, cfg: Dict[str, Any]):
        import sherpa_onnx  # 只在用到声音感知时才需要（项目 .venv）

        models_dir = Path(models_dir)
        sv = models_dir / cfg.get("sense_voice_dir", SENSE_VOICE_DIR)
        self.rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(sv / "model.int8.onnx"), tokens=str(sv / "tokens.txt"),
            num_threads=int(cfg.get("num_threads", 2)), use_itn=True, language=cfg.get("language", "zh"))
        vc = sherpa_onnx.VadModelConfig()
        vc.silero_vad.model = str(models_dir / cfg.get("vad_file", VAD_FILE))
        vc.silero_vad.threshold = float(cfg.get("vad_threshold", 0.5))
        vc.silero_vad.min_silence_duration = float(cfg.get("min_silence_seconds", 0.3))
        vc.silero_vad.min_speech_duration = float(cfg.get("min_speech_seconds", 0.25))
        vc.silero_vad.max_speech_duration = float(cfg.get("max_speech_seconds", 15))
        vc.sample_rate = SAMPLE_RATE
        self.window = vc.silero_vad.window_size
        self._vc, self._mk_vad = vc, sherpa_onnx.VoiceActivityDetector
        self.reset()

    def clone(self) -> "SpeechTranscriber":
        """给另一路样本流用的新实例：共用已加载的识别模型，VAD 和计数各自独立。
        同一时刻只能有一个线程调用识别模型（实时采集时由单个感知线程轮流处理各直播间）。"""
        c = copy.copy(self)
        c.reset()
        return c

    def reset(self) -> None:
        """换一路新的样本流（例如断流后的新一段录制）：VAD 状态和计数清零，模型不重新加载。"""
        self.vad = self._mk_vad(self._vc, buffer_size_in_seconds=60)
        self.buf = np.zeros(0, dtype=np.float32)
        self.pos = 0  # 已经交给 VAD 的样本数
        self.speech: Dict[int, List[int]] = {}  # 第几秒 → [说话的窗口数, 窗口总数]

    def feed(self, x: np.ndarray) -> List[Dict[str, Any]]:
        """喂入 16 kHz 单声道 float32 样本，返回这次说完的句子（start、end 是从流开始的秒数）。"""
        self.buf = np.concatenate([self.buf, x.astype(np.float32, copy=False)])
        n = (self.buf.size // self.window) * self.window
        for i in range(0, n, self.window):
            self.vad.accept_waveform(self.buf[i:i + self.window])
            c = self.speech.setdefault(self.pos // SAMPLE_RATE, [0, 0])
            c[0] += int(self.vad.is_speech_detected())
            c[1] += 1
            self.pos += self.window
        self.buf = self.buf[n:]
        return self._drain()

    def pop_speech(self) -> Dict[int, float]:
        """已经处理完的那些秒里，VAD 判为"在说话"的窗口占比。"""
        done = self.pos // SAMPLE_RATE
        out = {k: round(v[0] / v[1], 2) for k, v in self.speech.items() if k < done}
        for k in out:
            del self.speech[k]
        return out

    def flush(self) -> List[Dict[str, Any]]:
        self.vad.flush()
        return self._drain()

    def _drain(self) -> List[Dict[str, Any]]:
        out = []
        while not self.vad.empty():
            seg = self.vad.front
            s = self.rec.create_stream()
            s.accept_waveform(SAMPLE_RATE, seg.samples)
            self.rec.decode_stream(s)
            text = s.result.text.strip()
            if text:
                out.append({"start": round(seg.start / SAMPLE_RATE, 2),
                            "end": round((seg.start + len(seg.samples)) / SAMPLE_RATE, 2), "text": text})
            self.vad.pop()
        return out
