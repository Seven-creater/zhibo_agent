"""部署自检：python -m zhibo check（EVO-029）。

在新机器（比如 Linux 服务器）上跑一遍，逐项检查采集和出片要用的东西，缺什么就说怎么装。
资源门槛来自 EVO-028 在本机的实测：两个直播间常驻内存约 1.2 GB、出片时再加约 1 GB；
原画每个直播间约 19 Mbps；录制器在磁盘剩余低于 15 GB 时停录。
只用标准库和 os.sysconf，macOS 和 Linux 都能跑，不读 /proc。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

OK, WARN, FAIL = "通过", "注意", "缺失"


def _cmd_version(cmd: str) -> str:
    try:
        r = subprocess.run([cmd, "-version"], capture_output=True, text=True, timeout=10)
        return (r.stdout or r.stderr).split("\n")[0]
    except (OSError, subprocess.SubprocessError):
        return ""


def _chrome() -> str:
    env = os.environ.get("DYHUB_CHROME")
    if env and Path(env).exists():
        return env
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return mac if Path(mac).exists() else ""


def checks(cfg: Dict[str, Any], data_root: Path) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    add = lambda status, item, detail: out.append((status, item, detail))  # noqa: E731

    v = sys.version_info
    add(OK if v >= (3, 9) else FAIL, "Python", f"{v.major}.{v.minor}.{v.micro}（要 3.9 以上）")

    for cmd in ("ffmpeg", "ffprobe"):
        ver = _cmd_version(cmd)
        add(OK if ver else FAIL, cmd, ver or "没找到。Debian/Ubuntu：apt install ffmpeg；macOS：brew install ffmpeg")

    for mod, pkg in (("numpy", "numpy"), ("sherpa_onnx", "sherpa-onnx==2.0.3"), ("PIL", "Pillow==11.3.0")):
        try:
            __import__(mod)
            add(OK, f"Python 包 {pkg}", "已安装")
        except ImportError:
            add(FAIL, f"Python 包 {pkg}", "没装。用项目 .venv 运行本命令（.venv/bin/python -m zhibo check），"
                                          "或 .venv/bin/pip install -r requirements.txt")

    p = cfg.get("perceive", {})
    md = Path(p.get("models_dir", "models"))
    from .perceive.speech import SENSE_VOICE_DIR, VAD_FILE
    from .perceive.tagger import TAGGER_DIR
    for name in (VAD_FILE, SENSE_VOICE_DIR, TAGGER_DIR):
        add(OK if (md / name).exists() else FAIL, f"模型 {name}", "已下载" if (md / name).exists()
            else "没有。运行 sh scripts/fetch_models.sh")
    a = cfg.get("asr", {})
    if a.get("enabled") and a.get("engine", "local") == "local":  # 出片前重识别字幕的大模型（EVO-035）
        name = (a.get("local") or {}).get("dir", "")
        add(OK if name and (md / name).exists() else FAIL, f"模型 {name}（字幕重识别）",
            "已下载" if name and (md / name).exists() else "没有。运行 sh scripts/fetch_models.sh，或在配置里关掉 asr.enabled")

    try:
        from .edit.textlayer import resolve_font
        path, idx = resolve_font(cfg.get("package", {}))
        add(OK, "中文字体", f"{path}（第 {idx} 个）")
    except ImportError:
        add(FAIL, "中文字体", "要先装 Pillow")
    except (OSError, FileNotFoundError) as e:
        add(FAIL, "中文字体", str(e))

    hub = Path("third_party/dyhub")
    add(OK if (hub / "dist" / "index.js").exists() else FAIL, "弹幕采集 dyhub",
        "已构建" if (hub / "dist" / "index.js").exists() else "没有 third_party/dyhub/dist，按 docs/部署.md 安装")
    node = shutil.which("node")
    add(OK if node else FAIL, "Node.js", node or "没找到。dyhub 要 Node 20 以上")
    chrome = _chrome()
    add(OK if chrome else WARN, "Chrome / Chromium", chrome or
        "没找到。dyhub 默认的浏览器内核要用；Linux 装 chromium 后设 DYHUB_CHROME=路径")

    env_file = Path(".env")
    has_key = bool(os.environ.get("ZHIPU_API_KEY")) or (
        env_file.exists() and any(line.strip().startswith("ZHIPU_API_KEY=") and line.split("=", 1)[1].strip()
                                  for line in env_file.read_text(encoding="utf-8").splitlines()))
    add(OK if has_key else WARN, "GLM 密钥", "已配置（不显示内容）" if has_key
        else "没有。判定和包装用 GLM 时要在 .env 写 ZHIPU_API_KEY=...，用模拟判定可以不配")

    try:
        mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
        add(OK if mem >= 3.5 else WARN, "内存", f"{mem:.1f} GB（建议 4 GB：两个直播间常驻约 1.2 GB，出片时再加约 1 GB）")
    except (ValueError, OSError, AttributeError):
        add(WARN, "内存", "读不到")
    add(OK if (os.cpu_count() or 1) >= 2 else WARN, "CPU", f"{os.cpu_count()} 核（声音感知约 0.1 核/直播间，出片编码吃满多核）")

    data_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(data_root).free / 1e9
    add(OK if free >= 40 else (WARN if free >= 20 else FAIL), "磁盘剩余",
        f"{free:.0f} GB（剩 15 GB 时停录；两个直播间一晚约 7 GB 录像 + 每个直播间约 0.9 GB 缓冲，建议 40 GB 以上）")
    days = (cfg.get("retention") or {}).get("video_days")
    add(OK if days else WARN, "录像自动清理", f"采集前清理 {days} 天前的画面录像" if days
        else "没开。配置 retention.video_days，或定期运行 python -m zhibo cleanup --yes")
    add(WARN, "带宽", "没法自动测。原画每个直播间下行约 19 Mbps，两个直播间约 40 Mbps；买服务器时确认入方向带宽")
    return out


def run_checks(cfg: Dict[str, Any], data_root: Path) -> bool:
    rows = checks(cfg, data_root)
    for status, item, detail in rows:
        print(f"[{status}] {item}：{detail}")
    bad = [r for r in rows if r[0] == FAIL]
    print(f"\n{len(rows)} 项，{len(bad)} 项缺失，{sum(r[0] == WARN for r in rows)} 项需要注意。")
    return not bad
