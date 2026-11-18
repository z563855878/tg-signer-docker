#!/bin/bash
# 按 2026-09-26 实测固化的 tg-signer-runner 重建命令。
# 用于容器丢失/启动参数被改坏时原样恢复，勿随意改动参数顺序。
# 关键：--session_dir /data/sessions 与 -a $TG_ACCOUNT 缺一即会退化为交互式登录 -> EOF -> 崩溃循环。
# 这是「回滚形态」：只跑签到，没有 WebUI、没有配置热重载、没有群级幂等补丁。

set -uo pipefail

# ---------- 可配置项（环境变量优先）----------
ROOT="${TG_SIGNER_ROOT:-/opt/tg-signer}"
TASK="${TG_SIGNER_TASK:-my_sign}"
ACCOUNT="${TG_ACCOUNT:-my_account}"
PROXY="${TG_PROXY:-}"
IMAGE="${TG_SIGNER_IMAGE:-ghcr.io/amchii/tg-signer:0.9.2-webui}"
PORT="${GUI_PORT:-9090}"
: "${TG_SIGNER_GUI_AUTHCODE:?请先设置 TG_SIGNER_GUI_AUTHCODE，见 README 部署步骤 2}"

echo "[$(date '+%F %T')] recreate tg-signer-runner start"

docker rm -f tg-signer-runner >/dev/null 2>&1

docker run -d \
  --name tg-signer-runner \
  --network host \
  --restart unless-stopped \
  -v "$ROOT/data:/data" \
  -v "$ROOT/data/.signer:/data/.signer" \
  -v /var/log:/var/log \
  -w "$ROOT" \
  -e TZ=Asia/Shanghai \
  -e "TG_PROXY=$PROXY" \
  -e "TG_ACCOUNT=$ACCOUNT" \
  "$IMAGE" \
  tg-signer \
  --log-dir /data/logs/runner \
  --log-file /var/log/tg_checkin.log \
  --session_dir /data/sessions \
  -a "$ACCOUNT" \
  -w /data/.signer \
  run "$TASK"

rc=$?
echo "[$(date '+%F %T')] recreate exit=$rc"
if [ "$rc" -ne 0 ]; then
  echo "[$(date '+%F %T')] recreate FAILED, see output above"
  exit "$rc"
fi

sleep 8
docker ps --filter name=tg-signer-runner --format '{{.Names}} {{.Status}}'
