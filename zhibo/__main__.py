"""命令行入口。

  python3 -m zhibo demo                      # 一条命令：生成演示数据 → 跑本系统 → 跑基线 → 出对照
  python3 -m zhibo make-data [--force]       # 只生成合成数据集
  python3 -m zhibo run --config configs/demo.json [--set judge.kind=mock ...]
  python3 -m zhibo eval --run runs/system    # 只重算指标和报告
  python3 -m zhibo collect --room 陈泽=<直播间号> --start-hub   # 实测采集
  .venv/bin/python -m zhibo perceive --data data/live_<…>      # 给数据集补算声音感知（要项目 .venv）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .core.config import load_config


def _print_metrics(m) -> None:
    keys = ["recall", "precision", "start_err_mean_abs", "end_err_mean_abs", "attribution_accuracy",
            "miss_watch_rate", "judge_calls", "n_clips", "fingerprint"]
    print(f"[{m['run_name']}] " + "  ".join(f"{k}={m.get(k)}" for k in keys))


def cmd_make_data(args) -> None:
    from .evaluate.demo_data import make_demo_dataset

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
    from .evaluate.evaluate import evaluate
    from .evaluate.report import write_report

    run = Path(args.run)
    cfg = json.loads((run / "config.json").read_text(encoding="utf-8"))
    m = evaluate(run, Path(args.data), cfg)
    write_report(run, Path(args.data), m)
    _print_metrics(m)


def cmd_demo(args) -> None:
    from .pipeline import run_pipeline
    from .evaluate.report import write_summary

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


def cmd_cleanup(args) -> None:
    from .retention import prune_videos

    prune_videos(Path(args.data_root), args.days, apply=args.yes)


def cmd_check(args) -> None:
    from .deploy_check import run_checks

    sys.exit(0 if run_checks(load_config(Path(args.config)), Path(args.data_root)) else 1)


def cmd_collect(args) -> None:
    from .collect import cmd_collect as run_collect

    run_collect(args, load_config(Path(args.config)))


def cmd_perceive(args) -> None:
    try:
        import sherpa_onnx  # noqa: F401  模型加载时才真正用到，这里先检查装没装
        from .perceive.hearing import perceive_dataset
    except ImportError as e:
        raise SystemExit(f"声音感知需要项目 .venv 里的 numpy 和 sherpa-onnx（{e}）。用 .venv/bin/python -m zhibo perceive ...")
    perceive_dataset(Path(args.data), load_config(Path(args.config), args.set or []))


def cmd_package(args) -> None:
    try:
        import PIL  # noqa: F401  画标题和字幕要用
        from .edit.package_run import package_run
    except ImportError as e:
        raise SystemExit(f"成片包装需要项目 .venv 里的 Pillow（{e}）。用 .venv/bin/python -m zhibo package ...")
    package_run(Path(args.run), Path(args.data), load_config(Path(args.config), args.set or []),
                only=set(args.only) if args.only else None)


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
    p = sub.add_parser("collect", help="实测采集：dyhub 采弹幕 + ffmpeg 录制，落成数据集")
    p.add_argument("--room", action="append", required=True, help="名字=直播间号或链接，可写多次")
    p.add_argument("--hours", type=float, default=4.0)
    p.add_argument("--out", help="输出目录，默认 data/live_<日期_时间>")
    p.add_argument("--port", type=int, default=8757)
    p.add_argument("--start-hub", action="store_true", help="dyhub 没在运行时顺带启动")
    p.add_argument("--quality", default="ORIGIN", choices=["ORIGIN", "FULL_HD1", "HD1", "SD2", "SD1"],
                   help="录制清晰度：ORIGIN 原画（默认，11-24 实测 1080p60 约 19 Mbps、8.5 GB/时，随主播变化），FULL_HD1 蓝光，"
                        "HD1 超清，SD2 高清 720p 约 1 GB/时，SD1 标清")
    p.add_argument("--min-free-gb", type=float, default=15.0, help="磁盘剩余低于这个值就停止录制")
    p.add_argument("--full-record", action="store_true", help="全程录制（默认是环形缓冲，只保留触发的片段）")
    p.add_argument("--detector", default="auto", choices=["auto", "chat", "fused"],
                   help="检测方式：fused 弹幕 + 实时声音融合（要项目 .venv 和 models/）；chat 只看弹幕；"
                        "auto（默认）能用声音就用 fused，否则 chat")
    p.add_argument("--config", default="configs/live.json")
    p.set_defaults(fn=cmd_collect)
    p = sub.add_parser("package", help="把一次运行里接受的片段做成竖屏成片：标题、动态字幕、布局（要项目 .venv）")
    p.add_argument("--run", required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--config", default="configs/fused_glm.json")
    p.add_argument("--set", action="append")
    p.add_argument("--only", action="append", help="只做这些候选号，可写多次")
    p.set_defaults(fn=cmd_package)
    p = sub.add_parser("perceive", help="给数据集补算声音感知：每秒音量、笑声等和逐句稿（要项目 .venv）")
    p.add_argument("--data", required=True)
    p.add_argument("--config", default="configs/live.json")
    p.add_argument("--set", action="append")
    p.set_defaults(fn=cmd_perceive)
    p = sub.add_parser("cleanup", help="按天数清理数据集里的画面录像（音频、弹幕、标注保留），默认只列出不删")
    p.add_argument("--data-root", default="data")
    p.add_argument("--days", type=float, default=3.0, help="最后修改早于几天前的才清理")
    p.add_argument("--yes", action="store_true", help="真的删除（不加只列出）")
    p.set_defaults(fn=cmd_cleanup)
    p = sub.add_parser("check", help="部署自检：Python、依赖、模型、字体、ffmpeg、dyhub、内存、磁盘")
    p.add_argument("--config", default="configs/fused_glm.json")
    p.add_argument("--data-root", default="data")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("eval")
    p.add_argument("--run", required=True)
    p.add_argument("--data", default="data/demo")
    p.set_defaults(fn=cmd_eval)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
