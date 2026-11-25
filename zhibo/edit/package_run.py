"""给一次运行里已接受的片段批量做竖屏成片：python -m zhibo package --run runs/<名> --data data/<集>。

片段、判定结果来自 runs/<名>/candidates.jsonl；逐句稿、声音感知来自数据集。
成片写到 runs/<名>/vertical/<候选号>.mp4，汇总写到 runs/<名>/vertical/summary.json。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from ..core.ledger import Ledger
from ..ingest.sources import ReplaySource
from . import evidence
from .package import GLMPlanner, MockPlanner, make_plan, package_clip, plan_request


def package_run(run: Path, data: Path, cfg: Dict[str, Any], only=None, log=print) -> Dict[str, Any]:
    run, data = Path(run), Path(data)
    p = cfg.get("package", {})
    src = ReplaySource(data)
    rooms = {r.room_id: r for r in src.rooms}
    planner = GLMPlanner(cfg["judge"]) if p.get("planner", cfg["judge"].get("kind")) == "glm" else MockPlanner(p)
    ledger = Ledger(Path(cfg["ledger"]["path"]), replay_only=bool(cfg["ledger"].get("replay_only")))
    out_dir = run / "vertical"
    out_dir.mkdir(exist_ok=True)
    summary = {}
    for line in (run / "candidates.jsonl").open(encoding="utf-8"):
        c = json.loads(line)
        if c["status"] != "accepted" or not (c.get("clip") or {}).get("path"):
            continue
        if only and c["cand_id"] not in only:
            continue
        room = rooms[c["room_id"]]
        perception = src.perception(c["room_id"])
        sents = [dict(s, laugh=evidence.sentence_laugh(s, perception) >= evidence.LAUGH_MARK)
                 for s in src.transcript(c["room_id"]) if s["end"] > c["start"] and s["start"] < c["end"]]
        req = plan_request(room.anchor, c.get("judge") or {}, sents)
        plan = make_plan(planner, ledger, req)
        layout = (p.get("layouts") or {}).get(room.anchor, {"kind": "blur_fill"})
        out = out_dir / f"{c['cand_id']}.mp4"
        res = package_clip(run / c["clip"]["path"], out, sents, c["start"], c["end"], plan, layout, p)
        res["plan"] = {"title": plan["title"], "highlight": plan["highlight"], "fixes": plan["fixes"]}
        res["path"] = str(out.relative_to(run))
        summary[c["cand_id"]] = res
        log(f"[包装] {room.anchor} {c['cand_id']}：{res['layout']}，{res['duration']:.1f} 秒，标题 {' / '.join(plan['title'])}，"
            f"字幕 {res['captions']} 块、高亮 {res['highlights']} 处、改错字 {len(plan['fixes'])} 句")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary
