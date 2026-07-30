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
class GameBridge:
    def __init__(self, assistant, emotion, tts, host="127.0.0.1", port=8765):
        self.assistant = assistant
        self.emotion = emotion
        self.tts = tts
        self.host = host
        self.port = port
        self._clients = set()          # 已连接的 websocket
        self._lock = threading.Lock()
        self._loop = None               # 运行中的事件循环（连接时缓存）
        # —— 3D 世界感知 + 主动探索 ——
        self.world_state = SymbolicWorldState()   # 已加载范围内的符号工作记忆
        self.explorer = AutonomousExplorer(
            self.world_state, assistant, self._broadcast
        )

    # ----- 事件发送：线程安全的“推到主事件循环再 send” -----
    def _push(self, ws, msg: dict):
        # 注意：回调可能在 executor 线程里触发，那里没有“当前 loop”，
        # 必须用连接时缓存的运行中 loop 引用来调度。
        loop = self._loop
        if loop is None:
            return
        data = json.dumps(msg, ensure_ascii=False)
        loop.call_soon_threadsafe(asyncio.ensure_future, ws.send(data))

    def _broadcast(self, msg: dict):
        with self._lock:
            for ws in list(self._clients):
                try:
                    self._push(ws, msg)
                except Exception:
                    pass

    # ----- 处理一条用户输入（在 executor 线程里跑，避免阻塞事件循环）-----
    def _handle_user_input(self, ws, text):
        text = (text or "").strip()
        if not text:
            return

        # 流式文本回调：小念每吐一个片段就实时推给游戏端（气泡/字幕）
        def on_token(piece):
            self._push(ws, {"type": "token", "text": piece})

        def on_tool(name, args, result):
            self._push(ws, {"type": "tool", "name": name,
                            "result": str(result)[:500]})

        try:
            reply = self.assistant.chat(text, on_tool=on_tool, on_token=on_token)
        except Exception as e:
            self._push(ws, {"type": "token", "text": f"（出错了：{e}）"})
            return

        if not reply:
            return

        # 当前主导情绪 → 表情状态（游戏端据此选面部 Blendshape）
        dom = None
        if self.emotion is not None:
            try:
                dom = self.emotion.dominant()
            except Exception:
                dom = None

        # 动作识别 → 动画触发
        action = detect_action(reply)

        # 语音合成：把每句 wav 字节推给游戏端播放（本机不发音）
        def on_play():
            # 开始说话：同时下发情绪(表情)与动作
            if dom:
                self._push(ws, {"type": "emotion", "dominant": dom})
            if action:
                self._push(ws, {"type": "action", "name": action})
            self._push(ws, {"type": "speech_start"})

        def on_audio(wav_bytes):
            b64 = base64.b64encode(wav_bytes).decode("ascii")
            self._push(ws, {"type": "audio", "wav": b64})

        if self.tts.is_ready():
            self.tts.speak(reply, on_play=on_play, on_audio=on_audio)
            self._push(ws, {"type": "talk_stop"})
        else:
            # 没配语音：仅文字（仍给情绪/动作，让肢体有反应）
            if dom:
                self._push(ws, {"type": "emotion", "dominant": dom})
            if action:
                self._push(ws, {"type": "action", "name": action})

    # ----- 低频视觉快照：结合符号感知做「符号+像素」联合推理 -----
    def _handle_visual_snapshot(self, msg):
        """收到 Unity 推来的 1080p 视觉快照(base64)，做视觉推理并融合符号感知。

        - 把 base64 解码成图片，按 world_vision_max_width 压缩（降 token/延迟）；
        - 视觉 prompt 里注入「当前已加载范围内的符号感知文本」，实现“结合符号感知推理”；
        - 推理结果回填 world_state.on_vision（供意识层联想 + 下一次 prompt 注入）；
        - 整个过程不参与、也不阻塞玩家对话。
        """
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
        symbolic = self.world_state.snapshot_text()
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
            self.world_state.on_vision(desc)
            # 视觉洞察也回写意识层（与符号感知一起长进联想图）
            # 注意：learn 需要 think 产生的 ConsciousState，必须先 think 再 learn
            mind = getattr(self.assistant, "mind", None)
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
        self._push(ws, {"type": "ready",
                        "name": CONFIG.get("name", "小念"),
                        "voice_ready": self.tts.is_ready()})
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")
                if mtype == "user_input":
                    # 在 executor 线程跑（chat 是阻塞的）
                    loop = asyncio.get_event_loop()
                    loop.run_in_executor(
                        None, self._handle_user_input, ws, msg.get("text", "")
                    )
                elif mtype == "ping":
                    self._push(ws, {"type": "pong"})
                # ---- 3D 世界感知事件（来自 Unity 符号感知脚本）----
                elif mtype == "world_load":
                    # 区域(预加载)加载/卸载：{region_id, loaded}
                    self.world_state.on_region(
                        msg.get("region_id", ""), bool(msg.get("loaded", False))
                    )
                elif mtype == "symbolic_percept":
                    # 符号感知批量更新：{agent_pos, objects:[...]}（无图像）
                    self.world_state.on_percept(msg)
                elif mtype == "visual_snapshot":
                    # 低频 1080p 视觉快照：{cam_pos, image_b64}（base64 jpg/png）
                    # 视觉推理较重，放 executor 线程，避免阻塞事件循环
                    loop = asyncio.get_event_loop()
                    loop.run_in_executor(
                        None, self._handle_visual_snapshot, msg
                    )
                # 其它类型可在此扩展（如 mind_sleep / set_api ...）
        except Exception:
            pass
        finally:
            with self._lock:
                self._clients.discard(ws)

    def run(self):
        print(f"[桥] 小念事件桥启动：ws://{self.host}:{self.port}")
        print("[桥] 等待 3D 游戏端连接……（Ctrl+C 退出）")
        # 启动主动探索引擎（后台线程，非被动等待玩家）
        if CONFIG.get("world_autonomy_enabled", True):
            self.explorer.start()
            print("[桥] 主动探索引擎已启动（符号感知 + 意识层驱动）")

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
            self.explorer.stop()


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="小念 ⇄ 3D 游戏事件桥")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    assistant, emotion, tts = build_assistant()
    bridge = GameBridge(assistant, emotion, tts, host=args.host, port=args.port)
    # 让小念对话时也能“感知”到 3D 世界的符号环境
    assistant.set_world_context_provider(lambda: bridge.world_state.snapshot_text())
    bridge.run()


if __name__ == "__main__":
    main()
