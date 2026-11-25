# 第三方代码和模型清单

本仓库按 AGPL-3.0 发布（见 [LICENSE](LICENSE)）。下面列出复用或改写的外部代码、作为外部程序调用的项目，以及下载使用的模型。复用代码的文件开头也写了出处。

## 复用或改写的代码

| 我们的文件 | 来源 | 提交 | 许可证 | 复用方式 |
|---|---|---|---|---|
| `zhibo/perceive/audio.py` | [clips-studio](https://github.com/ColinGPT9/clips-studio) `analysis/audio_features.py` | `bd39d8b` | AGPL-3.0 | 改写成流式；突变改为只看之前 30 秒 |
| `zhibo/perceive/audio.py`（dBFS 算法） | [frigate](https://github.com/blakeblackshear/frigate) `frigate/events/audio.py` | `d20d6dc` | MIT | 参照算法 |
| `zhibo/perceive/speech.py` | [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) `python-api-examples/simulate-streaming-sense-voice-microphone.py`（Copyright (c) 2025 Xiaomi Corporation） | 2.0.3 版发布时的示例 | Apache-2.0 | 改写：输入从麦克风换成样本流，参数按直播调整 |
| `zhibo/detect/fuse.py` | [hotclip](https://github.com/xixihhhh/hotclip) `src/core/highlight/moments.ts` 的按品类加权（`MOMENT_WEIGHTS.reaction`）、共振加成（`MOMENT_AGREEMENT_BONUS`）、饱和降权（`MOMENT_SATURATION`） | `3543584` | AGPL-3.0 | 思路和常数改写成 Python，用在逐秒的实时融合上 |
| `zhibo/edit/evidence.py` | clips-studio `ARCHITECTURE.md` 4.4 节"把信号写成事件时间线文字给模型"；hotclip `src/core/highlight/vision.ts` 的九宫格拼图 | `bd39d8b`；`3543584` | AGPL-3.0 | 思路改写成 Python |
| `zhibo/edit/package.py` | hotclip `src/core/subtitle.ts` 的字幕安全区（画面高度 62～72%）和"关键词换色"样式；clips-studio 游戏布局（摄像头 + 游戏）；OpenShorts 的模糊背景竖屏；video-alchemy 的"剪辑方案即数据" | `3543584`；`bd39d8b`；`d9f14ad`；`3c7fd6b` | AGPL-3.0；AGPL-3.0；MIT；MIT | 思路和参数，用 Python + Pillow + ffmpeg 重写 |
| `zhibo/edit/package.py`（剪辑手法默认不加、要写理由、程序门槛） | [cut-as-code](https://github.com/WhiteTowerAI/cut-as-code) 动效 skill 的"Zero cues is valid"、逐条回答 `avoid_when`；[DaVinci-AutoEdit-Agent](https://github.com/liuluhaixiu/DaVinci-AutoEdit-Agent) `editorial-best-practices.md` 的"BGM 只在用户授权时加""保留已认可版本"；hotclip `coldopen.ts` 的离片头距离门槛 | `a96c13e6`；`93cf371`；`3543584` | MIT；MIT；AGPL-3.0 | 思路，提示词和门槛自己写 |
| `zhibo/edit/timeline.py`、`sound.py`、`textlayer.py` | hotclip `src/core/coldopen.ts`（高潮前置、结局闪现）、`sound-design.ts`（音效打点、BGM 闪避、本地合成音效）、`fillers.ts` 与调研（去停顿时保护笑声）、`subtitle.ts`（弹入回弹参数） | `3543584` | AGPL-3.0 | 思路和参数，用 Python + Pillow + ffmpeg 重写 |
| `zhibo/edit/camfind.py` | clips-studio `gaming/detect.py`（找主播摄像头小窗的需求和判据） | `bd39d8b` | AGPL-3.0 | 思路；实现改为让视觉大模型看一帧 |
| `zhibo/perceive/tagger.py` | sherpa-onnx `python-api-examples/audio-tagging-from-a-file.py`；frigate `frigate/events/audio.py` 的"过音量门槛才分类、按类别设阈值" | 同上；`d20d6dc` | Apache-2.0；MIT | 参照调用方式和做法 |

## 作为外部程序或库使用

| 项目 | 许可证 | 用途 |
|---|---|---|
| [dyhub](https://github.com/ymstar/dyhub) | MIT | 弹幕采集（`third_party/dyhub`，不进仓库） |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 2.0.3 | Apache-2.0 | 说话检测、转写、声音分类（项目 `.venv`） |
| numpy | BSD-3-Clause | 声音特征计算（项目 `.venv`） |
| Pillow 11.3.0 | MIT-CMU（HPND） | 成片包装：把标题、字幕画成透明图片（项目 `.venv`） |
| macOS 自带字体 Hiragino Sans GB（冬青黑体） | 系统字体，随系统授权，本仓库不分发 | 标题、字幕字体 |
| FFmpeg | LGPL/GPL（随本机安装） | 录制、解码、裁切 |

## 模型（`scripts/fetch_models.sh` 下载到 `models/`，不进仓库）

| 模型 | 来源 | 许可证 |
|---|---|---|
| silero_vad_v5.onnx | [silero-vad](https://github.com/snakers4/silero-vad)，经 sherpa-onnx 发布页 | MIT |
| sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09 | [SenseVoice](https://github.com/FunAudioLLM/SenseVoice)（FunAudioLLM），经 sherpa-onnx 转换 | 仓库为 MIT；模型权重的许可条款【待核实】 |
| sherpa-onnx-zipformer-small-audio-tagging-2024-04-15 | k2-fsa（icefall 训练，AudioSet） | Apache-2.0 |

## 借鉴了思路、还没复用代码的项目

clips-studio 的候选排序（rerank）；frigate 的 `cutoff_time`（EVO-012 的结束规则）。真正复用到代码时再补进上面的表。
