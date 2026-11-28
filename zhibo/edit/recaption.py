"""字幕重识别（EVO-034、EVO-035）：出片前把每一句的声音用更准的识别模型再识别一遍，用它的文字替换字幕。

为什么：逐句稿由本地小模型 SenseVoice 实时识别。连麦时几个人的麦混在一起，再加上背景音乐，短句常把笑声或杂音
认成字（陈泽 ev004："哈呀""行回事了""高兄的"）。GLM 剪辑方案只看文字，没法改。成片只有几十秒，
用大模型再识别一遍也不慢。

做法：
- 每句的起止时间沿用逐句稿（切片前已按画面原声对时，EVO-033），前后各多留 pad_seconds 秒，从切好的片段里截声音；
- 两种识别引擎（`asr.engine`）：
  - local（默认）：本地大模型，经 sherpa-onnx 加载，模型放在 models/，见 scripts/fetch_models.sh；
  - glm：智谱语音识别 GLM-ASR-2512。接口一次最多 30 秒，只返回文字，不带时间
    （https://docs.z.ai/api-reference/audio/audio-transcriptions）。要账户有语音识别额度，会把片段声音发给智谱；
    请求走账本（键里含声音的 sha256），重跑时直接回放；
- 主播名字等作为热词；
- 识别结果为空（只有笑声、杂音）的句子不再出字幕；识别失败保留本地识别的原文。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import media
from ..core.ledger import Ledger

EMPTY = re.compile(r"^[\s，。！？、,.!?；;…~～\-—]*$")
SAMPLE_RATE = 16000
TAG = re.compile(r"<[^<>]{1,20}>")  # FireRedASR 把笑声等非语音输出成 <sil> 这类标记


def _one(pattern: str) -> str:
    hits = sorted(glob.glob(pattern))
    if not hits:
        raise FileNotFoundError(f"没找到模型文件：{pattern}（先运行 scripts/fetch_models.sh）")
    return hits[0]


class LocalASR:
    """本地识别模型（sherpa-onnx）。kind：qwen3（Qwen3-ASR）、fire_red（FireRedASR / FireRedASR2 的 AED 版）。"""

    def __init__(self, cfg: Dict[str, Any], models_dir: Path):
        import sherpa_onnx  # 项目 .venv

        self.kind = cfg.get("kind", "qwen3")
        d = str(Path(models_dir) / cfg["dir"])
        threads = int(cfg.get("num_threads", 2))
        if self.kind == "qwen3":
            self.rec = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
                conv_frontend=_one(f"{d}/conv*.onnx"), encoder=_one(f"{d}/encoder*.onnx"),
                decoder=_one(f"{d}/decoder*.onnx"), tokenizer=_one(f"{d}/tokenizer"), num_threads=threads,
                hotwords=",".join(cfg.get("hotwords") or []))
        elif self.kind == "fire_red":
            self.rec = sherpa_onnx.OfflineRecognizer.from_fire_red_asr(
                encoder=_one(f"{d}/encoder*.onnx"), decoder=_one(f"{d}/decoder*.onnx"),
                tokens=_one(f"{d}/tokens.txt"), num_threads=threads)
        else:
            raise ValueError(f"不认识的本地识别模型：{self.kind}")
        self.model = f"{self.kind}:{cfg['dir']}"

    def recognize(self, samples) -> str:
        s = self.rec.create_stream()
        s.accept_waveform(SAMPLE_RATE, samples)
        self.rec.decode_stream(s)
        return s.result.text.strip()


class GLMASR:
    """智谱语音识别（云端）。"""

    def __init__(self, cfg: Dict[str, Any]):
        from .judge import _load_env_file

        env = {**_load_env_file(), **os.environ}
        self.key = env.get("ZHIPU_API_KEY", "").strip()
        self.url = cfg.get("url", "https://api.z.ai/api/paas/v4/audio/transcriptions")
        self.model = cfg.get("model", "glm-asr-2512")
        self.timeout = float(cfg.get("timeout", 60))
        self.retry_waits = [float(x) for x in cfg.get("retry_waits", [15, 45, 90])]
        self.base_dir = Path(".")  # 请求里的声音路径相对于它
        self.disabled = ""  # 余额不足等不会自己好的错误：记下原因，这一轮后面的句子不再发

    def respond(self, request: Dict[str, Any]) -> str:
        if self.disabled:
            raise RuntimeError(self.disabled)
        if not self.key:
            raise RuntimeError("缺少 ZHIPU_API_KEY：在项目根目录的 .env 里写一行 ZHIPU_API_KEY=你的密钥")
        audio = (self.base_dir / request["audio"]).read_bytes()
        fields = [("model", request["model"]), ("stream", "false")]
        fields += [("hotwords", w) for w in request.get("hotwords") or []]
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in fields:
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode("utf-8")
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                 f"Content-Type: audio/wav\r\n\r\n").encode("utf-8") + audio + b"\r\n"
        body += f"--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
        waits = list(self.retry_waits)
        while True:  # 429 和 5xx 等一会儿再试（同 judge._send）
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read()).get("text") or ""
            except urllib.error.HTTPError as e:
                msg = e.read()[:300].decode("utf-8", "replace")
                if '"1113"' in msg:  # 余额不足或没有资源包（2026-11-28 实测：现有 key 不含语音识别额度）
                    self.disabled = f"语音识别余额不足（智谱错误码 1113）：{msg}"
                    print("[语音识别] 智谱账户余额不足或没有资源包，这一轮字幕保留本地识别", flush=True)
                    raise RuntimeError(self.disabled)
                if (e.code == 429 or e.code >= 500) and waits:
                    wait = waits.pop(0)
                    print(f"[语音识别] HTTP {e.code}，{wait:.0f} 秒后重试（还能重试 {len(waits)} 次）", flush=True)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"语音识别 HTTP {e.code}: {msg}")


_SHARED: Dict[str, Any] = {}


def shared(cfg: Dict[str, Any]):
    """一次运行共用一个识别器：本地模型只加载一次；云端余额不足的记号要跨片段保留。"""
    a = cfg.get("asr", {})
    engine = a.get("engine", "local")
    if engine not in _SHARED:
        if engine == "glm":
            _SHARED[engine] = GLMASR(a)
        else:
            models_dir = Path(cfg.get("perceive", {}).get("models_dir", "models"))
            _SHARED[engine] = LocalASR(a.get("local", {}), models_dir)
    return _SHARED[engine]


def _pcm(clip: Path):
    import numpy as np

    raw = subprocess.run([media.ffmpeg(), "-v", "error", "-i", str(clip), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                          "-f", "s16le", "-"], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


def _wav(samples, out: Path) -> bytes:
    import wave

    import numpy as np

    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return out.read_bytes()


def recaption(sents: List[Dict[str, Any]], clip: Path, clip_start: float, cfg: Dict[str, Any],
              ledger: Optional[Ledger], asr, work: Path, hotwords: List[str]) -> List[Dict[str, Any]]:
    """sents：片段内的逐句稿（epoch 秒）。返回新的逐句稿：文字换成重识别结果，原文留在 text_local；
    识别为空的句子去掉；识别失败的句子保留原文。云端引擎的 work 要在 asr.base_dir 下面。"""
    a_cfg = cfg.get("asr", {})
    pad = float(a_cfg.get("pad_seconds", 0.25))
    x = _pcm(clip)
    clip_end = clip_start + x.size / SAMPLE_RATE
    out = []
    for i, s in enumerate(sents):
        inside = min(s["end"], clip_end) - max(s["start"], clip_start)
        if inside < float(a_cfg.get("min_inside", 0.5)) * max(s["end"] - s["start"], 0.01):
            continue  # 大半句在片子外面（被切点截断）：只剩半个词，不出字幕（陈泽 ev004 片尾 0.3 秒的"高兄的"）
        a = max(0, int((s["start"] - clip_start - pad) * SAMPLE_RATE))
        b = min(x.size, int((s["end"] - clip_start + pad) * SAMPLE_RATE), a + 30 * SAMPLE_RATE)
        r = dict(s)
        if b - a < 0.2 * SAMPLE_RATE or getattr(asr, "disabled", ""):
            r["asr"] = "skipped"
            out.append(r)
            continue
        seg = x[a:b]
        if isinstance(asr, GLMASR):
            path = work / f"s{i:03d}.wav"
            data = _wav(seg, path)
            req = {"task": "asr", "model": asr.model, "audio_sha256": hashlib.sha256(data).hexdigest(),
                   "audio": str(path.relative_to(asr.base_dir)), "hotwords": hotwords}
            status, text = ledger.call(req, asr.respond)
        else:
            try:
                status, text = "local", asr.recognize(seg)
            except Exception as e:  # noqa: BLE001  识别出错不能拖垮出片
                status, text = f"failed: {e}", None
        if text is None:
            r["asr"] = status
            out.append(r)
            continue
        text = TAG.sub("", text).strip()
        r["text_local"], r["asr"] = s["text"], status
        if EMPTY.match(text):
            continue
        r["text"] = text
        if text != s["text"]:
            r.pop("ts", None)  # 逐字时间是本地小模型的字，文字变了就对不上
        out.append(r)
    return out
