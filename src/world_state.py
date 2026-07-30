# -*- coding: utf-8 -*-
"""
3D 世界「符号感知」工作记忆（小念在游戏里的环境大脑）。

设计要点（对应需求）：
- 预加载 / 只感知已加载范围：维护 loaded_regions 集合；来自未加载区域的符号感知
  一律丢弃——小念和玩家一样，走到哪、加载到哪，只能看到「已加载范围内」的东西。
- 符号感知完全无图像：Unity 脚本遍历场景，把结构化文本(物体名/类型/坐标/状态)推过来，
  本模块只存文本与坐标，从不接触任何像素。
- 低频视觉快照另行融合（见 bridge 的视觉处理），本模块只负责「符号」侧的增删改查 +
  把当前可见符号渲染成文本，供意识层(clayer)做联想、供视觉推理做「结合符号」的注入。
- 主动探索的候选目标也在这里算：基于「未见过的物体(新颖度) + 类型兴趣 + 距离」打分，
  只返回已加载范围内、且在兴趣半径内的目标。

坐标统一用 {x,y,z} 浮点字典；距离用欧氏距离。
"""

from __future__ import annotations
from typing import Dict, List, Optional, Set
import time


# 高兴趣类型：看到就想凑过去看看/互动的物体（可按项目扩展）
_INTERESTING_TYPES = {
    "treasure", "chest", "loot", "npc", "character", "enemy", "monster",
    "door", "gate", "lever", "switch", "machine", "terminal", "artifact",
    "crystal", "shrine", "quest", "sign", "book", "note", "fruit", "flower",
}
# 可交互类型（探索到近前会触发 interact 指令）
_INTERACTABLE_TYPES = {
    "chest", "loot", "npc", "character", "door", "gate", "lever", "switch",
    "machine", "terminal", "artifact", "shrine", "quest", "book", "note",
}


def _dist(a, b) -> float:
    if not a or not b:
        return 1e9
    try:
        return ((a.get("x", 0) - b.get("x", 0)) ** 2
                + (a.get("y", 0) - b.get("y", 0)) ** 2
                + (a.get("z", 0) - b.get("z", 0)) ** 2) ** 0.5
    except Exception:
        return 1e9


class SymbolicWorldState:
    """小念当前「已加载世界」的符号工作记忆。线程安全（bridge 多线程写、explorer 读）。"""

    # 符号感知的“视野保鲜期”：超过这么久没再被感知到的物体，视为离开视野丢弃。
    # Unity 端默认每 0.5s 推一次感知，10s 足够宽裕；防止“走远了物体还永久可见”。
    STALE_AFTER = 10.0

    def __init__(self):
        self._lock = __import__("threading").Lock()
        self.loaded_regions: Set[str] = set()      # 已加载(预加载)的区域 id
        self.objects: Dict[str, dict] = {}          # obj_id -> {id,name,type,pos,state,region,last_seen}
        self.agent_pos: Optional[dict] = None       # 小念自身坐标
        self.last_vision: str = ""                  # 最近一次视觉快照的文字理解
        self.last_vision_ts: float = 0.0
        self.seen_ids: Set[str] = set()             # 曾经见过的物体(新颖度判定)
        self.visited_ids: Set[str] = set()          # 探索时走近过的(避免来回打转)
        self._percept_ts: float = 0.0

    # ----- 区域(预加载) -----
    def on_region(self, region_id: str, loaded: bool):
        if not region_id:
            return
        with self._lock:
            if loaded:
                self.loaded_regions.add(region_id)
            else:
                self.loaded_regions.discard(region_id)
                # 区域卸载：丢弃该区域内所有物体（小念“看不见”了）
                for oid in [oid for oid, o in self.objects.items()
                            if o.get("region") == region_id]:
                    self.objects.pop(oid, None)

    # ----- 符号感知批量更新 -----
    def on_percept(self, batch: dict):
        """批量符号感知：{agent_pos, objects:[...], ts}。只保留已加载区域内的物体。"""
        if not isinstance(batch, dict):
            return
        with self._lock:
            ap = batch.get("agent_pos")
            if isinstance(ap, dict):
                self.agent_pos = ap
            objs = batch.get("objects") or []
            now = time.time()
            for o in objs:
                if not isinstance(o, dict):
                    continue
                oid = o.get("id") or o.get("name")
                if not oid:
                    continue
                region = o.get("region")
                # 关键：只接受「已加载区域」内的感知；未加载区域直接丢弃
                if region and region not in self.loaded_regions:
                    self.objects.pop(oid, None)
                    continue
                rec = {
                    "id": oid,
                    "name": o.get("name", oid),
                    "type": (o.get("type") or "object").lower(),
                    "pos": o.get("pos") or {"x": 0, "y": 0, "z": 0},
                    "state": o.get("state", ""),
                    "region": region or "",
                    "last_seen": now,
                }
                self.objects[oid] = rec
            self._percept_ts = now
            self._purge_stale(now)

    def _purge_stale(self, now: float):
        """（须在持锁状态下调用）丢弃超过保鲜期没再被感知到的物体。"""
        stale = [oid for oid, o in self.objects.items()
                 if now - o.get("last_seen", 0) > self.STALE_AFTER]
        for oid in stale:
            self.objects.pop(oid, None)

    # ----- 视觉快照的文字结果(由 bridge 的视觉推理回填) -----
    def on_vision(self, text: str):
        with self._lock:
            self.last_vision = text or ""
            self.last_vision_ts = time.time()

    # ----- 渲染当前符号态为文本(喂意识层 / 注入视觉 prompt) -----
    def snapshot_text(self) -> str:
        with self._lock:
            self._purge_stale(time.time())
            if not self.objects:
                base = "（当前已加载范围内没有可感知的物体）"
            else:
                lines = []
                for o in sorted(self.objects.values(),
                                key=lambda r: _dist(self.agent_pos, r["pos"])):
                    d = _dist(self.agent_pos, o["pos"])
                    st = f"，状态={o['state']}" if o["state"] else ""
                    lines.append(
                        f"- {o['name']}（类型:{o['type']}，距离约{d:.1f}米{st}）"
                    )
                base = "已加载范围内可见物体：\n" + "\n".join(lines)
            vision_part = ""
            if self.last_vision:
                vision_part = f"\n最近一次视觉观察：{self.last_vision}"
            return base + vision_part

    # ----- 探索候选目标(已加载范围 + 兴趣半径内) -----
    def interesting_targets(self, interest_radius: float) -> List[tuple]:
        """返回 [(obj, score)]，按分数降序；只含已加载范围、且在兴趣半径内的物体。

        打分 = 新颖度(未见过的物体加分) + 类型兴趣(命中 _INTERESTING_TYPES 加分)
               - 距离惩罚(越远越低) - 已访问惩罚(避免来回打转)。
        """
        with self._lock:
            self._purge_stale(time.time())
            ap = self.agent_pos
            cands = []
            for o in self.objects.values():
                region = o.get("region")
                if region and region not in self.loaded_regions:
                    continue  # 双保险：未加载区域不探索
                d = _dist(ap, o["pos"])
                if d > interest_radius:
                    continue
                novelty = 0.0 if o["id"] in self.seen_ids else 1.0
                type_int = 0.6 if o["type"] in _INTERESTING_TYPES else 0.0
                visited_pen = 0.5 if o["id"] in self.visited_ids else 0.0
                score = (novelty * 1.2 + type_int
                         - min(d / max(interest_radius, 0.1), 1.0) * 0.6
                         - visited_pen)
                cands.append((o, score))
            cands.sort(key=lambda x: x[1], reverse=True)
            return cands

    def mark_seen(self, obj_id: str):
        with self._lock:
            self.seen_ids.add(obj_id)

    def mark_visited(self, obj_id: str):
        with self._lock:
            self.visited_ids.add(obj_id)

    def reset_visited(self):
        with self._lock:
            self.visited_ids.clear()

    def has_loaded(self) -> bool:
        with self._lock:
            return bool(self.loaded_regions)

    def stats(self) -> dict:
        with self._lock:
            return {
                "loaded_regions": len(self.loaded_regions),
                "visible_objects": len(self.objects),
                "seen": len(self.seen_ids),
                "visited": len(self.visited_ids),
            }
