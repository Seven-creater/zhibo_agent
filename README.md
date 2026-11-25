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

## 实测采集（真实直播间）

弹幕用 [dyhub](https://github.com/ymstar/dyhub)（MIT，浏览器内核）采集，录制由我们自己用 ffmpeg 拉 dyhub 给出的直播流地址。第一次使用前先安装 dyhub（需要 Node ≥ 20 和 Chrome）：

```bash
git clone --depth 1 https://github.com/ymstar/dyhub.git third_party/dyhub
cd third_party/dyhub && npm ci --ignore-scripts && npm run build && cd ../..
```

开始采集（没开播的直播间会每 30 秒重试，开播后自动接上，所以可以提前启动）：

```bash
python3 -m zhibo collect --start-hub --hours 4 --room 陈泽=<直播间号或链接> --room 德华=<...> --room coke=<...>
```

- 每分钟打印一行：在线人数、弹幕数、笑点弹幕数、录像大小、状态。
- 按 Ctrl+C 结束，或到时间自动结束，然后自动整理成数据集（默认 `data/live_<日期_时间>/`）。
- 跑分析：`python3 -m zhibo run --config configs/live.json --data data/live_<日期_时间>`。实测数据没有金标准，检出率等指标为空，主要看报告里的热度异常、归因和切片。
- 不登录抖音账号，拿不到礼物数据（dyhub 游客态）。观众 ID 只存加盐哈希，昵称不落盘。
- 所有连抖音的进程（dyhub、ffmpeg）都去掉了代理环境变量，直连。
- **默认是环形缓冲（即时重放），不全程录**：ffmpeg 按 10 秒切小段，只保留最近约 6 分钟；采集时同步跑检测和归因（和离线回放同一套代码）。
  - 一出现异常就先把"开始前 5 分钟起"的录像占住，免得等结束的这段时间里被删掉。
  - 笑点和主导信号**同时**安静 40 秒才算结束，中途再起来算同一次（参考 frigate 的 `cutoff_time`）。
  - 确认结束后，判定为内容驱动的搞笑就从确认时刻**往回存 5 分钟**（异常本身更长时从异常开始前 60 秒起存）；不是搞笑就释放。
  - 同一个 ffmpeg 顺带把**整场音频**原样存下来（不重新编码，约 0.1 GB/小时），画面删了声音也还在，给声音分析和离线实验用，条目在 `audio.json`。
  - 结束时只把保留的片段拼成录像，触发记录在 `triggers.jsonl`。需要全程录制时加 `--full-record`。
- 录像默认取**原画**（`--quality ORIGIN`）。实测一个游戏直播间 1080p 60 帧约 2.8 GB/小时，码率随主播变化，可能到 7～9 GB/小时；所以默认不全程录。磁盘剩余低于 15 GB 自动停录（`--min-free-gb`）。断流后会重新取地址、开新的一段。
- 复制粘贴的"串"（同一句话 60 秒内 3 人以上发）不计入笑点；带"福袋""中一次"等口令词的串用来识别福袋（游客态收不到福袋消息）。

### 用 GLM 判定（先粗后细）

在项目根目录建 `.env`（已被 git 忽略），写一行 `ZHIPU_API_KEY=你的智谱密钥`，然后：

```bash
python3 -m zhibo run --config configs/live_glm.json --data data/live_<日期_时间>
```

先按"异常前 45 秒到结束后 10 秒"截宽窗口，按说话停顿切成编号小段，每段给 GLM 一张截图和这段的弹幕；GLM 只回答铺垫、高潮、反应分别是哪几段，最终切点取这些段的边界（模型不报时间戳）。所有请求先记账再发出，重跑时直接回放，不重复花钱。

### 声音感知（重构第 2 阶段，暂时只能离线补算）

第一次用先装依赖和模型（建项目 `.venv`，装 sherpa-onnx、numpy，下载约 280 MB 模型到 `models/`）：

```bash
scripts/fetch_models.sh
```

然后给一个数据集补算声音感知（优先用 `audio.json` 的整场音频，老数据集从录像里取声音）：

```bash
.venv/bin/python -m zhibo perceive --data data/live_<日期_时间>
```

输出 `perception/<room_id>.jsonl`（每秒一行：音量 dBFS、相对突变、短促爆发、过零率、说话占比，以及笑声、喊叫、欢呼、枪炮、音乐的概率）和 `transcript/<room_id>.jsonl`（主播的逐句稿）。M1 上约 25 倍实时。

### 融合检测（重构第 3 阶段，离线可用）

补算过声音感知的数据集，可以用弹幕 + 声音的融合检测跑：

```bash
python3 -m zhibo run --config configs/fused.json --data data/live_<日期_时间>
```

每路信号变成 0～1 的激活度（弹幕和音量看相对本直播间基线的 z 值，笑声等看分类概率），保持最近 10 秒的最大值，按权重相加；几路同时亮加分，整场常亮的降权。声音比弹幕早，先往后挪 5 秒再对齐。参数在配置的 `fuse` 一节，都是起步值，要用真实数据调。`configs/live.json` 本身仍然只用弹幕（`heat.kind = chat_z`）。

### 判定证据包第 2 版（重构第 5 阶段）

`configs/fused.json` 同时打开了第 2 版证据包（`judge.evidence = v2`），`configs/fused_glm.json` 在它基础上用 GLM 判定：

```bash
python3 -m zhibo run --config configs/fused_glm.json --data data/live_<日期_时间>
```

- 宽窗口就是即时重放保存的那段（确认结束时刻往回 5 分钟），起止吸附到句子停顿。
- 按逐句稿的句子停顿切成至少 8 秒的小段；没有逐句稿时退回静音边界。
- 每段告诉模型：主播说了什么、声音里有什么（笑声、音量突增、欢呼）、对齐后的弹幕；截图拼成 3×3 九宫格，一张看 9 段，最多 4 张。
- 主播的每句话都编了号，模型除了选段，还要给出"最精简的版本从第几句到第几句"（目标 10～20 秒）；切点从那几句开始前 3 秒到最后一句后 2 秒，笑点在第一句时自动带上前一句铺垫，时长限制在 8～25 秒（`judge.clip_min_seconds`、`clip_max_seconds`）。
- 模型只报段号和句号，不报时间戳；切出来的片段旁边有同名 `.srt`，mp4 里也封了可开关的字幕轨。

### 竖屏成片包装

把一次运行里接受的片段做成抖音竖屏成片（1080×1920）：

```bash
.venv/bin/python -m zhibo package --run runs/<运行名> --data data/live_<日期_时间>
```

- GLM 只看文字给出包装方案：两行标题、字幕里要换色的关键词、明显的转写错别字修正（不许编造）；用 `--set package.planner=mock` 可以不调模型。
- 布局：配置 `package.layouts` 里写了摄像头小窗位置的主播用"摄像头在上、游戏在下"；其他主播原画面居中，上下用模糊背景铺满。
- 标题、字幕用 Pillow 画成透明图片再用 ffmpeg 叠上去（本机 ffmpeg 没有文字滤镜）；字幕一屏一短句、关键词换黄色，放在画面高度约 65% 处；字幕里的脏话打码。
- 成片在 `runs/<运行名>/vertical/`，方案和统计在 `vertical/summary.json`。

### 实时采集接声音（重构第 4 阶段）

用项目 `.venv` 的 Python 运行采集，就会自动启用弹幕 + 声音融合检测：

```bash
.venv/bin/python -m zhibo collect --start-hub --hours 5 --room 吕德华=<主页链接> ...
```

- 录原画的同一个 ffmpeg 再多输出一路 16 kHz 单声道 PCM，交给一个感知线程逐秒处理。各直播间共用一份模型，约 600 MB 内存。
- 感知线程处理不过来时直接丢掉那一秒并计数（每分钟日志里有"积压、丢弃"），绝不卡住画面录制。
- 每秒感知和逐句稿实时写进数据集的 `perception/`、`transcript/`，事后用 `configs/fused.json` 离线回放，能重现实时的检测。
- `--detector chat` 强制只看弹幕；用系统 `python3`（没有 sherpa-onnx）运行时，会自动退回只看弹幕。

## 代码结构

重构进行中（方案见 [docs/重构方案_声音主导.md](docs/重构方案_声音主导.md)）。第 1 阶段只搬了结构、没改行为。

```
zhibo/
  engine.py        每秒引擎 RoomEngine：实时采集和离线回放共用（聚合 → 检测 → 报告异常开始/结束）
  pipeline.py      离线回放：引擎 → 归因 → 调度/录制覆盖 → 回溯 → 判定 → 出片 → 评测
  collect.py       实时采集：dyhub 弹幕 + 录制 + 引擎 + 即时重放
  core/            models 数据结构 · config 配置 · ledger 模型请求账本 · media FFmpeg 工具
  ingest/          douyin 抖音接口 · hub dyhub 客户端 · recorder 录制（环形缓冲 + 整场音频） · sources 回放数据源
  perceive/        chat 弹幕信号 · audio 音量特征 · speech 说话检测和转写 · tagger 声音分类 · hearing 声音感知总装（以后加 scene）
  detect/          episode 只看弹幕的检测 · fuse 弹幕 + 声音融合检测 · attribution 归因
  act/             replay 即时重放保留策略 · scheduler 录制路数调度
  edit/            backtrack 回溯找边界 · judge 判定器 · render 出片
  evaluate/        evaluate 指标 · report 报告 · demo_data 合成数据
```

## 数据流（离线回放）

```
ReplaySource（消息流 + 录像）                       ingest/sources.py
   │ 按秒推进的回放时钟
   ▼
RoomEngine：每秒聚合 → 热度检测                      engine.py
   ├ SignalAggregator：按秒聚合、礼物去重计价          perceive/chat.py   M2
   └ HeatDetector：每间自己的基线，z 值进出阈值        detect/episode.py  M5
   ▼ 异常结束时
attribute：福袋/PK/开播/单人刷礼物/刷屏/内容          detect/attribution.py M5
   ▼ 只有"内容驱动"往下走
Scheduler.covered：这段有没有录到（环形缓冲）         act/scheduler.py   M4 M3
   ▼
backtrack.plan：信号滞后回推 + 吸附静音边界 + 时长    edit/backtrack.py  M6
   ▼
judge_candidate：证据包 → 判定器 → 格式校验/修订      edit/judge.py      M7
   └─ Ledger：先记账再发出，已收到的直接回放           core/ledger.py     M11
   ▼
render_clip：裁切 + 自检                             edit/render.py     M8
   ▼
evaluate + report：指标、逐条对照、基线对照           evaluate/          M9
```

实时采集走同一个 `RoomEngine`，之后由 `act/replay.py` 决定先占住、保留还是释放录像。调度器每 15 秒按"log(在线人数) × (1 + 0.4 × 平滑热度)"重新分配录制路数。

## 数据集格式

换成真实数据时，按这个格式放到 `data/<名字>/`，再用 `--data` 指定：

| 文件 | 内容 |
|---|---|
| `rooms.json` | 直播间列表：room_id、主播、品类、来源、开播/下播时间（epoch 秒） |
| `messages/<room_id>.jsonl` | 每行一条消息：`room_id`、`recv_ts`、`type`、`payload` |
| `recordings.json` | 每个直播间的录像：单段写成对象，多段写成列表；每段有文件路径和第 0 秒对应的 epoch 时间（时钟对齐） |
| `audio.json` | 实测采集才有：每个直播间的整场音频（`recordings/<room_id>/audio_<开始毫秒>.ts`），每段有文件路径、第 0 秒对应的 epoch 时间和时长。画面只保留触发片段，声音全程保留 |
| `perception/<room_id>.jsonl` | `perceive` 命令补算：每秒一行声音感知，字段 `t`（epoch 秒）、`db`、`rms`、`spike`、`burst`、`zcr`、`speech`、`laugh`、`scream`、`cheer`、`gun`、`music` |
| `transcript/<room_id>.jsonl` | `perceive` 命令补算：主播逐句稿，`start`、`end`（epoch 秒）、`text` |
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
