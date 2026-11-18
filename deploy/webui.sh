#!/bin/bash
# tg-signer 一体化容器管理（2026-10-17 23:34 起生效）
#
# 现在【只有一个容器 tg-signer-runner】，里面同时跑：
#   pid A : webgui  —— 0.0.0.0:$GUI_PORT，工作目录 /data/.signer
#   pid B : runner  —— run $TG_SIGNER_TASK，唯一持有 $TG_ACCOUNT 会话
# 入口 container/entrypoint-all.sh 负责拉起两者 + 配置变更自动重启 runner 子进程。
#
# 关键前提（实测）：WebUI 不连 Telegram（tg_signer/webui/*.py 无任何 pyrogram 调用，
#   容器内外部连接数 0），它只是读写 /data/.signer 下 config.json / logs / 签到记录的
#   编辑器，所以与 runner 同容器长期共存互不干扰。
#
# WebUI 工作目录坑（已修）：data.py 的 DEFAULT_WORKDIR 是相对路径 ".signer"，
#   而容器 cwd 是 /data/.signer，不设 TG_SIGNER_WORKDIR 会解析成
#   /data/.signer/.signer（空目录）→ 界面上什么任务都看不到。
#
# 用法:
#   webui.sh status   看全貌
#   webui.sh apply    手动重启 runner 子进程让配置立刻生效（容器不动）
#   webui.sh rebuild  按 all-in-one.create.sh 重建整个容器
#   webui.sh rollback 回滚到「纯 runner + 独立 WebUI」双容器形态

set -uo pipefail

NAME=tg-signer-runner
ROOT="${TG_SIGNER_ROOT:-/opt/tg-signer}"
WORKDIR="$ROOT/data/.signer"
TASK="${TG_SIGNER_TASK:-my_sign}"
CFG="$WORKDIR/signs/$TASK/config.json"
PORT="${GUI_PORT:-9090}"

cfg_nums() { python3 -c "import json;print(len(json.load(open('$CFG'))['chats']))" 2>/dev/null || echo '?'; }
cfg_md5()  { md5sum "$CFG" 2>/dev/null | awk '{print $1}' | cut -c1-8; }

do_status() {
  echo "---- 容器（应为 1 个）----"
  docker ps -a --filter name=tg-signer --format '{{.Names}}\t{{.Status}}'
  echo "---- 容器内进程 ----"
  docker exec "$NAME" sh -c 'for p in /proc/[0-9]*; do c=$(tr "\0" " " < $p/cmdline 2>/dev/null); case "$c" in *tg-signer*) echo "  pid=${p#/proc/}";; esac; done' 2>/dev/null
  echo "---- 端口 $PORT ----"
  ss -lntp 2>/dev/null | grep ":$PORT" | head -1 || echo "  未监听"
  echo "---- WebUI 工作目录 ----"
  docker exec "$NAME" python3 -c "
from tg_signer.webui.data import get_workdir, DEFAULT_WORKDIR, list_task_names
w = get_workdir(DEFAULT_WORKDIR)
print('  workdir =', w)
print('  任务    =', list_task_names('signer', w))
" 2>/dev/null
  echo "---- 配置 ----"
  echo " $(cfg_nums) 个群  md5=$(cfg_md5)  mtime=$(stat -c %y "$CFG" 2>/dev/null | cut -d. -f1)"
  echo "---- 健康 ----"
  echo -n " TimeoutError: "; docker logs "$NAME" 2>&1 | grep -c 'TimeoutError' || true
  echo -n " runner 子进程启动次数: "; grep -c 'runner started' /var/log/tg-signer-all.log 2>/dev/null || echo 0
  echo "---- 下次签到 ----"
  grep '下次运行时间' /var/log/tg_checkin.log 2>/dev/null | tail -1
}

case "${1:-status}" in
  status)   do_status ;;
  apply)
    # 直接改 config 也行，entrypoint 每 10s 会自己发现；
    # 这里是「不想等」的手动触发：重启 runner 子进程，容器和 WebUI 都不动
    pid=$(docker exec -e "TASK=$TASK" "$NAME" sh -c 'for p in /proc/[0-9]*; do c=$(tr "\0" " " < $p/cmdline 2>/dev/null); case "$c" in *"run $TASK"*) echo "${p#/proc/}";; esac; done' | head -1)
    if [ -z "$pid" ]; then echo "没找到 runner 子进程"; exit 1; fi
    echo "重启 runner 子进程 pid=$pid（容器与 WebUI 不受影响）"
    docker exec "$NAME" kill -TERM "$pid"
    sleep 15
    grep '下次运行时间' /var/log/tg_checkin.log | tail -1
    echo "当前配置：$(cfg_nums) 个群 md5=$(cfg_md5)"
    ;;
  rebuild)  exec bash "$ROOT/deploy/all-in-one.create.sh" ;;
  rollback)
    echo "回滚到双容器形态：纯 runner + 独立 WebUI"
    bash "$ROOT/deploy/runner.only.create.sh"
    bash "$ROOT/deploy/webui.create.sh" start
    ;;
  *) echo "用法: $0 {status|apply|rebuild|rollback}" ; exit 1 ;;
esac
