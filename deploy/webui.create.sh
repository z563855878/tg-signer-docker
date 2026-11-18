#!/bin/bash
# 常驻 WebUI 容器（2026-10-17 起）
#
# 关键前提（已实测）：tg_signer/webui/*.py 里没有任何 pyrogram Client / subprocess，
# WebUI 不连 Telegram（容器内 ss -tn 外部连接数为 0），只读写 workdir 下的
#   signs/<task>/config.json、logs/、users/*/latest_chats.json、签到记录
# 所以它和跑签到的 tg-signer-runner 可以长期并存，互不影响。
#
# 注意：不要传 TG_ACCOUNT / --session_dir / TG_PROXY —— WebUI 用不到，
# 传了反而容易让人误以为它会占会话。
#
# 配置改动怎么生效：
#   本脚本是「回滚形态」用的独立 WebUI 容器，不带热重载。
#   推荐形态是 all-in-one.create.sh：由 container/entrypoint-all.sh 监听 md5 并重载 runner。

set -uo pipefail

NAME=tg-signer-webui
DATA="${TG_SIGNER_ROOT:-/opt/tg-signer}/data"
PORT="${GUI_PORT:-9090}"
IMAGE="${TG_SIGNER_IMAGE:-ghcr.io/amchii/tg-signer:0.9.2-webui}"
: "${TG_SIGNER_GUI_AUTHCODE:?请先设置 TG_SIGNER_GUI_AUTHCODE}"
SECRET_FILE="$DATA/.signer/.gui_storage_secret"
mkdir -p "$DATA/.signer"
if [ -n "${TG_SIGNER_GUI_STORAGE_SECRET:-}" ]; then
  SECRET="$TG_SIGNER_GUI_STORAGE_SECRET"
else
  [ -f "$SECRET_FILE" ] || head -c 32 /dev/urandom | base64 -w0 > "$SECRET_FILE"
  SECRET=$(cat "$SECRET_FILE")
fi

case "${1:-start}" in
  start)
    docker rm -f "$NAME" >/dev/null 2>&1
    docker run -d \
      --name "$NAME" \
      --network host \
      --restart unless-stopped \
      -v "$DATA":/data \
      -v /var/log:/var/log \
      -w /data/.signer \
      -e TZ=Asia/Shanghai \
      -e "TG_SIGNER_GUI_AUTHCODE=$TG_SIGNER_GUI_AUTHCODE" \
      -e "TG_SIGNER_GUI_STORAGE_SECRET=$SECRET" \
      "$IMAGE" \
      tg-signer -w /data/.signer webgui -H 0.0.0.0 -P "$PORT" -S "$SECRET"
    sleep 8
    docker ps --filter name="$NAME" --format '{{.Names}} {{.Status}}'
    curl -s -o /dev/null -w "HTTP %{http_code}\n" "http://127.0.0.1:$PORT" || true
    ;;
  stop)
    docker rm -f "$NAME" >/dev/null 2>&1
    echo "$NAME 已移除（runner 不受影响）"
    ;;
  *) echo "用法: $0 {start|stop}" ; exit 1 ;;
esac
