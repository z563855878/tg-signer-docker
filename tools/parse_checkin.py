#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析 tg-signer 运行日志，输出每日签到结果报告（群名 / 积分 / 连续天数 / 是否成功）。

设计要点（保证「群名与数量 100% 正确」+「字段归属不串群」）：

1. 轮次定位
   tg-signer 每完成一轮会打印「下次运行时间: ...」。以此为轮次分隔符，
   从文件尾部反推最近一轮的 [START, END) 区间，避免把历史多轮累加，
   也避免最后一个群块「吃掉」常驻监听期间的全部群聊噪音（实测会多吞 600+ 条）。

2. 群块切分
   每个群一个「开始执行:」块，块头自带任务名与 ║ Name: / ║ Chat ID:。
   块数 == 群数，这是权威来源（与配置里的 chat 列表一一对应）。

3. 消息归属（关键）
   bot 回复是异步涌入的，一个块内会混进其他群的消息。
   但 tg-signer 自己会对不属于当前 chat 的消息打印
   「忽略意料之外的聊天」WARNING。因此规则为：
       某条「收到来自「X」的消息」若紧跟 忽略意料之外的聊天 -> 丢弃
       否则 -> 确实属于当前群
   这是程序自身的 chat_id 判定结果，比任何文本猜测都可靠。

4. 字段提取
   在归属正确的消息中，先找「签到成功」类消息，只从这一条里取积分/连续天数，
   避免把「您的积分: 873」「本群积分榜」等无关消息误当成签到结果。
   找不到成功消息时，再找「已经签到过」类消息（判为「今日已签过」）。

用法:
    python3 parse_checkin.py [LOG_PATH] [TASK_NAME] [--json]
默认 LOG_PATH=/var/log/tg_checkin.log, TASK_NAME 自动取最近一轮的任务名。
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
from datetime import date
from typing import Dict, List, Optional, Tuple

DEFAULT_LOG = os.environ.get("TG_CHECKIN_LOG", "/var/log/tg_checkin.log")

# 最多往回拼接几个轮转文件（1 = 只拼 log.1）
MAX_ROTATED = 3

BLOCK_RE = re.compile(r"任务「([^」]+)」:\s*开始执行")
ROUND_END_RE = re.compile(r"下次运行时间:")
NAME_RE = re.compile(r"║\s*Name:\s*(.+?)\s*║")
CHATID_RE = re.compile(r"║\s*Chat ID:\s*(-?\d+)\s*║")
TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
# 每条日志记录以 [LEVEL] 开头
REC_RE = re.compile(r"^\[(?:INFO|WARNING|ERROR|DEBUG)\] ", re.M)
MSG_RE = re.compile(r"收到来自「([^」]+)」的消息:")
IGNORE_RE = re.compile(r"忽略意料之外的聊天")
# 本块真正把「签到」发出去的时刻；此前出现的消息是上一个群的迟到回复
SENT_RE = re.compile(r"处理完成:\s*action=")

# 「本次签到成功」语义
SUCCESS_RE = re.compile(r"签到成功|打卡成功")
# 「今天已经签过了」语义（重复运行 / 已由其他渠道签过）
ALREADY_RE = re.compile(
    r"已经签到过|已签过到|今日已签到|已经签到了|已留下足迹|重复签到|今天已签到"
)
# 积分：覆盖「获得积分 10」「获得：10 积分」「获得: 50 积分」「积分加10」
POINTS_RE = re.compile(r"获得积分\s*(\d+)|获得[：:]\s*(\d+)\s*积分|积分加\s*(\d+)")
# 连续签到：覆盖「连续签到 42 天」「已连续签到2天」
STREAK_RE = re.compile(r"连续签到\s*(\d+)\s*天")


def _read_text(path: str) -> str:
    """读取日志正文，兼容被 logrotate 压缩过的 .gz。"""
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as f:
            return f.read()
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


def load_log_text(path: str) -> str:
    """读取日志，并把被轮转切走的兄弟文件按「旧 → 新」拼在前面。

    tg-signer 用 RotatingFileHandler（约 3MiB）写 /var/log/tg_checkin.log。
    一旦轮转正好发生在签到执行过程中，一次完整的签到会被切成两半：
    前半段进了 log.1，后半段留在新 log。只解析 log 就会少统计大半的群
    （实测 2026-10-17：27 个群只报了 10 个）。
    """
    d = os.path.dirname(path) or "."
    base = os.path.basename(path)
    pat = re.compile(re.escape(base) + r"\.(\d+)(?:\.gz)?$")
    rotated: List[Tuple[int, str]] = []
    try:
        for name in os.listdir(d):
            m = pat.match(name)
            if m:
                rotated.append((int(m.group(1)), os.path.join(d, name)))
    except OSError:
        rotated = []
    # 数字越大越旧；只取最近 MAX_ROTATED 个，再倒序成「旧 → 新」
    rotated.sort(key=lambda x: x[0])
    rotated = rotated[:MAX_ROTATED][::-1]

    chunks: List[str] = []
    for _, p in rotated:
        try:
            chunks.append(_read_text(p))
        except OSError:
            pass
    chunks.append(_read_text(path))  # 主文件不存在时照旧抛 FileNotFoundError
    return "\n".join(chunks)


def _pick(m: Optional[re.Match]) -> Optional[int]:
    """从多分组正则匹配中取出第一个非空数字分组。"""
    if not m:
        return None
    for g in m.groups():
        if g is not None:
            return int(g)
    return None


def locate_round(text: str, task_name: Optional[str]) -> Tuple[str, List[int], int]:
    """定位最近一轮执行的群块位置。

    Args:
        text: 完整日志文本。
        task_name: 指定任务名；None 表示自动取最近一轮所属任务。

    Returns:
        (任务名, 该轮各群块起始偏移列表, 该轮结束偏移)。无数据时返回 ("", [], 0)。
    """
    blocks = [(m.start(), m.group(1)) for m in BLOCK_RE.finditer(text)]
    if task_name:
        blocks = [(p, t) for (p, t) in blocks if t == task_name]
    if not blocks:
        return (task_name or ""), [], 0

    bounds = [m.start() for m in ROUND_END_RE.finditer(text)]
    last_pos, actual = blocks[-1]

    # 该轮结束：最后一个块之后的第一个「下次运行时间」；没有则到文件尾
    after = [b for b in bounds if b > last_pos]
    end = after[0] if after else len(text)
    # 该轮开始：最后一个块之前最近的「下次运行时间」（同轮块之间不会出现该标记）
    before = [b for b in bounds if b < last_pos]
    start = max(before) if before else -1

    positions = [p for (p, t) in blocks if start < p < end and t == actual]
    return actual, positions, end


def split_records(block: str) -> List[str]:
    """把一个群块切成独立的日志记录（每条以 [LEVEL] 开头）。"""
    idx = [m.start() for m in REC_RE.finditer(block)] + [len(block)]
    return [block[idx[i] : idx[i + 1]] for i in range(len(idx) - 1)]


def split_by_send(block: str) -> Tuple[List[str], List[str]]:
    """把块内「确属某群」的消息按发送时刻拆成 (pre, post)。

    日志块以「开始执行」切分，但本群的「签到」消息要到「处理完成: action=」
    才真正发出。因此块内消息分两类：

    - pre : 位于「处理完成」之前 —— 此时本群还没发言，这些其实是
            **上一个群** 的迟到回复（实测：四川情报局的 SCQBJ_bot 回复
            延迟 2.2s，落到了下一个块的开头）。
    - post: 位于「处理完成」之后 —— 属于本群。

    两类都会先剔除紧跟「忽略意料之外的聊天」WARNING 的消息，
    那是 tg-signer 依据 chat_id 判定的非本群消息。

    Returns:
        (pre, post) 两个消息正文列表。
    """
    recs = split_records(block)
    sent_at = next((i for i, r in enumerate(recs) if SENT_RE.search(r)), len(recs))

    pre: List[str] = []
    post: List[str] = []
    for i, rec in enumerate(recs):
        if not MSG_RE.search(rec):
            continue
        nxt = recs[i + 1] if i + 1 < len(recs) else ""
        if IGNORE_RE.search(nxt):
            continue
        (pre if i < sent_at else post).append(rec)
    return pre, post


def analyze(msgs: List[str]) -> Dict[str, object]:
    """从归属正确的消息中判定签到状态并提取积分/连续天数。

    优先采纳「签到成功」消息；只从该条消息内提取积分与连续天数，
    避免「您的积分: 873」「本群积分榜」等无关文本污染结果。
    """
    for m in msgs:
        if SUCCESS_RE.search(m):
            return {
                "status": "success",
                "points": _pick(POINTS_RE.search(m)),
                "streak": _pick(STREAK_RE.search(m)),
            }
    for m in msgs:
        if ALREADY_RE.search(m):
            return {
                "status": "already",
                "points": None,
                "streak": _pick(STREAK_RE.search(m)),
            }
    return {"status": "none", "points": None, "streak": None}


STATUS_LABEL = {
    "success": "✅ 签到成功",
    "already": "☑️ 今日已签过",
    "none": "❌ 无签到回复",
}


def parse(text: str, task_name: Optional[str]) -> Tuple[str, str, List[dict]]:
    """解析日志，返回 (任务名, 本轮时间, 每群结果列表)。"""
    actual, positions, end = locate_round(text, task_name)
    if not positions:
        return actual, "", []

    # 时间戳位于「开始执行」匹配点之前（同一行行首），需回退到行首再搜
    head = text[text.rfind("\n", 0, positions[0]) + 1 : positions[0]]
    ts_m = TS_RE.search(head)
    run_at = ts_m.group(1) if ts_m else ""

    # 先按块拆出 (群名, chat_id, pre, post)
    parsed: List[Tuple[str, str, List[str], List[str]]] = []
    for i, p in enumerate(positions):
        be = positions[i + 1] if i + 1 < len(positions) else end
        blk = text[p:be]
        name_m = NAME_RE.search(blk)
        cid_m = CHATID_RE.search(blk)
        pre, post = split_by_send(blk)
        parsed.append(
            (
                name_m.group(1).strip() if name_m else "(未知群)",
                cid_m.group(1) if cid_m else "",
                pre,
                post,
            )
        )

    # 每群消息 = 自己的 post + 下一块的 pre（下一块发言前到达的迟到回复属于本群）
    results: List[dict] = []
    for i, (name, cid, _pre, post) in enumerate(parsed):
        msgs = list(post)
        if i + 1 < len(parsed):
            msgs += parsed[i + 1][2]
        info = analyze(msgs)
        results.append(
            {
                "群名": name,
                "chat_id": cid,
                "是否签到成功": STATUS_LABEL[info["status"]],
                "_status": info["status"],
                "获得积分": info["points"] if info["points"] is not None else "-",
                "连续签到天数": info["streak"] if info["streak"] is not None else "-",
            }
        )

    # 去重：同一 chat_id 只保留最新结果（优先选签到成功的，其次选已签过的）
    seen: Dict[str, dict] = {}
    for r in results:
        cid = r["chat_id"]
        if cid not in seen:
            seen[cid] = r
        else:
            # 优先保留更完整的结果：success > already > none
            priority = {"success": 2, "already": 1, "none": 0}
            if priority.get(r["_status"], 0) > priority.get(seen[cid]["_status"], 0):
                seen[cid] = r
    results = list(seen.values())
    return actual, run_at, results


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv
    log_path = args[0] if len(args) > 0 else DEFAULT_LOG
    task_name = args[1] if len(args) > 1 else None

    try:
        text = load_log_text(log_path)
    except FileNotFoundError:
        print(f"⚠️ 日志文件不存在：{log_path}")
        print("今天 tg-signer 还没有执行签到，或日志路径不正确。")
        return

    actual, run_at, results = parse(text, task_name)
    if not results:
        print(f"⚠️ 日志中未找到任务「{task_name or actual or '?'}」的签到记录。")
        print("今天 tg-signer 还没有执行签到，或日志路径不正确。")
        return

    total = len(results)
    ok = sum(1 for r in results if r["_status"] == "success")
    already = sum(1 for r in results if r["_status"] == "already")
    fail = total - ok - already
    pts = sum(r["获得积分"] for r in results if isinstance(r["获得积分"], int))

    if as_json:
        print(
            json.dumps(
                {
                    "task": actual,
                    "run_at": run_at,
                    "total": total,
                    "success": ok,
                    "already": already,
                    "failed": fail,
                    "points_total": pts,
                    "groups": [
                        {k: v for k, v in r.items() if k != "_status"} for r in results
                    ],
                },
                ensure_ascii=False,
            )
        )
        return

    # 日期守卫：runner 若挂掉，日志里最近一轮可能是昨天的，必须显式提示
    today = date.today().isoformat()
    if run_at and not run_at.startswith(today):
        print(f"⚠️ 警告：最近一次签到发生在 {run_at}，并非今天（{today}）。")
        print("   tg-signer 今天可能尚未执行签到，请检查 runner 容器状态。")
        print()

    print(f"📅 TG 每日签到汇报")
    print(f"任务：{actual}    执行时间：{run_at}")
    print("=" * 46)
    print(f"共 {total} 个群 ｜ ✅成功 {ok} ｜ ☑️已签 {already} ｜ ❌无回复 {fail}")
    print(f"本次累计获得积分：{pts}")
    print("-" * 46)
    for i, r in enumerate(results, 1):
        print(f"{i:2d}. {r['群名']}")
        line = f"    {r['是否签到成功']}"
        if r["获得积分"] != "-":
            line += f" ｜ 积分 +{r['获得积分']}"
        if r["连续签到天数"] != "-":
            line += f" ｜ 连续 {r['连续签到天数']} 天"
        print(line)
    print("-" * 46)
    print(f"合计 {total} 个群，成功 {ok} 个，已签 {already} 个，无回复 {fail} 个。")


if __name__ == "__main__":
    main()
