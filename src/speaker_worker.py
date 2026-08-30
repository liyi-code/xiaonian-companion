# -*- coding: utf-8 -*-
"""声纹识别子进程（在带 torch 的环境里跑，如 D:\\sovits_env）。

项目 venv 是 Python 3.14（无 torch 轮子），声纹引擎装不进去；
本脚本由 src/speaker.py 用 sovits_env 的 python 以子进程方式调用。

用法：
    python speaker_worker.py enroll <名字> <wav路径>
    python speaker_worker.py identify <wav路径>      # 输出 "名字\t分数" 或 "none\t0.0"
    python speaker_worker.py list                     # 输出已注册名单（逗号分隔）

声纹库存放：<项目>/data/voiceprints/（与主进程共享）。
依赖：pip install speechbrain（首次会从 hf-mirror 下载 ECAPA-TDNN 模型 ~25MB）。
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 国内镜像：模型下载走 hf-mirror（与 whisper 同一策略）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from config import CONFIG

VP_DIR = os.path.join(CONFIG.get("data_dir", "."), "voiceprints")
MATCH_THRESHOLD = 0.60

_SPK = None
_MODEL_DIR = os.path.normpath(os.path.join(_HERE, "..", "models", "speechbrain_ecapa"))


def _load_spk():
    global _SPK
    if _SPK is not None:
        return _SPK
    from speechbrain.inference.speaker import EncoderClassifier
    _SPK = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=_MODEL_DIR,
        run_opts={"device": "cpu"},
    )
    return _SPK


def _embedding(path):
    import numpy as np
    emb = _load_spk().encode_file(path)   # torch tensor (1, 192)
    if hasattr(emb, "cpu"):
        emb = emb.cpu().numpy()
    emb = np.asarray(emb, dtype=np.float32).reshape(-1)
    return emb / (np.linalg.norm(emb) + 1e-9)


def cmd_enroll(name, wav_path):
    import numpy as np
    emb = _embedding(wav_path)
    os.makedirs(VP_DIR, exist_ok=True)
    safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|') or "unnamed"
    np.savez(os.path.join(VP_DIR, f"{safe}.npz"), emb=emb, name=name, ts=time.time())
    print(f"OK {safe}")


def cmd_identify(wav_path):
    import numpy as np
    if not os.path.isdir(VP_DIR):
        print("none\t0.0")
        return
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
        print(f"{best}\t{best_score:.3f}")
    else:
        print("none\t0.0")


def cmd_list():
    import numpy as np
    names = []
    if os.path.isdir(VP_DIR):
        for f in os.listdir(VP_DIR):
            if f.endswith(".npz"):
                try:
                    names.append(str(np.load(os.path.join(VP_DIR, f))["name"]))
                except Exception:
                    pass
    print(",".join(names))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd == "enroll" and len(sys.argv) >= 4:
            cmd_enroll(sys.argv[2], sys.argv[3])
        elif cmd == "identify" and len(sys.argv) >= 3:
            cmd_identify(sys.argv[2])
        elif cmd == "list":
            cmd_list()
        else:
            print("ERR 用法: enroll <名字> <wav> | identify <wav> | list")
    except Exception as e:
        print(f"ERR {e}")


if __name__ == "__main__":
    main()
