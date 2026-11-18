#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在容器内验证 csp-patch 的「群级跳过」逻辑（不发任何 Telegram 消息）。

做法：构造一个临时 workdir，复制真实的 task config，跑真正的
UserSigner.normal_run(only_once=True) —— 也就是被补丁改过的 sign_once 循环本身。
只把 sign_a_chat（真正发签到消息那步）和 persist_sign_record（写记录库）换成桩，
其余代码（读进度、跳过、落盘）全部是线上那份 core.py 在跑。

场景：
  阶段 1：预置 30/35 已签到 -> 期望只签剩余 5 个
  阶段 2：再跑一次（模拟重启）-> 期望 0 个，全部跳过
"""
import asyncio
import json
import logging
import os
import shutil
import tempfile
from types import SimpleNamespace

import tg_signer.core as C

logging.basicConfig(level=logging.INFO, format="%(message)s")

TASK = os.environ.get("TG_SIGNER_TASK", "my_sign")
REAL_CFG = os.environ.get("TG_SIGNER_CFG", "/data/.signer/signs/%s/config.json" % TASK)
USER_ID = os.environ.get("TG_SIGNER_USER_ID", "123456789")

tmp = tempfile.mkdtemp(prefix="csp_selftest_")
os.makedirs(os.path.join(tmp, "signs", TASK), exist_ok=True)
shutil.copy(REAL_CFG, os.path.join(tmp, "signs", TASK, "config.json"))

cfg = json.load(open(REAL_CFG, encoding="utf-8"))
chat_ids = [int(c["chat_id"]) for c in cfg["chats"]]
print("真实配置群数: %d" % len(chat_ids))
print("补丁版本标记: %s" % C._CSP_TAG)


class FakeApp:
    """替代 pyrogram Client：只提供 normal_run 需要的接口，不连网。"""

    def __init__(self):
        self.loop = None

    def add_handler(self, *a, **k):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


# 避免构造真 Client（它会去读 api_id/api_hash / 建 session 目录）
C.get_client = lambda *a, **k: FakeApp()


def make_signer(signed):
    s = C.UserSigner(
        task_name=TASK,
        session_dir=os.path.join(tmp, "sessions"),
        account="my_account",
        workdir=tmp,
    )
    s.user = SimpleNamespace(id=int(USER_ID))
    s.app = FakeApp()

    async def fake_sign(chat):
        signed.append(int(chat.chat_id))
        await asyncio.sleep(0)  # 不发消息，也不等 action_interval

    s.sign_a_chat = fake_sign
    s.persist_sign_record = lambda *a, **k: print("  >> persist_sign_record 已调用")
    return s


def reset_progress():
    p = os.path.join(tmp, "signs", TASK, "progress")
    shutil.rmtree(p, ignore_errors=True)


def progress_file():
    return os.path.join(tmp, "signs", TASK, "progress", "%s-%s.json" % (
        USER_ID, str(C.get_now().date())))


# ---------- 阶段 1：预置 30 个已签到 ----------
print("\n=== 阶段 1：预置 %d/%d 已签到，期望只签剩余 %d 个 ===" % (
    len(chat_ids) - 5, len(chat_ids), 5))
reset_progress()
pre = chat_ids[:-5]
for cid in pre:
    C._csp_add(os.path.join(tmp, "signs"), TASK, USER_ID, str(C.get_now().date()), cid)
print("预置完成，进度文件含 %d 个群" % len(C._csp_load(
    os.path.join(tmp, "signs"), TASK, USER_ID, str(C.get_now().date()))))

signed1 = []
asyncio.run(make_signer(signed1).normal_run(only_once=True))
print(">> 阶段1 实际发送签到: %d 个 %s" % (len(signed1), signed1))
print(">> 期望剩余 5 个: %s" % chat_ids[-5:])
ok1 = sorted(signed1) == sorted(chat_ids[-5:])

# ---------- 阶段 2：模拟重启后再跑一轮 ----------
print("\n=== 阶段 2：模拟 runner 重启，进度已是 35/35，期望 0 个 ===")
print("进度文件内容:", json.load(open(progress_file(), encoding="utf-8"))["done"].__len__(), "个群")
signed2 = []
asyncio.run(make_signer(signed2).normal_run(only_once=True))
print(">> 阶段2 实际发送签到: %d 个 %s" % (len(signed2), signed2))
ok2 = len(signed2) == 0

# ---------- 阶段 3：对比原生行为 ----------
print("\n=== 阶段 3：清空进度后再跑（等价于未打补丁时重启），期望 35 个 ===")
reset_progress()
signed3 = []
asyncio.run(make_signer(signed3).normal_run(only_once=True))
print(">> 阶段3 实际发送签到: %d 个" % len(signed3))
ok3 = len(signed3) == len(chat_ids)

print("\n===== 结果 =====")
print("阶段1 只签剩余 %d 个 : %s" % (5, "PASS" if ok1 else "FAIL"))
print("阶段2 重启后跳过全部 : %s" % ("PASS" if ok2 else "FAIL"))
print("阶段3 无进度=全签   : %s" % ("PASS" if ok3 else "FAIL"))
shutil.rmtree(tmp, ignore_errors=True)
print("ALL: %s" % ("PASS" if (ok1 and ok2 and ok3) else "FAIL"))
