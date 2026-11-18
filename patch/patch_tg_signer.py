#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 tg_signer 打「群级签到幂等」补丁（幂等、可回滚）。

为什么需要
----------
tg_signer/core.py 的 normal_run() -> sign_once() 是这样写的：

    async def sign_once():
        for chat in config.chats:          # 35 个群
            ...
            await self.sign_a_chat(chat)
            await asyncio.sleep(config.sign_interval)
        self.persist_sign_record(...)      # ← 整轮跑完才写记录

签到记录是「任务级」的（每个 task+账号+日期一条），而且**整轮遍历结束后**才落库。
所以 runner 只要在遍历中途断开（网络重连、容器重启、配置热重载、看门狗 restart），
这一轮的进度就全丢，重启后 need_sign() 判定"今天没签过"，于是从第 1 个群重新开始，
已经签过的群被二次、三次发送"签到" —— 这正是要修的问题。

改法
----
不动调度、不动记录库，只在 sign_once 的循环里加一层「群级进度」：

    for chat in config.chats:
        if str(chat.chat_id) in 今日已完成集合:
            continue                        # 跳过已签群
        ...
        _csp_add(...)                       # 每签完一个群立刻落盘

进度文件：<tasks_dir>/<task_name>/progress/<user_id>-<YYYY-MM-DD>.json
强制忽略进度重签：环境变量 CSP_SIGN_FORCE=1

安全性
------
- 补丁前备份 core.py.csp.bak；打完做 py_compile 校验，失败自动回滚。
- 幂等：文件里已有 MARK 就直接退出，重复执行无副作用。
- 只在「成功签完一个群」之后写进度；RPCError 走原有异常分支，不写进度，下次会重试。
- 任何异常都被吞掉，最坏情况是退化成原生行为（重签整轮），不会让签到跑不起来。
"""
import os
import shutil
import subprocess
import sys

MARK = "csp-patch v1"

# 注入到 core.py 末尾的模块级 helper（不含 f-string，避免引号麻烦）
HELPERS = '''

# ==== csp-patch v1 begin ====
# 群级签到幂等：见 /data/_patch/patch_tg_signer.py 的说明。
# 作用：runner 中途重连/重启时，跳过当天已经签到成功的群，不再从头遍历 35 个群。
_CSP_TAG = "csp-patch v1"


def _csp_state_path(tasks_dir, task_name, user_id, day):
    d = os.path.join(str(tasks_dir), str(task_name), "progress")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%s-%s.json" % (user_id, day))


def _csp_read(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return set(str(x) for x in json.load(f).get("done", []))
    except Exception:
        return set()


def _csp_load(tasks_dir, task_name, user_id, day):
    """读当天已签到群集合；CSP_SIGN_FORCE=1 时返回空集（强制重签）。"""
    if os.environ.get("CSP_SIGN_FORCE") == "1":
        return set()
    return _csp_read(_csp_state_path(tasks_dir, task_name, user_id, day))


def _csp_add(tasks_dir, task_name, user_id, day, chat_id):
    """记录一个群已签到成功（原子写）。"""
    p = _csp_state_path(tasks_dir, task_name, user_id, day)
    done = _csp_read(p)
    done.add(str(chat_id))
    payload = {
        "task": str(task_name),
        "user": str(user_id),
        "date": day,
        "done": sorted(done),
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:
        return
    _csp_gc(os.path.dirname(p), 10)


def _csp_gc(folder, keep_days):
    """清理 10 天前的进度文件，避免目录堆积。"""
    try:
        cutoff = (datetime.now() - timedelta(days=int(keep_days))).date()
        for name in os.listdir(folder):
            if not name.endswith(".json"):
                continue
            parts = name[:-5].rsplit("-", 3)
            if len(parts) != 4:
                continue
            try:
                d = datetime.strptime("-".join(parts[1:]), "%Y-%m-%d").date()
            except Exception:
                continue
            if d < cutoff:
                os.remove(os.path.join(folder, name))
    except Exception:
        pass
# ==== csp-patch v1 end ====
'''

# 插入点 1：进入群循环前读进度，循环内跳过已签群
OLD_LOOP = """        async def sign_once():
            for chat in config.chats:
                route_key = None
                try:
"""

NEW_LOOP = """        async def sign_once():
            _csp_day = str(now.date())
            _csp_done = _csp_load(self.tasks_dir, self.task_name, str(self.user.id), _csp_day)
            if _csp_done:
                self.log(f"[{_CSP_TAG}] 今日已签到 {len(_csp_done)} 个群，本轮跳过，只处理剩余的")
            for chat in config.chats:
                if str(chat.chat_id) in _csp_done:
                    self.log(f"[{_CSP_TAG}] 跳过已签到: {getattr(chat, 'name', '')} ({chat.chat_id})")
                    continue
                route_key = None
                try:
"""

# 插入点 2：成功签完一个群后立刻落盘
OLD_MARK = """                if route_key is not None:
                    self.context.chat_messages[route_key].clear()
                await asyncio.sleep(config.sign_interval)
"""

NEW_MARK = """                if route_key is not None:
                    self.context.chat_messages[route_key].clear()
                _csp_add(self.tasks_dir, self.task_name, str(self.user.id), _csp_day, chat.chat_id)
                await asyncio.sleep(config.sign_interval)
"""


def find_core():
    """用 python 自己定位包路径，避免写死 python3.12 目录。"""
    code = "import tg_signer.core as m; print(m.__file__)"
    try:
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=60)
    except Exception as e:
        print("FAIL: 无法定位 tg_signer.core: %r" % (e,))
        return None
    path = (out.stdout or "").strip()
    if out.returncode != 0 or not path or not os.path.isfile(path):
        print("FAIL: 定位 tg_signer.core 失败 rc=%s err=%s"
              % (out.returncode, (out.stderr or "").strip()[:300]))
        return None
    return path


def main():
    # --file <path>：离线演练用，直接指定 core.py 副本，不依赖已安装的包
    if "--file" in sys.argv:
        path = sys.argv[sys.argv.index("--file") + 1]
        if not os.path.isfile(path):
            print("FAIL: 文件不存在 %s" % path)
            return 1
    else:
        path = find_core()
        if not path:
            return 1
    print("target: %s" % path)

    # 以二进制读入后统一成 LF 再匹配；写回时用 newline="" 保证输出恒为 LF。
    # 这样在 Windows 上演练也不会把文件变成 CRLF，容器内(Linux)行为完全一致。
    with open(path, "rb") as f:
        raw = f.read()
    src = raw.decode("utf-8").replace("\r\n", "\n")

    if MARK in src:
        print("SKIP: 已打过 %s，无需重复打补丁" % MARK)
        return 0

    # 校验插入点，任何一个不匹配就不打（免得改坏别人的代码）
    for label, old in (("sign_once 循环头", OLD_LOOP), ("成功落点", OLD_MARK)):
        n = src.count(old)
        if n != 1:
            print("FAIL: 插入点「%s」匹配 %d 次（应为 1），源码结构已变，放弃打补丁" % (label, n))
            return 2
    if src.count("self.persist_sign_record(sign_record") != 1:
        print("WARN: persist_sign_record 调用点数量异常，继续但请留意")

    bak = path + ".csp.bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
        print("backup: %s" % bak)
    else:
        print("backup 已存在，保留首次的原始版本: %s" % bak)

    new = src.replace(OLD_LOOP, NEW_LOOP).replace(OLD_MARK, NEW_MARK)
    new = new.rstrip("\n") + "\n" + HELPERS

    # 先写临时文件，编译通过再替换
    tmp = path + ".csp.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    chk = subprocess.run([sys.executable, "-m", "py_compile", tmp],
                         capture_output=True, text=True)
    if chk.returncode != 0:
        print("FAIL: 补丁后语法校验失败，已放弃（原文件未改动）")
        print((chk.stderr or "").strip()[:800])
        os.remove(tmp)
        return 3

    os.replace(tmp, path)
    # 清掉旧字节码，避免加载到缓存
    pyc = os.path.join(os.path.dirname(path), "__pycache__")
    if os.path.isdir(pyc):
        for f in os.listdir(pyc):
            if f.startswith("core.") and f.endswith(".pyc"):
                try:
                    os.remove(os.path.join(pyc, f))
                except Exception:
                    pass
    print("OK: %s 已打补丁（%s）" % (path, MARK))
    return 0


if __name__ == "__main__":
    sys.exit(main())
