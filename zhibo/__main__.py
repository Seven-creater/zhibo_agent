"""命令行入口。

  python3 -m zhibo demo                      # 一条命令：生成演示数据 → 跑本系统 → 跑基线 → 出对照
  python3 -m zhibo make-data [--force]       # 只生成合成数据集
  python3 -m zhibo run --config configs/demo.json [--set judge.kind=mock ...]
  python3 -m zhibo eval --run runs/system    # 只重算指标和报告
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .config import load_config


def _print_metrics(m) -> None:
    keys = ["recall", "precision", "start_err_mean_abs", "end_err_mean_abs", "attribution_accuracy",
            "miss_watch_rate", "judge_calls", "n_clips", "fingerprint"]
    print(f"[{m['run_name']}] " + "  ".join(f"{k}={m.get(k)}" for k in keys))


def cmd_make_data(args) -> None:
    from .demo_data import make_demo_dataset

    out = Path(args.data)
    if out.exists() and not args.force:
        print(f"{out} 已存在，跳过（加 --force 重新生成）")
        return
    if out.exists():
        shutil.rmtree(out)
    make_demo_dataset(out)
    print(f"演示数据已生成：{out}")


def cmd_run(args) -> None:
    from .pipeline import run_pipeline

    cfg = load_config(Path(args.config), args.set or [])
    out = Path(args.out or f"runs/{cfg['run_name']}")
    m = run_pipeline(cfg, Path(args.data), out)
    _print_metrics(m)
    print(f"报告：{out / 'report.md'}")


def cmd_eval(args) -> None:
    from .evaluate import evaluate
    from .report import write_report

    run = Path(args.run)
    cfg = json.loads((run / "config.json").read_text(encoding="utf-8"))
    m = evaluate(run, Path(args.data), cfg)
    write_report(run, Path(args.data), m)
    _print_metrics(m)


def cmd_demo(args) -> None:
    from .pipeline import run_pipeline
    from .report import write_summary

    data = Path(args.data)
    if not data.exists():
        args.force = False
        cmd_make_data(args)
    results = []
    for conf in ("configs/demo.json", "configs/baseline_chat_density.json"):
        cfg = load_config(Path(conf))
        m = run_pipeline(cfg, data, Path(f"runs/{cfg['run_name']}"))
        _print_metrics(m)
        results.append(m)
    out = write_summary(Path("runs/summary.md"), results)
    print(f"对照表：{out}\n本系统报告：runs/system/report.md\n切片：runs/system/clips/")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="zhibo", description="抖音直播高光切片 Agent（离线回放 demo）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("demo")
    p.add_argument("--data", default="data/demo")
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("make-data")
    p.add_argument("--data", default="data/demo")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_make_data)
    p = sub.add_parser("run")
    p.add_argument("--config", default="configs/demo.json")
    p.add_argument("--data", default="data/demo")
    p.add_argument("--out")
    p.add_argument("--set", action="append", help="覆盖配置，如 --set scheduler.record_budget=3")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("eval")
    p.add_argument("--run", required=True)
    p.add_argument("--data", default="data/demo")
    p.set_defaults(fn=cmd_eval)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
