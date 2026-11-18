#!/bin/bash
# 2026-11-04 起：本脚本只保留文件名，实际委托给 all-in-one.create.sh。
#
# 为什么要改
# ----------
# monitor.sh 的 L1/L2 是写死调 runner.create.sh（$CREATE）的，而 2026-10-17 之后线上
# 一直是 all-in-one 形态：一个容器里跑 entrypoint-all.sh，同时拉起 WebUI 和 runner。
# 旧版本这里直接 `docker run ... tg-signer run`，所以看门狗一旦真触发重建，就会：
#   1) 丢掉 WebUI（9090 端口）；
#   2) 丢掉 entrypoint 的配置热重载 + runner 看门狗；
#   3) 用镜像里**未经 csp-patch 修改**的 core.py 启动 —— 群级签到去重补丁直接失效，
#      于是又退回到「每次重启把 35 个群全部重签一遍」的老问题。
# 委托给 all-in-one.create.sh 后，重建出来仍是当前形态，且 entrypoint 每次启动 runner
# 前都会跑一遍 /data/_patch/patch_tg_signer.py（幂等），补丁不会因容器重建而丢失。
#
# 想回滚到「只跑签到、没有 WebUI」的旧形态，用 runner.only.create.sh。

set -uo pipefail

echo "[$(date '+%F %T')] runner.create.sh -> delegate to all-in-one.create.sh"
exec bash "${TG_SIGNER_ROOT:-/opt/tg-signer}/deploy/all-in-one.create.sh" "$@"
