"""麦克风调试脚本：录一段音并保存为 debug_rec.wav，同时打印音量、跑一次识别。
用法（在 ai-girlfriend 目录下）：
  venv\Scripts\python.exe debug_mic.py            # 自动选「花再」设备，录 6 秒
  venv\Scripts\python.exe debug_mic.py --idx 8    # 指定设备索引
  venv\Scripts\python.exe debug_mic.py --sec 8    # 录 8 秒
运行后请对着麦克风说几句话，结束后会生成 debug_rec.wav，用播放器听听是不是你的声音。
"""
import sys
import time
import wave
import argparse
import numpy as np

try:
    import sounddevice as sd
except Exception as e:
    print("缺少 sounddevice：", e); sys.exit(1)


def list_devices():
    print("=== 输入设备（带 [IN] 的才能录音）===")
    picks = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            mark = "  <-- 含「花再」" if "花再" in d["name"] else ""
            print(f"  [{i}] {d['name']}{mark}")
            if "花再" in d["name"]:
                picks.append(i)
    return picks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idx", type=int, default=None, help="设备索引，不填则自动选花再")
    ap.add_argument("--sec", type=float, default=6.0, help="录音秒数")
    args = ap.parse_args()

    picks = list_devices()
    idx = args.idx if args.idx is not None else (picks[0] if picks else None)
    if idx is None:
        print("没找到「花再」设备，请用 --idx 指定上面列表里的某个 [IN] 索引")
        sys.exit(1)
    print(f"\n使用设备索引 {idx}：{sd.query_devices(idx)['name']}")

    sr = 16000
    print(f"准备录音 {args.sec} 秒，请现在开始说话…")
    frames = []
    t0 = time.time()

    def cb(indata, n, tinfo, status):
        if status:
            print("  [stream status]", status)
        frames.append(indata.copy())

    stream = sd.InputStream(device=idx, callback=cb, channels=1,
                            samplerate=sr, dtype="int16")
    with stream:
        while time.time() - t0 < args.sec:
            time.sleep(0.1)
    print("录音结束，保存中…")

    audio_i16 = np.concatenate(frames, axis=0)
    audio_f32 = audio_i16.astype(np.float32) / 32768.0
    # 存 wav
    with wave.open("debug_rec.wav", "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
        wf.writeframes(audio_i16.tobytes())

    rms = float(np.sqrt(np.mean(audio_f32 ** 2)))
    peak = float(np.max(np.abs(audio_f32)))
    print(f"  时长={len(audio_f32)/sr:.1f}s  音量RMS={rms:.4f}  峰值={peak:.4f}")
    if rms < 0.005:
        print("  [!] 音量极低，说明基本没录到你的声音（设备不对/麦没开/离太远）")
    else:
        print("  [OK] 有音量。请用播放器打开 debug_rec.wav 确认里面是不是你的声音。")

    # 试识别
    try:
        from faster_whisper import WhisperModel
        print("加载识别模型…")
        m = WhisperModel("base", device="cpu", compute_type="int8")
        segs, _ = m.transcribe(audio_f32, language="zh", beam_size=5)
        txt = "".join(s.text for s in segs).strip()
        print("识别结果：", repr(txt) if txt else "（空）")
    except Exception as e:
        print("识别失败：", e)


if __name__ == "__main__":
    main()
