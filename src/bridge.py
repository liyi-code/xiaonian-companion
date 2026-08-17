"""小念 ⇄ 3D 游戏引擎的事件桥（引擎无关，纯 Python）。

设计目标：把「大脑」(assistant/emotion/clayer/memory/voice) 与「身体」(渲染端)
彻底解耦。本文件**不依赖任何游戏引擎**，只做一件事：
    1. 监听 WebSocket，接收游戏端发来的用户输入 / 指令；
    2. 调用小念大脑生成回复、合成语音；
    3. 把结果以标准化 JSON 事件推回游戏端。

游戏端（Unity / Godot / Unreal …）只要会 WebSocket + 解析 JSON，就能驱动任意
3D 模型。事件协议见 README。

运行：
    venv\\Scripts\\python.exe -m src.bridge            # 默认 ws://127.0.0.1:8765
    venv\\Scripts\\python.exe -m src.bridge --port 9001 --host 0.0.0.0

依赖：pip install websockets
"""

import argparse
import asyncio
import base64
import json
import sys
import threading
import os
import time

# 让 src 目录可被 import（无论从哪启动）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from config import CONFIG  # 项目配置（读 .env）

try:
    import websockets
except ImportError:
    print("[桥] 缺少依赖 websockets，请先：venv\\Scripts\\python.exe -m pip install websockets")
    raise

# 默认动作意图码（Unity 端需实现同名的动画/状态机触发）
DEFAULT_ACTIONS = [
    "[ACT_IDLE]",       # 待机
    "[ACT_LOOKAROUND]", # 环顾（小幅左右看）
    "[ACT_TURN]",       # 转身（整圈 180° 面向身后）
    "[ACT_STAND]",      # 立正（回正直立姿态）
    "[ACT_WAVE]",       # 招手
    "[ACT_SIT]",        # 坐下
    "[ACT_WALK]",       # 随意走动
    "[ACT_FOLLOW]",     # 跟随玩家
    "[ACT_RUN]",        # 奔跑
    "[ACT_POINT]",      # 指向某处
]

# --------------------------------------------------------------------------- #
# 构造小念大脑（与 gui.py 启动逻辑保持一致，只是不建任何 tkinter/界面）
# --------------------------------------------------------------------------- #
def build_assistant():
    """构建 Assistant 及其依赖（emotion / autonomy / tts），与 GUI 启动同构。"""
    autonomy = None
    try:
        from autonomy import Autonomy
        # Autonomy 构造需要一个带 append/log 的宿主；用最小桩对象即可
        autonomy = Autonomy(_HostStub())
    except Exception as e:
        print(f"[桥] 自主权限未启动：{e}")

    emotion = None
    try:
        from emotion import EmotionEngine
        emotion = EmotionEngine(_HostStub())
    except Exception as e:
        print(f"[桥] 情感系统未启动：{e}")

    from assistant import Assistant
    assistant = Assistant(autonomy=autonomy, emotion=emotion)

    # 语音输出端（仅合成+交字节，不在本机播放）
    from voice import TTS
    tts = TTS(
        enabled=CONFIG.get("voice_output_enabled", False),
        url=CONFIG.get("sovits_url", "http://127.0.0.1:9880"),
        ref_audio=CONFIG.get("sovits_ref_audio", ""),
        ref_text=CONFIG.get("sovits_ref_text", ""),
        speed=CONFIG.get("sovits_speed", 1.0),
        volume=CONFIG.get("sovits_volume", 1.0),
        if_sr=CONFIG.get("sovits_if_sr", False),
        sample_steps=CONFIG.get("sovits_sample_steps", 8),
        output_device=CONFIG.get("tts_output_device", ""),
    )
    return assistant, emotion, tts


class _HostStub:
    """给 Autonomy/EmotionEngine 用的极简宿主桩：它们只调用 append/log 之类。"""
    def append(self, *a, **k):
        pass
    def log(self, *a, **k):
        pass
    def __getattr__(self, name):
        # 任何其它属性/方法都返回 no-op，避免崩溃
        def _noop(*a, **k):
            return None
        return _noop


# --------------------------------------------------------------------------- #
# 极简动作识别（与 gui._detect_action 思路一致，仅取最常用的几条）
# 游戏端据此触发动画状态机；没有对应动画也不影响对话。
# --------------------------------------------------------------------------- #
# 动作关键词 → Unity 端 Animator 的 ACT_* 触发器名（与 NpcController.controller 对齐）
_ACTION_KEYWORDS = [
    # 打招呼/告别统一触发挥手（含常见问候语，避免"你好"也点头）
    ("ACT_WAVE", ("挥手", "招手", "拜拜", "再见", "嗨", "哈喽", "你好", "您好",
                  "早上好", "晚上好", "中午好", "下午好", "晚安", "在吗", "在干嘛",
                  "打招呼", "摇手", "拜", "see you")),
    ("ACT_RUN", ("跳", "蹦", "跳起来", "跑", "奔跑", "冲")),
    ("ACT_TURN", ("转身", "转过去", "转个身", "转过去背对我")),
    ("ACT_LOOKAROUND", ("环顾", "看四周", "左右看", "东张西望")),
    ("ACT_POINT", ("指", "指向", "那边", "这里", "看那里")),
    ("ACT_SIT", ("坐", "坐下", "休息一下", "歇会儿")),
    ("ACT_STAND", ("立正", "站好", "站直", "别动", "停下", "停住")),
    ("ACT_FOLLOW", ("跟", "跟着我", "过来", "跟上来")),
]


def detect_action(text):
    if not text:
        return None
    for name, kws in _ACTION_KEYWORDS:
        if any(k in text for k in kws):
            return name
    return None


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def compute_action_params(brain):
    """把「情感(joy 兴奋度) × 性格(trait 肢体偏好)」融合成动作参数。

    返回 dict：speed / amplitude / trait / lean / micro / desc。
    - 情感 joy 决定基础快慢大小（开心→更快更大）；
    - 性格乘率再塑形（活泼→更轻快大幅、敏感→更慢更小…）；
    两者都进 Unity，让小念的肢体风格随性格+当下心情一起变。
    """
    joy = 0.0
    trait = "温柔平静"
    mp = None
    emo = getattr(brain, "emotion", None)
    if emo is not None:
        # emo 可能是 EmotionEngine 实例，也可能是裸情绪 dict，两种都兼容
        if hasattr(emo, "motion_params"):
            # EmotionEngine：joy 在其内部 emotion 字典里
            try:
                joy = float(getattr(emo, "emotion", {}).get("joy", 0.0))
            except Exception:
                joy = 0.0
            try:
                mp = emo.motion_params()
            except Exception:
                mp = None
        else:
            # 裸 dict：直接当情绪读取
            try:
                joy = float(emo.get("joy", 0.0))
            except Exception:
                joy = 0.0
    joy = _clamp(joy, 0.0, 1.0)
    if mp is None:
        from emotion import motion_params_for
        mp = motion_params_for(trait)
        mp["trait"] = trait

    # 情感基础：0.3 慢悠悠 ~ 1.0 兴奋（与原逻辑一致）
    base_speed = 0.3 + joy * 0.7
    base_amp = 0.7 + joy * 0.3
    # 性格塑形
    speed = round(_clamp(base_speed * mp.get("speed_mul", 1.0), 0.2, 1.5), 2)
    amplitude = round(_clamp(base_amp * mp.get("amplitude_mul", 1.0), 0.5, 1.4), 2)
    return {
        "speed": speed,
        "amplitude": amplitude,
        "trait": mp.get("trait", trait),
        "lean": round(float(mp.get("lean", 0.0)), 3),
        "micro": list(mp.get("micro", []) or []),
        "desc": mp.get("desc", ""),
    }


# --------------------------------------------------------------------------- #
# 事件桥
# --------------------------------------------------------------------------- #
class NPCBrain:
    """单个 NPC 的全部「大脑」组分，彼此独立（多 NPC 时互不串台）。"""
    def __init__(self, npc_id, name, assistant, emotion, tts, broadcast):
        self.npc_id = npc_id
        self.name = name
        self.assistant = assistant
        self.emotion = emotion
        self.tts = tts
        self.broadcast = broadcast


class GameBridge:
    def __init__(self, host="127.0.0.1", port=8765, auto_default=True):
        self.host = host
        self.port = port
        self._clients = set()          # 已连接的 websocket
        self._lock = threading.RLock()  # spawn_npc 与 _broadcast 可能同线程嵌套调用
        self._loop = None               # 运行中的事件循环（连接时缓存）
        self.npcs = {}                  # npc_id -> NPCBrain
        # —— 动作教学间：等待 Unity 回执的录制配对（教学句 → 动作clip）——
        self._pending_capture = None    # {trigger_text, meta, ts}
        # 默认 NPC：Unity 连上即可对话（未显式 spawn 时用它兜底）
        if auto_default:
            self.spawn_npc("xiaonian", CONFIG.get("name", "小念"))

    # ----- NPC 生命周期 -----
    def spawn_npc(self, npc_id, name=None):
        with self._lock:
            if npc_id in self.npcs:
                return self.npcs[npc_id]
            assistant, emotion, tts = build_assistant()
            brain = NPCBrain(npc_id, name or npc_id, assistant, emotion, tts,
                             self._broadcast)
            # 把动作意图码注册进意识层，供自发动作选择使用
            if assistant.mind is not None:
                try:
                    assistant.mind.register_actions(DEFAULT_ACTIONS)
                    print(f"[桥] {npc_id} 已注册 {len(DEFAULT_ACTIONS)} 个动作概念")
                except Exception as _e:
                    print(f"[桥] {npc_id} 动作概念注册失败：{_e}")
            self.npcs[npc_id] = brain
            return brain

    def despawn_npc(self, npc_id):
        with self._lock:
            brain = self.npcs.pop(npc_id, None)
        return brain is not None

    # ----- 事件发送：线程安全的“推到主事件循环再 send” -----
    @staticmethod
    async def _safe_send(ws, data):
        try:
            await ws.send(data)
            # 仅对 action 类消息做确认日志，避免刷屏
            try:
                _d = json.loads(data)
                if _d.get("type") in ("action", "action_intent"):
                    print(f"[桥] [_safe_send] 已发出 {_d.get('type')}:{_d.get('name', _d.get('action'))}", flush=True)
            except Exception:
                pass
        except Exception as _e:
            print(f"[桥] [_safe_send] 发送失败: {_e}", flush=True)

    def _push(self, ws, msg: dict):
        # 注意：回调可能在 executor 线程里触发，那里没有“当前 loop”，
        # 必须用连接时缓存的运行中 loop 引用来调度。
        loop = self._loop
        if loop is None:
            return
        data = json.dumps(msg, ensure_ascii=False)
        # 用线程安全的方式调度协程，比 call_soon_threadsafe(ensure_future, ...) 更可靠
        try:
            asyncio.run_coroutine_threadsafe(self._safe_send(ws, data), loop)
        except Exception as _e:
            print(f"[桥] _push 调度失败: {_e}", flush=True)

    def _broadcast(self, msg: dict):
        with self._lock:
            for ws in list(self._clients):
                try:
                    self._push(ws, msg)
                except Exception:
                    pass

    async def _restlessness_heartbeat(self, ws, npc_id):
        """周期性下发躁动度，驱动 Unity 叠加层（呼吸/转头频率）。
        无聊时低，长时间无对话→略升（期待/等待感）。聊天时重置计时。"""
        import time as _time
        self._last_chat_by_npc = getattr(self, "_last_chat_by_npc", {})
        try:
            while True:
                await asyncio.sleep(4.0)
                brain = self.npcs.get(npc_id)
                base = 0.2
                last = self._last_chat_by_npc.get(npc_id, _time.time())
                try:
                    joy = float(getattr(brain, "emotion", {}).get("joy", 0.0)) if brain else 0.0
                    idle = min(1.0, (_time.time() - last) / 60.0)  # 60s 内从 0→1
                    rest = max(0.0, min(1.0, base + joy * 0.4 + idle * 0.4))
                except Exception:
                    rest = base
                self._push(ws, {"type": "restlessness",
                                "value": round(rest, 2), "npc_id": npc_id})
        except asyncio.CancelledError:
            pass

    # ----- 处理一条用户输入（在 executor 线程里跑，避免阻塞事件循环）-----
    def _handle_user_input(self, ws, text, npc_id="default"):
        text = (text or "").strip()
        print(f"[桥] [_handle_user_input] npc_id={npc_id} raw_text={text!r}", flush=True)
        if not text:
            print(f"[桥] [_handle_user_input] 输入为空，直接返回", flush=True)
            return
        print(f"[桥] 收到来自 {npc_id} 的输入: {text[:80]}", flush=True)
        brain = self.npcs.get(npc_id) or self.spawn_npc(npc_id)
        # 聊天发生：重置躁动度计时（刚聊完不显得“等得着急”）
        try:
            import time as _t
            self._last_chat_by_npc = getattr(self, "_last_chat_by_npc", {})
            self._last_chat_by_npc[npc_id] = _t.time()
        except Exception:
            pass

        # 先给 Unity 一个即时的肢体反馈：
        # - 命中动作关键词（打招呼/再见/转身/过来…）→ 对应动作
        # - 未命中（普通聊天）→ 发轻量“点头/反应”(ACT_NOD)，不再一律挥手，
        #   避免“不管说什么都挥手”的怪异感；角色靠说话律动表达其余情绪
        # 动作带 speed/amplitude/lean/trait：由「情感(joy) × 性格(trait)」融合，
        # 让小念的肢体风格随性格与当下心情一起变（见 emotion.TRAIT_MOTION）。
        quick_action = detect_action(text)
        if not quick_action:
            quick_action = "ACT_NOD"
        ap = compute_action_params(brain)
        print(f"[桥] [{npc_id}] 先下发即时动作: {quick_action} "
              f"(trait={ap['trait']} joy→speed={ap['speed']} amp={ap['amplitude']})", flush=True)
        self._push(ws, {"type": "action", "name": quick_action,
                        "duration": 1.5, "speed": ap["speed"],
                        "amplitude": ap["amplitude"], "lean": ap["lean"],
                        "trait": ap["trait"], "npc_id": npc_id})

        # 流式文本回调：小念每吐一个片段就实时推给游戏端（气泡/字幕）
        token_count = [0]

        def on_token(piece):
            token_count[0] += 1
            self._push(ws, {"type": "token", "text": piece, "npc_id": npc_id})

        def on_tool(name, args, result):
            self._push(ws, {"type": "tool", "name": name,
                            "result": str(result)[:500], "npc_id": npc_id})

        # 意识层涟漪：think() 产出的多念竞争结果，实时推给游戏端驱动行为/可视化
        # 意识层「多念竞争」快照：主念概念 + 多道并行念（注意力份额）
        # 通过 on_conscious 在 think() 后实时拿到 ConsciousState（见 assistant._chat）
        def on_conscious(state):
            try:
                concepts = []
                # 主念 chosen：概念名 + 选取概率
                for cname, prob in (getattr(state, "chosen", []) or []):
                    concepts.append({"name": cname, "weight": round(float(prob), 3),
                                     "primary": True})
                # 并行竞争的多道念（每道一个主概念 + 注意力份额）
                for th in (getattr(state, "thoughts", []) or []):
                    if not getattr(th, "is_primary", False):
                        c0 = th.concepts[0] if th.concepts else None
                        if c0:
                            concepts.append({"name": c0, "weight": round(float(th.attention), 3),
                                             "primary": False})
                if concepts:
                    self._push(ws, {"type": "concepts", "items": concepts,
                                    "entropy": round(float(getattr(state, "entropy", 0.0)), 3),
                                    "npc_id": npc_id})
            except Exception:
                pass

        import time as _time
        t0 = _time.time()
        print(f"[桥] [{npc_id}] 收到输入，开始生成…", flush=True)
        try:
            reply = brain.assistant.chat(text, on_tool=on_tool, on_token=on_token,
                                          on_conscious=on_conscious)
        except Exception as e:
            print(f"[桥] [{npc_id}] chat 异常：{e}", flush=True)
            self._push(ws, {"type": "chat", "text": f"（出错了：{e}）", "npc_id": npc_id})
            return
        t1 = _time.time()
        print(f"[桥] [{npc_id}] LLM 回复完成，耗时 {t1 - t0:.2f}s：{reply[:40]!r}", flush=True)

        if not reply:
            print(f"[桥] [{npc_id}] 回复为空，不继续 TTS/动作", flush=True)
            return

        # 下发完整文字（Unity 用 chat 类型显示气泡/字幕）
        self._push(ws, {"type": "chat", "npc_id": npc_id, "text": str(reply)})

        # 当前情绪 5 维 → 表情状态（游戏端据此驱动 Blendshape）
        # 推完整 5 维权重（英文维度名），供 Unity ExpressionController 直接映射
        emotion_vec = None
        dom = None
        if brain.emotion is not None:
            try:
                ev = brain.emotion.emotion  # {joy,anger,sadness,calm,anxiety} 已 clamp[0,1]
                emotion_vec = {k: round(float(ev.get(k, 0.0)), 3) for k in
                               ("joy", "anger", "sadness", "calm", "anxiety")}
                dom = brain.emotion.dominant()
            except Exception:
                emotion_vec = None
                dom = None

        # 动作识别 → 动画触发
        # 命中关键词用对应动作；否则按「性格 micro 倾向 → 情绪」给默认回应动作，
        # 确保“对输入有反应”且动作类型也随性格变（黏人倾向靠近、傲娇倾向别过脸…）。
        action = detect_action(reply)
        if not action:
            micro = []
            try:
                micro = brain.emotion.motion_params().get("micro", []) or []
            except Exception:
                pass
            if micro:                       # 性格自带的招牌小动作优先
                action = micro[0]
            elif dom == "joy":
                action = "ACT_WAVE"
            else:
                action = "ACT_LOOKAROUND"
        print(f"[桥] [{npc_id}] 决定动作: {action} (dom={dom})", flush=True)
        # 按字数估算动作/口型持续时间：中文约 4 字/秒，受 TTS 语速影响
        speed = float(getattr(brain.tts, "speed", 1.0) or 1.0)
        action_duration = min(max(len(reply.strip()) * 0.25 / max(speed, 0.1), 1.5), 8.0)

        # 语音合成：把每句 wav 字节推给游戏端播放（本机不发音）
        def on_play():
            # 开始说话：同时下发情绪(表情)与动作
            if emotion_vec:
                self._push(ws, {"type": "emotion", "vector": emotion_vec,
                                "dominant": dom, "npc_id": npc_id})
            if action:
                # 说话动作也带「情感×性格」融合参数，让肢体风格随性格变
                _ap = compute_action_params(brain)
                self._push(ws, {"type": "action", "name": action,
                                "duration": round(action_duration, 2),
                                "speed": _ap["speed"], "amplitude": _ap["amplitude"],
                                "lean": _ap["lean"], "trait": _ap["trait"],
                                "npc_id": npc_id})
            self._push(ws, {"type": "speech_start", "npc_id": npc_id})

        def on_audio(wav_bytes):
            b64 = base64.b64encode(wav_bytes).decode("ascii")
            self._push(ws, {"type": "audio", "wav": b64, "npc_id": npc_id})

        t2 = _time.time()
        if brain.tts.is_ready():
            print(f"[桥] [{npc_id}] 开始 TTS（语速={speed}，预估动作时长={action_duration:.1f}s）",
                  flush=True)
            brain.tts.speak(reply, on_play=on_play, on_audio=on_audio)
            self._push(ws, {"type": "talk_stop", "npc_id": npc_id})
            t3 = _time.time()
            print(f"[桥] [{npc_id}] TTS/动作 总耗时 {t3 - t2:.2f}s，token 共 {token_count[0]} 个",
                  flush=True)
        else:
            # 没配语音：仅文字（仍给情绪/动作，让肢体有反应）
            if emotion_vec:
                self._push(ws, {"type": "emotion", "vector": emotion_vec,
                                "dominant": dom, "npc_id": npc_id})
            if action:
                _ap = compute_action_params(brain)
                self._push(ws, {"type": "action", "name": action,
                                "duration": round(action_duration, 2),
                                "speed": _ap["speed"], "amplitude": _ap["amplitude"],
                                "lean": _ap["lean"], "trait": _ap["trait"],
                                "npc_id": npc_id})
            print(f"[桥] [{npc_id}] 无 TTS，已推文字/动作，token 共 {token_count[0]} 个",
                  flush=True)

    # ----- WebSocket 连接处理 -----
    async def _on_connect(self, ws):
        # 缓存当前运行中的事件循环，供 _push 在 executor 线程里安全调度
        self._loop = asyncio.get_running_loop()
        with self._lock:
            self._clients.add(ws)
        # 连接成功日志（第一关自查用：Unity 连上时此处必须打印 Client connected）
        peer = getattr(ws, "remote_address", None)
        print(f"[桥] Client connected（当前连接数={len(self._clients)}，来自 {peer}）",
              flush=True)
        # ready 事件携带当前所有 NPC 列表，Unity 据此实例化 Agent
        with self._lock:
            npc_list = [{"npc_id": k, "name": v.name,
                         "voice_ready": v.tts.is_ready()}
                        for k, v in self.npcs.items()]
        self._push(ws, {"type": "ready",
                        "name": CONFIG.get("name", "小念"),
                        "npcs": npc_list})
        # 躁动度(restlessness)心跳：每 4 秒下发一次，驱动 Unity 叠加层
        # （呼吸频率/转头频率）。无聊→低，长时间未对话→偏高期待。
        # 连接建立时还不知道后续消息里的 npc_id，先取当前第一个 NPC 作为默认。
        with self._lock:
            default_npc_id = next(iter(self.npcs.keys()), "default")
        rest_task = asyncio.ensure_future(self._restlessness_heartbeat(ws, default_npc_id))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")
                npc_id = msg.get("npc_id") or "default"
                if mtype == "user_input":
                    print(f"[桥] [ws] 收到 user_input npc_id={npc_id} text={msg.get('text','')[:40]!r}",
                          flush=True)
                    # 在 executor 线程跑（chat 是阻塞的）
                    loop = asyncio.get_event_loop()
                    loop.run_in_executor(
                        None, self._handle_user_input, ws, msg.get("text", ""), npc_id
                    )
                elif mtype == "ping":
                    self._push(ws, {"type": "pong"})
                # ---- NPC 生命周期（多 NPC 支持）----
                elif mtype == "spawn_npc":
                    nid = msg.get("npc_id") or msg.get("id")
                    nm = msg.get("name") or nid
                    if nid:
                        self.spawn_npc(nid, nm)
                        self._push(ws, {"type": "npc_spawned", "npc_id": nid,
                                        "name": nm})
                elif mtype == "despawn_npc":
                    nid = msg.get("npc_id") or msg.get("id")
                    if nid and nid != "default":
                        ok = self.despawn_npc(nid)
                        self._push(ws, {"type": "npc_despawned", "npc_id": nid,
                                        "ok": ok})
                # ---- 动作教学间：Unity 录制回执（与教学句配对入库）----
                elif mtype == "capture_result":
                    self._handle_capture_result(msg)
                # ---- 动作教学间：请求 Unity 录制最近 N 秒动作 ----
                elif mtype == "teach_capture":
                    self._pending_capture = {
                        "trigger_text": str(msg.get("trigger_text") or "").strip(),
                        "meta": msg.get("meta"),
                        "ts": time.time(),
                    }
                    seconds = float(msg.get("seconds", 10) or 10)
                    self._broadcast({"type": "capture_action",
                                     "seconds": seconds})
                    print(f"[桥] 教学录制：请求 Unity 截取最近 {seconds:.0f}s 动作", flush=True)
                # ---- 动作库：播放学到的动画 ----
                elif mtype == "play_action":
                    self.play_action_clip(
                        msg.get("action") or msg.get("action_id") or "",
                        npc_id=npc_id)
                # ---- 动作库：列出已学动作 ----
                elif mtype == "list_actions":
                    self._push(ws, {"type": "chat", "npc_id": npc_id,
                                    "text": self.format_action_library()})
                # ---- 3D 环境刺激（Unity Raycast 接近 / 玩家行为）----
                elif mtype == "stimuli":
                    # {"stimuli":["玩家接近","社交","晚上","椅子"], "weight":0.9}
                    brain = self.npcs.get(npc_id) or self.spawn_npc(npc_id)
                    stimuli = [s for s in (msg.get("stimuli", []) or []) if s]
                    # 灌进意识层作为概念种子，让环境与动作建立联想
                    if stimuli and brain.assistant.mind is not None:
                        try:
                            text = " ".join(stimuli)
                            brain.assistant.mind.learn_async(text)
                        except Exception:
                            pass

                # ---- 自发动作：Unity 空闲时请求，意识层纯图计算、不调用 LLM ----
                elif mtype == "get_spontaneous_action":
                    # {"context":["晚上","椅子","安静"], "threshold":0.15}
                    brain = self.npcs.get(npc_id) or self.spawn_npc(npc_id)
                    if brain.assistant.mind is None:
                        self._push(ws, {"type": "action_intent", "npc_id": npc_id,
                                        "action": "[ACT_IDLE]", "prob": 0.0})
                    else:
                        ctx = [c for c in (msg.get("context", []) or []) if c]
                        action, prob = brain.assistant.mind.spontaneous_action(
                            ctx, action_prefix="[ACT_"
                        )
                        threshold = float(msg.get("threshold", 0.15) or 0.15)
                        if action is None or prob < threshold:
                            action = "[ACT_IDLE]"
                            prob = 0.0
                        # 自发动作也带「情感×性格」融合的 speed/amplitude/trait/lean
                        _ap = compute_action_params(brain)
                        self._push(ws, {"type": "action_intent", "npc_id": npc_id,
                                        "action": action, "prob": round(prob, 3),
                                        "context": ctx,
                                        "duration": round(min(max(2.5, prob * 6.0), 5.0), 2),
                                        "speed": _ap["speed"], "amplitude": _ap["amplitude"],
                                        "lean": _ap["lean"], "trait": _ap["trait"]})

                # ---- 动作执行反馈：成功强化、失败弱化（避免对着空气重复） ----
                elif mtype == "action_feedback":
                    # {"action":"[ACT_SIT]", "context":["晚上","椅子"], "success":false}
                    brain = self.npcs.get(npc_id)
                    if brain is not None and brain.assistant.mind is not None:
                        try:
                            action = msg.get("action", "")
                            ctx = [c for c in (msg.get("context", []) or []) if c]
                            success = bool(msg.get("success", True))
                            brain.assistant.mind.reinforce_action(
                                action, ctx, success=success
                            )
                            tag = "成功" if success else "失败"
                            print(f"[桥] {npc_id} 动作反馈 {tag}: {action} <- {ctx}")
                        except Exception as _e:
                            print(f"[桥] 动作反馈处理失败：{_e}")

                # ---- 优雅关闭：触发记忆整合，避免进程僵尸 ----
                elif mtype == "shutdown":
                    brain = self.npcs.get(npc_id)
                    if brain is not None and brain.assistant.mind is not None:
                        try:
                            brain.assistant.mind.consolidate_memory()
                            brain.assistant.mind.save()
                            print(f"[桥] NPC {npc_id} 记忆已整合并保存。")
                        except Exception as _e:
                            print(f"[桥] NPC {npc_id} 记忆整合失败：{_e}")
                # 其它类型可在此扩展（如 mind_sleep / set_api ...）
        except Exception:
            pass
        finally:
            with self._lock:
                self._clients.discard(ws)

    # ------------------------------------------------------------------ #
    # 动作教学间（动作库闭环：录制回执配对 / 播放 / 列表）
    # ------------------------------------------------------------------ #
    def _handle_capture_result(self, msg):
        """Unity 录完动作回执：{clip_path, duration, name}。
        与最近的 _pending_capture（教学句）配对写进动作库。"""
        import action_library as al
        clip = str(msg.get("clip_path") or "").strip()
        if not clip:
            print("[桥] capture_result 缺少 clip_path", flush=True)
            return
        dur = float(msg.get("duration", 0) or 0)
        name = (str(msg.get("name") or "").strip()
                or os.path.basename(clip).rsplit(".", 1)[0].replace("_", " "))
        pend = self._pending_capture
        if pend and pend["ts"] and (time.time() - pend["ts"]) < 120 and pend["trigger_text"]:
            aid = al.add_action(name, clip, duration=dur, source="unity_taught")
            ok, m = al.add_prompt(aid, pend["trigger_text"], meta=pend["meta"])
            self._broadcast({"type": "chat", "npc_id": "xiaonian", "text": m})
            print(f"[桥] 动作入库：{clip} ← 「{pend['trigger_text']}」（{m}）", flush=True)
        else:
            al.add_action(name, clip, duration=dur, source="unity_taught")
            self._broadcast({"type": "chat", "npc_id": "xiaonian",
                             "text": f"动作「{name}」已入库～告诉我什么时候用它吧"})
            print(f"[桥] 动作入库（未配对）：{clip}", flush=True)
        self._pending_capture = None

    def play_action_clip(self, name_or_id, npc_id="xiaonian"):
        """按名字/ID 播放动作库里学到的动画（广播给 Unity 播放）。"""
        import action_library as al
        a = al.find_action(name_or_id)
        if a is None:
            print(f"[桥] 动作库没有「{name_or_id}」（控制台 /actions 查看）", flush=True)
            return
        self._broadcast({"type": "play_clip", "npc_id": npc_id,
                         "clip_path": a["clip"], "duration": a["duration"]})
        print(f"[桥] 播放动作：{a['name']} ({a['clip']})", flush=True)

    def format_action_library(self):
        import action_library as al
        actions = al.list_actions()
        if not actions:
            return "动作库还是空的～在 Unity 里按 C 录一个动作，或说「我XX的时候你就做这个」教我。"
        lines = [f"【动作库 · {len(actions)} 个动作】"]
        for a in actions:
            ps = "、".join(f"「{p['text']}」({p['strength']:.2f})" for p in a["prompts"]) or "（待配触发条件）"
            lines.append(f"- {a['name']} [{a['id']}] {a['duration']:.1f}s → {ps}")
        return "\n".join(lines)

    def _console_loop(self):
        """控制台测试命令：/actions 列表 /capture <秒> <触发词> /play <动作名>。"""
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                return
            line = (line or "").strip()
            if not line:
                continue
            if line.startswith("/actions"):
                print(self.format_action_library(), flush=True)
            elif line.startswith("/capture"):
                parts = line.split(" ", 2)
                sec = float(parts[1]) if len(parts) > 1 and parts[1].replace(".", "").isdigit() else 10.0
                trig = parts[2] if len(parts) > 2 else ""
                from osc_bridge import parse_teaching
                parsed = parse_teaching(trig) if trig else None
                self._pending_capture = {
                    "trigger_text": (parsed or {}).get("trigger_text", trig),
                    "meta": (parsed or {}).get("meta"),
                    "ts": time.time(),
                }
                self._broadcast({"type": "capture_action", "seconds": sec})
                print(f"[桥] 请求录制最近 {sec:.0f}s（触发词：{trig or '无'}）", flush=True)
            elif line.startswith("/play"):
                name = line[5:].strip()
                self.play_action_clip(name)
            else:
                print("[桥] 未知命令。可用：/actions  /capture <秒> [触发词]  /play <动作名>", flush=True)

    def run(self):
        print(f"[桥] 小念事件桥(多NPC)启动：ws://{self.host}:{self.port}")
        print(f"[桥] 已就绪 NPC：{list(self.npcs.keys())}")
        print("[桥] 等待 3D 游戏端连接……（Ctrl+C 退出）")
        # 控制台测试命令（动作库闭环：/actions /capture /play）
        threading.Thread(target=self._console_loop, daemon=True).start()

        # 端口绑定：默认端口被占用时自动顺延，避免直接崩溃退出。
        host, port = self.host, self.port
        import socket as _socket
        while True:
            _s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            try:
                _s.bind((host, port))
                _s.close()
                break  # 端口可用
            except OSError:
                _s.close()
                if port == self.port:
                    print(f"[桥] 警告：端口 {port} 已被占用（可能有旧 bridge 未退出），"
                          f"自动顺延到 {port + 1} …")
                port += 1
                if port > self.port + 100:
                    raise RuntimeError("找不到可用端口（已尝试 100 个）")
        self.port = port  # 回写实际端口，供日志/客户端读取

        async def _serve_forever():
            # 注意：不能直接 asyncio.run(websockets.serve(...))——那样 serve 协程
            # 一返回事件循环就关闭，服务器“启动即退出”。必须挂起等待。
            async with websockets.serve(self._on_connect, host, port):
                await asyncio.Future()   # 永久挂起，直到 Ctrl+C

        print(f"[桥] 实际监听地址：ws://{host}:{port}")
        try:
            asyncio.run(_serve_forever())
        except KeyboardInterrupt:
            print("\n[桥] 已停止。")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="小念 ⇄ 3D 游戏事件桥(多NPC)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    bridge = GameBridge(host=args.host, port=args.port)
    bridge.run()


if __name__ == "__main__":
    main()
