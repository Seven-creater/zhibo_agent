"""主流程：L1 信号 → 热度异常 → 归因 → 调度/录制覆盖 → 回溯 → 判定 → 出片 → 评测。

按秒推进的事件循环。回放模式下时钟来自消息的接收时间；实时模式换成 LiveSource 后，
同一套循环照常工作（需求 8.3 节"两种模式共用同一套业务代码"）。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

from . import backtrack, media
from .attribution import attribute
from .evaluate import evaluate
from .heat import HeatDetector
from .judge import OUTPUT_SPEC, judge_candidate, make_judge
from .ledger import Ledger
from .models import Attribution, Candidate, HeatEvent, write_jsonl
from .render import render_clip
from .report import write_report
from .scheduler import Scheduler
from .signals import SignalAggregator
from .sources import ReplaySource


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def run_pipeline(cfg: Dict[str, Any], dataset_dir: Path, out_dir: Path) -> Dict[str, Any]:
    wall0 = time.time()
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    (out_dir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    src = ReplaySource(dataset_dir)
    rooms = {r.room_id: r for r in src.rooms}
    aggs = {rid: SignalAggregator(rid, cfg) for rid in rooms}
    dets = {rid: HeatDetector(rid, cfg) for rid in rooms}
    sched = Scheduler(rooms, cfg)
    judge = make_judge(cfg["judge"])
    ledger = Ledger(Path(cfg["ledger"]["path"]), replay_only=bool(cfg["ledger"]["replay_only"]))
    bounds: Dict[str, List[float]] = {}
    signals: List[Dict[str, Any]] = []
    events: List[HeatEvent] = []
    attrs: List[Attribution] = []
    cands: List[Candidate] = []
    stages: List[Dict[str, Any]] = []
    counters: Counter = Counter()
    b = cfg["backtrack"]

    def stage(cand: Candidate, name: str, now: float, t0: float) -> None:
        stages.append({"cand_id": cand.cand_id, "stage": name, "t": now, "wall_ms": round((time.time() - t0) * 1000, 1)})

    def rec_bounds(rec) -> List[float]:
        key = str(rec.path)
        if key not in bounds:
            rel = media.silence_boundaries(rec.path, b["silence_noise_db"], b["silence_min_seconds"])
            bounds[key] = [rec.start_ts + x for x in rel]
        return bounds[key]

    def handle(ev: HeatEvent, now: float) -> None:
        events.append(ev)
        room = rooms[ev.room_id]
        attr = attribute(ev, room, cfg)
        attrs.append(attr)
        if attr.cause not in cfg["trigger_causes"]:
            return
        t0 = time.time()
        rec = src.recording(ev.room_id, ev.peak)
        if rec is None:
            cands.append(Candidate(
                cand_id=f"{ev.event_id}-c", room_id=ev.room_id, event_id=ev.event_id, cause=attr.cause,
                start=ev.start, end=ev.end, start_source="none", end_source="none", created_at=now,
                status="lost_not_recording", note="高潮时刻没有录像（录制中断或还没开始）",
            ))
            return
        p = backtrack.plan(ev, rec, rec_bounds(rec), cfg)
        # 候选不能超出这个直播间的数据范围（实测在数据末尾的异常，收尾时间会超出最后一条消息）
        if room.end_ts is not None and p.end > room.end_ts:
            p.end, p.end_source = room.end_ts, "data_end"
        if p.start < room.start_ts:
            p.start, p.start_source = room.start_ts, "data_start"
        cand = Candidate(
            cand_id=f"{ev.event_id}-c", room_id=ev.room_id, event_id=ev.event_id, cause=attr.cause,
            start=p.start, end=p.end, start_source=p.start_source, end_source=p.end_source,
            created_at=now, status="planned",
        )
        cands.append(cand)
        stage(cand, "backtrack", now, t0)

        ok, why = sched.covered(ev.room_id, p.start, p.end, now)
        if not ok:
            cand.status, cand.note = "lost_not_recording", why
            return
        for other in cands[:-1]:
            if other.room_id == cand.room_id and other.status == "accepted":
                ov = _overlap(other.start, other.end, cand.start, cand.end)
                if ov >= float(b["duplicate_overlap"]) * min(other.end - other.start, cand.end - cand.start):
                    cand.status, cand.note = "duplicate", f"与 {other.cand_id} 重叠，合并去重"
                    return

        if judge.uses_ledger and counters["judge_new_calls"] >= int(cfg["judge"]["budget"]):
            cand.status, cand.note = "deferred", "GLM 配额用完：素材已保留，延后判定"
            return

        t0 = time.time()
        frames = []
        n_frames = int(cfg["judge"].get("frames", 0))
        if n_frames and judge.kind != "accept_all":
            times = [p.start + 1, ev.peak, p.end - 1][:n_frames]
            paths = media.extract_frames(rec.path, [rec.rel(t) for t in times], out_dir / "evidence" / cand.cand_id)
            frames = [str(Path(x).relative_to(out_dir)) for x in paths]
        curve = [s["chat_users"] for s in signals if s["room_id"] == ev.room_id and p.start <= s["t"] <= p.end]
        request = {
            "task": "判断这个直播片段值不值得切成独立切片，严格按 output_spec 输出 JSON；只选段号，不要输出时间戳",
            "judge": judge.kind,
            "output_spec": OUTPUT_SPEC,
            "room": {"room_id": room.room_id, "anchor": room.anchor, "category": room.category},
            "attribution": {"cause": attr.cause, "confidence": attr.confidence, "evidence": attr.evidence},
            "segments": [
                {"id": s["id"], "start": round(s["start"] - p.start, 2), "end": round(s["end"] - p.start, 2)}
                for s in p.segments
            ],
            "evidence": {
                "frames": frames,
                "top_texts": ev.stats["top_texts"],
                "laugh_ratio": ev.stats["laugh_ratio"],
                "chat_users": ev.stats["chat_users"],
                "gift_total": ev.stats["gift_total"],
                "signal_start_rel": round(ev.start - p.start, 2),
                "peak_rel": round(ev.peak - p.start, 2),
                "chat_users_curve": curve,
            },
        }
        res = judge_candidate(judge, ledger, request, int(cfg["judge"]["max_revisions"]))
        for st in res["ledger"]:
            counters[f"judge_{st}"] += 1
            if st == "received":
                counters["judge_new_calls"] += 1
        stage(cand, "judge", now, t0)
        if res["status"] != "ok":
            cand.status, cand.note = "judge_failed", res["error"]
            return
        cand.judge = res["result"]
        if not cand.judge["worth_clipping"]:
            cand.status, cand.note = "rejected", cand.judge["reason"]
            return
        cand.status = "accepted"
        if cfg["render"]["enabled"]:
            t0 = time.time()
            cand.clip = render_clip(cand, rec, out_dir, cfg)
            stage(cand, "render", now, t0)
            if not cand.clip["check_ok"]:
                cand.status, cand.note = "render_failed", "；".join(cand.clip["problems"])

    def process_second(t: float) -> None:
        for rid in sorted(rooms):
            if not rooms[rid].is_live(t):
                continue
            pt = aggs[rid].close(t)
            signals.append(pt.to_row())
            _, ev = dets[rid].update(pt)
            sched.observe(rid, t, pt.online, dets[rid].z_smooth)
            if ev:
                handle(ev, t)
        sched.tick(t)

    cur = None
    for msg in src.messages():
        sec = float(int(msg.recv_ts))
        if cur is None:
            cur = sec
        while cur < sec:
            process_second(cur)
            cur += 1
        aggs[msg.room_id].feed(msg)
    end_t = max((r.end_ts or cur + 1) for r in rooms.values())
    while cur is not None and cur < end_t:
        process_second(cur)
        cur += 1
    for rid in sorted(rooms):
        ev = dets[rid].flush(end_t)
        if ev:
            handle(ev, end_t)
    sched.finalize(end_t)

    write_jsonl(out_dir / "signals.jsonl", signals)
    write_jsonl(out_dir / "heat_events.jsonl", events)
    write_jsonl(out_dir / "attributions.jsonl", attrs)
    write_jsonl(out_dir / "candidates.jsonl", cands)
    write_jsonl(out_dir / "decisions.jsonl", sched.decisions)
    write_jsonl(out_dir / "stages.jsonl", stages)
    (out_dir / "recording_intervals.json").write_text(
        json.dumps({k: v for k, v in sched.intervals.items()}, indent=2), encoding="utf-8"
    )

    fp_src = json.dumps({
        "events": [asdict(e) for e in events],
        "attrs": [asdict(a) for a in attrs],
        "decisions": [asdict(d) for d in sched.decisions],
        "cands": [{k: v for k, v in asdict(c).items() if k != "clip"} for c in cands],
    }, ensure_ascii=False, sort_keys=True)
    meta = {
        "run_name": cfg["run_name"],
        "dataset": str(dataset_dir),
        "fingerprint": hashlib.sha256(fp_src.encode("utf-8")).hexdigest()[:16],
        "counters": dict(counters),
        "recorded_seconds": sched.recorded_seconds(),
        "wall_seconds": round(time.time() - wall0, 2),
    }
    (out_dir / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = evaluate(out_dir, Path(dataset_dir), cfg)
    write_report(out_dir, Path(dataset_dir), metrics)
    return metrics
