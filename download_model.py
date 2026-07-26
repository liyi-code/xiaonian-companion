"""下载 faster-whisper 模型到本地 models/ 目录（走国内镜像，绕过 HuggingFace 缓存机制）。

用法：
  cd d:\AI训练\ai-girlfriend
  .\venv\Scripts\python.exe download_model.py
下载完成后，voice.py 会自动优先使用本地 models/faster-whisper-small/，
不再依赖运行时联网下载（避免被墙导致的 ConnectTimeout / 模型不全）。
"""
import os
import sys
import time

import requests

# 读取 .env 里的 ASR_MODEL（若存在），让手动运行也能下对模型尺寸
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

MIRROR = "https://hf-mirror.com"
REPO = "Systran/faster-whisper-small"
# 模型尺寸，可改 base / small / medium 等；默认与 .env 的 ASR_MODEL 一致用 small
MODEL = os.environ.get("ASR_MODEL", "small")

base = os.path.dirname(os.path.abspath(__file__))
local = os.path.join(base, "models", f"faster-whisper-{MODEL}")
os.makedirs(local, exist_ok=True)

# faster-whisper 的 CTranslate2 模型目录：必需文件（缺一不可）
files = [
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.txt",
]
# 可选文件：部分仓库没有（404），缺了不影响 faster-whisper 识别，下不到就跳过
optional_files = [
    "preprocessor_config.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
]


def _remote_size(url):
    """取服务器文件大小（Content-Length）；取不到返回 0。"""
    try:
        h = requests.head(url, timeout=25, allow_redirects=True)
        if h.status_code == 404:
            return -1   # 文件不存在
        return int(h.headers.get("Content-Length", 0))
    except Exception:
        return 0


def download_one(fname, optional=False):
    """支持断点续传的下载：大文件中途断线时从断点接着下，
    直到本地大小与服务器一致才算完成。"""
    url = f"{MIRROR}/{REPO}/resolve/main/{fname}"
    dest = os.path.join(local, fname)

    total = _remote_size(url)
    if total == -1:
        if optional:
            print(f"    · 仓库无此可选文件（404），跳过 {fname}")
            return True
        print(f"    ! 服务器上不存在必需文件 {fname}")
        return False

    # 已完整下载则跳过
    if total > 0 and os.path.exists(dest) and os.path.getsize(dest) == total:
        print(f"  · 已完整，跳过 {fname} ({total} bytes)")
        return True

    tries = 1 if optional else 30   # 大文件断点续传，允许多次续传
    for attempt in range(tries):
        have = os.path.getsize(dest) if os.path.exists(dest) else 0
        if total > 0 and have >= total:
            print(f"    ✓ 完成 {fname} ({have} bytes)")
            return True
        headers = {}
        mode = "wb"
        if have > 0 and total > 0:
            headers["Range"] = f"bytes={have}-"    # 断点续传
            mode = "ab"
            print(f"  ↺ 续传 {fname} 从 {have}/{total} ({have/total*100:.1f}%) ...")
        else:
            print(f"  ↓ {fname} (第{attempt+1}次) ...")
        try:
            with requests.get(url, stream=True, timeout=(20, 900), headers=headers) as r:
                if optional and r.status_code == 404:
                    print(f"    · 仓库无此可选文件（404），跳过 {fname}")
                    return True
                # 服务器不支持续传会返回 200（全量），此时从头写
                if headers.get("Range") and r.status_code == 200:
                    mode = "wb"
                    have = 0
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "")
                if "text/html" in ctype:
                    raise RuntimeError("返回的是网页而非文件（可能 404/鉴权）")
                done = have
                with open(dest, mode) as fp:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        fp.write(chunk)
                        done += len(chunk)
                        if total:
                            print(f"    {done/total*100:5.1f}%  ({done}/{total})",
                                  end="\r", flush=True)
            if total <= 0 or os.path.getsize(dest) >= total:
                print(f"\n    ✓ 完成 {fname} ({os.path.getsize(dest)} bytes)")
                return True
            print(f"\n    · 未下完({os.path.getsize(dest)}/{total})，将续传")
        except Exception as e:
            if optional:
                # 可选文件（部分仓库没有/镜像拉不到）：不影响识别，安静跳过即可，
                # 不再打印吓人的超时堆栈，也不用重试。
                print(f"\n    · 可选文件 {fname} 获取失败，已跳过（不影响离线识别）")
                return True
            print(f"\n    ! 中断：{e}（{attempt+1}/{tries}）稍后续传")
            time.sleep(3)
    return total > 0 and os.path.exists(dest) and os.path.getsize(dest) == total


if __name__ == "__main__":
    print(f">> 目标模型：{REPO}（{MODEL}）")
    print(f">> 本地目录：{local}")
    print(f">> 镜像源：{MIRROR}\n")
    all_ok = True
    for f in files:
        if not download_one(f):
            all_ok = False
            print(f"!! 必需文件 {f} 下载失败，请检查网络/镜像后重试本脚本")
    # 可选文件：下不到不影响识别
    for f in optional_files:
        download_one(f, optional=True)
    if all_ok:
        print("\n✅ 必需模型文件已就绪。现在重启小念，语音输入即可离线识别。")
    else:
        print("\n⚠ 部分必需文件未下载成功，可再次运行本脚本（已下的会跳过）。")
        sys.exit(1)
