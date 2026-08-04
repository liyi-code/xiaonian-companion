"""小念 ⇄ 3D 游戏引擎的事件桥（引擎无关，纯 Python）。

设计目标：把「大脑」(assistant/emotion/clayer/memory/voice) 与「身体」(渲染端)
彻底解耦。本文件**不依赖任何游戏引擎**，只做一件事：
    1. 监听 WebSocket，接收游戏端发来的用户输入 / 指令；
    2. 调用小念大脑生成回复、合成语音；
    3. 把结果以标准化 JSON 事件推回游戏端。

游戏端（Unity / Godot / Unreal …）只要会 WebSocket + 解析 JSON，就能驱动任意
3D 模型。事件协议见文件底部 EVENT 说明，或 README。

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

# 3D 世界感知 + 主动探索（建立在意识模型之上）
from world_state import SymbolicWorldState
from explorer import AutonomousExplorer

# 小镇（我的世界村庄式自给自足小镇）：经济模拟 + 预置村民职业
from town import TownSim
from villagers import default_spawns, preset_by_id

# 默认动作意图码（Unity 端需实现同名的动画/状态机触发）
DEFAULT_ACTIONS = [
    "[ACT_IDLE]",       # 待机
    "[ACT_LOOKAROUND]", # 环顾
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
def build_assistant(persona: str = None):
    """构建 Assistant 及其依赖（emotion / autonomy / tts），与 GUI 启动同构。

    persona: 可选角色设定文本，注入该系统提示前缀，用于村民职业人格。
    """
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

    # 注入村民职业人格（不影响通用大脑链路）
    if persona:
        try:
            assistant.set_persona(persona)
        except Exception:
            pass

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
_ACTION_KEYWORDS = [
    ("jump", ("跳", "蹦", "跳起来")),
    ("turn", ("转身", "转过去", "转个身")),
    ("wave", ("挥手", "招手", "嗨")),
    ("pat", ("摸摸头", "摸头", "拍拍")),
    ("nod", ("点头", "嗯嗯")),
]


def detect_action(text):
    if not text:
        return None
    for name, kws in _ACTION_KEYWORDS:
        if any(k in text for k in kws):
            return name
    return None


# --------------------------------------------------------------------------- #
# 事件桥
# --------------------------------------------------------------------------- #
class NPCBrain:
    """单个 NPC 的全部「大脑 + 世界 + 探索 + 任务」组分，彼此独立（隔离哲学见 multiplayer_memory）。"""
    def __init__(self, npc_id, name, assistant, emotion, tts, broadcast):
        self.npc_id = npc_id
        self.name = name
        self.assistant = assistant
        self.emotion = emotion
        self.tts = tts
        # —— 3D 世界感知 + 主动探索（每个 NPC 一份）——
        self.world_state = SymbolicWorldState()
        # explorer 的 emit 自动带 npc_id 再广播，Unity 才能路由到对应 Agent
        self.explorer = AutonomousExplorer(
            self.world_state, assistant,
            lambda m: broadcast(dict(m, npc_id=npc_id)),
        )
        # 任务系统
        from quest import QuestManager
        self.quests = QuestManager(npc_id, lambda m: broadcast(dict(m, npc_id=npc_id)))


class GameBridge:
    def __init__(self, host="127.0.0.1", port=8765, auto_default=True):
        self.host = host
        self.port = port
        self._clients = set()          # 已连接的 websocket
        self._lock = threading.RLock()  # spawn_npc 与 _broadcast 可能同线程嵌套调用
        self._loop = None               # 运行中的事件循环（连接时缓存）
        self.npcs = {}                  # npc_id -> NPCBrain
        # —— 小镇（我的世界村庄式自给自足小镇）：全局唯一，所有 NPC 共享 ——
        self.town = TownSim(self._broadcast)
        # 向后兼容：没有显式 spawn 时，默认存在一个 "default" NPC（即原来的单 NPC 行为）
        if auto_default:
            self.spawn_npc("default", CONFIG.get("name", "小念"))
        # 自动按预设 spawn 村民，构成完整生存链的「多人小镇」
        if CONFIG.get("town_auto_spawn", True):
            for p in default_spawns():
                self.spawn_npc(p["npc_id"], p["name"], role=p["role"],
                               persona=p.get("persona"))

    # ----- NPC 生命周期 -----
    def spawn_npc(self, npc_id, name=None, role=None, persona=None):
        with self._lock:
            if npc_id in self.npcs:
                return self.npcs[npc_id]
            assistant, emotion, tts = build_assistant(persona=persona)
            brain = NPCBrain(npc_id, name or npc_id, assistant, emotion, tts,
                             self._broadcast)
            # 让该 NPC 对话时也能“感知”3D 世界符号环境
            try:
                assistant.set_world_context_provider(
                    lambda: brain.world_state.snapshot_text())
            except Exception:
                pass
            # 把动作意图码注册进意识层，供自发动作选择使用
            if assistant.mind is not None:
                try:
                    assistant.mind.register_actions(DEFAULT_ACTIONS)
                    print(f"[桥] {npc_id} 已注册 {len(DEFAULT_ACTIONS)} 个动作概念")
                except Exception as _e:
                    print(f"[桥] {npc_id} 动作概念注册失败：{_e}")
            self.npcs[npc_id] = brain
            # 注册进小镇（按职业参与生产/需求网络）
            if role:
                self.town.register_villager(npc_id, role)
            return brain

    def despawn_npc(self, npc_id):
        with self._lock:
            brain = self.npcs.pop(npc_id, None)
        if brain is not None:
            try:
                brain.explorer.stop()
            except Exception:
                pass
        return brain is not None

    # ----- 事件发送：线程安全的“推到主事件循环再 send” -----
    @staticmethod
    async def _safe_send(ws, data):
        try:
            await ws.send(data)
        except Exception:
            # 连接已关闭等：静默丢弃，不刷异常噪声
            pass

    def _push(self, ws, msg: dict):
        # 注意：回调可能在 executor 线程里触发，那里没有“当前 loop”，
        # 必须用连接时缓存的运行中 loop 引用来调度。
        loop = self._loop
        if loop is None:
            return
        data = json.dumps(msg, ensure_ascii=False)
        loop.call_soon_threadsafe(asyncio.ensure_future, self._safe_send(ws, data))

    def _broadcast(self, msg: dict):
        with self._lock:
            for ws in list(self._clients):
                try:
                    self._push(ws, msg)
                except Exception:
                    pass

    # ----- 处理一条用户输入（在 executor 线程里跑，避免阻塞事件循环）-----
    def _handle_user_input(self, ws, text, npc_id="default"):
        text = (text or "").strip()
        if not text:
            return
        print(f"[桥] 收到来自 {npc_id} 的输入: {text[:80]}", flush=True)
        brain = self.npcs.get(npc_id) or self.spawn_npc(npc_id)

        # 流式文本回调：小念每吐一个片段就实时推给游戏端（气泡/字幕）
        def on_token(piece):
            self._push(ws, {"type": "token", "text": piece, "npc_id": npc_id})

        def on_tool(name, args, result):
            self._push(ws, {"type": "tool", "name": name,
                            "result": str(result)[:500], "npc_id": npc_id})

        # 意识层涟漪：think() 产出的多念竞争结果，实时推给游戏端驱动行为/可视化
        def on_conscious(state):
            try:
                self._push(ws, {
                    "type": "conscious",
                    "npc_id": npc_id,
                    "winner": state.winner,
                    "picked": list(state.picked),
                    "salient": {k: round(float(v), 3) for k, v in (state.salient or {}).items()},
                })
            except Exception:
                pass

        # 任务：对话可能触发接任务（规则判定，不依赖 LLM 回复，先判避免被 chat 阻塞）
        try:
            brain.quests.maybe_offer_from_dialogue(text)
        except Exception:
            pass

        try:
            reply = brain.assistant.chat(text, on_tool=on_tool, on_token=on_token,
                                          on_conscious=on_conscious)
        except Exception as e:
            self._push(ws, {"type": "chat", "text": f"（出错了：{e}）", "npc_id": npc_id})
            return

        if not reply:
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
        action = detect_action(reply)

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

        # 语音合成：把每句 wav 字节推给游戏端播放（本机不发音）
        def on_play():
            # 开始说话：同时下发情绪(表情)与动作
            if emotion_vec:
                self._push(ws, {"type": "emotion", "vector": emotion_vec,
                                "dominant": dom, "npc_id": npc_id})
            if action:
                self._push(ws, {"type": "action", "name": action, "npc_id": npc_id})
            self._push(ws, {"type": "speech_start", "npc_id": npc_id})

        def on_audio(wav_bytes):
            b64 = base64.b64encode(wav_bytes).decode("ascii")
            self._push(ws, {"type": "audio", "wav": b64, "npc_id": npc_id})

        if brain.tts.is_ready():
            brain.tts.speak(reply, on_play=on_play, on_audio=on_audio)
            self._push(ws, {"type": "talk_stop", "npc_id": npc_id})
        else:
            # 没配语音：仅文字（仍给情绪/动作，让肢体有反应）
            if emotion_vec:
                self._push(ws, {"type": "emotion", "vector": emotion_vec,
                                "dominant": dom, "npc_id": npc_id})
            if action:
                self._push(ws, {"type": "action", "name": action, "npc_id": npc_id})

    # ----- 低频视觉快照：结合符号感知做「符号+像素」联合推理 -----
    def _handle_visual_snapshot(self, msg, npc_id="default"):
        """收到 Unity 推来的 1080p 视觉快照(base64)，做视觉推理并融合符号感知。

        - 把 base64 解码成图片，按 world_vision_max_width 压缩（降 token/延迟）；
        - 视觉 prompt 里注入「当前已加载范围内的符号感知文本」，实现“结合符号感知推理”；
        - 推理结果回填 world_state.on_vision（供意识层联想 + 下一次 prompt 注入）；
        - 整个过程不参与、也不阻塞玩家对话。
        """
        brain = self.npcs.get(npc_id) or self.spawn_npc(npc_id)
        world_state = brain.world_state
        if not CONFIG.get("world_vision_enabled", True):
            return
        try:
            import vision
            if not vision.is_available():
                return
        except Exception:
            return

        b64 = msg.get("image_b64") or msg.get("image")
        if not b64:
            return
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return

        # 写临时图片（数据目录），必要时压缩
        from PIL import Image
        import io
        d = os.path.join(CONFIG.get("data_dir", "."), "world_watch")
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f"snap_{int(time.time()*1000)}.jpg")
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            max_w = int(CONFIG.get("world_vision_max_width", 1280))
            w, h = img.size
            if w > max_w:
                img = img.resize((max_w, int(h * max_w / w)))
            img.save(tmp, "JPEG", quality=82)
        except Exception:
            # 解码/压缩失败就直接用原始字节落盘
            try:
                with open(tmp, "wb") as f:
                    f.write(raw)
            except Exception:
                return

        # 注入符号感知文本：让视觉推理“结合符号”而不是盲看
        symbolic = world_state.snapshot_text()
        question = (
            "这是小念在 3D 世界里的视角画面。请结合她当前已感知到的符号信息：\n"
            f"{symbolic}\n"
            "描述画面中实际看到的物体、环境样貌，以及是否有值得注意的细节"
            "（颜色/材质/是否发光/是否有文字/是否有人物）。只描述你真实看到的，"
            "不要编造，不超过 80 字。"
        )
        try:
            desc = vision.look(question, image_path=tmp, max_tokens=200)
        except Exception:
            desc = None
        try:
            os.remove(tmp)
        except Exception:
            pass
        if desc:
            world_state.on_vision(desc)
            # 视觉洞察也回写意识层（与符号感知一起长进联想图）
            # 注意：learn 需要 think 产生的 ConsciousState，必须先 think 再 learn
            mind = getattr(brain.assistant, "mind", None)
            if mind is not None:
                try:
                    st = mind.think(desc)
                    mind.learn_async(desc, "", st)
                except Exception:
                    pass

    # ----- WebSocket 连接处理 -----
    async def _on_connect(self, ws):
        # 缓存当前运行中的事件循环，供 _push 在 executor 线程里安全调度
        self._loop = asyncio.get_running_loop()
        with self._lock:
            self._clients.add(ws)
        # ready 事件携带当前所有 NPC 列表，Unity 据此实例化 Agent
        with self._lock:
            npc_list = [{"npc_id": k, "name": v.name,
                         "voice_ready": v.tts.is_ready()}
                        for k, v in self.npcs.items()]
        self._push(ws, {"type": "ready",
                        "name": CONFIG.get("name", "小念"),
                        "npcs": npc_list})
        # 连接即下发当前小镇状态（村庄面板/建筑/村民），Unity 据此渲染村庄
        try:
            self.town._broadcast_state()
        except Exception:
            pass
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")
                npc_id = msg.get("npc_id") or "default"
                if mtype == "user_input":
                    # 在 executor 线程跑（chat 是阻塞的）
                    loop = asyncio.get_event_loop()
                    loop.run_in_executor(
                        None, self._handle_user_input, ws, msg.get("text", ""), npc_id
                    )
                elif mtype == "ping":
                    self._push(ws, {"type": "pong"})
                # ---- 3D 世界感知事件（来自 Unity 符号感知脚本）----
                elif mtype == "world_load":
                    # 区域(预加载)加载/卸载：{region_id, loaded}
                    brain = self.npcs.get(npc_id) or self.spawn_npc(npc_id)
                    brain.world_state.on_region(
                        msg.get("region_id", ""), bool(msg.get("loaded", False))
                    )
                elif mtype == "symbolic_percept":
                    # 符号感知批量更新：{agent_pos, objects:[...]}（无图像）
                    brain = self.npcs.get(npc_id) or self.spawn_npc(npc_id)
                    brain.world_state.on_percept(msg)
                    # 探索发现 type=='quest' 物体时触发对应任务
                    for o in msg.get("objects", []) or []:
                        try:
                            brain.quests.maybe_offer_from_explore(o)
                        except Exception:
                            pass
                elif mtype == "visual_snapshot":
                    # 低频 1080p 视觉快照：{cam_pos, image_b64}（base64 jpg/png）
                    # 视觉推理较重，放 executor 线程，避免阻塞事件循环
                    loop = asyncio.get_event_loop()
                    loop.run_in_executor(
                        None, self._handle_visual_snapshot, msg, npc_id
                    )
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
                # ---- 任务进度事件（来自 Unity）----
                elif mtype == "quest_event":
                    brain = self.npcs.get(npc_id) or self.spawn_npc(npc_id)
                    brain.quests.on_event(
                        msg.get("kind", "custom"),
                        object_id=msg.get("object_id"),
                        npc_id_from=msg.get("npc_id_from"),
                    )
                # ---- 小镇：玩家/村民上缴资源（自给自足建设）----
                elif mtype == "town_contribute":
                    self.town.contribute(
                        msg.get("npc_id") or npc_id,
                        msg.get("resource", ""),
                        float(msg.get("amount", 0) or 0),
                    )
                # ---- 小镇：村民到达/交互建筑，完成一轮生产 ----
                elif mtype == "town_event":
                    self.town.on_villager_event(
                        msg.get("npc_id") or npc_id,
                        msg.get("kind", "interact"),
                        msg.get("object_id"),
                    )
                # ---- 3D 环境刺激（Unity Raycast 接近 / 玩家行为）----
                elif mtype == "stimuli":
                    # {"stimuli":["玩家接近","社交","晚上","椅子"], "weight":0.9}
                    brain = self.npcs.get(npc_id) or self.spawn_npc(npc_id)
                    stimuli = [s for s in (msg.get("stimuli", []) or []) if s]
                    weight = float(msg.get("weight", 0.5) or 0.5)
                    try:
                        brain.world_state.on_stimuli(stimuli, weight)
                    except Exception:
                        pass
                    # 同时灌进意识层作为概念种子，让环境与动作建立联想
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
                        # 也把世界状态里的近处物体名加进上下文，增加 grounded
                        try:
                            snapshot = brain.world_state.snapshot_text()
                        except Exception:
                            snapshot = ""
                        # 从 snapshot 提取物体名作为辅助种子（简单分词）
                        extra = []
                        if snapshot:
                            import re
                            extra = re.findall(r"- ([^（]+)（", snapshot)
                        seeds = ctx + extra[:5]
                        action, prob = brain.assistant.mind.spontaneous_action(
                            seeds, action_prefix="[ACT_"
                        )
                        threshold = float(msg.get("threshold", 0.15) or 0.15)
                        if action is None or prob < threshold:
                            action = "[ACT_IDLE]"
                            prob = 0.0
                        self._push(ws, {"type": "action_intent", "npc_id": npc_id,
                                        "action": action, "prob": round(prob, 3),
                                        "context": seeds})

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

    def run(self):
        print(f"[桥] 小念事件桥(多NPC)启动：ws://{self.host}:{self.port}")
        print(f"[桥] 已就绪 NPC：{list(self.npcs.keys())}")
        print("[桥] 等待 3D 游戏端连接……（Ctrl+C 退出）")
        # 启动所有 NPC 的主动探索引擎（后台线程，非被动等待玩家）
        if CONFIG.get("world_autonomy_enabled", True):
            for nid, brain in self.npcs.items():
                brain.explorer.start()
            print(f"[桥] 主动探索引擎已启动（{len(self.npcs)} 个 NPC，符号感知 + 意识层驱动）")

        # 启动小镇经济模拟（自给自足 tick）
        self.town.start()
        print(f"[桥] 小镇经济模拟已启动（{len(self.town.villagers)} 位村民，目标：自给自足）")

        async def _serve_forever():
            # 注意：不能直接 asyncio.run(websockets.serve(...))——那样 serve 协程
            # 一返回事件循环就关闭，服务器“启动即退出”。必须挂起等待。
            async with websockets.serve(self._on_connect, self.host, self.port):
                await asyncio.Future()   # 永久挂起，直到 Ctrl+C

        try:
            asyncio.run(_serve_forever())
        except KeyboardInterrupt:
            print("\n[桥] 已停止。")
        finally:
            for brain in self.npcs.values():
                try:
                    brain.explorer.stop()
                except Exception:
                    pass


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
