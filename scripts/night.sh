#!/usr/bin/env bash
# 一晚的完整流程：实时采集 → GLM 判定 → 竖屏成片。日志写到 logs/。
# 用法：bash scripts/night.sh 小时数 名字=主页链接或直播间号 [名字=... 可写多个]
# 例：  bash scripts/night.sh 4 "陈泽=615189692839" "coke老师=https://www.douyin.com/user/<sec_uid>"
#
# 环境变量（都可不设）：
#   DYHUB_COLLECTOR  弹幕采集内核，默认 lightweight。腾讯云 4 GB 机器实测：浏览器内核一个直播间 Chrome 就占约 1.4 GB，
#                    轻量内核不开浏览器、弹幕条数差不多（EVO-030）。被风控收不到弹幕时改成 browser。
#   MIN_FREE_GB      磁盘剩余低于这个值就停录，默认 15。
set -euo pipefail
cd "$(dirname "$0")/.."
hours=$1; shift
args=()
for r in "$@"; do args+=(--room "$r"); done
name=live_$(date +%Y%m%d_%H%M)
mkdir -p logs
export DYHUB_COLLECTOR=${DYHUB_COLLECTOR:-lightweight}
{
  echo "== $(date '+%F %T') 采集 $hours 小时：$*（弹幕内核 $DYHUB_COLLECTOR）"
  .venv/bin/python -m zhibo collect --start-hub --hours "$hours" --min-free-gb "${MIN_FREE_GB:-15}" \
    --out "data/$name" "${args[@]}"
  echo "== $(date '+%F %T') 判定和出片"
  .venv/bin/python -m zhibo run --config configs/fused_glm.json --data "data/$name" --out "runs/$name"
  echo "== $(date '+%F %T') 完成：成片在 runs/$name/vertical/"
} 2>&1 | tee -a "logs/$name.log"
