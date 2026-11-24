"""评测报告（FR-9.7 的文字版）：report.md 单次运行；summary.md 多次运行对照（FR-9.5）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..detect.attribution import CAUSE_ZH
from ..core.models import read_jsonl

METRIC_ROWS = [
    ("recall", "高光检出率", "pct"),
    ("recall_with_signal_peak", "　其中有信号峰的", "pct"),
    ("recall_without_signal_peak", "　其中安静高光", "pct"),
    ("precision", "精确率", "pct"),
    ("false_positive_rate", "误报率", "pct"),
    ("start_err_mean_abs", "起点平均误差（秒）", "num"),
    ("end_err_mean_abs", "终点平均误差（秒）", "num"),
    ("attribution_accuracy", "归因准确率", "pct"),
    ("miss_watch_rate", "漏盯率", "pct"),
    ("latency_mean", "端到端时延（秒，回放时钟）", "num"),
    ("judge_calls", "GLM 调用数（含回放）", "num"),
    ("judge_new_calls", "　其中新发出的", "num"),
    ("judge_calls_per_hit", "每条有效高光的 GLM 调用", "num"),
    ("recorded_hours", "录制时长（小时）", "num"),
    ("n_clips", "产出切片数", "num"),
    ("n_heat_events", "热度异常数", "num"),
]


def _fmt(v: Any, kind: str) -> str:
    if v is None:
        return "—"
    if kind == "pct":
        return f"{v * 100:.0f}%"
    return str(v)


def _mmss(ts: float, room: Dict[str, Any]) -> str:
    s = max(0, int(round(ts - room["start_ts"])))
    return f"{s // 60:02d}:{s % 60:02d}"


def write_report(run_dir: Path, dataset_dir: Path, m: Dict[str, Any]) -> Path:
    rooms = m["_rooms"]
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    lines: List[str] = [f"# 运行报告：{m['run_name']}", ""]
    if manifest.get("synthetic"):
        lines += ["> 数据集是合成的，只用来检查流程是否跑通，数字不代表方法效果。", ""]
    lines += [f"数据集 `{dataset_dir}`，结果指纹 `{m['fingerprint']}`（同一输入重跑应一致），用时 {m['wall_seconds']} 秒。", ""]

    lines += ["## 指标", "", "| 指标 | 值 |", "|---|---|"]
    lines += [f"| {name} | {_fmt(m.get(k), kind)} |" for k, name, kind in METRIC_ROWS]

    lines += ["", "## 金标准逐条", "", "| 直播间 | 时段 | 内容 | 结果 | 覆盖 | 起点误差 | 终点误差 | 说明 |", "|---|---|---|---|---|---|---|---|"]
    for r in m["_gold_rows"]:
        g = r["gold"]
        room = rooms[g["room_id"]]
        lines.append(
            f"| {g['room_id']} | {_mmss(g['start'], room)}–{_mmss(g['end'], room)} | {g['reason']} | "
            f"{'✅' if r['hit'] else '❌'} | {r['coverage']:.0%} | {_fmt(r['start_err'], 'num')} | "
            f"{_fmt(r['end_err'], 'num')} | {r['why']} |"
        )

    lines += ["", "## 产出切片", ""]
    if not m["_clip_rows"]:
        lines.append("（没有）")
    else:
        lines += ["| 切片 | 时段 | 切点来源（起/止） | 对应金标准 | 标题 | 文件 |", "|---|---|---|---|---|---|"]
        for r in m["_clip_rows"]:
            c = r["cand"]
            room = rooms[c["room_id"]]
            title = (c.get("judge") or {}).get("title", "")
            path = (c.get("clip") or {}).get("path", "（未渲染）")
            match = r["gold"]["reason"] if r["gold"] else "**误报**"
            lines.append(
                f"| {c['cand_id']} | {_mmss(c['start'], room)}–{_mmss(c['end'], room)} | "
                f"{c['start_source']} / {c['end_source']} | {match} | {title} | {path} |"
            )

    lines += ["", "## 热度异常与归因", "", "| 异常 | 时段 | 峰值信号 | 归因 | 标注 | 证据 |", "|---|---|---|---|---|---|"]
    attrs = {a["event_id"]: a for a in read_jsonl(run_dir / "attributions.jsonl")}
    for r in m["_attr_rows"]:
        e = r["event"]
        room = rooms[e["room_id"]]
        a = attrs.get(e["event_id"], {})
        mark = "" if r["ok"] is None else (" ✅" if r["ok"] else " ❌")
        lines.append(
            f"| {e['event_id']} | {_mmss(e['start'], room)}–{_mmss(e['end'], room)} | {e['channel']} z={e['peak_z']} | "
            f"{CAUSE_ZH.get(r['pred'], r['pred'])}{mark} | {CAUSE_ZH.get(r['label'], '无标注') if r['label'] else '无标注'} | "
            f"{'；'.join(a.get('evidence', []))} |"
        )

    lines += ["", "## 候选片段处理结果", "", "| 候选 | 状态 | 说明 |", "|---|---|---|"]
    for c in read_jsonl(run_dir / "candidates.jsonl"):
        lines.append(f"| {c['cand_id']} | {c['status']} | {c.get('note', '')} |")

    lines += ["", "## 调度决策", "", "| 时刻 | 直播间 | 动作 | 原因 |", "|---|---|---|---|"]
    for d in read_jsonl(run_dir / "decisions.jsonl"):
        room = rooms[d["room_id"]]
        lines.append(f"| {_mmss(d['t'], room)} | {d['room_id']} | {d['action']} | {d['reason']} |")

    out = run_dir / "report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def write_summary(out: Path, runs: List[Dict[str, Any]]) -> Path:
    lines = ["# 本系统与基线对照", "", "> 合成数据，只检查流程；基线 = 只看弹幕密度、不归因、不判定、固定名单不调度。", ""]
    lines += ["| 指标 | " + " | ".join(r["run_name"] for r in runs) + " |", "|---" * (len(runs) + 1) + "|"]
    for k, name, kind in METRIC_ROWS:
        lines.append(f"| {name} | " + " | ".join(_fmt(r.get(k), kind) for r in runs) + " |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
