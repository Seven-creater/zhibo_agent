"""评测（M9，FR-9.4）：从一次运行的产物和金标准算出第 1.4 节的指标。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.models import read_jsonl


def _ov(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _mean(xs: List[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 2) if xs else None


def evaluate(run_dir: Path, dataset_dir: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    gold = json.loads((dataset_dir / "gold.json").read_text(encoding="utf-8"))
    causes = json.loads((dataset_dir / "causes.json").read_text(encoding="utf-8"))
    rooms = {r["room_id"]: r for r in json.loads((dataset_dir / "rooms.json").read_text(encoding="utf-8"))}
    cands = read_jsonl(run_dir / "candidates.jsonl")
    events = read_jsonl(run_dir / "heat_events.jsonl")
    attrs = {a["event_id"]: a for a in read_jsonl(run_dir / "attributions.jsonl")}
    intervals = json.loads((run_dir / "recording_intervals.json").read_text(encoding="utf-8"))
    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    th = float(cfg["eval"]["min_gold_coverage"])

    clips = [c for c in cands if c["status"] == "accepted"]
    used: set = set()
    gold_rows: List[Dict[str, Any]] = []
    for g in sorted(gold, key=lambda x: (x["room_id"], x["start"])):
        glen = g["end"] - g["start"]
        pool = [c for c in clips if c["room_id"] == g["room_id"] and c["cand_id"] not in used]
        best = max(pool, key=lambda c: _ov(c["start"], c["end"], g["start"], g["end"]), default=None)
        cov = _ov(best["start"], best["end"], g["start"], g["end"]) / glen if best else 0.0
        hit = best is not None and cov >= th
        if hit:
            used.add(best["cand_id"])
        watched = any(a <= g["start"] < b2 for a, b2 in intervals.get(g["room_id"], []))
        row = {
            "gold": g, "hit": hit, "coverage": round(cov, 3), "watched": watched,
            "clip": best["cand_id"] if hit else None,
            "start_err": round(best["start"] - g["start"], 2) if hit else None,
            "end_err": round(best["end"] - g["end"], 2) if hit else None,
            "latency": round(best["created_at"] - g["start"], 1) if hit else None,
            "why": "命中" if hit else _miss_reason(g, cov, best, events, attrs, cands),
        }
        gold_rows.append(row)

    hits = [r for r in gold_rows if r["hit"]]

    def recall(rows: List[Dict[str, Any]]) -> Optional[float]:
        return round(sum(r["hit"] for r in rows) / len(rows), 3) if rows else None

    # 归因准确率：每次热度异常取重叠最多的原因标注；没有标注的不计入
    attr_rows = []
    for ev in events:
        spans = [c for c in causes if c["room_id"] == ev["room_id"]]
        best = max(spans, key=lambda c: _ov(c["start"], c["end"], ev["start"], ev["end"]), default=None)
        label = best["cause"] if best and _ov(best["start"], best["end"], ev["start"], ev["end"]) > 0 else None
        pred = attrs.get(ev["event_id"], {}).get("cause")
        attr_rows.append({"event": ev, "pred": pred, "label": label, "ok": (pred == label) if label else None})
    labeled = [r for r in attr_rows if r["label"]]

    clip_rows = []
    for c in clips:
        g = next((r["gold"] for r in gold_rows if r["clip"] == c["cand_id"]), None)
        clip_rows.append({"cand": c, "gold": g})

    counters = meta["counters"]
    judge_calls = sum(v for k, v in counters.items() if k in ("judge_received", "judge_replayed"))
    metrics = {
        "run_name": meta["run_name"],
        "fingerprint": meta["fingerprint"],
        "n_gold": len(gold),
        "n_clips": len(clips),
        "recall": recall(gold_rows),
        "recall_with_signal_peak": recall([r for r in gold_rows if r["gold"]["has_signal_peak"]]),
        "recall_without_signal_peak": recall([r for r in gold_rows if not r["gold"]["has_signal_peak"]]),
        "precision": round(len(hits) / len(clips), 3) if clips else None,
        "false_positive_rate": round(1 - len(hits) / len(clips), 3) if clips else None,
        "start_err_mean_abs": _mean([abs(r["start_err"]) for r in hits]),
        "end_err_mean_abs": _mean([abs(r["end_err"]) for r in hits]),
        "attribution_accuracy": round(sum(r["ok"] for r in labeled) / len(labeled), 3) if labeled else None,
        "n_heat_events": len(events),
        "miss_watch_rate": round(sum(not r["watched"] for r in gold_rows) / len(gold_rows), 3) if gold_rows else None,
        "latency_mean": _mean([r["latency"] for r in hits]),
        "judge_calls": judge_calls,
        "judge_new_calls": counters.get("judge_received", 0),
        "judge_replayed": counters.get("judge_replayed", 0),
        "recorded_hours": round(meta["recorded_seconds"] / 3600, 3),
        "judge_calls_per_hit": round(judge_calls / len(hits), 2) if hits else None,
        "recorded_hours_per_hit": round(meta["recorded_seconds"] / 3600 / len(hits), 3) if hits else None,
        "wall_seconds": meta["wall_seconds"],
        "_gold_rows": gold_rows,
        "_attr_rows": attr_rows,
        "_clip_rows": clip_rows,
        "_rooms": rooms,
    }
    public = {k: v for k, v in metrics.items() if not k.startswith("_")}
    (run_dir / "metrics.json").write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")
    return metrics


def _miss_reason(g, cov, best, events, attrs, cands) -> str:
    near = [e for e in events if e["room_id"] == g["room_id"] and _ov(e["start"], e["end"], g["start"], g["end"]) > 0]
    if not near:
        if not g["has_signal_peak"]:
            return "没有信号峰（安静高光，信号触发的漏斗看不到）"
        return "没有检出热度异常"
    for e in near:
        a = attrs.get(e["event_id"], {})
        if a.get("cause") != "content":
            return f"归因为 {a.get('cause')}，没进入剪辑"
        c = next((c for c in cands if c["event_id"] == e["event_id"]), None)
        if c and c["status"] != "accepted":
            return f"候选状态 {c['status']}：{c.get('note', '')}"
    if best is not None:
        return f"切片只覆盖了金标准的 {cov:.0%}"
    return "未知"
