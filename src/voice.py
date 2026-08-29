"""语音能力：麦克风输入(ASR) + GPT-SoVITS 克隆声音输出(TTS)。

设计原则：
- 配置驱动、可降级。缺依赖/缺配置时不崩溃，只给友好提示（在 gui 日志里 append）。
- ASR 默认走本地 faster-whisper（离线、免费、国内可用、CPU 也能跑 int8）。
  也可切到 OpenAI Whisper API（ASR_BACKEND=openai，需能访问 openai 的 key）。
- TTS 走 GPT-SoVITS 的 HTTP 推理接口（默认 http://127.0.0.1:9880/tts），
  需要你先准备好「参考音频 + 对应文本」用于声音克隆。
"""

import os
import io
import re
import wave
import threading
import tempfile
import numpy as np


def _sanitize_tts_text(text):
    """清洗要合成成语音的文本。

    GPT-SoVITS 在中文 Windows 上 stdout 默认用 GBK 编码，遇到 emoji 这类
    非 BMP 字符会在内部 print(text) 时抛 'gbk' codec can't encode 直接崩溃
    (返回 400 tts failed)，导致“生成失败”。这里去掉 emoji / 控制字符 /
    常见 markdown 标记，保证稳定合成，且朗读内容干净（不会念出“星号/井号”）。
    """
    if not text:
        return ""
    out = []
    for ch in text:
        o = ord(ch)
        if o > 0xFFFF:               # emoji 等补充平面字符
            continue
        if o < 0x20 and ch not in "\t\n":  # 控制字符
            continue
        out.append(ch)
    s = "".join(out)
    # 去掉常见 markdown / 符号，避免 TTS 念出“星号/井号/反引号”
    s = re.sub(r"[*_~`>#]", "", s)
    # 去掉括号内内容（不朗读）：半角/全角圆括号、方括号。
    # 角色扮演常用（笑）/（轻声）/【动作】等动作/情绪提示，朗读出来会很怪，故剔除。
    # 注意：只影响 TTS 朗读，聊天窗口里显示的原文保持不变。
    s = re.sub(r"\([^()]*\)", "", s)     # 半角 ()
    s = re.sub(r"（[^（）]*）", "", s)   # 全角 （）
    s = re.sub(r"\[[^\[\]]*\]", "", s)   # 半角 []
    s = re.sub(r"【[^【】]*】", "", s)   # 全角 【】
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _split_long_text(text, max_chars=150):
    """把长文本切成适合单次 TTS 的小段。

    优先在句末标点（。！？；.!?; 及换行）处断开，让语音在句间自然停顿；
    若某段仍超过 max_chars（如长串无标点文字），再按长度硬切。
    返回片段列表（每段非空、已 strip）。
    """
    if not text:
        return []
    # 在句末标点后切分（标点保留在前一段末尾）
    raw = re.split(r"(?<=[。！？!?；;])", text)
    pieces = []
    for p in raw:
        p = p.strip()
        if not p:
            continue
        # 单段仍过长：进一步按逗号/空格软切，再不行硬切
        if len(p) > max_chars:
            sub = re.split(r"(?<=[，,、\s])", p)
            buf = ""
            for s in sub:
                if buf and len(buf) + len(s) > max_chars:
                    pieces.append(buf.strip())
                    buf = s
                else:
                    buf += s
            if buf.strip():
                pieces.append(buf.strip())
        else:
            pieces.append(p)
    # 兜底：极端情况下上面仍可能产生超长段，强制按 max_chars 硬切
    out = []
    for p in pieces:
        while len(p) > max_chars:
            out.append(p[:max_chars])
            p = p[max_chars:]
        if p.strip():
            out.append(p.strip())
    return out




# --------------------------------------------------------------------------- #
# 语音输入（麦克风录音 -> 文字）
# --------------------------------------------------------------------------- #
class VoiceInput:
    def __init__(self, enabled, backend="local", model="base", language="zh", device=""):
        self.enabled = enabled
        self.backend = backend
        self.model_name = model
        self.language = language
        self.device = device or ""    # ""=自动(优先花再)；或设备名子串/索引
        self._recording = False
        self._frames = []
        self._stream = None
        self._whisper = None          # 懒加载，避免启动就占内存
        self._lock = threading.Lock()
        self.last_device_name = ""    # 上次录音实际使用的设备名（用于 UI 确认）

    def _resolve_device(self):
        """解析录音设备：返回 sounddevice 设备索引或 None(用默认)。"""
        if not self.device or str(self.device).lower() in ("default", "auto"):
            return self._auto_pick()
        try:
            import sounddevice as sd
            try:
                return int(self.device)     # 直接给了索引
            except (ValueError, TypeError):
                pass
            name = str(self.device).lower()
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0 and name in d["name"].lower():
                    return i
        except Exception:
            pass
        return self._auto_pick()

    def _auto_pick(self):
        """自动选麦克风：优先系统默认输入设备（换耳机/插拔后系统会自动切到对的麦），
        找不到可用的默认输入时再兜底选名字含「花再」的设备。"""
        try:
            import sounddevice as sd
            # 优先系统默认输入设备（用户若在设置面板选了具体设备，不会走到这里）
            try:
                default_in = sd.default.device[0]
                if default_in is not None:
                    d = sd.query_devices(int(default_in))
                    if d.get("max_input_channels", 0) > 0:
                        return int(default_in)
            except Exception:
                pass
            # 兜底：名字含「花再」的输入设备
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0 and "花再" in d["name"]:
                    return i
        except Exception:
            pass
        return None

    # ---- 录音控制（点一下开始、再点一下结束）----
    def start(self):
        if not self.enabled:
            return False
        try:
            import sounddevice as sd
        except Exception as e:
            print("语音输入不可用（缺少 sounddevice）：", e)
            return False
        self._frames = []
        self._recording = True
        try:
            dev = self._resolve_device()
            self._stream = sd.InputStream(
                device=dev, callback=self._callback,
                channels=1, samplerate=16000, dtype="int16"
            )
            self._stream.start()
            # 记录实际使用的设备名，便于在 UI 上确认选对没
            try:
                if dev is None:
                    self.last_device_name = "系统默认输入"
                else:
                    self.last_device_name = sd.query_devices(int(dev)).get("name", str(dev))
            except Exception:
                self.last_device_name = "未知设备"
            return True
        except Exception as e:
            print("打开麦克风失败：", e)
            self._recording = False
            return False

    def _callback(self, indata, frames, time_info, status):
        if self._recording:
            self._frames.append(indata.copy())

    def stop(self):
        """停止录音，返回 (text, level)。level 为原始录音音量(0~1)，用于判断是否为空。"""
        if not self._recording:
            return "", 0.0
        self._recording = False
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        if not self._frames:
            return "", 0.0
        raw = np.concatenate(self._frames, axis=0).astype(np.float32) / 32768.0
        raw = raw.reshape(-1)        # 关键：麦克风回调是 (N,1) 2D，whisper 需 1D，否则识别为空
        level = float(np.sqrt(np.mean(raw ** 2))) if raw.size else 0.0
        self._frames = []

        # 轻音量归一化：把低音量拉到合适电平，whisper 对轻音短句更敏感
        audio = self._normalize(raw, target=0.8)
        text = self._transcribe(audio)
        # 仍为空且确有其声：再 boost 一次重试（处理特别轻的短句）
        if not text and level >= 0.003:
            text = self._transcribe(self._normalize(raw, target=1.0))
        return text, level

    @staticmethod
    def _normalize(audio, target=0.8):
        """峰值归一化到 target，避免增益过大爆音；近静音则不放大。"""
        if audio.size == 0:
            return audio
        peak = float(np.max(np.abs(audio)))
        if peak < 1e-4:
            return audio
        scale = min(target / peak, 30.0)   # 上限 30 倍，防近静音被无限放大
        return np.clip(audio * scale, -1.0, 1.0)

    # ---- 识别 ----
    def _transcribe(self, audio):
        if self.backend == "openai":
            return self._transcribe_openai(audio)
        return self._transcribe_local(audio)

    def _load_whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel
            # 优先使用本地已下载的模型目录（离线、稳定，避免运行时联网被墙导致下载失败）。
            # 目录由 download_model.py 从国内镜像拉取：models/faster-whisper-<model>
            here = os.path.dirname(os.path.abspath(__file__))
            local_dir = os.path.join(os.path.dirname(here),
                                     "models", f"faster-whisper-{self.model_name}")
            if os.path.isdir(local_dir) and os.path.exists(os.path.join(local_dir, "model.bin")):
                model_path = local_dir
            else:
                # 本地未就绪：回退到 hub 下载，并注入国内镜像源避免直连 HuggingFace 超时。
                if not os.environ.get("HF_ENDPOINT"):
                    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
                model_path = self.model_name
            # 强制 CPU：避免 device="auto" 在有独显但缺 cuBLAS 的机器上选 CUDA 导致推理崩溃；
            # small 模型 CPU int8 足够实时。
            self._whisper = WhisperModel(model_path, device="cpu", compute_type="int8")
        return self._whisper

    def _transcribe_local(self, audio):
        try:
            model = self._load_whisper()
        except Exception as e:
            return f"__VOICE_ERR__本地识别模型加载失败：{e}"
        try:
            segments, _ = model.transcribe(
                audio, language=self.language or None, beam_size=5,
                # 关掉“基于上文续写”，避免上一句的语义串到这一句造成误识/乱回
                condition_on_previous_text=False,
                # VAD(静音检测)过滤：切掉非语音段，极大减少“听到一点杂音就幻觉出一段
                # 文字”的误识(已读乱回的主因之一)；对轻声/短句也一并更稳定。
                vad_filter=True,
                vad_parameters=dict(
                    threshold=0.4,            # 灵敏度：越小越容易判为语音(更不易漏字)
                    min_speech_duration_ms=200,
                    max_speech_duration_s=30,
                    min_silence_duration_ms=600,
                    speech_pad_ms=300,
                ),
            )
            return "".join(s.text for s in segments).strip()
        except Exception as e:
            return f"__VOICE_ERR__识别失败：{e}"

    def _transcribe_openai(self, audio):
        # 把音频写成临时 wav 再交给 OpenAI 接口
        try:
            import requests
            from openai import OpenAI
            from config import CONFIG
            client = OpenAI(api_key=CONFIG["api_key"], base_url=CONFIG["base_url"])
        except Exception as e:
            return f"__VOICE_ERR__OpenAI 客户端不可用：{e}"
        path = None
        try:
            path = tempfile.mktemp(suffix=".wav")
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes((audio * 32768.0).astype(np.int16).tobytes())
            with open(path, "rb") as f:
                resp = client.audio.transcriptions.create(model="whisper-1", file=f)
            return (resp.text or "").strip()
        except Exception as e:
            return f"__VOICE_ERR__OpenAI 识别失败：{e}"
        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
# 语音输出（文字 -> 云端 AI 音色合成 -> 播放）
# --------------------------------------------------------------------------- #
def play_wav_bytes(data: bytes, volume=1.0, on_level=None, level_interval=0.07,
                   output_device=None):
    """播放一段标准 wav 字节流（用标准库 wave 解析，避免额外依赖）。

    volume: 0~1 的播放音量；用 soft-clip 避免爆音。
    on_level: 可选回调，播放期间按播放进度周期性调用 on_level(rms)，
              rms 为当前播放位置附近音频的 RMS 能量(0~1)，用于实时口型驱动(LipSync)。
    level_interval: on_level 回调间隔(秒)，默认 0.07(约 14Hz)，足够口型流畅。
    """
    import time
    import queue  # noqa: F401  (保留以备后续更精细的回调同步)
    import sounddevice as sd
    # 把输出设备参数规整成 sounddevice 能接受的形式：
    # 空串/None -> None（用系统默认输出）；数字串 -> 索引 int；其余当设备名子串。
    # 注意：绝不能把 "" 直接传给 sounddevice 的 device 参数（会匹配到所有设备而报错）。
    if output_device:
        try:
            out_dev = int(output_device)
        except (ValueError, TypeError):
            out_dev = str(output_device)
    else:
        out_dev = None
    with wave.open(io.BytesIO(data), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    arr = np.clip(arr * float(volume), -1.0, 1.0)   # 音量调节 + 防溢出
    if arr.size == 0:
        return
    # 关键修复：口型必须跟随「声卡真正正在播放」的音频，而不是墙钟时间。
    # 旧实现用 time.time() 跑一个预计算好的能量时间轴，与声卡缓冲节奏会逐渐漂移，
    # 导致循环跑完后嘴停在某值、声音（含句尾拖音）却还在放 -> “说到后面只有声音嘴不动”。
    # 新实现：在播放回调里实时计算「当前这片正在发声」的 RMS 存入共享变量，
    # 另起一个轻量泵线程按 level_interval 把最新值发给 on_level 驱动口型。
    # 这样嘴型严格对齐被听见的声音，长句与句尾都同步。
    pos = [0]
    latest_rms = [0.0]
    rms_lock = threading.Lock()

    def _cb(outdata, frames, time_info, status):
        i = pos[0]
        chunk = arr[i:i + frames]
        if chunk.size < frames:
            tmp = np.zeros(frames, dtype=np.float32)
            tmp[:chunk.size] = chunk
            chunk = tmp
        outdata[:] = chunk.reshape(-1, 1)
        pos[0] = i + frames
        # 计算当前正在播放的这片音频能量（口型依据）
        cnt = max(0, min(frames, arr.size - i))
        valid = chunk[:cnt]
        if valid.size:
            with rms_lock:
                latest_rms[0] = float(np.sqrt(np.mean(valid ** 2)))

    try:
        stream = sd.OutputStream(samplerate=sr, channels=1, dtype="float32",
                                 callback=_cb, device=out_dev)
        stream.start()
    except Exception:
        # 回调方式失败则回退到一次性阻塞播放（仅丢失实时口型，不影响发声）
        sd.play(arr, sr, device=out_dev)
        sd.wait()
        return

    # 口型泵：周期性取“正在播放”的最新能量并驱动口型（与声卡实际播放对齐）
    stop_ev = threading.Event()
    def _pump():
        while not stop_ev.is_set():
            time.sleep(level_interval)
            if on_level and not stop_ev.is_set():
                with rms_lock:
                    rms = latest_rms[0]
                try:
                    on_level(rms)
                except Exception:
                    pass
    threading.Thread(target=_pump, daemon=True).start()

    while pos[0] < arr.size:   # 等音频真正播放完
        time.sleep(0.02)
    time.sleep(0.05)           # 等声卡尾缓冲排空，避免嘴比声音先停
    stop_ev.set()
    try:
        stream.stop()
        stream.close()
    except Exception:
        pass


class TTS:
    def __init__(self, enabled, url, ref_audio, ref_text, speed=1.0, volume=1.0,
                 if_sr=False, sample_steps=8, output_device=""):
        self.enabled = enabled
        self.url = url.rstrip("/")
        self.ref_audio = ref_audio
        self.ref_text = ref_text
        self.speed = float(speed)
        self.volume = float(volume)
        # 用户配置的基准值：情绪语音风格（emotion.voice_style）以它为基准乘倍率，
        # 不会覆盖用户在设置里定的语速/音量。
        self.base_speed = self.speed
        self.base_volume = self.volume
        self.if_sr = bool(if_sr)            # 超分降噪(super_sampling)
        self.sample_steps = int(sample_steps)  # v2 采样步数：8 够用且快
        self.output_device = output_device  # 扬声器设备：""=系统默认；或索引/名子串

    def is_ready(self):
        return self.enabled and bool(self.ref_audio) and bool(self.url)

    def speak(self, text, on_play=None, on_level=None, on_audio=None, on_error=None):
        """合成并播放。出错时返回错误信息字符串（调用方决定是否展示）。

        on_play: 可选回调，在音频「开始播放」前被调用（仅 200 成功时）。
        用于把形象口型/肢体动作/气泡框对齐到声音起点，避免 TTS 生成耗时
        (~数秒) 期间动作已播完、声音才姗姗来迟的“不同步”现象。
        on_level: 可选回调，播放期间按播放进度周期性回调 on_level(rms)，
        用于实时口型驱动(LipSync)，让嘴型随音频能量张合，长句也同步。
        on_audio: 可选回调，每合成完一小段就把 wav 字节交给它（而非在本机
        播放）。用于把语音推给外部渲染端（如 3D 游戏引擎）自行播放——
        此时 on_play 仍会触发（告知“开始说话”），on_level 可选（若外部
        自行做口型同步则无需传）。
        on_error: 可选回调，合成失败时【立即】在后台线程调用 on_error(msg)，
        不用等已排队的音频播完（注意线程安全，UI 侧需自行切回主线程）。
        """
        if not self.enabled:
            return None
        if not self.url or not self.ref_audio:
            return "语音输出未配置：请在 .env 填 SOVITS_URL 和 SOVITS_REF_AUDIO/SOVITS_REF_TEXT"
        try:
            import requests
        except Exception as e:
            return f"语音输出不可用（缺少 requests）：{e}"
        text = (text or "").strip()
        if not text:
            return None
        # 清洗：去掉 emoji / 控制字符 / markdown 标记，避免服务端 GBK 编码崩溃
        text = _sanitize_tts_text(text)
        if not text:
            return None
        # 长文本切分：GPT-SoVITS 单次 /tts 对文本长度敏感，且整段合成耗时会随字数
        # 线性增长（实测 ~300 字约 25s）。若一条回复过长，单次请求极易超过超时
        # 而失败（"长语音不合成"）。这里按句切成小段。
        # 关键修复（断断续续的根因）：旧实现是「合成一句 → 播放一句 → 再合成下一句」，
        # 每两句之间隔着一次完整 HTTP 合成延迟（0.5~3s），表现就是"说几个字没声音、
        # 过一会又有声音"。现在改成流水线：后台线程不断合成后续句子，播放线程从
        # 队列里取出来连续播放——首句出来就开始说话，后面的句子在播放期间就绪，
        # 整段声音连续不断。
        chunks = _split_long_text(text, max_chars=150)
        if not chunks:
            return None
        import queue
        q = queue.Queue()
        err_box = []

        def _producer():
            for ch in chunks:
                try:
                    resp = requests.post(
                        self.url.rstrip("/") + "/tts",
                        # api_v2.py 的 POST /tts 端点(TTS_Request 模型)字段名如下：
                        # ref_audio_path / text_lang / prompt_lang / speed_factor /
                        # super_sampling(降噪) / sample_steps。旧代码用的 text_language/
                        # refer_wav_path/speed/if_sr 全部对不上，导致降噪、语速、参考音频
                        # 从未真正生效。
                        json={
                            "text": ch,
                            "text_lang": "zh",
                            "ref_audio_path": self.ref_audio,
                            "prompt_text": self.ref_text,
                            "prompt_lang": "zh",
                            "speed_factor": self.speed,
                            "super_sampling": self.if_sr,  # 超分降噪(默认关；开则更干净)
                            "sample_steps": self.sample_steps,  # 8 够用且快；越大越细腻越慢
                        },
                        timeout=90,
                    )
                except Exception as e:
                    err_box.append(f"语音合成/播放失败：{e}")
                    print(f"[TTS] 合成失败：{e}", flush=True)
                    if on_error:
                        try:
                            on_error(err_box[0])
                        except Exception:
                            pass
                    q.put(None)
                    return
                if resp.status_code != 200:
                    err_box.append(f"GPT-SoVITS 返回错误 {resp.status_code}：{resp.text[:200]}")
                    print(f"[TTS] {err_box[0]}", flush=True)
                    if on_error:
                        try:
                            on_error(err_box[0])
                        except Exception:
                            pass
                    q.put(None)
                    return
                q.put(resp.content)
            q.put(None)

        threading.Thread(target=_producer, daemon=True).start()

        played_first = False
        while True:
            wav = q.get()
            if wav is None:
                break
            # 首句音频即将开始播放：先触发形象口型/动作/气泡，使其与声音起点对齐
            if not played_first:
                if on_play:
                    try:
                        on_play()
                    except Exception:
                        pass
                played_first = True
            if on_audio is not None:
                # 3D 模式：把 wav 字节交给外部渲染端（如游戏引擎）播放，本机不发音
                try:
                    on_audio(wav)
                except Exception:
                    pass
            else:
                play_wav_bytes(wav, self.volume, on_level=on_level,
                               output_device=self.output_device)
        return err_box[0] if err_box else None
