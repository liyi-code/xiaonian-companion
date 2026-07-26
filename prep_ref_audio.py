#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
参考音频预处理工具（为 GPT-SoVITS 准备干净的参考音）

用法：
  1. 把你的音源（八重神子某句干净中文台词）放到本目录，例如 ref_yae.mp3
  2. 确认本机已安装 ffmpeg 且在 PATH 中
  3. 运行：python prep_ref_audio.py --src ref_yae.mp3 --text "你要对应的那句台词文字" --start 12 --dur 8

参数说明：
  --src    输入音频路径（mp3/wav 均可）
  --text   这句音频对应的中文文字（会写进 .env 的 SOVITS_REF_TEXT）
  --start  截取起点（秒），跳过开头可能的 BGM/淡入
  --dur    截取时长（秒），建议 3~10 秒，越长克隆越稳但越慢
  --out    输出 wav 路径（默认 ref_yae_clean.wav）

脚本会：
  - 裁出 [start, start+dur] 片段
  - 用 afftdn 做轻度降噪（去掉底噪，不动人声）
  - 转成 16k 单声道 pcm_s16le wav（GPT-SoVITS 最稳格式）
  - 把 .env 的 SOVITS_REF_AUDIO / SOVITS_REF_TEXT 改好（自动备份 .env.bak）
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")


def find_ffmpeg():
    # 1) 优先用 imageio-ffmpeg 自带的官方编译版（无需 PATH）
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    # 2) 退而求其次：系统 PATH 里的 ffmpeg
    sys_path = shutil.which("ffmpeg")
    if sys_path:
        return sys_path
    print("[错误] 找不到 ffmpeg：请 `pip install imageio-ffmpeg` 或把 ffmpeg 加入 PATH",
          file=sys.stderr)
    sys.exit(1)


FFMPEG = find_ffmpeg()


def run_ffmpeg(cmd):
    print(">>>", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("[ffmpeg 错误]\n", r.stderr, file=sys.stderr)
        sys.exit(1)


def prepare(src, text, start, dur, out):
    if not os.path.exists(src):
        print(f"[错误] 找不到音源：{src}", file=sys.stderr)
        sys.exit(1)
    # 1) 裁剪 + 降噪 + 重采样为 16k 单声道 wav
    filt = (
        f"atrim=start={start}:duration={dur},"
        f"asetpts=PTS-STARTPTS,"
        f"afftdn=nr=12:nf=-30,"      # 轻度降噪，保留人声
        f"highpass=f=80,lowpass=f=8000,"  # 去掉极低/极高无用频段
        f"loudnorm=I=-16:TP=-1.5"    # 统一响度
    )
    run_ffmpeg([
        FFMPEG, "-y", "-i", src,
        "-af", filt,
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        out,
    ])
    size = os.path.getsize(out)
    print(f"[完成] 已生成干净参考音：{out}  ({size/1024:.1f} KB)")
    return out


def update_env(out_abs, text):
    if not os.path.exists(ENV_PATH):
        print("[跳过] 未找到 .env，请手动改 SOVITS_REF_AUDIO / SOVITS_REF_TEXT")
        return
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    # 备份
    with open(ENV_PATH + ".bak", "w", encoding="utf-8") as f:
        f.write(content)

    def repl(key, val):
        nonlocal content
        pat = re.compile(rf'^{re.escape(key)}=.*$', re.M)
        line = f'{key}={val}'
        if pat.search(content):
            content = pat.sub(lambda m: line, content)
        else:
            content += "\n" + line + "\n"

    repl("SOVITS_REF_AUDIO", out_abs)
    repl("SOVITS_REF_TEXT", text)
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[完成] 已更新 .env：SOVITS_REF_AUDIO -> {out_abs}")
    print(f"[完成] 已更新 .env：SOVITS_REF_TEXT  -> {text}")
    print("[提示] 原 .env 已备份为 .env.bak，重启小念即可生效")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="输入音源路径")
    ap.add_argument("--text", required=True, help="该句对应中文文字")
    ap.add_argument("--start", type=float, default=0.0, help="截取起点(秒)")
    ap.add_argument("--dur", type=float, default=8.0, help="截取时长(秒)")
    ap.add_argument("--out", default=None, help="输出 wav 路径")
    args = ap.parse_args()

    out = args.out or os.path.join(HERE, "ref_yae_clean.wav")
    out_abs = os.path.abspath(out)
    prepare(args.src, args.text, args.start, args.dur, out_abs)
    update_env(out_abs, args.text)


if __name__ == "__main__":
    main()
