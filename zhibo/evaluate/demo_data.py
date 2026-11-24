"""合成演示数据集：3 个直播间、各 20 分钟，人为埋好事件和金标准。

**只用来跑通流程，不能用来评价方法好坏**：事件是按规则造的，系统的规则也是照着这些事件写的。
等有了真实录像和消息日志，换成 data/<真实集>/ 即可，目录格式相同。

画面是 FFmpeg 测试图（带时间计数），事件时段加了彩色边框，方便肉眼核对切点：
绿 = 内容高光（金标准）  青 = 安静高光（金标准，没有信号峰）
黄 = 福袋  红 = 单人刷礼物  橙 = 高光前的人气爬升
"""
from __future__ import annotations

import array
import hashlib
import json
import math
import random
import subprocess
import wave
from pathlib import Path
from typing import Any, Dict, List

from ..core.media import ffmpeg

BASE_TS = 1795478400.0  # 回放时钟的起点（任意固定值，保证可复现）
DURATION = 1200
SEED = 20261124
SAMPLE_RATE = 8000

ROOMS = [
    {"room_id": "room_a", "anchor": "主播A", "category": "娱乐", "online": 5000, "chat": 3.0, "like": 2.0, "gift": 0.3},
    {"room_id": "room_b", "anchor": "主播B", "category": "娱乐", "online": 800, "chat": 1.0, "like": 0.8, "gift": 0.1},
    {"room_id": "room_c", "anchor": "主播C", "category": "娱乐", "online": 200, "chat": 0.4, "like": 0.3, "gift": 0.04},
]

# 时间是相对开播的秒数。content 的 lag = 内容开始后多少秒信号才起来（信号滞后，FR-6.1）
SCRIPT = [
    {"room": "room_a", "kind": "content", "start": 282, "end": 345, "lag": 9, "reason": "主播整活翻车"},
    {"room": "room_a", "kind": "lucky_bag", "start": 600, "end": 660},
    {"room": "room_a", "kind": "gift_spam", "start": 900, "end": 930},
    {"room": "room_a", "kind": "quiet", "start": 1050, "end": 1090, "reason": "安静的走心聊天"},
    {"room": "room_b", "kind": "content", "start": 440, "end": 500, "lag": 7, "reason": "连麦嘉宾爆梗"},
    {"room": "room_c", "kind": "buildup", "start": 720, "end": 790},
    {"room": "room_c", "kind": "content", "start": 790, "end": 840, "lag": 10, "reason": "挑战成功"},
]

COLORS = {"content": "green", "quiet": "cyan", "lucky_bag": "yellow", "gift_spam": "red", "buildup": "orange"}

NEUTRAL_A = ["主播好", "来了", "晚上好", "这个好看", "今天播多久", "支持", "刚来", "在吗", "听歌", "666", "哈哈", "主播辛苦了"]
NEUTRAL_B = ["", "~", "！", "啊", "呀", "家人们", "主播", "😊", "👍", "了"]
LAUGH_A = ["哈哈哈", "哈哈哈哈哈", "笑死", "笑死我了", "绷不住了", "卧槽", "？？？", "名场面", "太离谱了", "哈哈哈哈哈哈哈"]
LAUGH_B = ["", "！", "!!!", "主播", "了家人们"]
BAG = ["参与福袋", "福袋福袋", "来了来了", "求中"]
BUILDUP = ["要开始了吗", "冲冲冲", "等着看", "这次能成吗", "紧张"]


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _active(ev: Dict[str, Any], sec: int) -> bool:
    if ev["kind"] == "content":
        return ev["start"] + ev["lag"] <= sec < ev["end"] - 3
    if ev["kind"] == "quiet":
        return False
    return ev["start"] <= sec < ev["end"]


def _online_mult(events: List[Dict[str, Any]], sec: int) -> float:
    m = 1.0
    for ev in events:
        s, e, k = ev["start"], ev["end"], ev["kind"]
        if k == "content":
            s += ev["lag"]
            bump, tail = 0.2, 60
        elif k == "lucky_bag":
            bump, tail = 0.4, 30
        elif k == "buildup":
            if s <= sec < e:
                m += 1.5 * (sec - s) / (e - s)
                continue
            s, e = e, e + 60  # 爬升之后保持一段，再回落
            bump, tail = 1.5, 120
        else:
            continue
        if s <= sec < e:
            m += bump
        elif e <= sec < e + tail:
            m += bump * (1 - (sec - e) / tail)
    return m


def _room_messages(room: Dict[str, Any], events: List[Dict[str, Any]], rng: random.Random) -> List[Dict[str, Any]]:
    rid = room["room_id"]
    out: List[Dict[str, Any]] = []
    whale_combo = {}
    for sec in range(DURATION):
        act = {ev["kind"] for ev in events if _active(ev, sec)}
        chat_m = like_m = gift_m = 1.0
        if "content" in act:
            chat_m, like_m, gift_m = 6.0, 5.0, 4.0
        if "lucky_bag" in act:
            chat_m = 8.0
        if "buildup" in act:
            chat_m, like_m = 3.0, 2.0
        if "gift_spam" in act:
            chat_m = 1.2
        online = int(room["online"] * _online_mult(events, sec) * (1 + rng.uniform(-0.03, 0.03)))
        msgs: List[Dict[str, Any]] = []

        def add(mtype: str, payload: Dict[str, Any]) -> None:
            msgs.append({"room_id": rid, "type": mtype, "payload": payload})

        if sec % 5 == 0:
            add("room_stats", {"online": online})
        if "lucky_bag" in act and sec == next(ev["start"] for ev in events if ev["kind"] == "lucky_bag"):
            add("lucky_bag", {"kind": "福袋", "duration": 60})
        for _ in range(_poisson(rng, room["chat"] * chat_m)):
            user = f"{rid}_u{rng.randrange(room['online'] * 3):05d}"
            r = rng.random()
            if "content" in act and r < 0.6:
                text = rng.choice(LAUGH_A) + rng.choice(LAUGH_B)
            elif "lucky_bag" in act and r < 0.85:
                text = rng.choice(BAG)
            elif "buildup" in act and r < 0.5:
                text = rng.choice(BUILDUP)
            elif "gift_spam" in act and r < 0.2:
                text = "谢谢大哥"
            else:
                text = rng.choice(NEUTRAL_A) + rng.choice(NEUTRAL_B)
            add("chat", {"user": user, "text": text})
        for _ in range(_poisson(rng, room["like"] * like_m)):
            add("like", {"user": f"{rid}_u{rng.randrange(room['online'] * 3):05d}", "count": rng.randint(1, 10)})
        for _ in range(_poisson(rng, room["online"] / 4000 * chat_m)):
            add("enter", {"user": f"{rid}_u{rng.randrange(room['online'] * 3):05d}"})
        for _ in range(_poisson(rng, room["gift"] * gift_m)):
            gift_id, diamond = rng.choice([(1001, 1), (1002, 1), (1003, 5), (1004, 10)])
            if "content" in act and rng.random() < 0.3:
                gift_id, diamond = rng.choice([(1005, 52), (1006, 99)])
            payload = {
                "user": f"{rid}_u{rng.randrange(room['online'] * 3):05d}",
                "gift_id": gift_id, "group_id": rng.randrange(10 ** 9), "combo": 1, "diamond": diamond,
            }
            add("gift", payload)
            if rng.random() < 0.3:  # 平台会重复推送同一次送礼
                add("gift", dict(payload))
        if "gift_spam" in act:
            spam = next(ev for ev in events if ev["kind"] == "gift_spam")
            group = (sec - spam["start"]) // 10 + 1
            for _ in range(rng.choice([1, 1, 2])):
                whale_combo[group] = whale_combo.get(group, 0) + 1
                payload = {"user": "whale_01", "gift_id": 2001, "group_id": group, "combo": whale_combo[group], "diamond": 520}
                add("gift", payload)
                if rng.random() < 0.5:
                    add("gift", dict(payload))
        stamps = sorted(rng.random() * 0.999 for _ in msgs)
        for m, frac in zip(msgs, stamps):
            m["recv_ts"] = round(BASE_TS + sec + frac, 3)
            out.append(m)
    return out


def _write_audio(path: Path, rng: random.Random) -> None:
    """用"有声片段 + 短静音"模拟说话，静音的位置就是句子边界。"""
    cycles = {}
    for freq in (200, 250, 320, 400):
        n = SAMPLE_RATE // freq
        cycles[freq] = array.array("h", (int(9000 * math.sin(2 * math.pi * i / n)) for i in range(n))).tobytes()
    total = DURATION * SAMPLE_RATE
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        written = 0
        while written < total:
            speech = int(rng.uniform(1.5, 6.0) * SAMPLE_RATE)
            gap = int(rng.uniform(0.4, 1.0) * SAMPLE_RATE)
            cyc = cycles[rng.choice(list(cycles))]
            per = len(cyc) // 2
            speech = min(speech // per * per, total - written)
            w.writeframes(cyc * (speech // per))
            written += speech
            gap = min(gap, total - written)
            w.writeframes(b"\x00\x00" * gap)
            written += gap


def _write_video(wav: Path, out: Path, events: List[Dict[str, Any]]) -> None:
    boxes = [
        f"drawbox=x=0:y=0:w=iw:h=ih:color={COLORS[ev['kind']]}:t=14:enable='between(t,{ev['start']},{ev['end']})'"
        for ev in events
    ]
    vf = ",".join(boxes) if boxes else "null"
    r = subprocess.run([
        ffmpeg(), "-hide_banner", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=10:duration={DURATION}",
        "-i", str(wav), "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "48k", "-ar", "16000", "-shortest", "-movflags", "+faststart", str(out),
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"生成演示录像失败：{r.stderr[-800:]}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_demo_dataset(out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    (out_dir / "messages").mkdir(parents=True, exist_ok=True)
    (out_dir / "recordings").mkdir(parents=True, exist_ok=True)
    rooms, recordings, gold, causes = [], {}, [], []
    for i, room in enumerate(ROOMS):
        rid = room["room_id"]
        rng = random.Random(SEED + i)
        events = [ev for ev in SCRIPT if ev["room"] == rid]
        rooms.append({
            "room_id": rid, "anchor": room["anchor"], "category": room["category"], "source": "manual",
            "start_ts": BASE_TS, "end_ts": BASE_TS + DURATION,
        })
        with (out_dir / "messages" / f"{rid}.jsonl").open("w", encoding="utf-8") as f:
            for m in _room_messages(room, events, rng):
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        wav = out_dir / "recordings" / f"{rid}.wav"
        _write_audio(wav, random.Random(SEED + 100 + i))
        _write_video(wav, out_dir / "recordings" / f"{rid}.mp4", events)
        wav.unlink()
        recordings[rid] = {"path": f"recordings/{rid}.mp4", "start_ts": BASE_TS, "duration": DURATION}
        for ev in events:
            if ev["kind"] in ("content", "quiet"):
                gold.append({
                    "room_id": rid, "start": BASE_TS + ev["start"], "end": BASE_TS + ev["end"],
                    "reason": ev["reason"], "has_signal_peak": ev["kind"] == "content", "source": "synthetic",
                })
            label = {"content": "content", "lucky_bag": "lucky_bag", "gift_spam": "single_user_gift"}.get(ev["kind"])
            if label:
                s = ev["start"] + ev.get("lag", 0)
                causes.append({"room_id": rid, "start": BASE_TS + s, "end": BASE_TS + ev["end"] + 5, "cause": label})

    def dump(name: str, obj: Any) -> None:
        (out_dir / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    dump("rooms.json", rooms)
    dump("recordings.json", recordings)
    dump("gold.json", gold)
    dump("causes.json", causes)
    files = sorted(p for p in out_dir.rglob("*") if p.is_file() and p.name != "manifest.json")
    dump("manifest.json", {
        "name": out_dir.name, "synthetic": True, "seed": SEED,
        "files": {str(p.relative_to(out_dir)): _sha256(p) for p in files},
    })
    return out_dir
