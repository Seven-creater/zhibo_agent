"""自动找主播摄像头小窗：抽一帧给 GLM 看，问"画面上有没有叠一个主播真人摄像头小窗、在哪里"。

clips-studio 用"谁在跟着声音说话"（TalkNet）识别主播的脸（https://github.com/ColinGPT9/clips-studio ，提交 bd39d8b，
AGPL-3.0，`gaming/detect.py`）；我们没有这类本地模型，先用视觉大模型看一帧，结果做合理性检查后再用。
请求走账本，同一帧不重复花钱。配置里手写了某个主播的小窗位置时，以配置为准。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import media
from ..core.ledger import Ledger
from .judge import GLMJudge

PROMPT = (
    "这是一帧抖音直播画面。画面上有没有**叠加**着一个主播真人摄像头小窗（画中画，通常在角落，里面是真人）？"
    "注意：游戏里的角色不算；如果整个画面本身就是摄像头拍的真人，也不算小窗。"
    "如果有，给出小窗的位置：左上角 x、y 和宽 w、高 h，都用占整幅画面宽、高的比例（0～1，保留三位小数），"
    "框要贴着小窗的边缘，不要带进旁边的游戏界面或文字。"
    '只输出一个 JSON 对象，不要其他文字：{"has_cam": true, "box": [x, y, w, h]} 或 {"has_cam": false}'
)


class CamFinder(GLMJudge):
    kind = "glm_camfind"

    def body(self, request: Dict[str, Any], max_tokens: Optional[int] = None) -> Dict[str, Any]:
        data = base64.b64encode(Path(self.frame_path).read_bytes()).decode("ascii")
        return {"model": self.model, "max_tokens": max_tokens or self.max_tokens,
                "thinking": {"type": "enabled", "budget_tokens": self.thinking_budget},
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                    {"type": "text", "text": PROMPT}]}]}


def _check(box: Any) -> Optional[List[float]]:
    try:
        x, y, w, h = [float(v) for v in box]
    except (TypeError, ValueError):
        return None
    if not (0 <= x < 1 and 0 <= y < 1 and 0.05 <= w <= 0.6 and 0.05 <= h <= 0.6 and x + w <= 1.01 and y + h <= 1.01):
        return None
    if not 0.02 <= w * h <= 0.4:  # 小窗占画面 2%～40%
        return None
    return [round(x, 4), round(y, 4), round(min(w, 1 - x), 4), round(min(h, 1 - y), 4)]


def find_cam(clip: Path, t: float, cfg: Dict[str, Any], ledger: Ledger, work: Path) -> Optional[List[float]]:
    """在片段第 t 秒抽一帧，让 GLM 找摄像头小窗；找不到或结果不合理返回 None。"""
    work.mkdir(parents=True, exist_ok=True)
    frame = work / "camfind.jpg"
    subprocess.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(clip),
                    "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "4", str(frame)], capture_output=True)
    if not frame.exists():
        return None
    # 账本按请求内容认，请求里只放帧的哈希，不放本机路径（换输出目录也能回放）
    req = {"task": "camfind", "rev": "2026-11-25a", "frame_sha": hashlib.sha256(frame.read_bytes()).hexdigest()[:16]}
    finder = CamFinder(cfg)
    finder.frame_path = frame
    status, text = ledger.call(req, finder.respond)
    if status not in ("received", "replayed") or not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    try:
        obj = json.loads(m.group(0) if m else text)
    except json.JSONDecodeError:
        return None
    return _check(obj.get("box")) if obj.get("has_cam") else None
