# 原版与本方案的详细差异

对照版本：`amchii/tg-signer` `0.9.2`（`main` 分支在该版本之后只改过 `AGENTS.md`，`tg_signer/webui/` 与 `core.py` 完全相同）。本仓库不修改也不重新分发上游的 `tg_signer/*.py`。

## 1. 先厘清一个常见误解

原版 WebUI **没有任何任务执行入口**。`tg_signer/webui/` 四个文件共 1485 行，全部 import 里没有 `pyrogram.Client`、`subprocess`、`Popen`、`os.system`。它能做的只有：

- 读写 `<workdir>/signs/<task>/config.json` 和 `<workdir>/monitors/<task>/config.json`
- 查看 `users/*/me.json`、`users/*/latest_chats.json`（缓存）
- 查看 SQLite 签到记录和日志文件
- 用「交互式配置」向导生成配置

官方 `docker/README.md` 的工作流是把 `start.sh` 从 `sleep infinity` 改成 `tg-signer run mytasks` 再重启容器，等于承认「配」和「跑」是两种互斥的容器状态。

所以这里说的「共存」，指的不是让 WebUI 去执行任务，而是让**配置编辑器**和**执行进程**同时常驻，并让前者写的文件能被后者及时看到。

## 2. 架构差异

| | 原版 | 本方案 |
|---|---|---|
| 容器数量 | 官方给两个独立 `docker run` 示例：`tg-signer`（CLI）和 `tg-signer-webui`（GUI） | 1 个 `tg-signer-runner` |
| 容器内进程 | 各 1 个 | `entrypoint-all.sh` 作为 1 号进程，拉起 WebUI 子进程 + runner 子进程 |
| 网络 | CLI 容器无端口；WebUI 容器 `-p 8080:8080` | `--network host`，WebUI 监听 `0.0.0.0:${GUI_PORT}` |
| 工作目录 | 两个容器各自挂载，文档未要求对齐 | 强制共享 `/data/.signer`，两边读写同一个 `config.json` |
| 会话持有者 | runner | runner（WebUI 不碰会话） |

一个容器还是两个容器不是关键，关键是**共享同一个 workdir**。原版即使你手工起两个容器共享卷，配置改了 runner 仍然不认——缺的是下面第 3 条。

## 3. 配置热重载（核心改动）

原版 `core.py` 的 `normal_run()` 开头只调用一次 `self.load_config(self.cfg_cls)`，随后 `config.chats` 被 `sign_once()` 闭包捕获。进程不提供任何重载信号、IPC 或文件监听。

`container/entrypoint-all.sh` 每 10 秒做一次：

```
CUR = md5sum /data/.signer/signs/$TASK/config.json
if CUR != LAST:
    若处于签到窗口 → PENDING=1，不动
    否则 → kill -TERM runner 子进程 → 重新拉起 → LAST=CUR
```

只重启 runner 子进程，容器和 WebUI 都不动，所以网页不会掉线。窗口保护为 23:50–00:40，目的是避免重载打断正在遍历群的签到。

进程判活用 `/proc/<pid>/status` 的 `State`，而不是 `kill -0`——后者对僵尸进程也返回真，会漏掉假死。

## 4. 群级签到幂等补丁

原版 `sign_once()` 的结构是：

```python
for chat in config.chats:
    ...
    await self.sign_a_chat(chat)
    ...
self.persist_sign_record(...)   # 整轮结束才写一次
```

签到记录是「任务 + 账号 + 日期」粒度的。只要 runner 在遍历中途断开（网络重连、容器重启、配置重载、看门狗介入），这一轮进度就全丢，重启后 `need_sign()` 判定「今天没签过」，于是从第 1 个群重签，同一个群一天被发多次签到。

`patch/patch_tg_signer.py` 在循环里插入群级进度：

- 每签完一个群立刻写 `progress/<user_id>-<YYYY-MM-DD>.json`（原子写）
- 下一轮跳过已在集合里的群
- `CSP_SIGN_FORCE=1` 可强制忽略进度重签
- 10 天前的进度文件自动清理
- 打补丁前备份 `core.py.csp.bak`，`py_compile` 失败自动回滚
- 幂等：已有标记就 SKIP；锚点匹配数不为 1 就拒绝修改并返回 exit 2

它是**运行时补丁**，每次启动 runner 前由入口脚本跑一遍，不落进镜像、不进本仓库的 Python 源码。

## 5. 外部看门狗

原版没有告警，也没有「今天到底签成功没有」的判定。

`patrol/monitor.sh` 分四层，每层可独立降级：

| 层 | 检查 | 动作 |
|---|---|---|
| L0 | 经 `TG_PROXY_URL` 探测 `api.telegram.org` | 本机 Clash 模式可重钉节点 / 刷订阅 / 重启；远程代理只探测 + 告警 |
| L1 | `tg-signer-runner` 是否 running | 停了就 start；容器没了按 `deploy/runner.create.sh` 重建 |
| L2 | `RestartCount` 是否持续增长 | 校验启动参数，缺 `--session_dir` 就重建 |
| L3 | 过了 00:20 当天日志有没有「下次运行时间」 | 没有就重启容器 + 告警；有则校验群数是否达标 |

告警走 WxPusher，同一天同一类只推一次，恢复正常后清除标记。统计群数时会把轮转的 `tg_checkin.log.[0-9]*` 一起算上，避免 logrotate 正好切在签到中途造成误报。

## 6. 运维工具

原版只有 CLI 和 WebUI。本方案额外提供：

| 文件 | 用途 |
|---|---|
| `tools/parse_checkin.py` | 解析日志出日报（群名 / 积分 / 连续天数 / 是否成功），处理 bot 异步消息归属和日志轮转 |
| `tools/chats.py` | 纯文件方式管理群列表（list/add/rm/text/find/tasks），不占 Telegram 会话 |
| `tools/edit_chats.py` | 按群名移除配置里的群，自动备份 |
| `docs/RUNBOOK.md` | 故障分层排查手册 |

## 7. 本方案没有做什么

这些容易被误认为「魔改了源码」，实际上都没有：

- 没有 fork `tg_signer/` 下的任何 Python 模块
- 没有改 WebUI 的界面或逻辑
- 没有改上游的调度算法、消息处理、AI 调用
- 没有内置任何 Telegram 账号、群 ID、会话或真实数据

所有改动都在容器外的 shell 脚本，以及一个运行时注入的补丁脚本里。
