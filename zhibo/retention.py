"""保留录像的自动清理（EVO-029）。

实测（EVO-028）：两个直播间一晚留下约 7 GB 原画录像，跑完不会自动删，服务器磁盘会被慢慢占满；
录制器在剩余空间低于 min_free_gb 时会停录。这里按天数清理数据集里的**画面录像**：
- 只删 recordings/ 下的画面 .ts；整场音频 audio_*.ts、弹幕、声音感知、逐句稿都很小（一晚几百 MB），保留，
  离线重跑检测仍然可以用；
- 有 labels.json（人工标注过）或 .keep 文件的数据集不动——标注集要拿来对照判定；
- 符号链接过来的数据集、合成数据集（data/demo，demo 要用）不动；
- 每次删除记到数据集的 pruned.json（删了哪些、多大、什么时候）。

删掉画面后，ReplaySource 会跳过不存在的录像文件，流水线把这段当作"没有录像"处理（不出片，只留检测结果）。
已经剪好的片子在 runs/ 下，不受影响。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DAY = 86400.0


def video_files(ds: Path) -> List[Path]:
    rec = ds / "recordings"
    if not rec.is_dir():
        return []
    return sorted(p for p in rec.rglob("*.ts") if p.is_file() and not p.name.startswith("audio_"))


def protected(ds: Path) -> str:
    """返回不清理的原因；空字符串表示可以清理。"""
    if ds.is_symlink():
        return "符号链接"
    if (ds / "labels.json").exists():
        return "有人工标注 labels.json"
    if (ds / ".keep").exists():
        return "有 .keep"
    try:
        if json.loads((ds / "manifest.json").read_text(encoding="utf-8")).get("synthetic"):
            return "合成数据集（demo 要用）"
    except (OSError, ValueError):
        pass
    return ""


def prune_videos(data_root: Path, days: float, apply: bool = False, log: Callable[[str], None] = print,
                 now: Optional[float] = None) -> Dict[str, Any]:
    """清理 data_root 下各数据集里最后修改时间早于 days 天前的画面录像。apply=False 只列出不删。"""
    now = time.time() if now is None else now
    total, freed, kept = [], 0, []
    for ds in sorted(Path(data_root).iterdir()):
        if not (ds / "recordings.json").exists():
            continue
        files = video_files(ds)
        if not files:
            continue
        size = sum(p.stat().st_size for p in files)
        why = protected(ds)
        if why:
            kept.append({"dataset": ds.name, "reason": why, "bytes": size})
            continue
        age = (now - max(p.stat().st_mtime for p in files)) / DAY
        if age < days:
            kept.append({"dataset": ds.name, "reason": f"{age:.1f} 天前，未到 {days:g} 天", "bytes": size})
            continue
        if apply:
            for p in files:
                p.unlink()
            log_file = ds / "pruned.json"
            hist = json.loads(log_file.read_text(encoding="utf-8")) if log_file.exists() else []
            hist.append({"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), "days": days,
                         "files": [str(p.relative_to(ds)) for p in files], "bytes": size})
            log_file.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
        total.append({"dataset": ds.name, "files": len(files), "bytes": size, "age_days": round(age, 1)})
        freed += size
    verb = "已删除" if apply else "可删除（加 --yes 才真删）"
    for t in total:
        log(f"[清理] {t['dataset']}：{verb} {t['files']} 个画面录像，{t['bytes'] / 1e9:.2f} GB（{t['age_days']} 天前）")
    for k in kept:
        log(f"[清理] {k['dataset']}：保留 {k['bytes'] / 1e9:.2f} GB（{k['reason']}）")
    log(f"[清理] 合计{verb.split('（')[0]} {freed / 1e9:.2f} GB")
    return {"pruned": total, "kept": kept, "freed_bytes": freed, "applied": apply}
