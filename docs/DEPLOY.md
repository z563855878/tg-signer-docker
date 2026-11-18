# 部署指南

从零部署一套一体化 tg-signer。假设目标机器是 Ubuntu/Debian，已装 Docker，且有能访问 Telegram 的网络出口。

## 0. 环境变量一览

所有脚本都读同一套环境变量。推荐写进 `.env` 后 `source .env` 再执行。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TG_SIGNER_GUI_AUTHCODE` | **无，必填** | WebUI 授权码。`all-in-one.create.sh` 检测到空值会直接退出 |
| `TG_SIGNER_ROOT` | `/opt/tg-signer` | 仓库落地目录 |
| `TG_SIGNER_IMAGE` | `ghcr.io/amchii/tg-signer:0.9.2-webui` | 镜像，钉版本不用 `latest` |
| `TG_SIGNER_TASK` | `my_sign` | 签到任务名，对应 `data/.signer/signs/<name>/config.json` |
| `TG_ACCOUNT` | `my_account` | 会话名，对应 `<account>.session` |
| `TG_PROXY` | 空 | 传给 tg-signer 的代理地址 |
| `GUI_PORT` | `9090` | WebUI 端口 |
| `TG_SIGNER_GUI_STORAGE_SECRET` | 空 | 留空则生成并持久化到 `data/.signer/.gui_storage_secret` |
| `TG_PROXY_URL` | 空 | 看门狗 L0 探测用的代理；留空跳过 L0 |
| `PROXY_LOCAL` | `0` | `1` = 代理在本机 Clash，启用自愈 |
| `PROXY_UNIT` | `verge-mihomo-standalone` | 本机代理的 systemd unit |

`monitor.sh` 另外从 `$TG_SIGNER_ROOT/monitor.env` 读 `WXPUSHER_TOKEN` 和 `WXPUSHER_UID`。

## 1. 安装仓库

```bash
sudo mkdir -p /opt/tg-signer
cd /opt/tg-signer
git clone https://github.com/z563855878/tg-signer-docker.git .
cp .env.example .env
chmod 600 .env
```

如果这台机器已有 upstream 的 clone，不要在上面直接覆盖。用新目录，把 `data/` 整个拷过来即可复用配置、会话和签到记录：

```bash
cp -a /旧目录/data /opt/tg-signer/data
```

## 2. 填写 `.env`

至少改这几项：

```bash
TG_SIGNER_GUI_AUTHCODE=$(openssl rand -hex 16)
TG_SIGNER_TASK=my_sign
TG_PROXY=socks5://你的代理:1080
```

把值写进 `.env`，不要把上面的命令输出直接粘到脚本里。

## 3. 首次登录与建任务

如果 `data/.signer/users/` 下已有 `me.json` 和 `latest_chats.json`，说明会话是现成的，可以跳过这一步。

```bash
source .env
docker run -it --rm --network host \
  -v "$TG_SIGNER_ROOT/data:/data" \
  -e TZ=Asia/Shanghai \
  -e "TG_PROXY=$TG_PROXY" \
  -e "TG_ACCOUNT=$TG_ACCOUNT" \
  "$TG_SIGNER_IMAGE" bash
```

容器内：

```bash
tg-signer login            # 输入手机号、验证码；会输出最近对话列表
tg-signer run my_sign      # 按提示建签到任务；建完 Ctrl+C 退出
exit
```

任务名必须和 `.env` 里的 `TG_SIGNER_TASK` 一致。建完确认文件存在：

```bash
ls -l /opt/tg-signer/data/.signer/signs/my_sign/config.json
```

> 如果目标群不在最近对话列表里，先建一个 Telegram 普通 Folder 手动把群加进去，再用 `tg-signer login --from-folder <名字或ID>`。

## 4. 启动一体化容器

```bash
source .env
bash deploy/all-in-one.create.sh
```

脚本会删掉同名旧容器重建，等 12 秒后打印容器状态和入口日志尾部。正常应看到：

```
tg-signer-runner  Up ...
[....] webgui started pid=...
[....] runner started pid=...
[....] csp-patch: OK: ... 已打补丁（csp-patch v1）
```

检查状态：

```bash
bash deploy/webui.sh status
```

输出包含容器、容器内两个进程、9090 监听、WebUI 工作目录、配置群数与 md5、TimeoutError 次数、runner 子进程启动次数、下次签到时间。

浏览器访问 `http://<机器IP>:9090`，输入 `TG_SIGNER_GUI_AUTHCODE`。

### 用 compose 代替脚本

```bash
cd /opt/tg-signer
cp deploy/docker-compose.yml .
docker compose up -d
```

compose 文件用的也是 `entrypoint-all.sh`，功能一致。注意 `entrypoint-all.sh` 要在 `./container/` 下，且 compose 能从 `.env` 读到变量。两种部署方式选一种即可，**不要同时用**。

## 5. 告警与巡检（可选）

```bash
cp monitor.env.example monitor.env
# 填 WXPUSHER_TOKEN / WXPUSHER_UID；不推送就留空
chmod 600 monitor.env

crontab -e
```

加入两行：

```
*/30 * * * * /opt/tg-signer/patrol/monitor.sh
20,40,55 0 * * * /opt/tg-signer/patrol/monitor.sh
```

第二行是签到窗口的加密巡检。

验证告警通路（会真推一条消息）：

```bash
MONITOR_TEST_ALERT=1 /opt/tg-signer/patrol/monitor.sh
```

不真正执行动作、只记录日志的演练模式：

```bash
MONITOR_DRYRUN=1 /opt/tg-signer/patrol/monitor.sh
```

## 6. 验证清单

部署完按这个顺序确认：

- [ ] `bash deploy/webui.sh status` 显示容器 Up、两个 pid、端口监听
- [ ] WebUI 里能看到任务名和群列表（看不到就是 `TG_SIGNER_WORKDIR` 没设，会拼成 `/data/.signer/.signer`）
- [ ] `/var/log/tg-signer-all.log` 里有 `csp-patch: OK`
- [ ] 日志里有「下次运行时间」
- [ ] 改一下配置保存，20 秒内日志出现 `配置变更 ... 重启 runner`，且 WebUI 没掉线
- [ ] `docker exec tg-signer-runner python3 /data/_patch/selftest_skip.py` 三个 stage 全 PASS

## 7. 卸载

```bash
docker rm -f tg-signer-runner tg-signer-webui 2>/dev/null
crontab -l | grep -v 'patrol/monitor.sh' | crontab -
```

`data/` 里是配置、会话和签到记录，卸载时不会删。确认不要了再手工移除。
