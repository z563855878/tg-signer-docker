#!/usr/bin/env python3
"""tg-signer 签到群管理（纯文件操作，不依赖 WebUI、不占用 Telegram 会话）。

用法:
  chats.py list [--task NAME] [--kw 关键字]      列出已配置的签到群
  chats.py add  <chat_id|@username> [选项]       新增一个签到群
  chats.py rm   <chat_id|@username|序号>         删除一个签到群
  chats.py text <chat_id|@username|序号> "文本"  修改签到词
  chats.py find <关键字>                         从本地缓存的会话表里反查 chat_id（不联网）
  chats.py tasks                                 列出所有任务目录

add 的选项:
  --name 群名           备注名（可选，仅用于人看）
  --text 签到           发送文本（action=1，默认「签到」）
  --dice 🎲             发送骰子表情（action=2）
  --click 按钮文字       按文字点击键盘按钮（action=3）
  --image               按图片选择选项（action=4）
  --calc               回复计算题（action=5）
  --thread ID           话题/子频道 ID
  --delete-after N      发送 N 秒后删除
  --apply               改完自动重启 runner 使配置生效

重要:
  1) runner 进程只在启动时读一次 config.json，改完需要重载才生效。
  2) 早期认为「WebUI 与 runner 共用会话会互踢」，2026-10-17 实测推翻：
     WebUI 不连 Telegram，只是文件编辑器，可与 runner 同容器共存。
     推荐形态下 --apply 只重启 runner 子进程，WebUI 不中断。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("TG_SIGNER_ROOT", "/opt/tg-signer"))
SIGN_DIR = ROOT / "data" / ".signer" / "signs"
DEFAULT_TASK = os.environ.get("TG_SIGNER_TASK", "my_sign")
RUNNER = "tg-signer-runner"
WEBUI = "tg-signer-webui"

ACTION_DESC = {
    1: "发送文本",
    2: "发送骰子",
    3: "点击按钮",
    4: "看图选选项",
    5: "答计算题",
}


# ---------- 基础工具 ----------

def die(msg: str, code: int = 1):
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(code)


def config_path(task: str) -> Path:
    p = SIGN_DIR / task / "config.json"
    if not p.exists():
        die(f"任务配置不存在：{p}\n可用任务：{', '.join(list_tasks())}")
    return p


def list_tasks():
    if not SIGN_DIR.exists():
        return []
    return sorted(d.name for d in SIGN_DIR.iterdir() if (d / "config.json").exists())


def load(task: str):
    p = config_path(task)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f), p


def backup(p: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dst = p.with_suffix(f".json.bak.{stamp}")
    shutil.copy2(p, dst)
    return dst


def save(cfg: dict, p: Path):
    bak = backup(p)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)
    print(f"已写入 {p}\n备份：{bak}")


def parse_id(value: str):
    """返回 int 或 @username（小写）。"""
    v = str(value).strip()
    if v.startswith("@"):
        if len(v) == 1:
            die("username 不能为空")
        return v.lower()
    try:
        return int(v)
    except ValueError:
        die(f"无法识别的 chat_id：{value}（应为整数或 @username）")


def id_str(v) -> str:
    return str(v)


def resolve(cfg: dict, key: str):
    """按 chat_id / @username / 序号 定位群，返回 (index, chat)。"""
    chats = cfg.get("chats", [])
    # 1) 序号
    if re.fullmatch(r"\d{1,4}", key) and 1 <= int(key) <= len(chats):
        i = int(key) - 1
        return i, chats[i]
    # 2) chat_id / username 精确匹配
    k = key.strip()
    if k.startswith("@"):
        k = k.lower()
    else:
        try:
            k = int(k)
        except ValueError:
            pass
    for i, c in enumerate(chats):
        if id_str(c.get("chat_id")) == str(k):
            return i, c
    # 3) 群名关键字
    hits = [i for i, c in enumerate(chats) if k and str(k) in str(c.get("name") or "")]
    if len(hits) == 1:
        return hits[0], chats[hits[0]]
    if len(hits) > 1:
        print("匹配到多个群，请用序号或 chat_id：")
        for i in hits:
            print(f"  {i + 1}. {chats[i].get('chat_id')}  {chats[i].get('name')}")
        die("目标不唯一")
    die(f"未找到：{key}")


def action_brief(actions: list) -> str:
    parts = []
    for a in actions or []:
        t = a.get("action")
        extra = a.get("text") or a.get("dice") or ""
        desc = ACTION_DESC.get(t, str(t))
        parts.append(f"{desc}:{extra}" if extra else desc)
    return " → ".join(parts) or "（空）"


# ---------- 子命令 ----------

def cmd_list(args):
    cfg, p = load(args.task)
    chats = cfg.get("chats", [])
    kw = args.kw
    print(f"任务：{args.task}   共 {len(chats)} 个群   "
          f"签到时间 {cfg.get('sign_at')} ±{cfg.get('random_seconds')}s")
    print(f"{'#':>3}  {'chat_id':<18}{'话题':<8}群名 / 动作")
    n = 0
    for i, c in enumerate(chats, 1):
        name = c.get("name") or ""
        line = f"{id_str(c.get('chat_id'))}  {name}  [{action_brief(c.get('actions'))}]"
        if kw and kw not in line:
            continue
        n += 1
        thread = c.get("message_thread_id") or "-"
        print(f"{i:>3}  {id_str(c.get('chat_id')):<18}{str(thread):<8}{name}  "
              f"[{action_brief(c.get('actions'))}]")
    if kw:
        print(f"（关键字「{kw}」命中 {n} 个）")


def cmd_add(args):
    cfg, p = load(args.task)
    cid = parse_id(args.target)
    for c in cfg.get("chats", []):
        if id_str(c.get("chat_id")) == str(cid):
            die(f"该群已在列表里（#{cfg['chats'].index(c) + 1} {c.get('name')}），无需重复添加")

    actions = []
    if args.dice:
        actions.append({"action": 2, "dice": args.dice})
    if args.text:
        actions.append({"action": 1, "text": args.text})
    if args.click:
        actions.append({"action": 3, "text": args.click})
    if args.image:
        actions.append({"action": 4})
    if args.calc:
        actions.append({"action": 5})
    if not actions:
        actions.append({"action": 1, "text": "签到"})

    chat = {
        "chat_id": cid,
        "message_thread_id": args.thread,
        "name": args.name or None,
        "delete_after": args.delete_after,
        "actions": actions,
        "action_interval": 1.0,
    }
    cfg.setdefault("chats", []).append(chat)
    print(f"新增：{cid}  {args.name or ''}  [{action_brief(actions)}]")
    print(f"  → 第 {len(cfg['chats'])} 个群")
    save(cfg, p)
    maybe_apply(args)


def cmd_rm(args):
    cfg, p = load(args.task)
    i, c = resolve(cfg, args.target)
    print(f"删除 #{i + 1}  {c.get('chat_id')}  {c.get('name')}  [{action_brief(c.get('actions'))}]")
    cfg["chats"].pop(i)
    print(f"剩余 {len(cfg['chats'])} 个群")
    save(cfg, p)
    maybe_apply(args)


def cmd_text(args):
    cfg, p = load(args.task)
    i, c = resolve(cfg, args.target)
    acts = c.get("actions") or []
    hit = False
    for a in acts:
        if a.get("action") == 1:
            old = a.get("text")
            a["text"] = args.text
            print(f"#{i + 1} {c.get('name')}：签到词「{old}」→「{args.text}」")
            hit = True
            break
    if not hit:
        acts.append({"action": 1, "text": args.text})
        print(f"#{i + 1} {c.get('name')}：新增文本动作「{args.text}」")
        c["actions"] = acts
    save(cfg, p)
    maybe_apply(args)


def cmd_find(args):
    base = ROOT / "data" / ".signer" / "users"
    files = sorted(base.glob("*/latest_chats.json")) if base.exists() else []
    if not files:
        die("没有本地会话缓存（users/*/latest_chats.json），只能从 WebUI 里复制 chat_id")
    kw = args.target.lower()
    print(f"{'chat_id':<18}{'type':<18}标题 / 用户名")
    n = 0
    for f in files:
        try:
            data = json.load(open(f, "r", encoding="utf-8"))
        except Exception:
            continue
        for c in data:
            blob = " ".join(str(c.get(k) or "") for k in ("title", "username", "first_name", "last_name"))
            if kw not in blob.lower():
                continue
            n += 1
            title = c.get("title") or " ".join(
                str(c.get(k) or "") for k in ("first_name", "last_name")
            ).strip()
            uname = f"@{c['username']}" if c.get("username") else ""
            print(f"{c.get('id')!s:<18}{str(c.get('type')):<18}{title} {uname}")
    print(f"（命中 {n} 条；缓存来自上次登录，新群可能查不到，请以 WebUI 列表为准）")


def cmd_tasks(args):
    for t in list_tasks():
        cfg, _ = load(t)
        print(f"{t:<28}{len(cfg.get('chats', [])):>3} 个群  sign_at={cfg.get('sign_at')}")


# ---------- 生效 ----------

def container_state(name: str) -> str:
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", name],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def maybe_apply(args):
    if not getattr(args, "apply", False):
        print("\n[提示] 推荐形态下 entrypoint 每 10s 自动检测 md5 并重载 runner，")
        print("       不想等就执行：webui.sh apply（只重启 runner 子进程，WebUI 不中断）")
        return
    apply_sh = ROOT / "deploy" / "webui.sh"
    if apply_sh.exists():
        r = subprocess.run(["bash", str(apply_sh), "apply"], capture_output=True, text=True)
        print(f"\nwebui.sh apply → rc={r.returncode}")
        print(r.stdout.strip() or r.stderr.strip())
        return
    # 兼容回滚形态（纯 runner + 独立 WebUI）
    if container_state(RUNNER):
        r = subprocess.run(["docker", "restart", RUNNER], capture_output=True, text=True)
        print(f"\ndocker restart {RUNNER} → rc={r.returncode} {r.stderr.strip()}")
    else:
        print(f"\n容器 {RUNNER} 不存在，改用 deploy/runner.create.sh 重建…")
        subprocess.run(["bash", str(ROOT / "deploy" / "runner.create.sh")])
    subprocess.run(
        ["docker", "ps", "--filter", f"name={RUNNER}", "--format", "{{.Names}} {{.Status}}"]
    )


# ---------- 入口 ----------

def main():
    ap = argparse.ArgumentParser(
        description="tg-signer 签到群管理（不依赖 WebUI）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--task", default=DEFAULT_TASK, help=f"任务名，默认 {DEFAULT_TASK}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出签到群")
    p.add_argument("--kw", help="按关键字过滤")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("add", help="新增签到群")
    p.add_argument("target", help="chat_id（如 -1001234567890）或 @username")
    p.add_argument("--name", help="备注群名")
    p.add_argument("--text", help="发送文本")
    p.add_argument("--dice", help="发送骰子表情 🎲🎯🏀⚽🎳🎰")
    p.add_argument("--click", help="按文字点击按钮")
    p.add_argument("--image", action="store_true", help="看图选选项")
    p.add_argument("--calc", action="store_true", help="答计算题")
    p.add_argument("--thread", type=int, help="话题 ID")
    p.add_argument("--delete-after", type=int, help="N 秒后删除")
    p.add_argument("--apply", action="store_true", help="改完自动重启 runner")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("rm", help="删除签到群")
    p.add_argument("target", help="chat_id / @username / 序号 / 群名关键字")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("text", help="修改签到词")
    p.add_argument("target")
    p.add_argument("text")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("find", help="从本地缓存反查 chat_id")
    p.add_argument("target", help="群名关键字")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("tasks", help="列出任务")
    p.set_defaults(func=cmd_tasks)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
