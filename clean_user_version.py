#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理为「用户版」：删除/备份当前项目里的个人敏感信息，便于分发给他人。

会做两件事（都先备份，可还原）：
  1. 备份 .env -> .env.personal_bak_<时间戳>，并把里面的个人密钥/账号清空：
     OPENAI_API_KEY, VISION_API_KEY, QQ_TOKEN, WECHAT_TOKEN, QQ_OWNER, WECHAT_OWNER
  2. 把整个 data/ 目录移动到 personal_backup_<时间戳>/（聊天记录、屏幕截图、
     自主权限改动、音色/模型偏好等全部个人数据都在这里），让新版本不含你的隐私。

用法：
  python clean_user_version.py            # 真正清理（先自动备份）
  python clean_user_version.py --dry-run  # 只显示将要做什么，不改动任何文件
  python clean_user_version.py --remove-ref  # 连角色参考音频 ref_yae_clean.wav 一并移走

注意：参考音频 ref_yae_clean.wav 默认保留（它是角色音色、不是你的隐私）；
如需一并移除，加 --remove-ref。所有被移走的东西都在 personal_backup_<时间戳>/ 里可还原。
"""
import os
import re
import shutil
import sys
import time

# 部分 Windows 控制台默认 GBK，打印 emoji 会报 UnicodeEncodeError；统一用 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))

# 需要清空的敏感键（你的账号/密钥）
BLANK_KEYS = [
    "OPENAI_API_KEY", "VISION_API_KEY",
    "QQ_TOKEN", "WECHAT_TOKEN", "QQ_OWNER", "WECHAT_OWNER",
]

KEEP_COMMENT = {
    "QQ_OWNER": "  # 主人的 QQ 号（留空）",
    "WECHAT_OWNER": "  # 你的微信号/备注（留空）",
}


def _ts():
    return time.strftime("%Y%m%d_%H%M%S")


def blank_env_keys(dry):
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        print("[跳过] 找不到 .env，无需清理")
        return
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    if not dry:
        shutil.copy2(path, os.path.join(ROOT, f".env.personal_bak_{_ts()}"))
        print(f"[备份] .env -> .env.personal_bak_{_ts()}")
    out = []
    for ln in lines:
        m = re.match(r"^(\s*#?\s*)([A-Z_]+)(\s*=).*$", ln)
        if m and m.group(2) in BLANK_KEYS:
            prefix = "# " if ln.lstrip().startswith("#") else ""
            tail = KEEP_COMMENT.get(m.group(2), "")
            out.append(f"{prefix}{m.group(2)}{m.group(3)}{tail}\n")
        else:
            out.append(ln)
    if not dry:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(out)
    cleared = [k for k in BLANK_KEYS if any(re.match(rf"^{k}=", l) for l in out)]
    print(f"[{'预览' if dry else '完成'}] 已清空个人密钥：{', '.join(cleared) or '（无）'}")


def move_data(dry):
    data = os.path.join(ROOT, "data")
    if not os.path.isdir(data):
        print("[跳过] 没有 data/ 目录，无需清理")
        return
    dest = os.path.join(ROOT, f"personal_backup_{_ts()}", "data")
    if not dry:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(data, dest)
    print(f"[{'预览' if dry else '完成'}] data/ -> {os.path.relpath(dest, ROOT)}（聊天记录/截图等个人数据已移走备份）")


def remove_ref(dry):
    ref = os.path.join(ROOT, "ref_yae_clean.wav")
    if not os.path.exists(ref):
        return
    dest = os.path.join(ROOT, f"personal_backup_{_ts()}", "ref_yae_clean.wav")
    if not dry:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(ref, dest)
    print(f"[{'预览' if dry else '完成'}] 参考音频 -> {os.path.relpath(dest, ROOT)}")


def main():
    dry = "--dry-run" in sys.argv
    remove = "--remove-ref" in sys.argv
    print("=" * 60)
    print(" 小念 · 清理为「用户版」" + ("（预览模式，不改动文件）" if dry else ""))
    print("=" * 60)
    blank_env_keys(dry)
    move_data(dry)
    if remove:
        remove_ref(dry)
    if not dry:
        print("\n✅ 已完成。你的个人数据都已备份到 personal_backup_<时间戳>/，可随时还原。")
        print("   现在这个目录可以打包发给别人了（对方填入自己的 Key 即可使用）。")
    else:
        print("\n这是预览，未做任何改动。去掉 --dry-run 才会真正执行。")


if __name__ == "__main__":
    main()
