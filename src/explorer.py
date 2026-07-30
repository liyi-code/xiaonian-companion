# -*- coding: utf-8 -*-
"""
小念在 3D 世界里的「主动探索」引擎（后台线程，非被动等待玩家交互）。

它是需求里“自主环境感知 / 主动探索世界 / 建立在意识模型上”的执行体：

1. 周期性读 world_state 的「已加载范围内符号感知」；
2. 把符号文本喂给意识层(clayer)做 think —— 意识层据此产生联想/价值/好奇信号
   （这是“建立在意识模型上”的硬连接，不是简单的 if/else 巡逻）；
3. 结合「新颖度 + 类型兴趣 + 距离」打分，挑一个已加载范围内、兴趣半径内的目标；
4. 通过 emit 回调向 Unity 下发 agent_command（move / look / interact），
   让小念自己走过去、看、互动 —— 真正的“主动”；
5. 把有意思的符号感知回写意识层 learn_async，让世界知识长进联想图
   （下次 think 就能“联想”起这些地方/物体）。

不会抢玩家对话：explorer 只在«没有玩家正在对话»时积极行动；玩家一开口它就退居幕后。
"""

from __future__ import annotations
import threading
import time
import random
import hashlib

from config import CONFIG


class AutonomousExplorer:
    def __init__(self, world_state, assistant, emit):
        """
        world_state: SymbolicWorldState 实例
        assistant:   小念 Assistant（提供 .mind 意识层 与 .memory）
        emit:        callable(cmd: dict) —— 把 agent_command / agent_thought 广播给 Unity
        """
        self.ws = world_state
        self.assistant = assistant
        self.emit = emit
        self._stop = threading.Event()
        self._thread = None
        self._moving_until = 0.0          # 估计移动到此时间戳才“到达”
        self._pending_interact = None     # (obj_id, at_ts)
        self._last_learn_hash = ""
        self._last_wander = 0.0
        self._idle_rounds = 0
        self._assistant_mod = None   # 懒加载，读 _user_chat_active 全局，判断玩家是否在对话

    def _is_user_chatting(self) -> bool:
        # 与 screen_feedback 同策略：玩家正在对话时，探索让位
        if self._assistant_mod is None:
            try:
                import assistant as _m
                self._assistant_mod = _m
            except Exception:
                return False
        try:
            return bool(getattr(self._assistant_mod, "_user_chat_active", False))
        except Exception:
            return False

    # ----- 生命周期 -----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ----- 主循环：低频、主动 -----
    def _loop(self):
        interval = max(1.0, float(CONFIG.get("world_explore_interval", 3.0)))
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._stop.wait(interval)

    def _tick(self):
        # 总开关
        if not CONFIG.get("world_autonomy_enabled", True):
            return
        # 玩家正在对话 → 让位（与 screen_feedback 同策略，避免抢 Ollama/思考槽）
        if self._is_user_chatting():
            return
        # 没有已加载区域 → 啥也感知不到，休眠
        if not self.ws.has_loaded():
            return

        # 1) 当前符号感知文本（已加载范围 + 视觉快照文字）
        text = self.ws.snapshot_text()
        # 2) 喂意识层：think 产生联想/价值/好奇；learn 把世界知识长进联想图
        mind = getattr(self.assistant, "mind", None)
        if mind is not None:
            state = None
            try:
                # think 不改图、不存盘，安全；产生“这一刻意识状态”
                state = mind.think(text)
            except Exception:
                state = None
            # 仅在符号态有变化(有新东西可学)时回写，避免刷屏式 learn/save
            # 注意：learn 必须带 think 产生的 ConsciousState，否则 TypeError 静默失效
            h = hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()
            if state is not None and h != self._last_learn_hash:
                self._last_learn_hash = h
                try:
                    mind.learn_async(text, "", state)   # 把世界符号感知长进联想图
                except Exception:
                    pass

        # 3) 处理“到达后交互”的待办
        now = time.time()
        if self._pending_interact and now >= self._pending_interact[1]:
            oid, _ = self._pending_interact
            self._pending_interact = None
            self.emit({"type": "agent_command", "action": "interact",
                       "object_id": oid})
            self.emit({"type": "agent_thought",
                       "text": "我凑近看了看，感觉挺有意思的～"})

        # 4) 还没走到上一次目标 → 不发新指令，等到达
        if now < self._moving_until:
            return

        # 5) 选下一个探索目标
        radius = float(CONFIG.get("world_interest_radius", 12.0))
        targets = self.ws.interesting_targets(radius)
        best = targets[0] if targets else None
        if best is not None and best[1] > 0.15:
            obj, score = best
            self.ws.mark_seen(obj["id"])
            self.ws.mark_visited(obj["id"])
            self.emit({"type": "agent_command", "action": "move",
                       "target": obj["pos"], "object_id": obj["id"]})
            # 顺带把镜头转向目标（Unity 可借此抓一帧视觉快照）
            self.emit({"type": "agent_command", "action": "look",
                       "target": obj["pos"]})
            # 估算到达时间，到了再交互
            d = self._dist(self.ws.agent_pos, obj["pos"])
            speed = max(0.5, float(CONFIG.get("world_move_speed", 2.5)))
            self._moving_until = now + max(0.8, d / speed) + 0.3
            if obj["type"] in _INTERACTABLE:
                self._pending_interact = (obj["id"], self._moving_until)
            self._idle_rounds = 0
            # 内心独白（不念出声，Unity 用“想法气泡”呈现）
            self.emit({"type": "agent_thought",
                       "text": f"那个「{obj['name']}」看起来有点意思，我去看看～"})
            return

        # 6) 没有有意思的目标 → 偶尔随意踱步，制造“她在自己活动”的感觉
        self._idle_rounds += 1
        if now - self._last_wander > 12.0:
            self._last_wander = now
            self._idle_rounds = 0
            self.ws.reset_visited()   # 重置访问标记，让旧地方重新有“新颖度”
            self.emit({"type": "agent_command", "action": "wander"})
            self._moving_until = now + 3.0

    @staticmethod
    def _dist(a, b):
        if not a or not b:
            return 1e9
        try:
            return ((a.get("x", 0) - b.get("x", 0)) ** 2
                    + (a.get("y", 0) - b.get("y", 0)) ** 2
                    + (a.get("z", 0) - b.get("z", 0)) ** 2) ** 0.5
        except Exception:
            return 1e9


_INTERACTABLE = {
    "chest", "loot", "npc", "character", "door", "gate", "lever", "switch",
    "machine", "terminal", "artifact", "shrine", "quest", "book", "note",
}
