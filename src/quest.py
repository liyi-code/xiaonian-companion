# -*- coding: utf-8 -*-
"""
小念 ⇄ 3D 游戏的任务系统（Python 端，引擎无关）。

设计目标：
  - 任务由「对话意图」或「探索发现」触发，进度由 Unity 上报的事件驱动；
  - 每个 NPC 拥有独立的 QuestManager（与 multiplayer_memory 的隔离哲学一致）；
  - 任务状态通过下行事件 quest_update 推给 Unity，由 QuestSystem.cs 呈现；
  - 纯规则驱动 + 可选 LLM 生成描述，不依赖任何游戏引擎。

事件协议：
  [Python -> Unity]  quest_update -> {npc_id, quest_id, title, desc, state, objectives:[{text,done}], reward}
                      state: offered | active | completed | rewarded
  [Unity -> Python]  quest_event -> {npc_id, kind, object_id?, pos?, npc_id_from?}
                      kind: reach | interact | talk | custom
"""

from __future__ import annotations
import time
import threading
import uuid


# 简单任务模板池（可按需扩展；之后也可由 LLM 动态生成）
_QUEST_TEMPLATES = [
    {
        "id": "q_explore_shrine",
        "title": "探秘北边神社",
        "desc": "小念说北边有座发光的小神社，想请你去看看是什么。",
        "objectives": [{"kind": "reach", "target": "shrine_north", "desc": "前往北边神社"}],
        "reward": "小念的好感 +1",
    },
    {
        "id": "q_find_chest",
        "title": "打开神秘宝箱",
        "desc": "小念注意到一个没开过的宝箱，想请你打开看看。",
        "objectives": [{"kind": "interact", "target": "chest_01", "desc": "打开神秘宝箱"}],
        "reward": "一枚小念手作的护符",
    },
    {
        "id": "q_meet_friend",
        "title": "去见见新朋友",
        "desc": "小念想让你去和那边的人聊聊。",
        "objectives": [{"kind": "talk", "target": "npc_lynn", "desc": "与 Lynn 对话一次"}],
        "reward": "解锁双人剧情",
    },
]


class QuestManager:
    """单个 NPC 的任务管理（线程安全）。"""

    def __init__(self, npc_id, emit):
        """
        npc_id: 所属 NPC 标识（下行事件携带）
        emit:   callable(msg: dict) —— 把 quest_update 推给 Unity
        """
        self.npc_id = npc_id
        self.emit = emit
        self._lock = threading.Lock()
        self.active = {}          # quest_id -> quest 运行时（含 progress）
        self._offered_cnt = 0     # 已派发任务计数（避免刷屏）

    # ---------------- 触发入口 ----------------
    def maybe_offer_from_dialogue(self, text):
        """玩家对话中可能触发接任务（简单关键词规则）。"""
        if not text:
            return
        lowered = text.lower()
        if any(k in text for k in ("任务", "帮忙", "找", "带我去", "看看", "quest", "help")):
            # 轮流派发模板，避免重复派同一个
            tpl = _QUEST_TEMPLATES[self._offered_cnt % len(_QUEST_TEMPLATES)]
            self._offered_cnt += 1
            self.offer(tpl)

    def maybe_offer_from_explore(self, obj):
        """探索发现 type=='quest' 的物体时触发对应任务。"""
        if not obj:
            return
        if obj.get("type") == "quest":
            tpl = _QUEST_TEMPLATES[1]  # chest 那一条最贴合
            self.offer(tpl)

    # ---------------- 任务生命周期 ----------------
    def offer(self, tpl):
        with self._lock:
            qid = tpl["id"]
            if qid in self.active:
                return  # 已存在，不重复派
            now = time.time()
            quest = {
                "id": qid,
                "title": tpl["title"],
                "desc": tpl["desc"],
                "reward": tpl.get("reward", ""),
                "objectives": [
                    {"kind": o["kind"], "target": o.get("target", ""),
                     "desc": o.get("desc", ""), "done": False}
                    for o in tpl["objectives"]
                ],
                "state": "offered",
                "created": now,
            }
            self.active[qid] = quest
        self._emit_quest(quest)

    def on_event(self, kind, object_id=None, pos=None, npc_id_from=None):
        """Unity 上报的进度事件。"""
        with self._lock:
            for quest in self.active.values():
                if quest["state"] in ("completed", "rewarded"):
                    continue
                changed = False
                for obj in quest["objectives"]:
                    if obj["done"]:
                        continue
                    if obj["kind"] == kind and (not obj["target"]
                                                 or obj["target"] == object_id
                                                 or obj["target"] == npc_id_from):
                        obj["done"] = True
                        changed = True
                if changed:
                    quest["state"] = "active"
                    if all(o["done"] for o in quest["objectives"]):
                        quest["state"] = "completed"
                self._emit_quest(quest)

    def claim_reward(self, quest_id):
        with self._lock:
            quest = self.active.get(quest_id)
            if quest and quest["state"] == "completed":
                quest["state"] = "rewarded"
                self._emit_quest(quest)
                return quest.get("reward", "")
        return ""

    # ---------------- 下行 ----------------
    def _emit_quest(self, quest):
        try:
            self.emit({
                "type": "quest_update",
                "npc_id": self.npc_id,
                "quest_id": quest["id"],
                "title": quest["title"],
                "desc": quest["desc"],
                "reward": quest["reward"],
                "state": quest["state"],
                "objectives": [{"text": o["desc"], "done": o["done"]}
                               for o in quest["objectives"]],
            })
        except Exception:
            pass

    def snapshot(self):
        with self._lock:
            return [
                {"id": q["id"], "title": q["title"], "state": q["state"]}
                for q in self.active.values()
            ]
