"""给一次运行里已接受的片段批量做竖屏成片：python -m zhibo package --run runs/<名> --data data/<集>。

片段、判定结果来自 runs/<名>/candidates.jsonl；逐句稿、声音感知来自数据集。
每段产出 runs/<名>/vertical/ 下的成片 <候选号>.mp4、封面 <候选号>_cover.jpg、发布文案 <候选号>_post.txt，
汇总写到 vertical/summary.json。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.ledger import Ledger
from ..ingest.sources import ReplaySource
from . import evidence
from .camfind import find_cam
from .package import GLMPlanner, MockPlanner, make_plan, package_clip, plan_request


def choose_layout(anchor: str, clip: Path, duration: float, cfg: Dict[str, Any], ledger: Ledger,
                  work: Path, use_glm: bool) -> Dict[str, Any]:
    """布局：配置里手写了这个主播的小窗位置就用配置；否则让 GLM 看一帧找小窗；都没有就模糊背景铺满。"""
    p = cfg.get("package", {})
    fixed = (p.get("layouts") or {}).get(anchor)
    if fixed:
        return dict(fixed, source="config")
    if use_glm and p.get("find_cam", True):
        box = find_cam(clip, duration / 2, cfg["judge"], ledger, work)
        if box:
            return {"kind": "cam_top", "cam": box, "top": int(p.get("cam_top", 300)), "source": "glm"}
    return {"kind": "blur_fill", "source": "default"}


def package_candidate(c: Dict[str, Any], run: Path, src: ReplaySource, anchor: str, cfg: Dict[str, Any],
                      planner, ledger: Ledger, use_glm: bool, out_dir: Path) -> Dict[str, Any]:
    p = cfg.get("package", {})
    perception = src.perception(c["room_id"])
    sents = [s for s in src.transcript(c["room_id"]) if s["end"] > c["start"] and s["start"] < c["end"]]
    prev_end: Optional[float] = None
    for s in sents:
        s["laugh"] = evidence.sentence_laugh(s, perception) >= evidence.LAUGH_MARK
        s["gap_before"] = 0.0 if prev_end is None else max(0.0, s["start"] - prev_end)
        prev_end = s["end"]
    clip = run / c["clip"]["path"]
    layout = choose_layout(anchor, clip, c["end"] - c["start"], cfg, ledger, out_dir / f"{c['cand_id']}_camfind", use_glm)
    plan = make_plan(planner, ledger, plan_request(anchor, c.get("judge") or {}, sents, layout["kind"],
                                                    c["start"], c["end"] - c["start"], p))
    bgm = Path(p["bgm"]) if p.get("bgm") and Path(p["bgm"]).exists() else None
    res = package_clip(clip, out_dir / f"{c['cand_id']}.mp4", sents, perception, c["start"], plan, layout, p, bgm)
    res["plan"] = plan
    res["layout_source"] = layout.get("source")
    res["cam_box"] = layout.get("cam")
    for k in ("path", "cover", "post"):
        res[k] = str(Path(res[k]).relative_to(run))
    return res


def package_run(run: Path, data: Path, cfg: Dict[str, Any], only=None, log=print) -> Dict[str, Any]:
    run, data = Path(run), Path(data)
    p = cfg.get("package", {})
    src = ReplaySource(data)
    rooms = {r.room_id: r for r in src.rooms}
    use_glm = p.get("planner", cfg["judge"].get("kind")) == "glm"
    planner = GLMPlanner(cfg["judge"]) if use_glm else MockPlanner(p)
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
        anchor = rooms[c["room_id"]].anchor
        res = package_candidate(c, run, src, anchor, cfg, planner, ledger, use_glm, out_dir)
        summary[c["cand_id"]] = res
        pl = res["plan"]
        log(f"[包装] {anchor} {c['cand_id']}：{res['layout']}（{res['layout_source']}），{res['source_duration']:.1f} → "
            f"{res['duration']:.1f} 秒；钩子 {res['hook']}，去停顿 {res['tighten_cut_seconds']} 秒，推近 {res['zooms']} 处，"
            f"音效 {res['sfx']}，BGM {'有' if res['bgm'] else '无'}；标题 {' / '.join(pl['title'])}；"
            f"额外手法 {[(e['kind'], e['n']) for e in pl['extras']] or '无'}，没采用 {[(d['kind'], d['dropped']) for d in pl['dropped']] or '无'}；"
            f"{'质检通过' if res['check_ok'] else '质检问题：' + '；'.join(res['problems'])}")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary
