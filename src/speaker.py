# -*- coding: utf-8 -*-
"""声纹识别 + 语音转写（自建 3D 语音通道的主机侧处理）。

- 转写(ASR)：faster-whisper，复用 voice.py 的本地模型目录策略（CPU int8）
- 声纹：wespeaker（wenet 开源、中文预训练、CPU 可跑；首次使用自动从 modelscope 下载）
  - 注册：enroll(名字, wav) → data/voiceprints/<名字>.npz
  - 识别：identify(wav) → (名字, 余弦分数)；低于阈值返回 (None, 0.0)
- 依赖缺失/下载失败时优雅降级：转写返回空串、识别返回未知名，不影响主流程

安装：venv\\Scripts\\python.exe -m pip install wespeaker modelscope
"""
import os
import base64
import time

from config import CONFIG

VP_DIR = os.path.join(CONFIG.get("data_dir", "."), "voiceprints")
MATCH_THRESHOLD = 0.60   # 余弦相似度阈值：低于它视为未注册说话人

_WHISPER = None
_SPK = None


def save_b64_wav(b64):
    """把 base64 wav 落盘到 data/voice_in/，返回路径（供转写/声纹使用）。"""
    d = os.path.join(CONFIG.get("data_dir", "."), "voice_in")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"v_{int(time.time() * 1000)}.wav")
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))
    return path


# ---------------- 转写（ASR） ----------------
def _load_whisper():
    global _WHISPER
    if _WHISPER is not None:
        return _WHISPER
    from faster_whisper import WhisperModel
    here = os.path.dirname(os.path.abspath(__file__))
    model_name = CONFIG.get("asr_model", "small")
    local_dir = os.path.join(os.path.dirname(here), "models", f"faster-whisper-{model_name}")
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


# ---------------- 声纹 ----------------
def _load_spk():
    global _SPK
    if _SPK is not None:
        return _SPK
    import wespeaker
    _SPK = wespeaker.load_model("chinese")   # 首次自动下载（modelscope 源）
    return _SPK


def _embedding(path):
    import numpy as np
    emb = _load_spk().extract_embedding(path)
    if hasattr(emb, "cpu"):
        emb = emb.cpu().numpy()
    emb = np.asarray(emb, dtype=np.float32).reshape(-1)
    return emb / (np.linalg.norm(emb) + 1e-9)


def enroll(name, wav_path):
    """注册声纹：名字 + 一段语音 → 嵌入存档。返回存档路径。"""
    import numpy as np
    emb = _embedding(wav_path)
    os.makedirs(VP_DIR, exist_ok=True)
    safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|') or "unnamed"
    path = os.path.join(VP_DIR, f"{safe}.npz")
    np.savez(path, emb=emb, name=name, ts=time.time())
    return path


def identify(wav_path):
    """识别说话人：返回 (名字, 分数) 或 (None, 0.0)。"""
    import numpy as np
    if not os.path.isdir(VP_DIR):
        return None, 0.0
    emb = _embedding(wav_path)
    best, best_score = None, 0.0
    for f in os.listdir(VP_DIR):
        if not f.endswith(".npz"):
            continue
        try:
            d = np.load(os.path.join(VP_DIR, f))
            ref = np.asarray(d["emb"], dtype=np.float32).reshape(-1)
            score = float(np.dot(emb, ref))
            if score > best_score:
                best_score, best = score, str(d["name"])
        except Exception:
            continue
    if best is not None and best_score >= MATCH_THRESHOLD:
        return best, round(best_score, 3)
    return None, 0.0


def list_speakers():
    """已注册声纹名单。"""
    out = []
    if not os.path.isdir(VP_DIR):
        return out
    for f in os.listdir(VP_DIR):
        if f.endswith(".npz"):
            try:
                import numpy as np
                out.append(str(np.load(os.path.join(VP_DIR, f))["name"]))
            except Exception:
                pass
    return out
