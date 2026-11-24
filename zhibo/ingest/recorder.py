"""直播录制：ffmpeg 按 10 秒切小段做环形缓冲（即时重放），同一个 ffmpeg 顺带存整场音频。"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import media
from .douyin import _env_no_proxy, quality_stream_url
from .hub import Hub


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
