"""主流程：L1 信号 → 热度异常 → 归因 → 调度/录制覆盖 → 回溯 → 判定 → 出片 → 评测。

按秒推进的事件循环。回放模式下时钟来自消息的接收时间；实时模式换成 LiveSource 后，
同一套循环照常工作（需求 8.3 节"两种模式共用同一套业务代码"）。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import copy
import time
from collections import Counter, deque
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Dict, List

from .edit import backtrack

from .core import media
from .detect.attribution import attribute
from .evaluate.evaluate import evaluate
from .edit.judge import OUTPUT_SPEC, PROMPT_REV_V2, judge_candidate, make_judge
from .core.ledger import Ledger
from .core.models import Attribution, Candidate, HeatEvent, write_jsonl
from .edit.render import render_clip
from .evaluate.report import write_report
from .act.scheduler import Scheduler
from .act.replay import ReplayPolicy
from .edit import avsync, evidence
from .engine import RoomEngine
from .ingest.sources import ReplaySource


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _quote_found(quote: str, text: str) -> float:
    """模型照抄的原话有多少能在选中的句子里找到：去掉标点空白后，原话里相邻两个字组成的"字对"出现在句子里的比例。"""
    def grams(s: str) -> set:
        s = "".join(ch for ch in s.lower() if ch.isalnum())
        return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())
    q, t = grams(quote), grams(text)
    return len(q & t) / len(q) if q else 0.0


def _cut_from_segments(cut: Dict[str, int], quote: str, sents: List[Dict[str, Any]], segments: List[Dict[str, Any]],
                       min_found: float) -> Dict[str, int]:
    """模型偶尔把段号当句号填进 cut（EVO-032：陈泽那段该填句号 53～56，填成了段号 S20～S21 的 20～21）。
    照抄的笑点原话不在所选句子里、但在 S{from}～S{to} 这几段的句子里时，换成这几段的句号；否则原样返回。"""
    if not quote or quote in ("（笑声）", "(笑声)"):
        return cut
    if _quote_found(quote, " ".join(x["text"] for x in sents[cut["from"] - 1:cut["to"]])) >= min_found:
        return cut
    seg = {sg["id"]: sg for sg in segments}
    a, b = seg.get(f"S{cut['from']}"), seg.get(f"S{cut['to']}")
    if not a or not b:
        return cut
    inside = [x for x in sents if a["start"] <= (x["start"] + x["end"]) / 2 < b["end"]]
    if inside and _quote_found(quote, " ".join(x["text"] for x in inside)) >= min_found:
        return {"from": inside[0]["n"], "to": inside[-1]["n"]}
    return cut


def run_pipeline(cfg: Dict[str, Any], dataset_dir: Path, out_dir: Path) -> Dict[str, Any]:
    wall0 = time.time()
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    (out_dir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    src = ReplaySource(dataset_dir)
    rooms = {r.room_id: r for r in src.rooms}
    engines = {rid: RoomEngine(rid, cfg) for rid in rooms}
    fused = cfg["heat"].get("kind", "chat_z") == "fused"
    v2 = cfg["judge"].get("evidence", "v1") == "v2"  # 证据包第 2 版（edit/evidence.py）
    perception = {rid: src.perception(rid) for rid in rooms} if fused or v2 else {}
    transcripts = {rid: src.transcript(rid) for rid in rooms} if fused or v2 else {}
    policy = ReplayPolicy(cfg)
    lead = float(cfg.get("fuse", {}).get("audio_lead_seconds", 0))
    sched = Scheduler(rooms, cfg)
    b0 = cfg["backtrack"]
    judge = make_judge(cfg["judge"])
    if hasattr(judge, "base_dir"):
        judge.base_dir = out_dir  # 截图路径相对于本次运行的输出目录
    refine = bool(cfg["judge"].get("refine"))
    plan_cfg = cfg
    if refine:  # 先粗后细：先截一个宽窗口，让判定器在里面选段，最后按选中的段收紧切点
        plan_cfg = copy.deepcopy(cfg)
        plan_cfg["backtrack"].update(signal_lag_seconds=0, preroll_seconds=float(b0["coarse_pre_seconds"]),
                                     postroll_seconds=float(b0["coarse_post_seconds"]),
                                     max_seconds=float(b0["coarse_max_seconds"]))
    room_chats = {rid: deque(maxlen=50000) for rid in rooms}  # (时刻, 弹幕)，给判定器按段看弹幕
    ledger = Ledger(Path(cfg["ledger"]["path"]), replay_only=bool(cfg["ledger"]["replay_only"]))
    bounds: Dict[str, List[float]] = {}
    signals: List[Dict[str, Any]] = []
    events: List[HeatEvent] = []
    attrs: List[Attribution] = []
    cands: List[Candidate] = []
    stages: List[Dict[str, Any]] = []
    counters: Counter = Counter()
    packers: Dict[str, Any] = {}  # 成片包装用的剪辑方案生成器（第一次用到时创建）
    b = cfg["backtrack"]

    def stage(cand: Candidate, name: str, now: float, t0: float) -> None:
        stages.append({"cand_id": cand.cand_id, "stage": name, "t": now, "wall_ms": round((time.time() - t0) * 1000, 1)})

    def rec_bounds(rec) -> List[float]:
        key = str(rec.path)
        if key not in bounds:  # 存录像内的相对时间：对时后同一个文件的起点会变
            bounds[key] = media.silence_boundaries(rec.path, b["silence_noise_db"], b["silence_min_seconds"])
        return [rec.start_ts + x for x in bounds[key]]

    def handle(ev: HeatEvent, now: float) -> None:
        events.append(ev)
        room = rooms[ev.room_id]
        if fused:  # 异常期间（换算回声音时间）主播说的话，给归因看关键词
            a0, a1 = ev.start - lead - 30, ev.end - lead
            ev.stats["transcript"] = " / ".join(x["text"] for x in transcripts.get(ev.room_id, [])
                                                 if a0 <= x["start"] <= a1)[:400]
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
        sync = avsync.measure(rec.path, rec.start_ts, ev.peak, transcripts.get(ev.room_id, []), cfg) if v2 else None
        if sync:  # 画面比逐句稿晚 offset 秒：把录像起点往前挪，之后截图、切片都按逐句稿的时间对齐（EVO-033）
            rec = replace(rec, start_ts=rec.start_ts - sync["offset"])
        if v2:  # 宽窗口 = 实时即时重放会保存的区间，切点吸附到句子停顿（没有逐句稿时退回静音边界）
            tr = transcripts.get(ev.room_id, [])
            sent = evidence.sentence_bounds(tr, rec.start_ts, rec.start_ts + rec.duration)
            a, bb = policy.window(ev)
            k = int(cfg["judge"].get("sheets_max", 4)) * 9
            seg_cfg = copy.deepcopy(cfg)
            p = backtrack.plan_window(a, bb, rec, sorted(set(sent) | set(rec_bounds(rec))), sent, seg_cfg)
            while len(p.segments) > k:  # 段太多九宫格放不下：加大每段最短时长
                seg_cfg["judge"]["segment_min_seconds"] = float(seg_cfg["judge"].get("segment_min_seconds", 8)) + 2
                p = backtrack.plan_window(a, bb, rec, sorted(set(sent) | set(rec_bounds(rec))), sent, seg_cfg)
        else:
            p = backtrack.plan(ev, rec, rec_bounds(rec), plan_cfg)
        # 候选不能超出这个直播间的数据范围（实测在数据末尾的异常，收尾时间会超出最后一条消息）
        if room.end_ts is not None and p.end > room.end_ts:
            p.end, p.end_source = room.end_ts, "data_end"
        if p.start < room.start_ts:
            p.start, p.start_source = room.start_ts, "data_start"
        # 候选窗口可能比检测时刻还晚（先粗后细时会往后多留几秒），要等窗口走完才能判定
        now = max(now, p.end)
        if v2:
            ns = sched.clamp_start(ev.room_id, p.start, p.end, now)
            if ns > p.start:
                p.start, p.start_source = ns, "recording_start"
                p.segments = [sg for sg in p.segments if sg["end"] > ns]
                if p.segments:
                    p.segments[0]["start"] = round(max(p.segments[0]["start"], ns), 3)
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
                if v2:  # 宽窗口有 5 分钟，彼此大段重叠；去重看已切出的片段有没有盖住这次异常本身（换算回声音时间）
                    core0, core1 = ev.start - lead, ev.end - lead
                    ov = _overlap(other.start, other.end, core0, core1)
                    dup = ov >= float(b["duplicate_overlap"]) * max(core1 - core0, 1.0)
                else:
                    ov = _overlap(other.start, other.end, cand.start, cand.end)
                    dup = ov >= float(b["duplicate_overlap"]) * min(other.end - other.start, cand.end - cand.start)
                if dup:
                    cand.status, cand.note = "duplicate", f"与 {other.cand_id} 重叠，合并去重"
                    return

        if judge.uses_ledger and counters["judge_new_calls"] >= int(cfg["judge"]["budget"]):
            cand.status, cand.note = "deferred", "GLM 配额用完：素材已保留，延后判定"
            return

        t0 = time.time()
        frames = []
        n_frames = int(cfg["judge"].get("frames", 0))
        if v2 and n_frames and judge.kind != "accept_all":
            paths = evidence.contact_sheets(rec.path, [rec.rel((sg["start"] + sg["end"]) / 2) for sg in p.segments],
                                            out_dir / "evidence" / cand.cand_id)
            frames = [str(Path(x).relative_to(out_dir)) for x in paths]
        elif n_frames and judge.kind != "accept_all":
            if refine:  # 每段中间一张图，最多 frames_max 张
                segs = p.segments
                k = int(cfg["judge"].get("frames_max", 8))
                pick = segs if len(segs) <= k else [segs[round(i * (len(segs) - 1) / (k - 1))] for i in range(k)]
                times = [(sg["start"] + sg["end"]) / 2 for sg in pick]
            else:
                times = [p.start + 1, ev.peak, p.end - 1][:n_frames]
            paths = media.extract_frames(rec.path, [rec.rel(t) for t in times], out_dir / "evidence" / cand.cand_id)
            frames = [str(Path(x).relative_to(out_dir)) for x in paths]
        curve = [s["chat_users"] for s in signals if s["room_id"] == ev.room_id and p.start <= s["t"] <= p.end]
        chats = list(room_chats[ev.room_id])
        request = {
            "task": "判断这个直播片段值不值得切成独立切片，严格按 output_spec 输出 JSON；只选段号，不要输出时间戳",
            "judge": judge.kind,
            "output_spec": OUTPUT_SPEC,
            "room": {"room_id": room.room_id, "anchor": room.anchor, "category": room.category},
            "attribution": {"cause": attr.cause, "confidence": attr.confidence, "evidence": attr.evidence},
            "segments": [
                {"id": s["id"], "start": round(s["start"] - p.start, 2), "end": round(s["end"] - p.start, 2),
                 "chats": [t[:20] for t, _ in Counter(
                     x for ts, x in room_chats[ev.room_id] if s["start"] <= ts < s["end"] and x).most_common(8)]}
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
        sents = []
        if v2:
            request["version"] = 2
            request["prompt_rev"] = PROMPT_REV_V2  # 提示改了，账本的键跟着变，不会回放按旧提示得到的回答
            # 这个直播间最近已经切过的笑点：避免在后面的窗口里又选中同一个梗（观众会持续刷好一阵）
            recent = [c.judge.get("punchline", "") for c in cands[:-1]
                      if c.room_id == cand.room_id and c.status == "accepted" and c.judge
                      and cand.start - c.end <= float(cfg["judge"].get("same_punchline_window_seconds", 600))]
            if recent:
                request["already_clipped"] = recent
            sents = evidence.window_sentences(transcripts.get(ev.room_id, []), p.start, p.end)
            request["sentence_count"] = len(sents)
            request["target_seconds"] = cfg["judge"].get("target_seconds", "15～30")
            if cfg["judge"].get("taste"):  # 人工审核总结出的口味，写进提示（hotclip 的 reviewMemory 做法）
                request["taste"] = list(cfg["judge"]["taste"])
            for seg, raw in zip(request["segments"], p.segments):
                seg.update(evidence.segment_details(raw, sents,
                                                    perception.get(ev.room_id, {}), chats, lead))
            request["evidence"]["audio_peaks"] = ev.stats.get("audio_peaks", {})
            request["evidence"]["transcript"] = ev.stats.get("transcript", "")
        res = judge_candidate(judge, ledger, request, int(cfg["judge"]["max_revisions"]))
        for st in res["ledger"]:
            counters[f"judge_{st}"] += 1
            if st in ("received", "failed"):  # 失败的调用也花了钱，同样计入配额
                counters["judge_new_calls"] += 1
        stage(cand, "judge", now, t0)
        if res["status"] != "ok":
            cand.status, cand.note = "judge_failed", res["error"]
            return
        cand.judge = res["result"]
        min_score = cfg["judge"].get("min_funny_score") if v2 else None
        if min_score is not None and cand.judge["worth_clipping"] and int(cand.judge.get("funny_score", 0)) < int(min_score):
            cand.status = "rejected"
            cand.note = f"好笑分 {cand.judge.get('funny_score')} 低于 {min_score}：{cand.judge.get('punchline', '')}"[:200]
            return
        if not cand.judge["worth_clipping"]:
            cand.status, cand.note = "rejected", cand.judge["reason"]
            return
        cand.status = "accepted"
        if refine or v2:
            chosen = [sid for part in ("setup", "climax", "reaction") for sid in cand.judge["structure"].get(part, [])]
            spans = [sg for sg in p.segments if sg["id"] in chosen]
            if spans:
                ns, ne = min(sg["start"] for sg in spans), max(sg["end"] for sg in spans)
                mn, mx = float(b0["min_seconds"]), float(b0["max_seconds"])
                ne = min(max(ne, ns + mn), ns + mx, p.end)
                cand.start, cand.end = round(ns, 3), round(ne, 3)
                cand.start_source = cand.end_source = "judge_segment"
        cut = cand.judge.get("cut") if v2 else None
        if cut and sents:  # 精剪：按模型给的句号范围，切点取句子起止（前留一点、后留笑声收尾）
            j = cfg["judge"]
            quote = str(cand.judge.get("key_quote") or "").strip()
            cut = _cut_from_segments(cut, quote, sents, p.segments, float(j.get("quote_min_found", 0.5)))
            if cut is not cand.judge["cut"]:
                cand.judge["cut_from_segments"] = [cut["from"], cut["to"]]
            frm = cut["from"]
            # 笑点就在选的第一句里时，把前面紧挨着的一句也带上当铺垫（用户反馈：吕德华"骂一半秒怂"那段
            # 切在了"我不顶死你呀"中间，看不出他为什么发火）
            if frm > 1 and quote and quote not in ("（笑声）", "(笑声)") \
                    and _quote_found(quote, sents[frm - 1]["text"]) >= float(j.get("quote_min_found", 0.5)) \
                    and sents[frm - 1]["start"] - sents[frm - 2]["end"] <= float(j.get("setup_max_gap", 6)):
                frm -= 1
                cand.judge["cut_setup_from"] = frm
            ns = sents[frm - 1]["start"] - float(j.get("cut_pre_seconds", 0.5))
            # 模型选的最后几句如果和前一句隔了很久，多半是为了把观众反应带上而多选的，去掉
            # （test10 Gus：多选了 10 秒后无关的第 40 句，片段从 13.8 秒变成 25.9 秒，用户嫌长）
            to = cut["to"]
            gap_max = float(j.get("max_sentence_gap", 4))
            while to > frm and sents[to - 1]["start"] - sents[to - 2]["end"] > gap_max:
                to -= 1
            if to != cut["to"]:
                cand.judge["cut_trimmed_to"] = to
            last = sents[to - 1]["end"]
            mn, mx = float(j.get("clip_min_seconds", 10)), float(j.get("clip_max_seconds", 40))
            if cand.judge.get("payoff") == "画面":  # 画面笑点要等动作做完，上限放宽（EVO-032）
                mx = max(mx, float(j.get("visual_clip_max_seconds", mx)))
            tail = float(j.get("cut_tail_seconds", 2))
            if last + tail - ns > mx:  # 太长：截到上限以内最后一句的结尾
                fit = [x["end"] for x in sents[frm - 1:to] if x["end"] + tail - ns <= mx]
                last = max(fit) if fit else ns + mx - tail
            # 最后一句后面紧接着有笑声，就把笑声留完（用户反馈：有的片段好笑的只是笑声本身）
            last = evidence.laugh_end(last, perception.get(ev.room_id, {}), ns + mx - tail)
            if cand.judge.get("payoff") == "画面":
                # 画面笑点：动作在台词之后，主播几乎不说话，笑声隔几秒才出来（EVO-032：陈泽 PK 受罚模仿那段，
                # 最后一句 208.8 秒说完，学动作在 211～226 秒，笑声在 217、224 秒，原来片子停在 210.8 秒）。
                # 往后最多看 visual_lookahead_seconds 秒，遇到主播开始说一句长话（换话题）就停，留到其中最晚的笑声
                look = last + float(j.get("visual_lookahead_seconds", 18))
                long_next = [x["start"] for x in sents[to:]
                             if x["start"] >= last and len(x["text"]) >= int(j.get("visual_stop_chars", 8))]
                if long_next:
                    look = min(look, long_next[0])
                tail_to = evidence.last_laugh(last, perception.get(ev.room_id, {}), min(look, ns + mx - tail))
                if tail_to > last:
                    cand.judge["visual_tail"] = [round(last, 3), round(tail_to, 3)]
                    last = tail_to
            ne = max(last + tail, ns + mn)
            ns, ne = max(ns, p.start), min(ne, p.end)
            cand.start, cand.end = round(ns, 3), round(ne, 3)
            cand.start_source = cand.end_source = "judge_sentence"
            # 核对模型照抄的笑点原话确实在选中的句子里（hotclip 的"引用原话反查、对不上就丢"）。
            # test10 Gus 第二个候选：窗口里只剩观众还在刷 hungry，模型说笑点是 hungry，选的却是"别的外的 / 一百零五"
            chosen_sents = sents[frm - 1:to]
            if quote in ("（笑声）", "(笑声)"):
                grounded = any(evidence.sentence_laugh(x, perception.get(ev.room_id, {})) >= evidence.LAUGH_MARK
                               for x in chosen_sents)
            else:
                grounded = _quote_found(quote, " ".join(x["text"] for x in chosen_sents)) >= \
                    float(j.get("quote_min_found", 0.5))
            if quote and not grounded:
                cand.status = "rejected"
                cand.note = f"模型说的笑点原话「{quote[:30]}」不在它选的句子里，可能看错了位置"
                return
        if v2:  # 宽窗口互相重叠，模型可能在另一次异常的窗口里又选中同一个笑点（test10 Gus 的 hungry 梗被选了两次）
            for other in cands[:-1]:
                if other.room_id == cand.room_id and other.status == "accepted":
                    ov = _overlap(other.start, other.end, cand.start, cand.end)
                    gap = max(other.start, cand.start) - min(other.end, cand.end)  # 不重叠时两段之间隔了多久
                    near = gap <= float(cfg["judge"].get("adjacent_gap_seconds", 10))
                    if near or ov >= float(b["duplicate_overlap"]) * min(other.end - other.start, cand.end - cand.start):
                        # 紧挨着的也算同一个笑点（test10 Gus：第二段是观众纠正 hungry 的后半截，和第一段只隔几秒）
                        cand.status, cand.note = "duplicate", f"判定选中的段和 {other.cand_id} 重叠或紧挨着，合并去重"
                        return
        if cfg["render"]["enabled"]:
            t0 = time.time()
            cand.clip = render_clip(cand, rec, out_dir, cfg, transcripts.get(ev.room_id) if v2 else None)
            if sync:
                cand.clip["av_offset"] = sync["offset"]
            stage(cand, "render", now, t0)
            if not cand.clip["check_ok"]:
                cand.status, cand.note = "render_failed", "；".join(cand.clip["problems"])
            elif v2 and cfg.get("package", {}).get("enabled"):  # 接受一段就出一段竖屏成片（edit/package.py）
                t0 = time.time()
                try:
                    from .edit.package_run import package_candidate
                    from .edit.package import GLMPlanner, MockPlanner

                    use_glm = cfg["package"].get("planner", cfg["judge"].get("kind")) == "glm"
                    if "planner" not in packers:
                        packers["planner"] = GLMPlanner(cfg["judge"]) if use_glm else MockPlanner(cfg["package"])
                    (out_dir / "vertical").mkdir(exist_ok=True)
                    res = package_candidate(asdict(cand), out_dir, src, room.anchor, cfg, packers["planner"], ledger,
                                            use_glm, out_dir / "vertical")
                    cand.clip["vertical"] = {k: res[k] for k in ("path", "cover", "post", "duration", "hook", "zooms",
                                                                 "sfx", "layout", "check_ok", "problems")}
                except ImportError as e:  # 没有 Pillow（系统 Python）：只出横屏片段
                    cand.clip["vertical"] = {"skipped": f"缺少依赖：{e}"}
                except Exception as e:  # noqa: BLE001  包装失败不影响横屏片段
                    cand.clip["vertical"] = {"failed": str(e)[:300]}
                stage(cand, "package", now, t0)

    def process_second(t: float) -> None:
        for rid in sorted(rooms):
            if not rooms[rid].is_live(t):
                continue
            if fused:
                engines[rid].feed_audio(perception[rid].get(int(t)))
            st = engines[rid].step(t)
            signals.append(st.point.to_row())
            sched.observe(rid, t, st.point.online, engines[rid].z_smooth)
            if st.event:
                handle(st.event, t)
        sched.tick(t)

    cur = None
    for msg in src.messages():
        sec = float(int(msg.recv_ts))
        if cur is None:
            cur = sec
        while cur < sec:
            process_second(cur)
            cur += 1
        engines[msg.room_id].feed(msg)
        if msg.type == "chat":
            room_chats[msg.room_id].append((msg.recv_ts, msg.payload.get("text", "")))
    end_t = max((r.end_ts or cur + 1) for r in rooms.values())
    while cur is not None and cur < end_t:
        process_second(cur)
        cur += 1
    for rid in sorted(rooms):
        ev = engines[rid].flush(end_t)
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
