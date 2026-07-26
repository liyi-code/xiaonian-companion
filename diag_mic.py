"""麦克风/ASR 命令行诊断：绕开 GUI，直接看录音电平与识别结果。

用法：
  cd d:\AI训练\ai-girlfriend
  .\venv\Scripts\python.exe diag_mic.py
运行后会录音 4 秒，请对着麦克风说话。脚本会打印：
  - 可用输入设备列表
  - 实际选用的设备
  - 录音期间的实时峰值（确认麦克风是否在收音）
  - 整段平均电平 level
  - faster-whisper 的识别文字
据此即可判断是“没录到声 / 录到了但没识别 / 模型加载失败”。
"""
import os
import sys
import time
import wave

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "src"))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import numpy as np
import sounddevice as sd
from config import CONFIG
from faster_whisper import WhisperModel


def main():
    print("===== 输入设备列表 =====")
    ins = [(i, d["name"]) for i, d in enumerate(sd.query_devices())
           if d.get("max_input_channels", 0) > 0]
    if not ins:
        print("  （没有发现任何输入设备！麦克风可能被系统禁用）")
        return
    for i, n in ins:
        print(f"  [{i}] {n}")

    # 选设备：命令行可指定（如 `diag_mic.py 30` 用第 30 号设备），
    # 否则系统默认输入优先（与 GUI 新逻辑一致）
    dev = None
    if len(sys.argv) > 1:
        try:
            dev = int(sys.argv[1])
            print(f"\n>> 按参数使用设备: [{dev}] {sd.query_devices(dev)['name']}")
        except Exception as e:
            print(f"\n>> 参数设备无效({e})，回退自动选择")
            dev = None
    if dev is None:
        try:
            dft = sd.default.device[0]
            if dft is not None and sd.query_devices(int(dft)).get("max_input_channels", 0) > 0:
                dev = int(dft)
                print(f"\n>> 使用系统默认输入设备: [{dev}] {sd.query_devices(dev)['name']}")
            else:
                dev = ins[0][0]
                print(f"\n>> 默认无输入，改用: [{dev}] {ins[0][1]}")
        except Exception as e:
            dev = ins[0][0]
            print(f"\n>> 取默认设备出错({e})，改用: [{dev}] {ins[0][1]}")

    frames = []

    def cb(indata, n, t, status):
        frames.append(indata.copy())
        peak = float(np.max(np.abs(indata)) / 32768.0)
        print(f"  实时峰值 {peak:.3f}", end="\r", flush=True)

    stream = sd.InputStream(device=dev, channels=1, samplerate=16000,
                            dtype="int16", callback=cb)
    stream.start()
    input("\n===== 准备好后按【回车】开始录音，然后请对着麦克风说话 6 秒 =====")
    print("  录音中…（请现在说话）")
    time.sleep(6)
    stream.stop()
    stream.close()

    if not frames:
        print("\n（没有录到任何音频数据，设备可能无信号）")
        return

    audio_int16 = np.concatenate(frames, axis=0)
    audio = audio_int16.astype(np.float32) / 32768.0
    audio = audio.reshape(-1)
    level = float(np.sqrt(np.mean(audio ** 2)))
    print(f"\n>> 平均电平 level = {level:.4f}  （<0.005 视为基本静音）")
    if level < 0.005:
        print(">> 录到的几乎全是静音！说明这个设备端点没收到你的声音。\n"
              "   请试其它设备：重新运行 `diag_mic.py <设备索引>`（列表里换一个数字），\n"
              "   例如 `diag_mic.py 30`。")

    # 保存 wav 供你用播放器回放，确认到底录到没
    try:
        os.makedirs("data", exist_ok=True)
        wav_path = os.path.join("data", "diag_recording.wav")
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(audio_int16.astype(np.int16).tobytes())
        print(f">> 已保存录音到 {wav_path}（可双击用播放器听一下，确认录到你的声音没）")
    except Exception as e:
        print(f"（保存 wav 失败：{e}）")

    print("===== 加载 faster-whisper 模型 =====")
    try:
        model = WhisperModel(CONFIG.get("asr_model", "small"),
                             device="cpu", compute_type="int8")
    except Exception as e:
        print(f"!! 模型加载失败：{e}")
        return

    print("===== 识别中 =====")
    try:
        segs, _ = model.transcribe(
            audio,
            language=CONFIG.get("asr_language", "zh") or None,
            beam_size=5,
            condition_on_previous_text=False,
        )
        text = "".join(s.text for s in segs).strip()
    except Exception as e:
        print(f"!! 识别失败：{e}")
        return

    print(f">> 识别文字 = {repr(text)}")
    if not text and level >= 0.005:
        print("（录到声音但没识别出文字：可尝试说话更清晰/靠近麦，或在 .env 把 ASR_MODEL 调成 larger）")
    elif not text:
        print("（基本静音：请检查 Windows 麦克风隐私权限、麦克风是否被其它程序独占、或音量是否过低）")


if __name__ == "__main__":
    main()
