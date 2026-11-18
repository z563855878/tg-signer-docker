# TG 每日签到链路 · 运维手册

> 本文用占位符表示本机独有信息：`<host>` 是服务器地址，`<task>` 是任务名，`<account>` 是会话名。
> 核心原则：所有逻辑都跑在服务器本机，跟你在哪台电脑上操作无关。

## 1. 架构总览

```
Telegram 出口（本机 Clash 或旁路由代理）
      │  上游 tg-signer 自己管理连接，不读系统代理
      ▼
tg-signer-runner 容器（host 网络，自调度）
      ├── webgui 子进程   WebUI，纯文件编辑器
      └── runner 子进程   每天按 config.sign_at 签到
      ▼
/var/log/tg_checkin.log
      │
      ▼
parse_checkin.py → 日报（可接推送）
```

时间线（示例，实际以 `config.json` 的 `sign_at` 为准）：

| 时间 | 执行者 | 动作 |
|---|---|---|
| 全天 每 30 分钟 | `patrol/monitor.sh` | 分层自检，异常自愈 + 推送告警 |
| 签到前 | 代理健康检查脚本（可选） | 重钉能访问 Telegram 的节点 |
| `sign_at` ± random_seconds | runner 子进程 | 执行当天签到（自调度，无外部 cron） |
| 窗口结束后 | `patrol/monitor.sh` | 检查完成度，未完成则重启 + 告警 |
| 按需 | `parse_checkin.py` | 解析日志出日报 |

## 2. 资产清单

### 容器

| 名称 | 形态 | 说明 |
|---|---|---|
| `tg-signer-runner` | 推荐 | 一个容器两个子进程（WebUI + runner）。**名字不能改**，看门狗按名字找它 |
| `tg-signer-webui` | 回滚形态 | 独立 WebUI 容器，只在 `webui.sh rollback` 后出现 |

### 关键文件

| 路径 | 用途 |
|---|---|
| `/opt/tg-signer/.env` | 部署参数（权限 600） |
| `/opt/tg-signer/monitor.env` | 告警凭据（权限 600） |
| `/opt/tg-signer/data/.signer/signs/<task>/config.json` | 签到配置，WebUI 与 runner 共享的唯一文件 |
| `/opt/tg-signer/data/.signer/users/<id>/latest_chats.json` | 最近会话缓存，WebUI 交互式配置靠它挑群 |
| `/opt/tg-signer/data/.signer/data.sqlite3` | 签到记录 |
| `/opt/tg-signer/data/_patch/patch_tg_signer.py` | 群级幂等补丁 |
| `/var/log/tg-signer-all.log` | 入口脚本日志（子进程启停、配置重载、补丁结果） |
| `/var/log/tg_checkin.log` | 签到执行日志（日报来源） |
| `/var/log/tg-signer-monitor.log` | 看门狗日志 |

### 定时任务

```
*/30 * * * *  /opt/tg-signer/patrol/monitor.sh
20,40,55 0 * * *  /opt/tg-signer/patrol/monitor.sh     # 窗口加密巡检
```

## 3. 常用命令

```bash
cd /opt/tg-signer
source .env

# 今天签到跑没跑
bash deploy/webui.sh status
grep '下次运行时间' /var/log/tg_checkin.log | tail -1

# 出一份日报（不推送）
python3 tools/parse_checkin.py

# 手动补签一次
docker exec tg-signer-runner tg-signer \
  --session_dir /data/sessions -a "$TG_ACCOUNT" -w /data/.signer run-once "$TG_SIGNER_TASK"

# 容器状态与日志
docker ps --filter name=tg-signer
docker logs --tail 100 tg-signer-runner
tail -50 /var/log/tg-signer-all.log

# 代理探测
curl -s -o /dev/null -w '%{http_code}\n' -x "$TG_PROXY" https://api.telegram.org

# 看门狗
MONITOR_DRYRUN=1 patrol/monitor.sh
tail -50 /var/log/tg-signer-monitor.log
```

## 4. 分层排查

### L1 Telegram 链路不通

症状：`TimeoutError`、`api.telegram.org` 探测非 200。

1. `curl -x "$TG_PROXY" https://api.telegram.org` 看返回码
2. 代理在本机：`systemctl status <PROXY_UNIT>`，检查订阅是否过期
3. 代理在旁路由：登路由器看 passwall / 节点状态
4. 注意 `TG_PROXY` 里的 `socks5h://` 与 `socks5://` 区别：前者域名交给代理解析

### L2 订阅过期 / 节点全挂

`MONITOR_DRYRUN=1 patrol/monitor.sh` 看 L0 判定。本机 Clash 模式会自动重钉节点、刷订阅、重启服务；远程代理模式只告警，因为本机动不了旁路由。

### L3 容器崩溃循环

症状：`RestartCount` 持续增长，日志反复出现「开始登录」。

1. `docker inspect tg-signer-runner` 看启动参数有没有 `--session_dir` 和 `-a <account>`——缺了会退化成交互式登录，读不到 stdin 就 EOF 然后崩
2. 参数不对就用 `bash deploy/all-in-one.create.sh` 重建
3. 入口脚本有退避：5 分钟内重启 6 次会 sleep 120 秒，给看门狗介入的时间

历史上的「开始登录」崩溃循环一度被归咎于 WebUI 抢会话。2026-10-17 实测推翻：`tg_signer/webui/*.py` 里没有 pyrogram 调用，WebUI 不连 Telegram。

### L4 今天没签到

1. `grep '下次运行时间' /var/log/tg_checkin.log | tail -1` 看有没有执行
2. 容器 Up 但没签到，多半是**进程假死**：事件循环卡在网络调用上，Docker 看不出来。用 `bash deploy/webui.sh apply` 重启 runner 子进程
3. 检查 `config.json` 的 `sign_at` 和时区（`TZ` 环境变量）
4. 看 `/var/log/tg-signer-all.log` 有没有 `配置变更 ... 重启 runner` 恰好卡在签到窗口前后

### L5 日报没推出来

1. `python3 tools/parse_checkin.py` 手工跑一遍看有没有输出
2. 日志轮转时一次签到可能被切成两半，解析器已兼容 `tg_checkin.log.[0-9]*`，但最多回溯 3 个
3. 检查 WebUI「签到记录」tab 有没有当天的记录

## 5. 回滚

```bash
# 回滚到上游形态：纯 runner + 独立 WebUI（无热重载、无群级补丁）
bash deploy/webui.sh rollback

# 回到推荐形态
source .env && bash deploy/all-in-one.create.sh

# 回到上一版镜像
sed -i 's|^TG_SIGNER_IMAGE=.*|TG_SIGNER_IMAGE=ghcr.io/amchii/tg-signer:0.9.2-webui|' .env
source .env && bash deploy/all-in-one.create.sh
```

数据全在 `./data`，镜像和脚本回滚都不影响它。

## 6. 换机器接手的前置条件

1. 新机器装 Docker，能访问 GHCR
2. 准备可用的 Telegram 出口和 `TG_PROXY`
3. 从旧机器拷贝整个 `/opt/tg-signer/data/`（含 `.signer`、sessions、logs、`_patch`）
4. 拷贝 `.env` 和 `monitor.env`，权限保持 600
5. 重装 crontab 两条巡检
6. 确认 `data/.signer/.gui_storage_secret` 一起带过去，否则 WebUI 需要重新输授权码

## 7. 长期存在的无害现象

| 现象 | 说明 |
|---|---|
| `CHANNEL_INVALID [400]` | 某个群 ID 失效或机器人已不在群里，不影响其他群 |
| `CHAT_WRITE_FORBIDDEN [403]` | 某群没发消息权限 |
| `TimeoutError` 偶发 | 网络抖动，入口脚本会拉起 runner |
| `webgui started` / `runner started` 计数增长 | 说明自愈在工作；短时间内暴增才要看退避日志 |
| `csp-patch SKIP` | 幂等标记已存在，正常；`WARN` 才是锚点失配 |
