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


# 清晰度：FULL_HD1=超清(_uhd)，HD1=高清(_hd)，SD2=标清(_sd)，SD1=流畅(_ld)。
# 实测（Gus 直播间）：HD1 720p 约 2.9 GB/时，SD2 720p 约 1.0 GB/时，SD1 540p 约 0.5 GB/时；
# 播放器默认拉的原画可达约 9 GB/时。
QUALITY_ORDER = ["SD2", "SD1", "HD1", "FULL_HD1"]


def quality_stream_url(sec_uid: str, quality: str) -> Optional[str]:
    flv = ((_reflow(sec_uid).get("stream_url") or {}).get("flv_pull_url")) or {}
    for q in [quality] + [q for q in QUALITY_ORDER if q != quality]:
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
    """一个直播间的录制：每次 ffmpeg 进程是一段，记下这段第一个字节落盘的时刻。"""

    def __init__(self, hub: Hub, room_id: str, out_dir: Path, stop: threading.Event, log,
                 sec_uid: Optional[str] = None, quality: str = "SD2", min_free_gb: float = 15.0):
        super().__init__(daemon=True)
        self.hub, self.room_id, self.dir, self.stop, self.log = hub, room_id, out_dir, stop, log
        self.sec_uid, self.quality, self.min_free_gb = sec_uid, quality, min_free_gb
        self.sessions: List[Dict[str, Any]] = []
        self.proc: Optional[subprocess.Popen] = None
        self.live = False
        self.disk_full = False

    def _free_gb(self) -> float:
        return shutil.disk_usage(self.dir).free / 1e9

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
            path = self.dir / f"{int(time.time() * 1000)}.ts"
            self.proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-rw_timeout", "15000000",
                 "-i", url, "-c", "copy", "-f", "mpegts", str(path)],
                env=_env_no_proxy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            start = None
            last_check = time.time()
            while self.proc.poll() is None and not self.stop.is_set():
                if start is None and path.exists() and path.stat().st_size > 0:
                    start = time.time()  # 第一个字节落盘的时刻 ≈ 录像第 0 秒（误差待实测，Q-6）
                if time.time() - last_check > 30:
                    last_check = time.time()
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
            if start and path.exists() and path.stat().st_size > 0:
                self.sessions.append({"path": path.name, "start_ts": round(start, 3), "end_ts": round(time.time(), 3)})
                self.log(f"[录制] {self.room_id} 一段结束，{path.stat().st_size / 1e6:.0f} MB" + (f"；{err.strip()}" if err.strip() else ""))
            elif path.exists():
                path.unlink()
            if not self.stop.is_set():
                self.stop.wait(5)


class Collector:
    def __init__(self, rooms: List[Dict[str, Optional[str]]], out: Path, hub: Hub, salt: str,
                 quality: str = "SD2", min_free_gb: float = 15.0):
        self.out, self.hub, self.salt = out, hub, salt
        self.quality, self.min_free_gb = quality, min_free_gb
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
                                       sec_uid=r.get("sec_uid"), quality=self.quality, min_free_gb=self.min_free_gb)
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
                size = sum(p.stat().st_size for p in (self.out / "recordings" / rid).glob("*.ts")) / 1e9
                parts.append(f"{r['anchor']}：在线 {self.online.get(rid, '-')}，弹幕 {c['chat']}/分，"
                             f"笑点 {c['laugh']}/分，录像 {size:.2f} GB，{self.status[rid]}")
            parts += [f"{r['anchor']}：等待开播" for r in list(self.pending)]
            self.log("[每分钟] " + "；".join(parts))

    def run(self, hours: float, laugh_words: List[str]) -> None:
        self.hub.ensure_account()
        self.started = True
        for rec in list(self.recorders.values()):
            rec.start()
        threading.Thread(target=self._resolve_pending, daemon=True).start()
        threading.Thread(target=self._keep_connected, daemon=True).start()
        threading.Thread(target=self._monitor, daemon=True).start()
        deadline = time.time() + hours * 3600
        threading.Thread(target=lambda: (self.stop.wait(max(0, deadline - time.time())), self.stop.set()), daemon=True).start()
        self.log(f"开始采集：{len(self.rooms)} 个直播间，另有 {len(self.pending)} 位主播等待开播；"
                 f"最长 {hours} 小时，Ctrl+C 结束。输出：{self.out}")
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
            for f in self.files.values():
                f.close()
            self.finalize()

    def finalize(self) -> None:
        rooms, recs = [], {}
        for rid, r in list(self.rooms.items()):
            sessions = []
            for s in self.recorders[rid].sessions:
                p = self.out / "recordings" / rid / s["path"]
                dur = media.probe(p).get("duration")
                if dur:
                    sessions.append({"path": f"recordings/{rid}/{s['path']}", "start_ts": s["start_ts"],
                                     "duration": round(dur, 3)})
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
        Collector(rooms, out, hub, salt_file.read_text().strip(), quality=args.quality,
                  min_free_gb=args.min_free_gb).run(args.hours, cfg["signals"]["laugh_words"])
    finally:
        if proc:
            proc.terminate()
