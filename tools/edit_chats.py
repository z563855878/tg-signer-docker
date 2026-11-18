"""从 tg-signer 签到任务配置里移除指定群（带备份）。

用法:
    python3 edit_chats.py <群名>            # 精确匹配并移除
    python3 edit_chats.py <群名> --contains # 名称包含即匹配
    python3 edit_chats.py <群名> --dry      # 只预览，不写入
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sys

TASK = os.environ.get("TG_SIGNER_TASK", "my_sign")
ROOT = os.environ.get("TG_SIGNER_ROOT", "/opt/tg-signer")
CFG = f"{ROOT}/data/.signer/signs/{TASK}/config.json"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: edit_chats.py <群名> [--contains] [--dry]")
        return 1
    target = args[0]
    contains = "--contains" in sys.argv
    dry = "--dry" in sys.argv

    if not os.path.exists(CFG):
        print(f"[edit-chats] config not found: {CFG}")
        return 1

    raw = open(CFG, encoding="utf-8").read()
    doc = json.loads(raw)
    chats = doc.get("chats")
    if not isinstance(chats, list):
        print("[edit-chats] config has no chats list")
        return 1

    def hit(c):
        name = str(c.get("name", ""))
        return target in name if contains else name == target

    matched = [c for c in chats if hit(c)]
    if not matched:
        print(f"[edit-chats] 未找到匹配的群: {target}（当前 {len(chats)} 个群）")
        return 1

    for c in matched:
        print(f"[edit-chats] 将移除: {c.get('name')}  chat_id={c.get('chat_id')}")

    if dry:
        print("[edit-chats] --dry，未写入")
        return 0

    backup = CFG + ".bak." + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CFG, backup)
    print(f"[edit-chats] 已备份 -> {backup}")

    doc["chats"] = [c for c in chats if not hit(c)]
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")

    verify = json.loads(open(CFG, encoding="utf-8").read())
    print(f"[edit-chats] 完成：{len(chats)} -> {len(verify['chats'])} 个群")
    print(f"[edit-chats] 剩余含「{target}」的群: "
          f"{sum(1 for c in verify['chats'] if target in str(c.get('name', '')))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
