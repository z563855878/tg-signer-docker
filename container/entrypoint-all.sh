#!/bin/bash
# tg-signer 一体化入口：一个容器里同时跑
#   1) WebUI（9090）—— 纯文件编辑器，读写的就是 runner 的那份 config.json
#   2) 签到 runner —— 唯一持有 my_account 会话的进程
#
# 为什么能共存（2026-10-17 实测结论）：
#   tg_signer/webui/*.py 里没有任何 pyrogram Client / subprocess 调用，
#   WebUI 不连 Telegram（容器内 ss -tn 外部连接数为 0），只读写 workdir 下的
#   config.json / logs / users/*/latest_chats.json / sign records。
#   所以「WebUI 与 runner 共用同一会话会互踢」这个旧结论不成立——
#   历史上的崩溃循环（2026-08-28 01:08~01:19 连续 8 次「开始登录」）是 runner
#   自身的问题，不是 WebUI 抢会话。
#
# 配置热加载：
#   runner 只在启动时读一次 config.json（core.py normal_run() 里 load_config()
#   之后闭包捕获 config.chats）。所以这里监听 config.json 的 md5，
#   一变就重启 runner 进程（WebUI 不受影响），网页上点完保存几秒内即生效。
#
# 签到窗口保护：
#   23:50~00:40 之间即使配置变了也不重启 runner，推迟到窗口结束后补做，
#   避免打断正在执行的签到。

set -uo pipefail

WORKDIR=/data/.signer
TASK="${TG_SIGNER_TASK:-my_sign}"
ACCOUNT="${TG_ACCOUNT:-my_account}"

# 必须显式给 WebUI 指工作目录，否则它会用 DEFAULT_WORKDIR=".signer"，
# 再拼上容器 cwd（/data/.signer）变成 /data/.signer/.signer —— 一个空目录，
# 界面里就看不到任何任务。实测踩过。
export TG_SIGNER_WORKDIR="$WORKDIR"
CFG="$WORKDIR/signs/$TASK/config.json"
PORT="${GUI_PORT:-9090}"
SECRET_FILE="$WORKDIR/.gui_storage_secret"
if [ -n "${TG_SIGNER_GUI_STORAGE_SECRET:-}" ]; then
  SECRET="$TG_SIGNER_GUI_STORAGE_SECRET"
else
  mkdir -p "$WORKDIR"
  [ -f "$SECRET_FILE" ] || head -c 32 /dev/urandom | base64 -w0 > "$SECRET_FILE"
  SECRET=$(cat "$SECRET_FILE")
fi
LOG=/var/log/tg-signer-all.log
WIN_START=2350   # 23:50 起
WIN_END=0040     # 00:40 止（跨午夜）

ts()  { date '+%F %T'; }
log() { echo "[$(ts)] $*" >>"$LOG"; }

md5() { md5sum "$CFG" 2>/dev/null | awk '{print $1}'; }

# 签到窗口内不重启 runner
in_window() {
  local now
  now=$(date +%H%M)
  if [ "$now" -ge "$WIN_START" ] || [ "$now" -lt "$WIN_END" ]; then
    return 0
  fi
  return 1
}

# kill -0 对僵尸进程也会返回真，所以要看 /proc/<pid>/status 的 State
alive() {
  local p=${1:-}
  [ -n "$p" ] || return 1
  [ -r "/proc/$p/status" ] || return 1
  grep -q '^State:.*Z' "/proc/$p/status" && return 1
  return 0
}

RUNNER_CMD=(tg-signer
  --log-dir /data/logs/runner
  --log-file /var/log/tg_checkin.log
  --session_dir /data/sessions
  -a "$ACCOUNT"
  -w "$WORKDIR"
  run "$TASK")

GUI_CMD=(tg-signer
  -w "$WORKDIR"
  webgui -H 0.0.0.0 -P "$PORT" -S "$SECRET")

# ==== csp-patch v1: 群级签到幂等补丁（自愈）====
# 问题：tg_signer/core.py 的 sign_once() 是「整轮遍历完 35 个群」之后才写一次签到记录，
#       所以 runner 只要中途重连/重启，这一轮进度全丢，重启后从头把 35 个群再签一遍，
#       同一个群一天被重复发"签到"多次，很容易被 Telegram 判为异常行为。
# 修法：补丁在 core.py 里加了群级进度文件，每签完一个群立刻落盘，重启后只补签剩余群。
# 脚本幂等：已打过会直接打印 SKIP 退出，因此每次启动 runner 前跑一遍是安全且廉价的。
PATCH=/data/_patch/patch_tg_signer.py
ensure_patch() {
  if [ ! -f "$PATCH" ]; then
    log "WARN: 未找到 $PATCH，跳过 csp-patch（退化为原生行为）"
    return 0
  fi
  local py out rc
  py=$(command -v python3 || command -v python) || {
    log "WARN: 找不到 python，跳过 csp-patch"
    return 0
  }
  out=$("$py" "$PATCH" 2>&1)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    log "WARN: csp-patch 执行失败 rc=$rc: $(printf '%s' "$out" | tr '\n' ' ')"
  else
    log "csp-patch: $(printf '%s' "$out" | tr '\n' ' ')"
  fi
  return 0
}

start_runner() {
  ensure_patch
  "${RUNNER_CMD[@]}" >>"$LOG" 2>&1 &
  RUNNER_PID=$!
  log "runner started pid=$RUNNER_PID"
}

stop_runner() {
  if [ -n "${RUNNER_PID:-}" ]; then
    kill -TERM "$RUNNER_PID" 2>/dev/null
    for _ in 1 2 3 4 5 6; do
      alive "$RUNNER_PID" || break
      sleep 1
    done
    kill -KILL "$RUNNER_PID" 2>/dev/null
    wait "$RUNNER_PID" 2>/dev/null
  fi
}

start_gui() {
  "${GUI_CMD[@]}" >>"$LOG" 2>&1 &
  GUI_PID=$!
  log "webgui started pid=$GUI_PID port=$PORT"
}

log "==== entrypoint-all start: task=$TASK gui=0.0.0.0:$PORT ===="
mkdir -p /data/logs/runner "$(dirname "$LOG")"

start_gui
start_runner

LAST=$(md5)
PENDING=0
RESTARTS=0
WINDOW_START_TS=$(date +%s)

while true; do
  sleep 10

  alive "$GUI_PID" || { log "webgui died -> restart"; start_gui; }

  if ! alive "$RUNNER_PID"; then
    RESTARTS=$((RESTARTS + 1))
    # 5 分钟内重启超过 6 次说明是崩溃循环（多半是会话失效/参数错了），
    # 退避 120 秒，别把自己刷死，也让 monitor.sh 有机会介入
    if [ "$RESTARTS" -ge 6 ] && [ $(( $(date +%s) - WINDOW_START_TS )) -lt 300 ]; then
      log "runner 5 分钟内重启 $RESTARTS 次 -> 疑似崩溃循环，退避 120s"
      sleep 120
      RESTARTS=0
      WINDOW_START_TS=$(date +%s)
    fi
    log "runner died -> restart (#$RESTARTS)"
    start_runner
    LAST=$(md5)
    PENDING=0
    continue
  fi

  CUR=$(md5)
  if [ "$CUR" != "$LAST" ]; then
    if in_window; then
      [ "$PENDING" = 0 ] && log "配置已变更，但处于签到窗口($WIN_START-$WIN_END) -> 延后重载"
      PENDING=1
    else
      log "配置变更 $(echo "$LAST" | cut -c1-8) -> $(echo "$CUR" | cut -c1-8)，重启 runner 使其生效"
      stop_runner
      start_runner
      LAST=$(md5)
      PENDING=0
    fi
  elif [ "$PENDING" = 1 ] && ! in_window; then
    log "签到窗口结束 -> 补做延后的配置重载"
    stop_runner
    start_runner
    LAST=$(md5)
    PENDING=0
  fi
done
