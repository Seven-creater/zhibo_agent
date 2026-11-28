#!/bin/sh
# 下载声音感知用的三个模型和字幕重识别用的一个模型到 models/（不进仓库），并建项目 .venv（装 requirements.txt）。来源都是 sherpa-onnx 的 GitHub 发布页（Apache-2.0）。
#   silero_vad_v5.onnx                                        约 2 MB    说话检测
#   sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09     约 166 MB  中文转写（压缩包）
#   sherpa-onnx-zipformer-small-audio-tagging-2024-04-15        约 111 MB  声音分类（压缩包）
#   sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25                  约 838 MB  出片前重识别字幕（压缩包，解压后约 1 GB；运行时约 1.8 GB 内存）
set -e
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -q -r requirements.txt
fi
mkdir -p models
cd models
B=https://github.com/k2-fsa/sherpa-onnx/releases/download
[ -f silero_vad_v5.onnx ] || curl -sSL -o silero_vad_v5.onnx "$B/asr-models/silero_vad_v5.onnx"
for pair in "asr-models sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09" \
            "audio-tagging-models sherpa-onnx-zipformer-small-audio-tagging-2024-04-15" \
            "asr-models sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"; do
  set -- $pair
  if [ ! -d "$2" ]; then
    curl -sSL -o "$2.tar.bz2" "$B/$1/$2.tar.bz2"
    if tar tjf "$2.tar.bz2" | grep -qE '^/|\.\./'; then echo "压缩包里有可疑路径：$2"; exit 1; fi
    tar xjf "$2.tar.bz2" && rm "$2.tar.bz2"
  fi
done
echo "模型已就绪：$(pwd)"
