# -*- coding: utf-8 -*-
"""
小镇（自给自足村庄）经济与生存模拟器 —— 一个「我的世界村庄」式的多人小镇。

设计目标（对应需求「类似我的世界村庄的小镇，可以自给自足的多人小镇」）：
  - 小镇由若干「职业村民」组成（农夫 / 樵夫 / 矿工 / 厨师 / 商人 …），每个职业
    对应 bridge.py 里的一个 NPCBrain；村民各自产资源、按需求网络互相交换。
  - 资源种类：小麦 / 木头 / 石头 / 铁矿 / 食物（成品）/ 工具 等，构成一条
    「采集 → 加工 → 消费」的生存链条。
  - 自给自足判定：当某种关键资源跌破安全线，TownSim 自动给对应职业的村民派发
    一条「采集/生产」任务（经 quest_update 下行），玩家也可以接这些任务一起共建。
  - 完全引擎无关：只维护一份数值状态 + 周期性 tick；把状态/事件广播给 Unity。
    Unity 端 TownView.cs 负责把数值渲染成村庄面板、把建筑/村民位置画出来。

事件协议（本模块发出，经 bridge 的 broadcast 带上 npc_id 或直接全局）：
  [Python -> Unity]  town_state -> {type:"town_state", resources:{...}, needs:{...},
                                    self_sufficient:bool, villagers:[...], buildings:[...]}
  [Python -> Unity]  town_task  -> {type:"town_task", npc_id, role, task_id, title,
                                    desc, objective:{kind,target}, reward}
  [Unity -> Python]  town_contribute -> {npc_id, resource, amount}   玩家/村民上缴资源
  [Unity -> Python]  town_event      -> {npc_id, kind, object_id}    村民完成生产事件

本模块被 bridge.GameBridge 持有（一个全局 TownSim 实例），所有 NPC 共享同一座小镇。
"""

from __future__ import annotations
import threading
import time
import random

# --------------------------------------------------------------------------- #
# 职业定义：每个职业产什么、缺什么、缺时派什么任务
# --------------------------------------------------------------------------- #
# product : 周期性 tick 自动产出的资源
# consumes: 该职业维持生产所需的其他资源（用于需求网络/自给自足判定也靠它）
# deficit_task: 当 product 短缺时派发的采集任务模板
ROLES = {
    "farmer": {
        "name": "农夫",
        "building": "farm",
        "product": "wheat",          # 小麦
        "produces": 2,               # 每个 tick 产量
        "consumes": {"tool": 0.1},   # 种地要工具
        "deficit_task": {
            "id": "t_plant_wheat", "title": "补种小麦",
            "desc": "农田的小麦存量告急，去田里补种并照料。",
            "target": "farm_field", "reward": "小镇粮食储备 +1 天",
        },
    },
    "lumberjack": {
        "name": "樵夫",
        "building": "lumber_mill",
        "product": "wood",           # 木头
        "produces": 3,
        "consumes": {"tool": 0.15},
        "deficit_task": {
            "id": "t_chop_wood", "title": "进山伐木",
            "desc": "仓库木头不够了，去林场多砍些木头回来。",
            "target": "forest", "reward": "获得建筑用木材",
        },
    },
    "miner": {
        "name": "矿工",
        "building": "mine",
        "product": "stone",          # 石头
        "produces": 2,
        "consumes": {"tool": 0.2, "food": 0.3},
        "deficit_task": {
            "id": "t_mine_stone", "title": "下矿采石",
            "desc": "石料见底，下矿洞采一批石头（顺便留意铁矿）。",
            "target": "mine_shaft", "reward": "解锁石质建筑",
        },
    },
    "cook": {
        "name": "厨师",
        "building": "kitchen",
        "product": "food",           # 成品食物（喂养全小镇）
        "produces": 2,
        "consumes": {"wheat": 1.0, "wood": 0.5},  # 用粮+柴做饭
        "deficit_task": {
            "id": "t_cook_meal", "title": "备餐",
            "desc": "粮仓有粮但灶火未起，生火给大家做饭。",
            "target": "kitchen_stove", "reward": "全体饱食度回升",
        },
    },
    "merchant": {
        "name": "商人",
        "building": "market",
        "product": "tool",           # 工具（用木头+石头打造，供其他职业消耗）
        "produces": 1,
        "consumes": {"wood": 1.0, "stone": 0.5},
        "deficit_task": {
            "id": "t_forge_tool", "title": "打造工具",
            "desc": "工具库紧张，用木料和石料打造几件新工具。",
            "target": "market_forge", "reward": "生产链恢复运转",
        },
    },
    "smith": {
        "name": "铁匠",
        "building": "forge",
        "product": "iron",           # 铁矿（高级资源）
        "produces": 1,
        "consumes": {"tool": 0.1, "food": 0.2},
        "deficit_task": {
            "id": "t_mine_iron", "title": "冶炼铁矿",
            "desc": "铁矿存量低，开炉冶炼一批铁矿。",
            "target": "forge_furnace", "reward": "解锁铁制工具",
        },
    },
}

# 资源中文名（用于下行展示）
RESOURCE_NAMES = {
    "wheat": "小麦", "wood": "木头", "stone": "石头",
    "iron": "铁矿", "food": "食物", "tool": "工具",
}

# 各种资源的安全线（低于此值视为短缺，触发对应职业任务）
SAFE_LINE = {
    "wheat": 10, "wood": 12, "stone": 10,
    "iron": 4, "food": 12, "tool": 4,
}

# 建筑布局（六分街式商业街：x=±6.5 两侧店铺，z 沿街道纵向；与 unity_client/unity_skills/build_sixth_street.py
# 和 unity_project Assets/Scripts/TownLayout.cs 严格对齐）
BUILDINGS = {
    "farm":        {"name": "农田",   "pos": {"x": -6.5, "y": 0, "z": 8}},
    "lumber_mill": {"name": "伐木场", "pos": {"x": 6.5,  "y": 0, "z": 8}},
    "mine":        {"name": "矿洞",   "pos": {"x": -6.5, "y": 0, "z": -8}},
    "kitchen":     {"name": "厨房",   "pos": {"x": -6.5, "y": 0, "z": 0}},
    "market":      {"name": "市集",   "pos": {"x": 6.5,  "y": 0, "z": 0}},
    "forge":       {"name": "铁匠铺", "pos": {"x": 6.5,  "y": 0, "z": -8}},
    "well":        {"name": "水井",   "pos": {"x": 0,    "y": 0, "z": 0}},
}


class TownSim:
    """小镇模拟器：维护资源、村民、建筑、需求网络，周期性 tick 推进自给自足。"""

    def __init__(self, emit, tick_sec: float = 5.0):
        """
        emit: callable(msg: dict) —— 把 town_state / town_task 广播给 Unity
        tick_sec: 模拟步进周期（秒）
        """
        self.emit = emit
        self.tick_sec = tick_sec
        self._lock = threading.RLock()  # _broadcast_state / snapshot 内部需要重入读取

        # 资源储备
        self.resources = {k: SAFE_LINE[k] * 2 for k in SAFE_LINE}
        # 村民登记：npc_id -> role
        self.villagers = {}
        # 已派发但未完成的任务
        self._open_tasks = set()
        self._stop = threading.Event()
        self._thread = None
        self.day = 1

    # ------------------------------------------------------------------ #
    # NPC 注册（由 bridge 在 spawn_npc 时按角色登记进小镇）
    # ------------------------------------------------------------------ #
    def register_villager(self, npc_id: str, role: str):
        if role not in ROLES:
            return False
        with self._lock:
            self.villagers[npc_id] = role
        self._broadcast_state()
        return True

    def unregister_villager(self, npc_id: str):
        with self._lock:
            self.villagers.pop(npc_id, None)

    # ------------------------------------------------------------------ #
    # 玩家/村民上缴资源（town_contribute）
    # ------------------------------------------------------------------ #
    def contribute(self, npc_id: str, resource: str, amount: float):
        if resource not in self.resources:
            return
        with self._lock:
            self.resources[resource] = round(self.resources[resource] + max(0.0, amount), 2)
            # 上缴对应任务目标后，清掉相关未完成任务
            role = self.villagers.get(npc_id)
            if role and ROLES[role]["product"] == resource:
                self._open_tasks.discard(ROLES[role]["deficit_task"]["id"])
        self._broadcast_state()

    # ------------------------------------------------------------------ #
    # 村民完成生产事件（town_event）—— 也可由探索到达建筑目标触发
    # ------------------------------------------------------------------ #
    def on_villager_event(self, npc_id: str, kind: str, object_id: str = None):
        role = self.villagers.get(npc_id)
        if not role:
            return
        rdef = ROLES[role]
        # 到达/交互对应建筑 → 补一批该产品
        if object_id == rdef["deficit_task"]["target"] or object_id == rdef["building"]:
            with self._lock:
                gain = rdef["produces"] * 3
                self.resources[rdef["product"]] = round(
                    self.resources[rdef["product"]] + gain, 2)
                self._open_tasks.discard(rdef["deficit_task"]["id"])
            self._broadcast_state()

    # ------------------------------------------------------------------ #
    # 自给自足判定
    # ------------------------------------------------------------------ #
    def is_self_sufficient(self) -> bool:
        with self._lock:
            return all(self.resources[k] >= SAFE_LINE[k] for k in SAFE_LINE)

    # ------------------------------------------------------------------ #
    # 周期 tick：生产 + 消耗 + 短缺派任务
    # ------------------------------------------------------------------ #
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        last = time.time()
        while not self._stop.is_set():
            time.sleep(self.tick_sec)
            if self._stop.is_set():
                break
            self._tick()
            # 每 12 个 tick 算一天（仅用于展示）
            if time.time() - last > self.tick_sec * 12:
                last = time.time()
                with self._lock:
                    self.day += 1

    def _tick(self):
        with self._lock:
            # 1) 每个村民按职业产出
            for npc_id, role in self.villagers.items():
                rdef = ROLES[role]
                # 消耗品不足则减产
                ok = all(self.resources.get(c, 0) >= amt
                         for c, amt in rdef["consumes"].items())
                if ok:
                    self.resources[rdef["product"]] = round(
                        self.resources[rdef["product"]] + rdef["produces"], 2)
                    # 扣掉消耗
                    for c, amt in rdef["consumes"].items():
                        self.resources[c] = round(self.resources[c] - amt, 2)
                else:
                    # 缺料：给该职业派采集任务（若还没派）
                    self._maybe_issue_task(role)

            # 2) 全小镇每日食物消耗（人口越多吃得越快）
            pop = max(1, len(self.villagers))
            self.resources["food"] = round(self.resources["food"] - pop * 0.5, 2)
            if self.resources["food"] < SAFE_LINE["food"]:
                self._maybe_issue_task("cook")

            # 3) 兜底：任何资源跌破安全线，给对应职业派任务
            for res, line in SAFE_LINE.items():
                if self.resources[res] < line:
                    role = self._role_of_product(res)
                    if role:
                        self._maybe_issue_task(role)

        self._broadcast_state()

    def _role_of_product(self, product: str):
        for role, rdef in ROLES.items():
            if rdef["product"] == product:
                return role
        return None

    def _maybe_issue_task(self, role: str):
        tpl = ROLES[role]["deficit_task"]
        if tpl["id"] in self._open_tasks:
            return
        self._open_tasks.add(tpl["id"])
        # 派给该职业对应的某个村民（没有则广播给全体）
        targets = [nid for nid, r in self.villagers.items() if r == role]
        npc_id = targets[0] if targets else "default"
        self.emit({
            "type": "town_task",
            "npc_id": npc_id,
            "role": role,
            "task_id": tpl["id"],
            "title": tpl["title"],
            "desc": tpl["desc"],
            "objective": {"kind": "interact", "target": tpl["target"]},
            "reward": tpl["reward"],
        })

    # ------------------------------------------------------------------ #
    # 下行：状态广播
    # ------------------------------------------------------------------ #
    def _broadcast_state(self):
        with self._lock:
            villagers = [
                {"npc_id": nid, "role": role,
                 "role_name": ROLES[role]["name"],
                 "building": ROLES[role]["building"]}
                for nid, role in self.villagers.items()
            ]
            buildings = [
                {"id": bid, "name": b["name"],
                 "pos": b["pos"], "role": self._building_role(bid)}
                for bid, b in BUILDINGS.items()
            ]
            msg = {
                "type": "town_state",
                "day": self.day,
                "resources": {RESOURCE_NAMES[k]: round(v, 1)
                              for k, v in self.resources.items()},
                "raw_resources": dict(self.resources),
                "self_sufficient": all(self.resources[k] >= SAFE_LINE[k] for k in SAFE_LINE),
                "villagers": villagers,
                "buildings": buildings,
            }
        try:
            self.emit(msg)
        except Exception:
            pass

    def _building_role(self, bid: str):
        for role, rdef in ROLES.items():
            if rdef["building"] == bid:
                return role
        return ""

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "day": self.day,
                "resources": dict(self.resources),
                "self_sufficient": all(self.resources[k] >= SAFE_LINE[k] for k in SAFE_LINE),
                "villagers": dict(self.villagers),
            }
