# -*- coding: utf-8 -*-
"""声纹识别 + 语音转写（自建 3D 语音通道的主机侧处理）。

- 转写(ASR)：faster-whisper（本进程直接跑，CPU int8）
- 声纹：speechbrain ECAPA-TDNN——项目 venv 是 Python 3.14（无 torch 轮子），
  改为子进程方案：用带 torch 的环境（默认 D:\\sovits_env\\python.exe，
  即 .env 的 SOVITS_PYTHON）执行 src/speaker_worker.py。
  首次使用会自动从 hf-mirror 下载声纹模型（~25MB）。
- 任何失败都优雅降级：转写返回空串、识别返回未知名，不影响主流程。

子进程环境安装：D:\\sovits_env\\python.exe -m pip install speechbrain
"""
import os
import base64
import subprocess
import time

from config import CONFIG

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKER = os.path.join(_HERE, "speaker_worker.py")
_SPK_PY = CONFIG.get("sovits_python") or r"D:\sovits_env\python.exe"
MATCH_THRESHOLD = 0.60

_WHISPER = None


def save_b64_wav(b64):
    """把 base64 wav 落盘到 data/voice_in/，返回路径（供转写/声纹使用）。

    防御：若上游给的是纯 PCM 裸数据（无 RIFF 头），自动补 16kHz 单声道 16bit 头。
    """
    import struct
    raw = base64.b64decode(b64)
    if raw[:4] != b"RIFF":
        header = (b"RIFF" + struct.pack("<I", 36 + len(raw)) + b"WAVEfmt "
                  + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
                  + b"data" + struct.pack("<I", len(raw)))
        raw = header + raw
    d = os.path.join(CONFIG.get("data_dir", "."), "voice_in")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"v_{int(time.time() * 1000)}.wav")
    with open(path, "wb") as f:
        f.write(raw)
    return path


# ---------------- 转写（ASR，本进程） ----------------
def _load_whisper():
    global _WHISPER
    if _WHISPER is not None:
        return _WHISPER
    from faster_whisper import WhisperModel
    model_name = CONFIG.get("asr_model", "small")
    local_dir = os.path.join(os.path.dirname(_HERE), "models", f"faster-whisper-{model_name}")
    if os.path.isdir(local_dir) and os.path.exists(os.path.join(local_dir, "model.bin")):
        model_path = local_dir
    else:
        if not os.environ.get("HF_ENDPOINT"):
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        model_path = model_name
    _WHISPER = WhisperModel(model_path, device="cpu", compute_type="int8")
    return _WHISPER


def transcribe_wav(path):
    """wav 文件 → 中文文本；失败返回空串（降级，不抛异常）。"""
    try:
        model = _load_whisper()
        segments, _ = model.transcribe(
            path, language="zh", beam_size=5,
            condition_on_previous_text=False, vad_filter=True,
        )
        return "".join(s.text for s in segments).strip()
    except Exception as e:
        print(f"[声纹] 转写失败: {e}")
        return ""


# ---------------- 声纹（子进程：带 torch 的环境） ----------------
def _run_spk(*args):
    """在带 torch 的环境里跑 speaker_worker.py，返回 (stdout, exit_code)。"""
    try:
        p = subprocess.run(
            [_SPK_PY, _WORKER] + list(args),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=600,   # 首次要下载模型，留足时间
        )
        return (p.stdout or "").strip(), p.returncode
    except subprocess.TimeoutExpired:
        return "ERR 声纹子进程超时（首次下载模型可能较慢）", -1
    except Exception as e:
        return f"ERR {e}", -1


def enroll(name, wav_path):
    """注册声纹：名字 + 一段语音 → 嵌入存档。"""
    out, rc = _run_spk("enroll", name, wav_path)
    if rc != 0 or out.startswith("ERR"):
        raise RuntimeError(out)
    return out


def identify(wav_path):
    """识别说话人：返回 (名字, 分数) 或 (None, 0.0)。"""
    out, rc = _run_spk("identify", wav_path)
    if rc != 0 or out.startswith("ERR"):
        print(f"[声纹] 识别失败（子进程）: {out}")
        return None, 0.0
    parts = out.split("\t")
    if len(parts) < 2 or parts[0] == "none":
        return None, 0.0
    try:
        return parts[0], round(float(parts[1]), 3)
    except Exception:
        return None, 0.0


def list_speakers():
    """已注册声纹名单。"""
    out, rc = _run_spk("list")
    if rc != 0 or out.startswith("ERR"):
        print(f"[声纹] 列表失败（子进程）: {out}")
        return []
    return [n for n in out.split(",") if n]
