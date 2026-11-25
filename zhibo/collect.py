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

import copy
import hashlib
import json
import queue
import re
import secrets
import signal
import threading
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from .detect.attribution import CAUSE_ZH, attribute
from .core.models import RawMessage, Room
from .ingest.douyin import parse_room, resolve_live_room
from .ingest.hub import Hub, start_hub
from .ingest.recorder import Recorder
from .act.replay import ReplayPolicy
from .engine import RoomEngine

SPECIAL_METHODS = [  # 未标准化消息按方法名归类（PK 类消息能否拿到是需求文档 Q-1）
    (re.compile(r"LuckyBox|Lottery", re.I), "lucky_bag"),
    (re.compile(r"Envelope|RedPacket", re.I), "red_packet"),
    (re.compile(r"Battle|PK|LinkMic", re.I), "pk"),
]


class Collector:
    def __init__(self, rooms: List[Dict[str, Optional[str]]], out: Path, hub: Hub, salt: str, cfg: Dict[str, Any],
                 quality: str = "ORIGIN", min_free_gb: float = 15.0, full_record: bool = False):
        self.out, self.hub, self.salt, self.cfg = out, hub, salt, cfg
        self.quality, self.min_free_gb = quality, min_free_gb
        self.policy = ReplayPolicy(cfg)  # 即时重放：先占住，确认结束后定稿或释放（act/replay.py）
        self.keep_seconds = self.policy.keep_seconds(full_record)
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
        # 融合检测要实时声音：录制进程多出一路 PCM，交给单独的感知线程（perceive/listener.py）
        self.fused = cfg["heat"].get("kind", "chat_z") == "fused"
        self.lead = float(cfg.get("fuse", {}).get("audio_lead_seconds", 0))
        self.aq: "queue.Queue" = queue.Queue()  # 感知线程 → 检测线程：(直播间, 一秒声音感知)
        self.sentences: Dict[str, Deque[Dict[str, Any]]] = defaultdict(lambda: deque(maxlen=3000))
        self.listener = None
        if self.fused:
            from .perceive.listener import LiveListener  # 要项目 .venv 的 numpy、sherpa-onnx

            self.listener = LiveListener(cfg, out, self.stop, self.log,
                                         on_row=lambda rid, row: self.aq.put((rid, row)),
                                         on_sentence=lambda rid, s: self.sentences[rid].append(s))
        for r in rooms:
            if r["room_id"]:
                self._add_room(r)

    def _add_room(self, r: Dict[str, Optional[str]]) -> None:
        rid = r["room_id"]
        self.files[rid] = (self.out / "messages" / f"{rid}.jsonl").open("a", encoding="utf-8")
        self.status[rid] = "waiting"
        self.recorders[rid] = Recorder(self.hub, rid, self.out / "recordings" / rid, self.stop, self.log,
                                       sec_uid=r.get("sec_uid"), quality=self.quality, min_free_gb=self.min_free_gb,
                                       keep_seconds=self.keep_seconds,
                                       pcm_sink=self.listener.sink if self.listener else None)
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
        engines: Dict[str, RoomEngine] = {}
        cur: Dict[str, int] = {}
        early: Dict[str, List[Dict[str, Any]]] = defaultdict(list)  # 引擎建好之前就到的声音

        def tick(rid: str, t: int) -> None:
            st = engines[rid].step(float(t))
            if st.opened is not None and rid in self.recorders:
                # 先占住，免得等"确认结束"的这段时间里，前面的录像被环形缓冲删掉
                self.held[rid] = self.recorders[rid].pin(self.policy.hold_start(st.opened))
            if st.event:
                self._on_event(st.event)

        while not (self.stop.is_set() and self.q.empty()):
            try:
                rid, msg = self.q.get(timeout=0.5)
            except queue.Empty:
                rid, msg = None, None
            if rid is not None:
                if rid not in engines:
                    engines[rid], cur[rid] = RoomEngine(rid, self.cfg), int(msg.recv_ts)
                    for row in early.pop(rid, []):
                        engines[rid].feed_audio(row)
                while cur[rid] < int(msg.recv_ts):
                    tick(rid, cur[rid])
                    cur[rid] += 1
                engines[rid].feed(msg)
            while True:  # 声音感知：领先弹幕 lead 秒，引擎按 t − lead 取用，一般早就到了
                try:
                    arid, row = self.aq.get_nowait()
                except queue.Empty:
                    break
                if arid in engines:
                    engines[arid].feed_audio(row)
                else:
                    early[arid].append(row)
            now = int(time.time())
            for r in list(cur):
                while cur[r] < now - 2:  # 留 2 秒给晚到的消息
                    tick(r, cur[r])
                    cur[r] += 1
        for r in list(engines):
            ev = engines[r].flush(float(cur[r]))
            if ev:
                self._on_event(ev)

    def _on_event(self, ev) -> None:
        info = self.rooms[ev.room_id]
        room = Room(ev.room_id, info["anchor"], "搞笑", "manual", self.first_ts.get(ev.room_id, ev.start),
                    stream_start_known=False, sec_uid=info.get("sec_uid"))
        if self.fused:  # 异常期间（换算回声音时间）主播说的话，给归因看关键词；和离线回放 pipeline.py 一致
            a0, a1 = ev.start - self.lead - 30, ev.end - self.lead
            ev.stats["transcript"] = " / ".join(x["text"] for x in list(self.sentences[ev.room_id])
                                                 if a0 <= x["start"] <= a1)[:400]
        attr = attribute(ev, room, self.cfg)
        self.anomalies += 1
        hhmm = time.strftime("%H:%M:%S", time.localtime(ev.start))
        keep = self.policy.keeps(attr.cause)
        msg = (f"[{'触发' if keep else '异常'}] {info['anchor']} {hhmm} 起 {ev.end - ev.start:.0f} 秒，"
               f"{CAUSE_ZH.get(attr.cause, attr.cause)}，{ev.channel} z={ev.peak_z}")
        rec, held = self.recorders.get(ev.room_id), self.held.pop(ev.room_id, None)
        if keep:
            a, b = self.policy.window(ev)
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
            if self.listener:
                st = self.listener.stats()
                parts.append(f"声音感知：已处理 {sum(st['done'].values())} 秒、积压 {st['backlog_seconds']} 秒、"
                             f"丢弃 {sum(st['dropped'].values())} 秒、累计用时 {st['work_seconds']} 秒")
            self.log("[每分钟] " + "；".join(parts) + f"｜累计异常 {self.anomalies}，触发保留 {len(self.triggers)}")

    def run(self, hours: float, laugh_words: List[str]) -> None:
        self.hub.ensure_account()
        self.started = True
        if self.listener:
            self.listener.start()
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
                f"环形缓冲（只保留最近 {self.keep_seconds / 60:.1f} 分钟；确认搞笑异常结束后往回存 {self.policy.replay / 60:.0f} 分钟）")
        self.log(f"开始采集：{len(self.rooms)} 个直播间，另有 {len(self.pending)} 位主播等待开播；"
                 f"最长 {hours} 小时，Ctrl+C 结束。录制方式：{mode}。"
                 f"检测：{'弹幕 + 声音融合（实时声音感知）' if self.fused else '只看弹幕'}。输出：{self.out}")
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
            if self.listener:
                self.listener.join(60)  # 把排队的声音处理完、文件关好
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
        if self.listener:
            st = self.listener.stats()
            lag = {self.rooms[rid]["anchor"]: self.recorders[rid].pcm_lag for rid in recs}
            self.log(f"[收尾] 声音感知：处理 {sum(st['done'].values())} 秒、丢弃 {sum(st['dropped'].values())} 秒、"
                     f"用时 {st['work_seconds']} 秒；PCM 和画面开头的时间差（秒）{lag}")
        self.log(f"[收尾] 数据集已整理：{self.out}（录像 {sum(len(s) for s in recs.values())} 段）。"
                 f"下一步：python3 -m zhibo run --config configs/live.json --data {self.out}")


def _choose_detector(want: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """--detector：fused 要 numpy、sherpa-onnx 和模型文件都在；auto 时缺了就退回只看弹幕。"""
    cfg = copy.deepcopy(cfg)
    if want == "chat":
        cfg["heat"]["kind"] = "chat_z"
        return cfg
    models = Path(cfg.get("perceive", {}).get("models_dir", "models"))
    try:
        import numpy  # noqa: F401
        import sherpa_onnx  # noqa: F401
        ok, why = models.is_dir(), f"找不到模型目录 {models}（先运行 scripts/fetch_models.sh）"
    except ImportError as e:
        ok, why = False, f"缺少 {e.name}（用项目 .venv/bin/python 运行）"
    if ok:
        cfg["heat"]["kind"] = "fused"
    elif want == "fused":
        raise SystemExit(f"--detector fused 用不了：{why}")
    else:
        print(f"声音感知用不了，只看弹幕：{why}", flush=True)
        cfg["heat"]["kind"] = "chat_z"
    return cfg


def cmd_collect(args, cfg: Dict[str, Any]) -> None:
    cfg = _choose_detector(getattr(args, "detector", "auto"), cfg)
    rooms = [parse_room(a) for a in args.room]
    out = Path(args.out or time.strftime("data/live_%Y%m%d_%H%M"))
    out.mkdir(parents=True, exist_ok=True)
    days = (cfg.get("retention") or {}).get("video_days")
    if days:  # 开始采集前先清掉过期的画面录像（EVO-029），免得服务器磁盘被慢慢占满
        from .retention import prune_videos

        prune_videos(out.parent, float(days), apply=True, log=lambda m: print(m, flush=True))
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
