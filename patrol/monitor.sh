#!/bin/bash
# tg-signer 看门狗 v2  (2026-09-26 重写)
#
# 分层自检，每层可独立降级：
#   L0 代理层 : 经 $PROXY_URL 探测 api.telegram.org
#               PROXY_LOCAL=1（代理在本机 Clash）: 失败 -> 重钉节点 + 刷订阅 + 重启 verge-mihomo
#               PROXY_LOCAL=0（代理在旁路由 passwall，2026-11-06 起）: 失败 -> 只探测 + 告警
#   L1 容器层 : tg-signer-runner 必须 running；不在则 start；容器消失则按
#               /opt/tg-signer/runner.create.sh 原样重建
#   L2 崩溃环 : RestartCount 持续增长 -> 校验启动参数，缺 --session_dir 则重建
#   L3 完成度 : 过了 00:20 后检查当日日志是否有「下次运行时间」
#               未完成 -> 重启容器 + WxPusher 告警（保守策略：不自动补签）
#               已完成 -> 校验当日「开始执行」块数是否达到 config.json 群数
#
# 告警只推 WxPusher（凭据来自 /opt/tg-signer/monitor.env）。
# 同一天同一类告警只推一次；当日签到恢复正常后自动清除标记。
#
# 演练/调试用环境变量（正常 cron 调用时不设置）：
#   MONITOR_DRYRUN=1     只记日志，不真正推送 WxPusher
#   MONITOR_TODAY=日期   覆盖「今天」，用于模拟「当日未完成」
#   MONITOR_NOW=HHMM     覆盖当前时间，用于跳过 pre-window 短路

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LOG=/var/log/tg-signer-monitor.log
CONTAINER=tg-signer-runner
CHECKIN_LOG=/var/log/tg_checkin.log
# tg-signer 自带 RotatingFileHandler（约 3MiB）。轮转若正好发生在签到执行中途，
# 一次完整签到会被切成两半（前半段进 log.1），只 grep 主文件会少计一大半群，
# 进而误报「群数不足」。所以统计时要把轮转兄弟文件一起算上。
CHECKIN_LOGS=$(ls -1 /var/log/tg_checkin.log /var/log/tg_checkin.log.[0-9]* 2>/dev/null)
TASK="${TG_SIGNER_TASK:-my_sign}"
SIGNER_DIR="${TG_SIGNER_ROOT:-/opt/tg-signer}"
CONFIG="$SIGNER_DIR/data/.signer/signs/$TASK/config.json"
STATE_DIR="$SIGNER_DIR/monitor.state"
ENV_FILE="$SIGNER_DIR/monitor.env"
CREATE="$SIGNER_DIR/deploy/runner.create.sh"
PROXY_UNIT=verge-mihomo-standalone
# 代理探测配置。全部留空时跳过 L0，只做 L1/L2/L3（容器与签到完成度）。
# TG_PROXY_URL 建议写 socks5h://<router>:1080，让域名交给代理解析，避免 DNS 污染。
# 若代理在本机 Clash 且能自愈，设 PROXY_LOCAL=1 并配置 PROXY_UNIT。
PROXY_URL="${TG_PROXY_URL:-${TG_PROXY:-}}"
PROXY_LOCAL="${PROXY_LOCAL:-0}"
PROXY_ROUTER="${PROXY_ROUTER:-}"
PROXY_PORT="${PROXY_PORT:-1080}"
PROXY_API="${PROXY_API:-}"
PROBE_URL=https://api.telegram.org
WINDOW_START=0020
LOCK=/var/lock/tg-signer-monitor.lock

TODAY=${MONITOR_TODAY:-$(date +%F)}
NOW_MIN=${MONITOR_NOW:-$(date +%H%M)}
DRYRUN=${MONITOR_DRYRUN:-0}

ts() { date '+%F %T'; }

mkdir -p "$STATE_DIR" /var/lock

# 防止上一次巡检未结束又起一次
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(ts)] another watchdog instance running, exit" >>"$LOG"
  exit 0
fi

# 日志自截断，避免无限增长
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 2097152 ]; then
  tail -n 2000 "$LOG" >"$LOG.tmp" && mv -f "$LOG.tmp" "$LOG"
fi

exec >>"$LOG" 2>&1

echo "[$(ts)] === watchdog run (now=$NOW_MIN today=$TODAY dryrun=$DRYRUN) ==="

# ---------- 工具 ----------
# once <key> : 当天该类事情没做过则返回 0 并打标记
once() {
  local f="$STATE_DIR/alert.$TODAY.$1"
  if [ -e "$f" ]; then return 1; fi
  touch "$f"
  return 0
}

# alert <标题> <正文>
alert() {
  local title="$1" body="$2"
  if [ "$DRYRUN" = "1" ]; then
    echo "[$(ts)] [DRYRUN] would alert: $title | $body"
    return 0
  fi
  if [ ! -f "$ENV_FILE" ]; then
    echo "[$(ts)] alert skipped: $ENV_FILE missing"
    return 0
  fi
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  if [ -z "${WXPUSHER_TOKEN:-}" ] || [ -z "${WXPUSHER_UID:-}" ]; then
    echo "[$(ts)] alert skipped: WXPUSHER_TOKEN/UID empty"
    return 0
  fi
  local j
  j=$(mktemp)
  cat >"$j" <<JSON
{"appToken":"$WXPUSHER_TOKEN","summary":"$title","contentType":2,"content":"$body","uids":["$WXPUSHER_UID"]}
JSON
  local resp
  resp=$(curl -s -m 15 -X POST https://wxpusher.zjiecode.com/api/send/message \
    -H 'Content-Type: application/json' -d "@$j")
  rm -f "$j"
  echo "[$(ts)] alert sent: $title -> $resp"
}

# act <cmd...> : DRYRUN=1 时只记录不执行，用于故障演练
act() {
  if [ "$DRYRUN" = "1" ]; then
    echo "[$(ts)] [DRYRUN] would run: $*"
    return 0
  fi
  "$@"
}

proxy_probe() {
  # 注意：不要写成 `curl ... || echo 000`——curl 失败时 -w 已经输出了 000，
  # 再 echo 会变成 "000000"，导致下面的等值判断永远不成立、自愈永不触发。
  local c
  c=$(curl -s -m 12 -o /dev/null -w '%{http_code}' -x "$PROXY_URL" "$PROBE_URL" 2>/dev/null)
  [ -z "$c" ] && c=000
  echo "${c:0:3}"
}

# MONITOR_TEST_ALERT=1 : 只发一条演练告警验证通路，然后退出（不影响任何状态）
if [ "${MONITOR_TEST_ALERT:-0}" = "1" ]; then
  echo "[$(ts)] test alert requested"
  alert "[演练] tg-signer 看门狗告警通路测试" \
    "这是一条演练消息：看门狗 v2 已上线，WxPusher 告警通路正常。<br>时间 $(date '+%F %T')"
  exit 0
fi

# ---------- L0 代理层（未配置 TG_PROXY_URL 时整层跳过）----------
if [ -z "$PROXY_URL" ]; then
  echo "[$(ts)] L0 skipped: TG_PROXY_URL/TG_PROXY not set"
else
if [ "$PROXY_LOCAL" = "1" ] && ! systemctl is-active --quiet "$PROXY_UNIT"; then
  echo "[$(ts)] L0 $PROXY_UNIT not active -> restart"
  act systemctl restart "$PROXY_UNIT" >/dev/null 2>&1
  sleep 15
fi

fix_tg_route() {
  # 把「电报代理」选择器重钉到真正能访问 Telegram 的节点。
  # Clash 的 URLTest 只用通用网址测速，选中的节点可能能上网但连不上 api.telegram.org。
  if [ -f /opt/clash_fix_tg_route.py ] && [ "$DRYRUN" != "1" ]; then
    /usr/bin/python3 /opt/clash_fix_tg_route.py >>"$LOG" 2>&1
  else
    echo "[$(ts)] [DRYRUN] would run: /opt/clash_fix_tg_route.py"
  fi
}

PCODE=$(proxy_probe)
echo "[$(ts)] L0 telegram probe code=$PCODE"

# 一级/二级自愈都是「折腾本机 Clash」的动作，出口在旁路由时无效，
# 因此按 PROXY_LOCAL 分流：本机代理 -> 原有三级自愈；远程代理 -> 只探测 + 告警。
if [ "$PROXY_LOCAL" = "1" ]; then

# 第一级自愈：只换路由，不重启服务（快，无副作用）
if [ "$PCODE" = "000" ]; then
  echo "[$(ts)] L0 route unusable -> re-pin tg-capable node"
  fix_tg_route
  PCODE=$(proxy_probe)
  echo "[$(ts)] L0 after route fix code=$PCODE"
fi

# 第二级自愈：节点可能整体过期 -> 刷新订阅 + 重启代理
if [ "$PCODE" = "000" ]; then
  echo "[$(ts)] L0 still unusable -> refresh subscription + restart proxy"
  if [ -f /opt/clash_sub_refresh.py ] && [ "$DRYRUN" != "1" ]; then
    /usr/bin/python3 /opt/clash_sub_refresh.py >>/var/log/clash-sub-refresh.log 2>&1
  fi
  act systemctl restart "$PROXY_UNIT" >/dev/null 2>&1
  sleep 20
  # 重启会把选择器重置回第一个节点，必须再修一次路由
  fix_tg_route
  PCODE=$(proxy_probe)
  echo "[$(ts)] L0 after restart code=$PCODE"
  if [ "$PCODE" = "000" ] && once proxy; then
    alert "TG签到链路告警：代理不通 Telegram" \
      "已尝试「重钉可用节点 + 刷新订阅 + 重启 $PROXY_UNIT」三级自愈，经 $PROXY_URL 仍然访问不了 $PROBE_URL。<br>大概率是机场订阅已过期或被封，tg-signer 今晚会连不上 Telegram。<br>详情：/var/log/tg-signer-monitor.log"
  fi
fi

else

# 远程代理（旁路由 passwall）：本机无法自愈，只做佐证探测 + 告警 + 给回滚指引
if [ "$PCODE" = "000" ]; then
  echo "[$(ts)] L0 remote proxy unreachable -> probe router & alert"
  RCODE=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://$PROXY_ROUTER/" 2>/dev/null)
  [ -z "$RCODE" ] && RCODE=000
  echo "[$(ts)] L0 router $PROXY_ROUTER luci code=$RCODE"
  if once proxy; then
    alert "TG签到链路告警：旁路由代理不通" \
      "tg-signer 出口已切到 $PROXY_URL（$PROXY_ROUTER 上的 passwall），经它访问 $PROBE_URL 失败。<br>排查顺序：① ping $PROXY_ROUTER ② 代理主机上 ss -ltn | grep $PROXY_PORT ③ /etc/init.d/passwall status（本次路由器 LuCI 探测返回 $RCODE）。<br>应急回滚：检查 $SIGNER_DIR/deploy/all-in-one.create.sh 里的 TG_PROXY 后重建容器。<br>详情：/var/log/tg-signer-monitor.log"
  fi
fi

fi

# ---------- L0 结束 ----------
fi

# ---------- WebUI 互斥（2026-10-17 移除）----------
# 旧逻辑：WebUI 在跑就跳过 L1/L2/L3。依据是「WebUI 与 runner 共用 my_account
# 会话会互相踢线」。实测推翻：tg_signer/webui/*.py 里没有任何 pyrogram Client /
# subprocess，WebUI 容器内 ss -tn 外部连接数为 0 —— 它根本不连 Telegram，
# 只是个读写 config.json / logs / 签到记录的编辑器。
# 所以 WebUI 常驻也不会干扰 runner，看门狗照常工作即可。
# （历史上的「开始登录」崩溃循环是 runner 自身问题，不是 WebUI 抢会话。）

# ---------- L1 容器层 ----------
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
    echo "[$(ts)] L1 $CONTAINER exists but stopped -> docker start"
    act docker start "$CONTAINER" >/dev/null 2>&1
  else
    echo "[$(ts)] L1 $CONTAINER GONE -> recreate via $CREATE"
    if [ -x "$CREATE" ]; then
      act bash "$CREATE" >>"$LOG" 2>&1
    else
      echo "[$(ts)] L1 recreate script missing!"
      if once container; then
        alert "TG签到链路告警：容器丢失" \
          "容器 $CONTAINER 不存在，且重建脚本 $CREATE 缺失/不可执行，需人工重建。<br>参考命令见 /opt/tg-signer/tg-signer-runner.inspect.bak3.json"
      fi
    fi
  fi
  sleep 12
fi

if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
  echo "[$(ts)] L1 $CONTAINER still not running"
  if once container; then
    alert "TG签到链路告警：容器拉起失败" \
      "容器 $CONTAINER 无法启动（docker start / 重建 均失败），今日签到不会执行，需人工介入。<br>先看：docker logs --tail 50 $CONTAINER"
  fi
  exit 0
fi

# ---------- L2 崩溃循环 ----------
RC=$(docker inspect -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo 0)
PREV_FILE="$STATE_DIR/restartcount"
PREV=$(cat "$PREV_FILE" 2>/dev/null || echo 0)
echo "$RC" >"$PREV_FILE"
echo "[$(ts)] L2 RestartCount=$RC prev=$PREV"

if [ "${RC:-0}" -ge 5 ] && [ "${RC:-0}" -gt "${PREV:-0}" ]; then
  CMD_JSON=$(docker inspect -f '{{json .Config.Cmd}}' "$CONTAINER" 2>/dev/null)
  echo "[$(ts)] L2 crashloop detected, cmd=$CMD_JSON"
  case "$CMD_JSON" in
  *--session_dir*)
    echo "[$(ts)] L2 cmd looks correct, do not blindly restart"
    ;;
  *)
    echo "[$(ts)] L2 cmd missing --session_dir -> recreate"
    if [ -x "$CREATE" ]; then act bash "$CREATE" >>"$LOG" 2>&1; fi
    ;;
  esac
  if once crashloop; then
    alert "TG签到链路告警：容器崩溃循环" \
      "容器 $CONTAINER RestartCount=$RC 且仍在增长，常见原因是启动参数丢失 --session_dir / 会话文件不可读。<br>已按参数校验结果处理，请人工确认。<br>排查：docker logs --tail 80 $CONTAINER"
  fi
fi

# ---------- L3b 汇报链路自检 ----------
# 历史坑：OpenClaw 汇报任务的命令曾被改回指向死日志
# /opt/tg-signer/data/logs/tg_checkin.log，导致每天汇报读到过期数据（"最近一次签到 9-25 / 共 1 个群"）。
# 这里每天校验一次，命令里必须出现真实日志路径。
REPORT_CRON_ID=98c538f9-6951-4d8f-9bbd-b0328aa814c6
REPORT_CFG_FLAG="$STATE_DIR/cfg.$TODAY.reportconfig"
if command -v openclaw >/dev/null 2>&1 && [ ! -e "$REPORT_CFG_FLAG" ]; then
  if ! openclaw cron get "$REPORT_CRON_ID" 2>/dev/null | grep -q "$CHECKIN_LOG"; then
    touch "$REPORT_CFG_FLAG"
    alert "TG签到告警：汇报任务读错日志" \
      "OpenClaw 定时任务「TG签到结果汇报」的命令里没有指向 $CHECKIN_LOG，会读到过期日志（历史症状：汇报显示「最近一次签到 2026-09-25 / 共 1 个群」）。<br>修复命令：<br>openclaw cron edit $REPORT_CRON_ID --command 'python3 /opt/tg-signer/parse_checkin.py $CHECKIN_LOG $TASK'"
  fi
fi

# ---------- L3 当日完成度 ----------
if [ "${NOW_MIN:-0000}" -lt "$WINDOW_START" ]; then
  echo "[$(ts)] L3 pre-window ($NOW_MIN < $WINDOW_START), skip completion check"
  exit 0
fi

DONE=$(cat $CHECKIN_LOGS 2>/dev/null | grep -c "$TODAY .*下次运行时间" || true)
DONE=${DONE:-0}
echo "[$(ts)] L3 completion markers today=$DONE"

if [ "$DONE" -gt 0 ]; then
  BLOCKS=$(cat $CHECKIN_LOGS 2>/dev/null | grep -c "$TODAY .*开始执行" || true)
  BLOCKS=${BLOCKS:-0}
  EXPECTED=$(grep -c '"chat_id"' "$CONFIG" 2>/dev/null || true)
  EXPECTED=${EXPECTED:-0}
  echo "[$(ts)] L3 OK blocks=$BLOCKS expected=$EXPECTED"
  if [ "$EXPECTED" -gt 0 ] && [ "$BLOCKS" -lt "$EXPECTED" ] && once partial; then
    alert "TG签到告警：群数不足" \
      "今日签到只处理了 $BLOCKS 个群，配置里应有 $EXPECTED 个。<br>可能是部分群被禁言/退群（ChatWriteForbidden）或中途断线。<br>日志：/var/log/tg_checkin.log"
  fi
  # 只清掉「未完成」标记，让之后若真的掉线还能再告警。
  # 注意：不能清 alert.$TODAY.* —— 那会把 partial 的「今日已告警过」标记一起删掉，
  # 导致群数不足的告警每 30 分钟重复发一次（2026-10-17 实测连发 3 条）。
  rm -f "$STATE_DIR"/alert."$TODAY".notdone 2>/dev/null
  exit 0
fi

# 未完成：保守策略 —— 重启容器让它自行补跑，但不主动 run-once；同时告警
echo "[$(ts)] L3 ALERT: no completion marker for $TODAY"
if once notdone; then
  act docker restart "$CONTAINER" >/dev/null 2>&1
  echo "[$(ts)] L3 container restarted (no auto run-once by policy)"
  alert "TG签到告警：今日未签到" \
    "截至 $(date '+%H:%M')，$CHECKIN_LOG 中没有 $TODAY 的「下次运行时间」记录。<br>已自动重启 $CONTAINER 尝试让它补跑；<b>按保守策略不会自动补签</b>。<br>如需立即补签请手动执行：<br>docker exec $CONTAINER tg-signer --session_dir /data/sessions -a my_account -w /data/.signer run-once $TASK"
fi

echo "[$(ts)] === watchdog run end ==="
