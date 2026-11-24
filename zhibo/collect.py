"""实测采集（M2 + M3 的实时版）：dyhub 采弹幕，ffmpeg 录直播流，落成 data/<集>/ 数据集格式。

用法：
  python3 -m zhibo collect --room 德华=<直播间号或链接> --room 陈泽=<...> --hours 4 --start-hub

- 弹幕：dyhub（third_party/dyhub，浏览器内核）通过 SSE 推送统一事件，这里转成 RawMessage 落盘；
- 录制：从 dyhub 拿播放器正在拉的 FLV 地址，ffmpeg 直接拷贝流；断了就重新取地址、开新的一段；
- 没开播的直播间每隔一会儿重试，开播后自动接上，所以可以提前启动；
- 按 Ctrl+C 或到时间后停止，自动整理成可以直接 `python3 -m zhibo run` 的数据集。
所有对抖音的连接都不走本机代理（抖音要看到本机真实 IP）。
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import media
from .attribution import CAUSE_ZH, attribute
from .heat import HeatDetector
from .models import RawMessage, Room
from .signals import SignalAggregator

HUB_DIR = Path(__file__).resolve().parent.parent / "third_party" / "dyhub"
PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SPECIAL_METHODS = [  # 未标准化消息按方法名归类（PK 类消息能否拿到是需求文档 Q-1）
    (re.compile(r"LuckyBox|Lottery", re.I), "lucky_bag"),
    (re.compile(r"Envelope|RedPacket", re.I), "red_packet"),
    (re.compile(r"Battle|PK|LinkMic", re.I), "pk"),
]


def _env_no_proxy() -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in PROXY_VARS}


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def parse_room(arg: str) -> Dict[str, Optional[str]]:
    """支持三种写法：名字=直播间号、名字=live.douyin.com 链接、名字=主播主页链接（douyin.com/user/<sec_uid>）。
    主页链接适合还没开播的主播：开播后自动查到直播间号。"""
    name, _, val = arg.partition("=") if "=" in arg else ("", "", arg)
    m = re.search(r"/user/(MS4w[\w-]+)", val) or re.match(r"(MS4w[\w-]+)$", val.strip())
    if m:
        return {"room_id": None, "sec_uid": m.group(1), "anchor": name or m.group(1)[:12]}
    m = re.search(r"(\d{6,})", val)
    if not m:
        raise SystemExit(f"认不出直播间：{arg}（要直播间号、live.douyin.com 链接或主播主页链接）")
    return {"room_id": m.group(1), "sec_uid": None, "anchor": name or m.group(1)}


def _reflow(sec_uid: str) -> Dict[str, Any]:
    """按主播 sec_uid 查直播间信息（不需要登录）。没开播时 status_code=10011。"""
    url = ("https://webcast.amemv.com/webcast/room/reflow/info/?type_id=0&live_id=1&room_id=2"
           f"&sec_user_id={sec_uid}&version_code=99.99.99&app_id=1128")
    try:
        with NO_PROXY.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15) as r:
            d = json.loads(r.read())
    except (OSError, json.JSONDecodeError):
        return {}
    room = (d.get("data") or {}).get("room") or {}
    return room if d.get("status_code") == 0 and room.get("status") == 2 else {}


def resolve_live_room(sec_uid: str) -> Optional[str]:
    """在播返回直播间号（web_rid），没播返回 None。"""
    return (_reflow(sec_uid).get("owner") or {}).get("web_rid")


# 清晰度（接口 resolution_name 的叫法）：ORIGIN=原画，FULL_HD1=蓝光，HD1=超清，SD2=高清，SD1=标清。
# 原画不在 flv_pull_url 里，在 live_core_sdk_data.pull_data.stream_data 的 origin 项。
# 2026-11-24 实测（一个游戏直播间，各录 20 秒）：原画 1920x1080 60 帧约 6.2 Mbps（2.8 GB/时）；
# 超清 1280x720 60 帧约 2.9 GB/时；高清 1280x720 30 帧约 1.0 GB/时；纯音频（ao）约 0.09 GB/时。
# 码率随主播的推流设置变化，原画在别的直播间可能到 7～9 GB/时。
QUALITY_ORDER = ["ORIGIN", "FULL_HD1", "HD1", "SD2", "SD1"]  # 从高到低


def _sdk_streams(room: Dict[str, Any]) -> Dict[str, str]:
    """live_core_sdk_data 里各档的 flv 地址：origin、uhd、hd、sd、ld、md、ao（纯音频）。"""
    raw = (((room.get("stream_url") or {}).get("live_core_sdk_data") or {}).get("pull_data") or {}).get("stream_data")
    try:
        data = json.loads(raw)["data"] if raw else {}
    except (ValueError, KeyError, TypeError):
        return {}
    return {k: (v.get("main") or {}).get("flv") for k, v in data.items() if (v.get("main") or {}).get("flv")}


def quality_stream_url(sec_uid: str, quality: str) -> Optional[str]:
    """按指定清晰度取流；没有这一档就先往低找、再往高找。"""
    room = _reflow(sec_uid)
    flv = dict(((room.get("stream_url") or {}).get("flv_pull_url")) or {})
    origin = _sdk_streams(room).get("origin")
    if origin:
        flv["ORIGIN"] = origin
    i = QUALITY_ORDER.index(quality) if quality in QUALITY_ORDER else 0
    for q in QUALITY_ORDER[i:] + QUALITY_ORDER[:i][::-1]:
        if flv.get(q):
            return flv[q]
    return next(iter(flv.values()), None)


class Hub:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _req(self, method: str, path: str, body: Optional[dict] = None, timeout: float = 120) -> Dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with NO_PROXY.open(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read() or b"{}")
            except json.JSONDecodeError:
                return {"error": f"HTTP {e.code}"}

    def alive(self) -> bool:
        try:
            self._req("GET", "/api/stats", timeout=3)
            return True
        except OSError:
            return False

    def ensure_account(self) -> None:
        if not self._req("GET", "/api/accounts").get("accounts"):
            self._req("POST", "/api/accounts", {"name": "游客"})

    def connect(self, room_id: str) -> Dict[str, Any]:
        d = self._req("POST", "/api/rooms/connect", {"roomId": room_id})
        return d.get("room") or d

    def room(self, room_id: str) -> Dict[str, Any]:
        return self._req("GET", f"/api/rooms/{room_id}").get("room") or {}

    def stream_url(self, room_id: str) -> Optional[str]:
        return self._req("GET", f"/api/rooms/{room_id}/stream").get("url")

    def events(self, stop: threading.Event):
        """SSE 事件流；断了自动重连。"""
        while not stop.is_set():
            try:
                with NO_PROXY.open(self.base + "/api/events", timeout=40) as r:
                    for raw in r:
                        if stop.is_set():
                            return
                        line = raw.decode("utf-8", "replace").rstrip("\n")
                        if line.startswith("data: "):
                            try:
                                yield json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
            except OSError:
                if not stop.wait(2):
                    continue


def start_hub(port: int) -> subprocess.Popen:
    if not (HUB_DIR / "dist" / "index.js").exists():
        raise SystemExit(f"没找到 dyhub：{HUB_DIR}，先按 README 安装")
    env = _env_no_proxy() | {"DYHUB_HOST": "127.0.0.1", "DYHUB_PORT": str(port)}
    log = (HUB_DIR / "dyhub.log").open("a")
    return subprocess.Popen(["node", "dist/index.js"], cwd=HUB_DIR, env=env, stdout=log, stderr=subprocess.STDOUT)


class Recorder(threading.Thread):
    """一个直播间的录制，ffmpeg 按 10 秒切成小段。

    - 环形缓冲模式（keep_seconds 有值）：只保留最近 keep_seconds 秒，过期且没被"保留"的小段直接删掉；
      检测到搞笑异常时调用 pin(起, 止) 把那段标为保留。磁盘占用和直播时长无关（FR-3.2、FR-3.3）。
    - 全程录制模式（keep_seconds=None）：所有小段都保留。
    结束时 export() 把要保留的连续小段拼成文件，返回 recordings.json 需要的条目。

    同一个 ffmpeg 还顺带把**整场音频**原样存成 audio.ts（不重新编码，mpegts 封装带时间戳，时长准确；约 0.1 GB/时），
    不管环形缓冲删没删画面，声音都全程保留，给声音分析和离线实验用。音频和画面来自同一路输入，
    时间天然对齐。结束时整理成 audio_<开始毫秒>.ts，条目在 audio_entries 里（写进 audio.json）。
    """

    SEG_SECONDS = 10

    def __init__(self, hub: Hub, room_id: str, out_dir: Path, stop: threading.Event, log,
                 sec_uid: Optional[str] = None, quality: str = "ORIGIN", min_free_gb: float = 15.0,
                 keep_seconds: Optional[float] = None):
        super().__init__(daemon=True)
        self.hub, self.room_id, self.dir, self.stop, self.log = hub, room_id, out_dir, stop, log
        self.sec_uid, self.quality, self.min_free_gb = sec_uid, quality, min_free_gb
        self.keep_seconds = keep_seconds
        self.segments: List[Dict[str, Any]] = []  # {path, start, end, deleted}
        self.pins: Dict[int, List[float]] = {}
        self._pin_lock = threading.Lock()
        self._pin_seq = 0
        self.proc: Optional[subprocess.Popen] = None
        self.live = False
        self.disk_full = False
        self.deleted_bytes = 0
        self.with_audio = True  # 流里没有音频时自动关掉，免得 ffmpeg 因为音频输出为空而整个失败
        self.sessions: List[Dict[str, Any]] = []  # {dir, start}，给整理音频用
        self.audio_entries: List[Dict[str, Any]] = []

    def pin(self, start: float, end: float = float("inf")) -> int:
        """标记一段要保留，返回编号；end 不给就是"先占住、还没定终点"。"""
        with self._pin_lock:
            self._pin_seq += 1
            self.pins[self._pin_seq] = [start, end]
            return self._pin_seq

    def set_pin(self, pid: int, start: float, end: float) -> None:
        with self._pin_lock:
            self.pins[pid] = [start, end]

    def unpin(self, pid: int) -> None:
        with self._pin_lock:
            self.pins.pop(pid, None)

    def _pinned(self, a: float, b: float) -> bool:
        with self._pin_lock:
            spans = list(self.pins.values())
        return any(a < pe and ps < b for ps, pe in spans)

    def _free_gb(self) -> float:
        return shutil.disk_usage(self.dir).free / 1e9

    def disk_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.dir.rglob("*.ts")) if self.dir.exists() else 0

    def _url(self) -> Optional[str]:
        """优先按主页 ID 取指定清晰度的流；取不到再用 dyhub 给的播放器地址（可能是原画，体积大）。"""
        if not self.sec_uid:
            self.sec_uid = ((self.hub.room(self.room_id).get("meta") or {}).get("secUid")) or None
        if self.sec_uid:
            url = quality_stream_url(self.sec_uid, self.quality)
            if url:
                return url
        self.log(f"[录制] {self.room_id} 取不到指定清晰度，退回播放器地址（可能是原画）")
        return self.hub.stream_url(self.room_id)

    def _scan(self, sess: Path, start: float, known: set) -> None:
        """读 ffmpeg 写的分段清单（每完成一段写一行：文件名,起,止，相对本段录制开头）。"""
        lst = sess / "list.csv"
        if not lst.exists():
            return
        for line in lst.read_text().splitlines():
            parts = line.strip().split(",")
            if len(parts) < 3 or parts[0] in known:
                continue
            known.add(parts[0])
            self.segments.append({"path": sess / parts[0], "start": start + float(parts[1]),
                                  "end": start + float(parts[2]), "deleted": False})

    def _expire(self, now: float) -> None:
        if self.keep_seconds is None:
            return
        for seg in self.segments:
            if not seg["deleted"] and seg["end"] < now - self.keep_seconds and not self._pinned(seg["start"], seg["end"]):
                try:
                    self.deleted_bytes += seg["path"].stat().st_size
                    seg["path"].unlink()
                except OSError:
                    pass
                seg["deleted"] = True

    def run(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        while not self.stop.is_set():
            if self._free_gb() < self.min_free_gb:  # 磁盘保护（FR-12.2）
                if not self.disk_full:
                    self.log(f"[录制] 磁盘剩余不足 {self.min_free_gb:.0f} GB，停止录制 {self.room_id}（弹幕继续采集）")
                    self.disk_full = True
                self.stop.wait(60)
                continue
            url = self._url() if self.live else None
            if not url:
                self.stop.wait(10)
                continue
            sess = self.dir / str(int(time.time() * 1000))
            sess.mkdir(parents=True, exist_ok=True)
            self.proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-rw_timeout", "15000000",
                 "-i", url, "-c", "copy", "-f", "segment", "-segment_time", str(self.SEG_SECONDS),
                 "-segment_format", "mpegts", "-reset_timestamps", "1",
                 "-segment_list", str(sess / "list.csv"), "-segment_list_type", "csv", str(sess / "seg_%05d.ts")]
                + (["-map", "0:a:0", "-c", "copy", "-f", "mpegts", str(sess / "audio.ts")] if self.with_audio else []),
                env=_env_no_proxy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            first = sess / "seg_00000.ts"
            start = None
            known: set = set()
            last_check = time.time()
            while self.proc.poll() is None and not self.stop.is_set():
                if start is None and first.exists() and first.stat().st_size > 0:
                    start = time.time()  # 第一个字节落盘的时刻 ≈ 本段录制第 0 秒（误差待实测，Q-6）
                    self.sessions.append({"dir": sess, "start": start})
                if start is not None and time.time() - last_check > 5:
                    last_check = time.time()
                    self._scan(sess, start, known)
                    self._expire(time.time())
                    if self._free_gb() < self.min_free_gb:
                        break
                time.sleep(0.2)
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            err = (self.proc.stderr.read() or b"").decode("utf-8", "replace")[-300:]
            if self.with_audio and "audio.ts" in err and ("does not contain any stream" in err or "matches no streams" in err):
                self.with_audio = False
                self.log(f"[录制] {self.room_id} 流里没有音频，之后只录画面")
            if start is not None:
                self._scan(sess, start, known)
                self.log(f"[录制] {self.room_id} 一段录制结束，共 {len(known)} 个小段"
                         + (f"；{err.strip()}" if err.strip() else ""))
            if not self.stop.is_set():
                self.stop.wait(5)

    def _export_audio(self, out_root: Path) -> None:
        """把每段录制的整场音频移出临时目录（临时目录随后会被删掉）。"""
        for ss in self.sessions:
            src = ss["dir"] / "audio.ts"
            if not src.exists() or src.stat().st_size == 0:
                continue
            dst = self.dir / f"audio_{int(ss['start'] * 1000)}.ts"
            shutil.move(str(src), str(dst))
            dur = media.probe(dst).get("duration")
            if dur:
                self.audio_entries.append({"path": str(dst.relative_to(out_root)), "start_ts": round(ss["start"], 3),
                                           "duration": round(dur, 3)})

    def export(self, out_root: Path) -> List[Dict[str, Any]]:
        """把要保留的小段按连续区间拼成文件，删掉其余小段。返回 recordings.json 的条目。"""
        self._export_audio(out_root)
        segs = sorted((s for s in self.segments if not s["deleted"] and s["path"].exists()), key=lambda s: s["start"])
        if self.keep_seconds is not None:
            segs = [s for s in segs if self._pinned(s["start"], s["end"])]
        groups: List[List[Dict[str, Any]]] = []
        for s in segs:
            if groups and s["path"].parent == groups[-1][-1]["path"].parent and s["start"] - groups[-1][-1]["end"] < 1.0:
                groups[-1].append(s)
            else:
                groups.append([s])
        entries = []
        for g in groups:
            out = self.dir / f"{int(g[0]['start'] * 1000)}.ts"
            lst = self.dir / "concat.txt"
            lst.write_text("".join(f"file '{s['path'].resolve()}'\n" for s in g))
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                                "-i", str(lst), "-c", "copy", "-f", "mpegts", str(out)], capture_output=True, text=True)
            lst.unlink()
            dur = media.probe(out).get("duration") if r.returncode == 0 else None
            if dur:
                entries.append({"path": str(out.relative_to(out_root)), "start_ts": round(g[0]["start"], 3),
                                "duration": round(dur, 3)})
            else:
                self.log(f"[录制] {self.room_id} 拼接失败：{r.stderr[-200:]}")
        for sess in [p for p in self.dir.iterdir() if p.is_dir()]:
            shutil.rmtree(sess, ignore_errors=True)
        return entries


class Collector:
    def __init__(self, rooms: List[Dict[str, Optional[str]]], out: Path, hub: Hub, salt: str, cfg: Dict[str, Any],
                 quality: str = "ORIGIN", min_free_gb: float = 15.0, full_record: bool = False):
        self.out, self.hub, self.salt, self.cfg = out, hub, salt, cfg
        self.quality, self.min_free_gb = quality, min_free_gb
        live = cfg.get("live", {})
        self.pin_causes = set(live.get("pin_causes", ["content"]))
        self.pin_pre = float(live.get("pin_pre_seconds", 60))
        self.pin_post = float(live.get("pin_post_seconds", 20))
        # 即时重放：确认异常结束后，从确认时刻往回存 replay_seconds 秒（异常本身更长时从异常开始前 pin_pre 秒起存）。
        # 异常一开始就先占住"开始时刻往前 replay_seconds 秒起"的录像，等结束、归因完再定稿或释放，
        # 所以缓冲只要盖住 replay_seconds 加上检测延迟的余量，和异常持续多久无关。
        self.replay = float(live.get("replay_seconds", 300))
        self.keep_seconds = None if full_record else self.replay + 60
        self.held: Dict[str, int] = {}  # 直播间 → 正在占住的保留编号
        self.q: "queue.Queue" = queue.Queue()
        self.triggers: List[Dict[str, Any]] = []
        self.anomalies = 0
        self.stop = threading.Event()
        (out / "messages").mkdir(parents=True, exist_ok=True)
        self.logf = (out / "collect.log").open("a", encoding="utf-8")
        self.rooms: Dict[str, Dict[str, Optional[str]]] = {}
        self.pending = [r for r in rooms if not r["room_id"]]  # 只给了主页、还没查到直播间号的主播
        self.files: Dict[str, Any] = {}
        self.status: Dict[str, str] = {}
        self.first_ts: Dict[str, float] = {}
        self.last_ts: Dict[str, float] = {}
        self.minute: Dict[str, Counter] = defaultdict(Counter)
        self.methods: Dict[str, Counter] = defaultdict(Counter)
        self.online: Dict[str, int] = {}
        self.recorders: Dict[str, Recorder] = {}
        for r in rooms:
            if r["room_id"]:
                self._add_room(r)

    def _add_room(self, r: Dict[str, Optional[str]]) -> None:
        rid = r["room_id"]
        self.files[rid] = (self.out / "messages" / f"{rid}.jsonl").open("a", encoding="utf-8")
        self.status[rid] = "waiting"
        self.recorders[rid] = Recorder(self.hub, rid, self.out / "recordings" / rid, self.stop, self.log,
                                       sec_uid=r.get("sec_uid"), quality=self.quality, min_free_gb=self.min_free_gb,
                                       keep_seconds=self.keep_seconds)
        self.rooms[rid] = r
        if self.started:
            self.recorders[rid].start()

    started = False

    def _resolve_pending(self) -> None:
        """每分钟按主页查一次还没开播的主播，开播了就加进采集。"""
        while self.pending and not self.stop.is_set():
            for r in list(self.pending):
                rid = resolve_live_room(r["sec_uid"])
                if rid:
                    self.pending.remove(r)
                    if rid in self.rooms:
                        continue
                    r = dict(r, room_id=rid)
                    self.log(f"[开播] {r['anchor']} 开播了，直播间号 {rid}，开始采集")
                    self._add_room(r)
                    self.hub.connect(rid)  # 立刻连，不等下一轮状态检查
            self.stop.wait(60)

    def log(self, msg: str) -> None:
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        self.logf.write(line + "\n")
        self.logf.flush()

    def _uid(self, user: Optional[dict]) -> Optional[str]:
        if not user or not user.get("id"):
            return None
        return hashlib.sha256((self.salt + str(user["id"])).encode()).hexdigest()[:12]  # 观众 ID 只存加盐哈希（NFR-7）

    def _map(self, ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        t, d = ev.get("type"), ev.get("data") or {}
        base = {"platform_ts": ev.get("ts")}
        user = self._uid(ev.get("user"))
        if t == "chat":
            return {"type": "chat", "payload": {**base, "user": user, "text": d.get("content") or "",
                                                "sticker": bool(d.get("sticker"))}}
        if t == "gift":
            return {"type": "gift", "payload": {
                **base, "user": user, "gift_id": d.get("giftId"), "gift_name": d.get("giftName"),
                "group_id": d.get("groupId"), "combo": d.get("repeatCount") or 1,
                "diamond": (d.get("diamondCount") or 0) * (d.get("groupCount") or 1)}}
        if t == "like":
            return {"type": "like", "payload": {**base, "user": user, "count": d.get("count") or 1}}
        if t == "member":
            return {"type": "enter", "payload": {**base, "user": user}}
        if t == "follow":
            return {"type": "follow", "payload": {**base, "user": user}}
        if t == "room":
            return {"type": "room_stats", "payload": {**base, "online": d.get("total"), "total_user": d.get("totalUser")}}
        if t == "roomStatus":
            return {"type": "room_status", "payload": {**base, "status": d.get("status"), "message": d.get("message")}}
        if t == "unknown":
            method = d.get("method") or ""
            for pat, kind in SPECIAL_METHODS:
                if pat.search(method):
                    return {"type": kind, "payload": {**base, "method": method}}
            return {"type": "other", "payload": {**base, "method": method}}
        return None

    def _detect(self) -> None:
        """实时检测：和离线回放用同一套信号聚合、热度检测、归因代码（需求 8.3 节）。"""
        aggs: Dict[str, SignalAggregator] = {}
        dets: Dict[str, HeatDetector] = {}
        cur: Dict[str, int] = {}

        def tick(rid: str, t: int) -> None:
            _, ev = dets[rid].update(aggs[rid].close(float(t)))
            opened = dets[rid].pop_opened()
            if opened is not None and rid in self.recorders:
                # 先占住，免得等"确认结束"的这段时间里，前面的录像被环形缓冲删掉
                self.held[rid] = self.recorders[rid].pin(opened - self.replay)
            if ev:
                self._on_event(ev)

        while not (self.stop.is_set() and self.q.empty()):
            try:
                rid, msg = self.q.get(timeout=0.5)
            except queue.Empty:
                rid, msg = None, None
            if rid is not None:
                if rid not in aggs:
                    aggs[rid], dets[rid], cur[rid] = SignalAggregator(rid, self.cfg), HeatDetector(rid, self.cfg), int(msg.recv_ts)
                while cur[rid] < int(msg.recv_ts):
                    tick(rid, cur[rid])
                    cur[rid] += 1
                aggs[rid].feed(msg)
            now = int(time.time())
            for r in list(cur):
                while cur[r] < now - 2:  # 留 2 秒给晚到的消息
                    tick(r, cur[r])
                    cur[r] += 1
        for r in list(dets):
            ev = dets[r].flush(float(cur[r]))
            if ev:
                self._on_event(ev)

    def _on_event(self, ev) -> None:
        info = self.rooms[ev.room_id]
        room = Room(ev.room_id, info["anchor"], "搞笑", "manual", self.first_ts.get(ev.room_id, ev.start),
                    stream_start_known=False, sec_uid=info.get("sec_uid"))
        attr = attribute(ev, room, self.cfg)
        self.anomalies += 1
        hhmm = time.strftime("%H:%M:%S", time.localtime(ev.start))
        keep = attr.cause in self.pin_causes
        msg = (f"[{'触发' if keep else '异常'}] {info['anchor']} {hhmm} 起 {ev.end - ev.start:.0f} 秒，"
               f"{CAUSE_ZH.get(attr.cause, attr.cause)}，{ev.channel} z={ev.peak_z}")
        rec, held = self.recorders.get(ev.room_id), self.held.pop(ev.room_id, None)
        if keep:
            # 即时重放：从"确认结束"的时刻往回存 replay 秒；异常本身更长时从异常开始前 pin_pre 秒起存
            a = min(ev.closed_at - self.replay, ev.start - self.pin_pre)
            b = max(ev.closed_at, ev.end + self.pin_post)
            if rec is not None:
                if held is not None:
                    rec.set_pin(held, a, b)
                else:
                    rec.pin(a, b)
            row = {"room_id": ev.room_id, "anchor": info["anchor"], "keep_start": round(a, 3), "keep_end": round(b, 3),
                   "event": {"start": ev.start, "peak": ev.peak, "end": ev.end, "confirmed_at": ev.closed_at, "channel": ev.channel, "peak_z": ev.peak_z,
                             "top_texts": ev.stats.get("top_texts")},
                   "cause": attr.cause, "evidence": attr.evidence}
            self.triggers.append(row)
            with (self.out / "triggers.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            msg += f"，保留录像 {time.strftime('%H:%M:%S', time.localtime(a))}–{time.strftime('%H:%M:%S', time.localtime(b))}"
            msg += f"；弹幕：{'、'.join((ev.stats.get('top_texts') or [])[:3])[:40]}"
        else:
            if rec is not None and held is not None:
                rec.unpin(held)  # 不是搞笑：释放先占住的录像，交还环形缓冲
            msg += "，不保留录像"
        self.log(msg)

    def _keep_connected(self) -> None:
        while not self.stop.is_set():
            for rid in list(self.rooms):
                info = self.hub.room(rid)
                st = info.get("status") or "not_connected"
                if st in ("closed", "error", "stopped", "not_connected"):
                    info = self.hub.connect(rid)
                    st = info.get("status") or ("error: " + str(info.get("error")))
                self.recorders[rid].live = st == "live"
                if st != self.status[rid]:
                    self.log(f"[状态] {self.rooms[rid]['anchor']}（{rid}）：{self.status[rid]} → {st}")
                    self.status[rid] = st
            self.stop.wait(30)

    def _monitor(self) -> None:
        while not self.stop.wait(60):
            parts = []
            for rid, r in list(self.rooms.items()):
                c = self.minute.pop(rid, Counter())
                size = self.recorders[rid].disk_bytes() / 1e6
                parts.append(f"{r['anchor']}：在线 {self.online.get(rid, '-')}，弹幕 {c['chat']}/分，"
                             f"笑点 {c['laugh']}/分，录像占用 {size:.0f} MB，{self.status[rid]}")
            parts += [f"{r['anchor']}：等待开播" for r in list(self.pending)]
            self.log("[每分钟] " + "；".join(parts) + f"｜累计异常 {self.anomalies}，触发保留 {len(self.triggers)}")

    def run(self, hours: float, laugh_words: List[str]) -> None:
        self.hub.ensure_account()
        self.started = True
        for rec in list(self.recorders.values()):
            rec.start()
        threading.Thread(target=self._resolve_pending, daemon=True).start()
        threading.Thread(target=self._keep_connected, daemon=True).start()
        threading.Thread(target=self._monitor, daemon=True).start()
        detector = threading.Thread(target=self._detect, daemon=True)
        detector.start()
        deadline = time.time() + hours * 3600
        threading.Thread(target=lambda: (self.stop.wait(max(0, deadline - time.time())), self.stop.set()), daemon=True).start()
        mode = ("全程录制" if self.keep_seconds is None else
                f"环形缓冲（只保留最近 {self.keep_seconds / 60:.1f} 分钟；确认搞笑异常结束后往回存 {self.replay / 60:.0f} 分钟）")
        self.log(f"开始采集：{len(self.rooms)} 个直播间，另有 {len(self.pending)} 位主播等待开播；"
                 f"最长 {hours} 小时，Ctrl+C 结束。录制方式：{mode}。输出：{self.out}")
        try:
            for ev in self.hub.events(self.stop):
                rid = ev.get("roomId")
                if rid not in self.rooms:
                    continue
                m = self._map(ev)
                if m is None:
                    continue
                recv = (ev.get("receivedAt") or time.time() * 1000) / 1000
                row = {"room_id": rid, "recv_ts": round(recv, 3), **m}
                self.files[rid].write(json.dumps(row, ensure_ascii=False) + "\n")
                self.first_ts.setdefault(rid, recv)
                self.last_ts[rid] = recv
                self.q.put((rid, RawMessage(rid, recv, m["type"], m["payload"])))
                if m["type"] == "chat":
                    self.minute[rid]["chat"] += 1
                    if any(w in m["payload"]["text"] for w in laugh_words):
                        self.minute[rid]["laugh"] += 1
                elif m["type"] == "room_stats" and m["payload"].get("online") is not None:
                    self.online[rid] = m["payload"]["online"]
                elif m["type"] == "room_status":
                    self.recorders[rid].live = m["payload"]["status"] == "live"
                elif m["type"] in ("other", "lucky_bag", "red_packet", "pk"):
                    self.methods[rid][m["payload"]["method"]] += 1
        except KeyboardInterrupt:
            self.log("收到 Ctrl+C，正在收尾……")
        finally:
            self.stop.set()
            for rec in self.recorders.values():
                rec.join(30)
            detector.join(30)
            for f in self.files.values():
                f.close()
            self.finalize()

    def finalize(self) -> None:
        rooms, recs = [], {}
        for rid, r in list(self.rooms.items()):
            sessions = self.recorders[rid].export(self.out)
            if rid not in self.first_ts:
                self.log(f"[收尾] {r['anchor']} 没有收到任何消息，跳过")
                continue
            rooms.append({"room_id": rid, "anchor": r["anchor"], "category": "搞笑", "source": "manual",
                          "start_ts": self.first_ts[rid], "end_ts": self.last_ts[rid] + 1,
                          "stream_start_known": False, "sec_uid": r.get("sec_uid")})
            recs[rid] = sessions
        dump = lambda name, obj: (self.out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        dump("rooms.json", rooms)
        dump("recordings.json", recs)
        dump("audio.json", {rid: self.recorders[rid].audio_entries for rid in recs})
        for name in ("gold.json", "causes.json"):
            if not (self.out / name).exists():
                dump(name, [])
        dump("unknown_methods.json", {rid: dict(c.most_common()) for rid, c in self.methods.items()})
        files = sorted(p for p in self.out.rglob("*") if p.is_file() and p.suffix in (".jsonl", ".json") and p.name != "manifest.json")
        dump("manifest.json", {
            "name": self.out.name, "synthetic": False, "collected_at": time.strftime("%Y-%m-%d %H:%M"),
            "files": {str(p.relative_to(self.out)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
            "recordings": {rid: len(s) for rid, s in recs.items()},
        })
        kept = sum(e["duration"] for s in recs.values() for e in s)
        size = sum(p.stat().st_size for p in (self.out / "recordings").rglob("*.ts") if not p.name.startswith("audio_")) / 1e6 if (self.out / "recordings").exists() else 0
        deleted = sum(r.deleted_bytes for r in self.recorders.values()) / 1e6
        audio_min = sum(e["duration"] for r in self.recorders.values() for e in r.audio_entries) / 60
        audio_mb = sum((self.out / e["path"]).stat().st_size for r in self.recorders.values() for e in r.audio_entries) / 1e6
        self.log(f"[收尾] 异常 {self.anomalies} 次，触发保留 {len(self.triggers)} 次；保留录像 {kept / 60:.1f} 分钟、{size:.0f} MB，"
                 f"缓冲中丢弃 {deleted:.0f} MB；整场音频 {audio_min:.1f} 分钟、{audio_mb:.0f} MB")
        self.log(f"[收尾] 数据集已整理：{self.out}（录像 {sum(len(s) for s in recs.values())} 段）。"
                 f"下一步：python3 -m zhibo run --config configs/live.json --data {self.out}")


def cmd_collect(args, cfg: Dict[str, Any]) -> None:
    rooms = [parse_room(a) for a in args.room]
    out = Path(args.out or time.strftime("data/live_%Y%m%d_%H%M"))
    out.mkdir(parents=True, exist_ok=True)
    salt_file = Path("state/salt.txt")
    salt_file.parent.mkdir(parents=True, exist_ok=True)
    if not salt_file.exists():
        salt_file.write_text(secrets.token_hex(16))
    hub = Hub(f"http://127.0.0.1:{args.port}")
    proc = None
    if not hub.alive():
        if not args.start_hub:
            raise SystemExit(f"dyhub 没在运行（端口 {args.port}），加 --start-hub 让我顺带启动")
        proc = start_hub(args.port)
        for _ in range(60):
            if hub.alive():
                break
            time.sleep(0.5)
        else:
            raise SystemExit("dyhub 启动失败，看 third_party/dyhub/dyhub.log")
    def _on_term(*_):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _on_term)
    try:
        Collector(rooms, out, hub, salt_file.read_text().strip(), cfg, quality=args.quality,
                  min_free_gb=args.min_free_gb, full_record=args.full_record).run(args.hours, cfg["signals"]["laugh_words"])
    finally:
        if proc:
            proc.terminate()
