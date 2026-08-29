# -*- coding: utf-8 -*-
"""
小念 ⇄ VRChat OSC 桥（双实例双端口）——"她和你同屏"方案的本地大脑接口。

拓扑（两个 VRChat 实例跑在同一台电脑）：
  你的实例（PC VR 串流）：OSC 输出 -> 本机 9011   （本模块读：你的手势/口型/状态）
  她的实例（桌面模式）  ：OSC 输入 <- 本机 9010   （本模块写：她的口型/手势/表情/Chatbox）
  GPT-SoVITS 语音       ：输出到 VB-CABLE 虚拟声卡 = 她实例的麦克风（全房间能听到她说话）

职责：
  1. 读你的 OSC：GestureLeft/Right、Viseme、Voice、AFK、TrackingType 等。
  2. 写她的 OSC：口型（TTS on_level 能量→Viseme）、手势、自定义表情参数、Chatbox。
  3. 情绪驱动默认表现：她的 5 维情绪（emotion.py）自动映射手势/表情。
  4. 【动作教学引擎】：
       - 你比个手势 + 说"我开心的时候你就做这个" → 她立刻模仿一遍；
       - 规则存入 custom_skills（带 action + 结构化触发条件），持久化到 data/；
       - 触发执行线程按 情绪/关键词/时间段 评估规则，命中时她自动表演 + Chatbox 说明。
  5. 对话：你的语音（faster-whisper）→ assistant.chat（LLM+意识层）→ 她的 Chatbox + TTS 说话。

依赖：pip install python-osc（缺省时启动会给出安装提示并退出）。
启动：venv\\Scripts\\python.exe -m src.osc_bridge
     --selftest   只跑离线自检（教学解析 + 规则库读写，不需要 OSC/VRChat）
     --no-asr     关闭麦克风识别（用控制台打字代替说话）
     --no-tts     关闭语音输出（只写字幕）
     --in-port / --out-port  覆盖默认 9011 / 9010
"""
import os
import re
import sys
import time
import threading
import argparse
from collections import deque

# 让 src 目录可被 import（无论从哪启动，与 bridge.py 同法）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Windows 控制台默认 GBK，emoji/生僻字会让 print 直接崩溃；统一 UTF-8 + 容错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config import CONFIG
from custom_skills import (add_skill, match_skill_with_action, taught_rules,
                           format_list, GESTURE_LIBRARY, EXPRESSION_ALLOWED)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
OSC_IN_PORT = int(CONFIG.get("osc_in_port", 9011))       # 你(VR实例)的 OSC 输出口
OSC_OUT_PORT = int(CONFIG.get("osc_out_port", 9010))     # 她(桌面实例)的 OSC 输入口
OSC_OUT_HOST = CONFIG.get("osc_out_host", "127.0.0.1")
OSC_LEARN_ENABLED = bool(CONFIG.get("osc_learn_enabled", True))
RULE_COOLDOWN = float(CONFIG.get("osc_rule_cooldown", 180.0))   # 同一条教学规则的最小触发间隔(秒)
EMOTION_TICK = 0.5
GESTURE_FRESH_SEC = 4.0          # 教学时：多少秒内的手势算"你在教我动作"

# 情绪主导 -> 默认表现（未命中教学规则时的基线；只作用于她，不让规则全空时像个木头人）
EMOTION_DEFAULT_GESTURE = {
    "joy": 4,        # ✌️
    "anger": 1,      # 握拳
    "sadness": 0,
    "calm": 0,
    "anxiety": 2,    # 摊手
}
# 情绪 -> 自定义表情参数（她化身需预置 ExprJoy/ExprAngry/... 参数，见《化身预置清单》）
EMOTION_EXPR_PARAM = {
    "joy": "ExprJoy", "anger": "ExprAngry", "sadness": "ExprSad",
    "calm": "ExprCalm", "anxiety": "ExprAnxious",
}

# --------------------------------------------------------------------------- #
# 教学语句解析（纯函数，可离线自检）
# --------------------------------------------------------------------------- #
EMOTION_WORDS = {
    "开心": "joy", "高兴": "joy", "兴奋": "joy", "难过": "sadness", "伤心": "sadness",
    "委屈": "sadness", "生气": "anger", "愤怒": "anger", "恼火": "anger",
    "不安": "anxiety", "紧张": "anxiety", "害怕": "anxiety", "平静": "calm",
    "放松": "calm", "困": "anxiety", "累": "anxiety",
}
TIME_WORDS = {
    "早上": list(range(5, 9)), "早晨": list(range(5, 9)), "上午": list(range(9, 12)),
    "中午": list(range(11, 14)), "下午": list(range(12, 18)), "傍晚": list(range(17, 19)),
    "晚上": list(range(18, 23)), "夜里": list(range(0, 5)), "深夜": list(range(0, 5)),
    "凌晨": list(range(0, 5)),
}
_TEACH_HINT = re.compile(r"(记住|记下|学会|教我|以后|从现在起|从现在开始|教给)")
_DO_HINT = re.compile(r"(做|比|表演|来个|用|换成|摆)(这个|这个动作|这样|一下)")
_EMO_RE = re.compile(r"|".join(re.escape(w) for w in EMOTION_WORDS))
_SAY_RE = re.compile(r"说[「“”『』\"]?([^「」『』“”\"]{1,20}?)[」』”\"]?的?时候")
_TIME_RE = re.compile(r"|".join(re.escape(w) for w in TIME_WORDS))


def parse_teaching(text):
    """解析教学语句。返回 dict 或 None。

    返回：{"trigger_text": 人类可读触发描述, "meta": 结构化触发条件(或 None),
          "emotion_trigger": True 表示按"她的情绪"触发(否则按你说的话触发)}
    例："我开心的时候你就做这个" -> meta={type:emotion,dim:joy,th:0.5}, emotion_trigger=False
        "你开心的时候就比这个"   -> meta={type:emotion,dim:joy,th:0.5}, emotion_trigger=True
    """
    if not text:
        return None
    if not _TEACH_HINT.search(text) and not _DO_HINT.search(text):
        return None

    # 1) 情绪触发："我开心的时候…"（你的状态）/ "你开心的时候…"（她的情绪）
    m_emo = _EMO_RE.search(text)
    if m_emo:
        dim = EMOTION_WORDS.get(m_emo.group(0))
        if dim:
            her = bool(re.search(rf"(你|她|小念)[^，。！？]*{re.escape(m_emo.group(0))}", text))
            label = m_emo.group(0)
            who = "我" if her else "你"
            return {
                "trigger_text": f"{who}{label}的时候",
                "meta": {"type": "emotion", "dim": dim, "th": 0.5},
                "emotion_trigger": her,
            }

    # 2) 关键词触发："我说『加油』的时候…"
    m_say = _SAY_RE.search(text)
    if m_say:
        kw = m_say.group(1).strip()
        if kw:
            return {
                "trigger_text": f"你说「{kw}」的时候",
                "meta": {"type": "keyword", "text": kw},
                "emotion_trigger": False,
            }

    # 3) 时间段触发："晚上…的时候…"
    m_time = _TIME_RE.search(text)
    if m_time:
        w = m_time.group(0)
        return {
            "trigger_text": f"{w}的时候",
            "meta": {"type": "time", "hours": TIME_WORDS[w]},
            "emotion_trigger": False,
        }

    # 4) 只说了"记住/以后…做这个"但没指明条件 → 无法建规则，返回 None（交给普通对话）
    return None


def viseme_from_rms(rms):
    """音频能量 → VRChat Viseme 参数(0~14)。非线性映射让张嘴更自然。"""
    if rms is None or rms <= 0:
        return 0
    v = int(min(14, (rms * 90.0) ** 0.6))
    return max(0, v)


# --------------------------------------------------------------------------- #
# 她的化身：OSC 输出封装
# --------------------------------------------------------------------------- #
class HerAvatar:
    """写入她实例的 OSC 参数（口型/手势/表情/Chatbox）。"""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self._client = None
        self._lock = threading.Lock()
        self._expr_state = {}       # 当前表情参数值，用于复位
        self._gesture = {"L": 0, "R": 0}
        self.available = False

    def connect(self):
        try:
            from pythonosc import udp_client
            self._client = udp_client.SimpleUDPClient(self.host, self.port)
            self.available = True
            return True
        except Exception as e:
            print(f"[OSC出] 初始化失败（{e}）。请确认她实例的 OSC 输入口是 {self.port}。")
            return False

    def _send(self, addr, value):
        if not self.available or self._client is None:
            return
        try:
            self._client.send_message(addr, value)
        except Exception:
            pass

    def set_viseme(self, v):
        self._send("/avatar/parameters/Viseme", int(v))

    def set_gesture(self, side, v):
        side = side.upper() if side.upper() in ("L", "R") else "L"
        v = int(v)
        if v not in GESTURE_LIBRARY:
            v = 0
        self._gesture[side] = v
        self._send(f"/avatar/parameters/Gesture{side}", v)

    def clear_gestures(self):
        for side in ("L", "R"):
            self._send(f"/avatar/parameters/Gesture{side}", 0)

    def set_expression(self, name, value):
        """name: ExprJoy/ExprAngry/...；value 0~1。"""
        v = max(0.0, min(1.0, float(value)))
        self._expr_state[name] = v
        self._send(f"/avatar/parameters/{name}", v)

    def reset_expressions(self):
        for name in list(self._expr_state):
            if self._expr_state[name] > 0.01:
                self._expr_state[name] = 0.0
                self._send(f"/avatar/parameters/{name}", 0.0)

    def chatbox(self, text, send=True):
        text = (text or "").replace("\n", " ")[:144]
        if not text:
            return
        # VRChat Chatbox：先写 typing 状态，再送正文（以 \n 结尾表示发送）
        self._send("/chatbox/typing", True)
        self._send("/chatbox/input", text + "\v" if not send else text + "\n")
        self._send("/chatbox/typing", False)

    def typing(self, on):
        self._send("/chatbox/typing", bool(on))

    def do_action(self, action):
        """执行一次动作（教学库里的 action dict）。"""
        if not isinstance(action, dict):
            return
        kind = action.get("kind")
        if kind == "gesture":
            self.set_gesture("L", action.get("value", 0))
        elif kind == "expression":
            self.set_expression(("Expr" + str(action.get("name", "")).capitalize()), action.get("value", 1.0))
        elif kind == "emote":
            # 化身预置 emote 用自定义参数触发（参数名约定见《化身预置清单》）
            self._send("/avatar/parameters/Emote", str(action.get("value", "")))


# --------------------------------------------------------------------------- #
# 教学引擎：捕获手势 + 建规则 + 触发执行
# --------------------------------------------------------------------------- #
class TeachingEngine:
    def __init__(self, her: HerAvatar, emotion):
        self.her = her
        self.emotion = emotion            # EmotionEngine 实例（触发条件评估用）
        self.user_gesture = 0             # 最近一次非自然手势(0~7)
        self.user_gesture_ts = 0.0
        self.recent_utterances = deque(maxlen=30)   # 你的近期话语（关键词触发用）
        self._cooldown = {}               # rule name -> 下次允许触发时间
        self._lock = threading.Lock()

    # ---- 输入侧 ----
    def on_user_gesture(self, v):
        v = int(v)
        if v != self.user_gesture:
            self.user_gesture = v
            self.user_gesture_ts = time.time()
            if v != 0:
                print(f"[教学] 捕获你的手势：{GESTURE_LIBRARY.get(v, v)}")

    def on_user_speech(self, text):
        """你的语音识别文本入口：先教学判定，命中教学则学习；否则返回 False 交给对话。"""
        text = (text or "").strip()
        if not text:
            return False
        self.recent_utterances.append(text)
        if not OSC_LEARN_ENABLED:
            return False
        parsed = parse_teaching(text)
        if not parsed:
            return False

        # 需要一个"教的动作"：优先用你刚比的手势；没有手势就看文本里有没有表情/emote 提示
        action, action_desc = self._action_from_context(text)
        if action is None:
            self.her.chatbox("你想让我做什么呀？先比个手势，再说一次「教我」吧～")
            return True

        meta = parsed["meta"]
        trigger_text = parsed["trigger_text"]
        if meta is None:
            # 无条件结构时按触发词匹配（文本库原生行为），不带动作不发规则
            return False
        name = f"teach_{int(time.time())}_{action.get('kind', 'x')}"
        reply = f"好呀，{trigger_text}我就{action_desc}～"
        ok, msg = add_skill(name, trigger_text, reply, action=action, trigger_meta=meta)
        if not ok:
            self.her.chatbox(msg)
            return True

        # 立即模仿一遍（2.5 秒后复位），给她"看会了"的即时反馈
        self.her.do_action(action)
        self.her.chatbox(reply)
        threading.Thread(target=self._release_after, args=(2.5,), daemon=True).start()
        print(f"[教学] 学会规则：{trigger_text} → {action_desc}")
        return True

    def _release_after(self, sec):
        time.sleep(sec)
        self.her.clear_gestures()
        self.her.reset_expressions()

    def _action_from_context(self, text):
        """从教学语境提取要学的动作：(action, 中文描述) 或 (None, None)。"""
        fresh = (time.time() - self.user_gesture_ts) <= GESTURE_FRESH_SEC
        if fresh and self.user_gesture in GESTURE_LIBRARY and self.user_gesture != 0:
            v = self.user_gesture
            return {"kind": "gesture", "value": v}, f"比「{GESTURE_LIBRARY[v]}」"
        for dim, cn in EXPRESSION_ALLOWED.items():
            if cn in text:
                return {"kind": "expression", "name": dim, "value": 1.0}, f"做「{cn}」表情"
        for em in ("挥手", "点头", "摇头", "鞠躬", "欢呼", "思考", "生闷气"):
            if em in text:
                return {"kind": "emote", "value": {
                    "挥手": "wave", "点头": "nod", "摇头": "shake_head", "鞠躬": "bow",
                    "欢呼": "cheer", "思考": "think", "生闷气": "sulk",
                }[em]}, f"表演「{em}」"
        return None, None

    # ---- 触发执行 ----
    def tick(self, hour=None):
        """周期评估教学规则；返回 (触发数)。命中执行动作 + Chatbox。"""
        fired = 0
        now = time.time()
        h = hour if hour is not None else time.localtime().tm_hour
        for rule in taught_rules():
            name = rule.get("name", "")
            if now < self._cooldown.get(name, 0.0):
                continue
            if not self._rule_matches(rule, h):
                continue
            action = rule.get("action")
            if not isinstance(action, dict):
                continue
            self._cooldown[name] = now + RULE_COOLDOWN
            self.her.do_action(action)
            self.her.chatbox(rule.get("reply", ""))
            threading.Thread(target=self._release_after, args=(3.0,), daemon=True).start()
            print(f"[教学] 触发规则「{rule.get('trigger')}」")
            fired += 1
        return fired

    def _rule_matches(self, rule, hour):
        meta = rule.get("trigger_meta")
        if meta is None:
            trig = (rule.get("trigger") or "").strip()
            return bool(trig) and any(trig in u for u in self.recent_utterances)
        t = meta.get("type")
        if t == "emotion":
            cur = 0.0
            try:
                cur = float(getattr(self.emotion, "emotion", {}).get(meta.get("dim"), 0.0))
            except Exception:
                return False
            return cur >= float(meta.get("th", 0.5))
        if t == "keyword":
            kw = meta.get("text", "")
            return bool(kw) and any(kw in u for u in self.recent_utterances)
        if t == "time":
            return hour in meta.get("hours", [])
        return False


# --------------------------------------------------------------------------- #
# 麦克风 VAD + 识别（复用 voice.VoiceInput 的完整链路）
# --------------------------------------------------------------------------- #
class SpeechListener:
    """轻量 VAD：有声音就开始攒帧，静音 1.2s 结束一段并交给 whisper 识别。"""

    SPEECH_RMS = 0.012
    SILENCE_SEC = 1.2
    MIN_SPEECH_SEC = 0.3
    MAX_SEGMENT_SEC = 20.0

    def __init__(self, on_text, enabled=True):
        self.on_text = on_text
        self.enabled = enabled
        self._vi = None
        self._stop = threading.Event()

    def start(self):
        if not self.enabled:
            print("[ASR] 已关闭（--no-asr 或配置关闭），可用控制台打字代替说话。")
            return
        try:
            from voice import VoiceInput
        except Exception as e:
            print(f"[ASR] 语音输入不可用：{e}")
            return
        self._vi = VoiceInput(
            enabled=True,
            backend=CONFIG.get("asr_backend", "local"),
            model=CONFIG.get("asr_model", "small"),
            language=CONFIG.get("asr_language", "zh"),
            device=CONFIG.get("asr_device", ""),
        )
        threading.Thread(target=self._loop, daemon=True).start()
        print("[ASR] 麦克风监听已启动（faster-whisper 首次加载需几十秒）")

    def _rms_of_tail(self, n=24000):
        frames = self._vi._frames
        if not frames:
            return 0.0
        tail = frames[-n:]
        import numpy as np
        arr = np.concatenate(tail) if len(tail) > 1 else tail[0]
        return float(np.sqrt(np.mean(np.square(arr)))) if arr.size else 0.0

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._vi._frames.clear()
                self._vi.start()
                speech_since = None
                last_voice = time.time()
                while not self._stop.is_set():
                    rms = self._rms_of_tail()
                    if rms > self.SPEECH_RMS:
                        last_voice = time.time()
                        if speech_since is None:
                            speech_since = time.time()
                    else:
                        if speech_since is not None and time.time() - last_voice > self.SILENCE_SEC:
                            break
                    if speech_since and time.time() - speech_since > self.MAX_SEGMENT_SEC:
                        break
                    time.sleep(0.08)
                self._vi.stop()
                if speech_since is None:
                    continue
                dur = time.time() - speech_since
                if dur < self.MIN_SPEECH_SEC:
                    continue
                frames = list(self._vi._frames)
                if not frames:
                    continue
                threading.Thread(target=self._transcribe, args=(frames,), daemon=True).start()
            except Exception as e:
                print(f"[ASR] 监听循环异常：{e}")
                time.sleep(2)

    def _transcribe(self, frames):
        try:
            text = self._vi._transcribe(frames)
            text = (text or "").strip()
            if text:
                print(f"[ASR] 你说：{text}")
                self.on_text(text)
        except Exception as e:
            print(f"[ASR] 识别失败：{e}")

    def stop(self):
        self._stop.set()
        try:
            if self._vi:
                self._vi.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 主桥
# --------------------------------------------------------------------------- #
class OscBridge:
    def __init__(self, args):
        self.args = args
        self.in_port = args.in_port or OSC_IN_PORT
        self.out_port = args.out_port or OSC_OUT_PORT
        self.her = HerAvatar(OSC_OUT_HOST, self.out_port)
        self.emotion = None
        self.assistant = None
        self.tts = None
        self.teach = None
        self.listener = None
        self._talking = False
        self._talk_lock = threading.Lock()

    # ---- 启动 ----
    def setup(self):
        # 大脑（复用 bridge.build_assistant：assistant + emotion + tts 全家桶）
        try:
            from bridge import build_assistant
            self.assistant, self.emotion, self.tts = build_assistant()
        except Exception as e:
            print(f"[大脑] 构建失败：{e}")
            return False
        self.teach = TeachingEngine(self.her, self.emotion)
        if not self.her.connect():
            print("[OSC出] 将继续运行，但她实例收不到任何表现（可只做规则学习）。")
        if not self.args.no_tts and self.tts.is_ready():
            print(f"[TTS] 就绪（输出设备：{self.tts.output_device or '默认'}，"
                  f"请确认它指向 VB-CABLE 且为她实例的麦克风）")
        else:
            print("[TTS] 未就绪——她将只用 Chatbox 写字幕。")
        return True

    def start(self):
        # OSC 输入服务器（读你的实例）
        osc_in = None
        try:
            from pythonosc.dispatcher import Dispatcher
            from pythonosc.osc_server import ThreadingOSCUDPServer
            disp = Dispatcher()
            disp.map("/avatar/parameters/GestureLeft", self._on_gesture_l)
            disp.map("/avatar/parameters/GestureRight", self._on_gesture_r)
            disp.map("/avatar/parameters/Viseme", self._on_user_viseme)
            disp.map("/avatar/parameters/Voice", self._on_user_voice)
            disp.map("/avatar/parameters/*", lambda *a: None)   # 其余参数静默忽略
            osc_in = ThreadingOSCUDPServer(("127.0.0.1", self.in_port), disp)
            threading.Thread(target=osc_in.serve_forever, daemon=True).start()
            print(f"[OSC入] 监听 {self.in_port}（你的实例输出口）")
        except Exception as e:
            print(f"[OSC入] 启动失败：{e}（请确认 python-osc 已安装、端口未被占用）")

        # 麦克风
        self.listener = SpeechListener(self._on_user_text, enabled=not self.args.no_asr)
        self.listener.start()

        # 情绪驱动默认表现 + 教学规则触发（主循环）
        print("[小念] 她已上线。等她看到你，就会用情绪和学过的规则回应你。")
        threading.Thread(target=self._tick_loop, daemon=True).start()

        # 控制台输入兜底（打字=说话，方便无麦克风调试）
        threading.Thread(target=self._console_loop, daemon=True).start()

    # ---- OSC 输入回调 ----
    def _on_gesture_l(self, addr, *vals):
        if vals:
            self.teach.on_user_gesture(vals[0])

    def _on_gesture_r(self, addr, *vals):
        if vals and int(vals[0]) != 0:
            self.teach.on_user_gesture(vals[0])

    def _on_user_viseme(self, addr, *vals):
        pass    # 你的口型暂不消费（教学不需要）；保留钩子

    def _on_user_voice(self, addr, *vals):
        pass

    # ---- 用户说话 ----
    def _on_user_text(self, text):
        if self.teach.on_user_speech(text):
            return   # 教学语句：已被学习流程消费
        # 普通对话 → LLM
        threading.Thread(target=self._chat_worker, args=(text,), daemon=True).start()

    def _chat_worker(self, text):
        with self._talk_lock:
            if self._talking:
                return   # 上一句还没说完，先不抢话
            self._talking = True
        try:
            self.her.typing(True)
            pieces = []
            def on_token(p):
                pieces.append(p)
            try:
                reply = self.assistant.chat(text, on_token=on_token)
            except Exception as e:
                reply = f"（我走神了：{e}）"
            reply = (reply or "").strip()
            if not reply:
                self.her.typing(False)
                return
            # 情绪感知（让她听你说的话产生情绪波动）
            try:
                self.emotion.perceive(text=text, source="user")
                self.emotion.perceive(text=reply, source="self")
            except Exception:
                pass
            self._speak_and_show(reply)
        finally:
            self.her.typing(False)
            with self._talk_lock:
                self._talking = False

    def _speak_and_show(self, reply):
        """她的表现出口：Chatbox 字幕 + 可选 TTS + 口型驱动。"""
        self.her.chatbox(reply)
        if self.args.no_tts or self.tts is None or not self.tts.is_ready():
            return

        def on_play():
            pass

        def on_level(rms):
            self.her.set_viseme(viseme_from_rms(rms))

        def _finish():
            self.her.set_viseme(0)

        # 情绪 → 语音：语速/音量随当下心情实时变化（用户基准 × 情绪倍率）
        try:
            if self.emotion is not None:
                vs = self.emotion.voice_style()
                self.tts.speed = self.tts.base_speed * vs["speed"]
                self.tts.volume = self.tts.base_volume * vs["volume"]
        except Exception:
            pass

        try:
            err = self.tts.speak(reply, on_play=on_play, on_level=on_level)
            if err:
                print(f"[TTS] {err}")
        except Exception as e:
            print(f"[TTS] 播放异常：{e}")
        finally:
            _finish()

    # ---- 周期任务 ----
    def _tick_loop(self):
        last_decay = 0.0
        while True:
            try:
                if self.teach.tick():
                    pass
                # 情绪衰减（半衰期机制由 emotion 自己维护，这里低频触发）
                now = time.time()
                if self.emotion is not None and now - last_decay > 30:
                    last_decay = now
                    self.emotion.decay()
                    # 默认情绪表现：先清旧表情，再按主导情绪设基线（教学规则优先于它）
                    dom = self.emotion.dominant()
                    self._apply_default_expression(dom)
            except Exception as e:
                print(f"[循环] {e}")
            time.sleep(EMOTION_TICK)

    def _apply_default_expression(self, dom):
        try:
            for name in list(self.her._expr_state):
                self.her.reset_expressions()
            g = EMOTION_DEFAULT_GESTURE.get(dom, 0)
            self.her.set_gesture("L", g)
            if dom in EMOTION_EXPR_PARAM:
                v = float(getattr(self.emotion, "emotion", {}).get(dom, 0.3))
                self.her.set_expression(EMOTION_EXPR_PARAM[dom], max(0.15, v))
        except Exception:
            pass

    # ---- 控制台输入 ----
    def _console_loop(self):
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                return
            line = (line or "").strip()
            if not line:
                continue
            if line in ("/skills", "/技能"):
                print(format_list())
                continue
            self._on_user_text(line)


# --------------------------------------------------------------------------- #
# 离线自检（不需要 OSC/VRChat/TTS，验证教学引擎与规则库）
# --------------------------------------------------------------------------- #
def selftest():
    print("[自检] 1) 教学语句解析")
    cases = [
        ("我开心的时候你就做这个", ("emotion", "joy")),
        ("你难过的时候也做这个", ("emotion", "sadness")),
        ("记住：我说『加油』的时候你就做这个", ("keyword", "加油")),
        ("以后晚上就比这个", ("time", None)),
        ("今天天气不错", None),
    ]
    for text, expect in cases:
        r = parse_teaching(text)
        ok = (r is None) == (expect is None)
        if r and expect:
            ok = r["meta"] is not None and r["meta"].get("type") == expect[0]
            if expect[1]:
                ok = ok and (r["meta"].get("dim") == expect[1] or r["meta"].get("text") == expect[1])
        print(f"   {'PASS' if ok else 'FAIL'}  {text!r} -> {r}")
    print("[自检] 2) 教学规则写入/读取")
    ok, msg = add_skill("__selftest__", "测试", "测试回复",
                        action={"kind": "gesture", "value": 7},
                        trigger_meta={"type": "emotion", "dim": "joy", "th": 0.5})
    print(f"   add: {ok} :: {msg}")
    rules = taught_rules()
    print(f"   taught_rules 条数: {len(rules)}")
    from custom_skills import remove_skill
    ok2, msg2 = remove_skill("__selftest__")
    print(f"   cleanup: {ok2} :: {msg2}")
    print("[自检] 3) 口型映射")
    for rms in (0.0, 0.01, 0.05, 0.2):
        print(f"   rms={rms} -> viseme {viseme_from_rms(rms)}")
    print("[自检] 完成")


def main():
    ap = argparse.ArgumentParser(description="小念 ⇄ VRChat OSC 桥")
    ap.add_argument("--selftest", action="store_true", help="离线自检（教学解析+规则库）")
    ap.add_argument("--no-asr", action="store_true", help="关闭麦克风识别")
    ap.add_argument("--no-tts", action="store_true", help="关闭语音输出")
    ap.add_argument("--in-port", type=int, default=None, help=f"你的实例OSC输出口(默认{OSC_IN_PORT})")
    ap.add_argument("--out-port", type=int, default=None, help=f"她实例的OSC输入口(默认{OSC_OUT_PORT})")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    try:
        import pythonosc  # noqa: F401
    except ImportError:
        print("[错误] 缺少 python-osc：请先安装\n"
              "    venv\\Scripts\\python.exe -m pip install python-osc\n"
              "之后运行：venv\\Scripts\\python.exe -m src.osc_bridge")
        sys.exit(1)

    bridge = OscBridge(args)
    if not bridge.setup():
        sys.exit(1)
    bridge.start()
    print("[桥] 运行中…… Ctrl+C 退出。控制台直接打字=对小念说话；/skills 查看她学过的行为。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[桥] 再见～")


if __name__ == "__main__":
    main()
