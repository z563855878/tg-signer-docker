#!/bin/bash
# tg-signer 一体化容器重建脚本（2026-10-17 起）
#
# 一个容器同时承载两件事：
#   1) WebUI  : 0.0.0.0:${GUI_PORT:-9090} —— 改的就是 runner 那份 config.json
#   2) 签到   : run ${TG_SIGNER_TASK:-my_sign}，唯一持有 ${TG_ACCOUNT:-my_account} 会话
# 入口脚本 container/entrypoint-all.sh 负责拉起两者 + 配置变更自动重载 runner。
#
# 容器名仍然叫 tg-signer-runner：monitor.sh 的 L1/L2/L3 都是按这个名字找容器的，
# 改名会让看门狗误判「容器丢失」并重建。名字不动 = 看门狗零改动。
#
# 回滚到「只跑签到、没有 WebUI」的旧形态：
#   docker rm -f tg-signer-runner && bash "$ROOT/deploy/runner.only.create.sh"

set -uo pipefail

# ---------- 可配置项（环境变量优先）----------
ROOT="${TG_SIGNER_ROOT:-/opt/tg-signer}"
TASK="${TG_SIGNER_TASK:-my_sign}"
ACCOUNT="${TG_ACCOUNT:-my_account}"
PROXY="${TG_PROXY:-}"
IMAGE="${TG_SIGNER_IMAGE:-ghcr.io/amchii/tg-signer:0.9.2-webui}"
PORT="${GUI_PORT:-9090}"
: "${TG_SIGNER_GUI_AUTHCODE:?请先设置 TG_SIGNER_GUI_AUTHCODE，见 README 部署步骤 2}"

echo "[$(date '+%F %T')] recreate tg-signer-runner (all-in-one) start"

docker rm -f tg-signer-runner >/dev/null 2>&1

docker run -d \
  --name tg-signer-runner \
  --network host \
  --restart unless-stopped \
  -v "$ROOT/data:/data" \
  -v /var/log:/var/log \
  -v "$ROOT/container/entrypoint-all.sh:/entrypoint-all.sh:ro" \
  -w /data/.signer \
  -e TZ=Asia/Shanghai \
  -e "TG_PROXY=$PROXY" \
  -e "TG_ACCOUNT=$ACCOUNT" \
  -e TG_SIGNER_TASK="$TASK" \
  -e "GUI_PORT=$PORT" \
  -e TG_SIGNER_WORKDIR=/data/.signer \
  -e "TG_SIGNER_GUI_AUTHCODE=$TG_SIGNER_GUI_AUTHCODE" \
  -e "TG_SIGNER_GUI_STORAGE_SECRET=${TG_SIGNER_GUI_STORAGE_SECRET:-}" \
  --entrypoint /bin/bash \
  "$IMAGE" \
  /entrypoint-all.sh

rc=$?
echo "[$(date '+%F %T')] recreate exit=$rc"
if [ "$rc" -ne 0 ]; then
  echo "[$(date '+%F %T')] recreate FAILED, see output above"
  exit "$rc"
fi

sleep 12
docker ps --filter name=tg-signer-runner --format '{{.Names}} {{.Status}}'
echo "---- 入口日志 ----"
tail -12 /var/log/tg-signer-all.log 2>/dev/null
