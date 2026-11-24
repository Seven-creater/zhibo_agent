# 抖音直播高光切片 Agent（骨架 demo）

对应需求文档 `~/Desktop/直播高光切片Agent_需求开发文档.md` v0.1。
现在是**离线回放模式的端到端骨架**：用合成数据把"信号 → 热度异常 → 归因 → 调度 → 回溯找边界 → 判定 → 出片 → 评测"整条链路跑通。每个模块都是能跑的最简实现，后面逐个替换成真零件。

## 快速开始

只需要 Python 3.9+ 和 FFmpeg（`brew install ffmpeg`），没有其他依赖。

```bash
cd ~/Desktop/zhibo_agent
python3 -m zhibo demo
```

大约 15 秒。会依次：生成合成数据集 → 跑本系统 → 跑"只看弹幕密度"基线 → 出对照表。

看结果：
- `runs/summary.md`：本系统和基线的指标对照
- `runs/system/report.md`：金标准逐条命中情况、每条切片、每次异常的归因证据、调度决策
- `runs/system/clips/*.mp4`：切出来的片段

合成录像是测试图，画面上有秒数计数；事件时段带彩色边框（绿 = 内容高光，青 = 安静高光，黄 = 福袋，红 = 单人刷礼物，橙 = 人气爬升），打开切片就能肉眼核对切点。

> 合成数据只用来检查流程。事件是按规则造的，系统规则也是照着写的，**指标数字不代表方法效果**。

## 数据流

```
ReplaySource（消息流 + 录像）                       sources.py
   │ 按秒推进的回放时钟
   ▼
SignalAggregator：按秒聚合、礼物去重计价             signals.py     M2
   ▼
HeatDetector：每间自己的基线，z 值进出阈值           heat.py        M5
   ▼ 异常结束时
attribute：福袋/PK/开播/单人刷礼物/刷屏/内容          attribution.py M5
   ▼ 只有"内容驱动"往下走
Scheduler.covered：这段有没有录到（环形缓冲）         scheduler.py   M4 M3
   ▼
backtrack.plan：信号滞后回推 + 吸附静音边界 + 时长    backtrack.py   M6
   ▼
judge_candidate：证据包 → 判定器 → 格式校验/修订      judge.py       M7
   └─ Ledger：先记账再发出，已收到的直接回放           ledger.py      M11
   ▼
render_clip：裁切 + 自检                             render.py      M8
   ▼
evaluate + report：指标、逐条对照、基线对照           evaluate.py report.py  M9
```

主循环在 `pipeline.py`。调度器每 15 秒按"log(在线人数) × (1 + 0.4 × 平滑热度)"重新分配录制路数。

## 各模块现状和替换方向

| 文件 | 现在 | 以后换成 |
|---|---|---|
| `sources.py` | 读冻结数据集 | `LiveSource`：dyhub 或 DouyinBarrage 适配层，DouyinLiveRecorder 录制加环形缓冲 |
| `signals.py` | 弹幕人数、笑点词、礼物去重、点赞、进场、在线 | 加弹幕去刷屏、语义特征（可移植 douyin-living） |
| `heat.py` | EWMA 基线 + z 值，三路信号取最大 | river 在线检测、多形态（加速、多路共振） |
| `attribution.py` | 规则 | 规则 + GLM 辅助归因 |
| `scheduler.py` | 只调度录制路数 | 账号、浏览器页、带宽、GLM 并发全部预算；开播规律；探索比例 |
| `backtrack.py` | 只有静音边界 | 语音转写句子边界（faster-whisper/FunASR）、镜头切换（PySceneDetect） |
| `judge.py` | `MockJudge` 按规则模拟 | `GLMJudge`：接 agentic-video 的 opencode_provider |
| `ledger.py` | 简版账本 | agentic-video 的 state.py |
| `render.py` | 裁切 + 时长/音视频轨自检 | 字幕、竖屏、封面 |
| `evaluate.py` | 第 1.4 节大部分指标 | 片段自洽率（人工）、按品类分组、留出集 |

## 数据集格式

换成真实数据时，按这个格式放到 `data/<名字>/`，再用 `--data` 指定：

| 文件 | 内容 |
|---|---|
| `rooms.json` | 直播间列表：room_id、主播、品类、来源、开播/下播时间（epoch 秒） |
| `messages/<room_id>.jsonl` | 每行一条消息：`room_id`、`recv_ts`、`type`、`payload` |
| `recordings.json` | 每个直播间的录像文件，以及录像第 0 秒对应的 epoch 时间（时钟对齐） |
| `gold.json` | 金标准高光：起止、原因、有没有信号峰 |
| `causes.json` | 原因标注时段，用来算归因准确率 |
| `manifest.json` | 文件哈希（数据集冻结） |

消息类型：`chat`（user, text）、`gift`（user, gift_id, group_id, combo, diamond）、`like`（user, count）、`enter`、`room_stats`（online）、`lucky_bag`、`red_packet`、`pk`。

## 常用命令

```bash
python3 -m zhibo run --config configs/demo.json --set scheduler.record_budget=1   # 改参数重跑
python3 -m zhibo run --config configs/demo.json --set ledger.replay_only=true     # 只回放账本，不发新请求
python3 -m zhibo eval --run runs/system                                          # 只重算指标
python3 -m zhibo make-data --force                                               # 重新生成合成数据
```

参数都在 `configs/demo.json`；基线配置 `configs/baseline_chat_density.json` 继承它，只改几项。同一输入重跑，报告里的"结果指纹"应该一致。

## 运行产物（`runs/<run_name>/`）

`signals.jsonl` 每秒信号 · `heat_events.jsonl` 热度异常 · `attributions.jsonl` 归因和证据 · `decisions.jsonl` 调度决策 · `candidates.jsonl` 候选片段和状态 · `evidence/` 给判定器的抽帧 · `clips/` 切片 · `stages.jsonl` 各阶段耗时 · `metrics.json` 指标 · `report.md` 报告。模型请求账本在 `state/ledger.jsonl`，跨运行共享。
