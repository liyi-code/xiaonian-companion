# -*- coding: utf-8 -*-
"""
双通路睡眠（DualPathwaySleep）—— 睡眠时的两种记忆处理，模拟"想象力 / 创造力"雏形。

设计目标（用户需求）：
  进入睡眠时，当天信息有两种处理：
    通路一：语义压缩备份（弱词并入强词，降遍历耗时）—— 复用既有 consolidate()。
    通路二：创新组合备份（把高权重节点随机组合成"合成概念"，打上新权重 = 成员权重之和）。
  两类压缩组合可以相互关联（弱连接）；当 AI 在表层（扩散激活）思索不出答案时，
  可回查合成概念索引来"尝试解决问题"——这是想象力的最小可运行形态。

安全 / 架构边界（朋友建议 + 工程实践）：
  1) 合成概念存【独立索引】（combos.json），不写入主关联图 AssocGraph —— 因此
     通路二可一键关闭（DUAL_PATHWAY_ENABLED=False），行为立即退回旧版；也不破坏
     C++/Python 数值 parity。
  2) 用依赖注入挂到 Consciousness，不在 think() 主循环里大改。
  3) slope_utility（边际效用）不存绝对值，而是记录「最近 N 次调用时用户状态的斜率变化」，
     检索时按当前情境二次过滤，负效用的组合直接丢弃。

依赖：cl_config（常量）、Consciousness（通过 set_owner 注入弱引用，避免循环导入）。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Tuple
import json
import os
import random
import threading

import cl_config as config


@dataclass
class Combo:
    """一个「合成概念」：由若干高权重概念组合而成。"""
    key: str                        # 合成概念的稳定标识（成员排序后 join）
    members: List[str] = field(default_factory=list)   # 成员概念（原图节点）
    weight: float = 0.0             # 新权重 = 成员权重之和
    pathway: int = 2                # 1=语义压缩 2=创新组合（当前仅 2）
    slope_utility: float = 0.0      # 边际效用：最近 N 次调用用户状态的斜率
    utility_history: List[float] = field(default_factory=list)  # 最近 N 次斜率记录
    weak_links: Dict[str, float] = field(default_factory=dict)  # 与其他 combo/原概念的弱连接
    use_count: int = 0              # 被"想象力出口"调用的次数
    last_used: int = 0              # 上次调用轮次

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "members": self.members,
            "weight": self.weight,
            "pathway": self.pathway,
            "slope_utility": self.slope_utility,
            "utility_history": self.utility_history,
            "weak_links": self.weak_links,
            "use_count": self.use_count,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Combo":
        return cls(
            key=d.get("key", ""),
            members=list(d.get("members", [])),
            weight=float(d.get("weight", 0.0)),
            pathway=int(d.get("pathway", 2)),
            slope_utility=float(d.get("slope_utility", 0.0)),
            utility_history=[float(x) for x in d.get("utility_history", [])],
            weak_links={k: float(v) for k, v in d.get("weak_links", {}).items()},
            use_count=int(d.get("use_count", 0)),
            last_used=int(d.get("last_used", 0)),
        )


class DualPathwaySleep:
    """
    睡眠引擎：通路二（创新组合）+ slope_utility 反馈 + 想象力出口。
    通过 set_owner(consciousness) 注入意识层引用，不反向导入。
    """

    def __init__(self):
        self._owner = None                 # 弱引用意识层（set_owner 注入）
        self.combos: Dict[str, Combo] = {}  # 合成概念索引 key -> Combo
        self._lock = threading.RLock()
        self._loaded = False

    # ---------- 注入 ----------
    def set_owner(self, consciousness) -> None:
        self._owner = consciousness

    @property
    def owner(self):
        return self._owner

    # ---------- 通路二：创新组合 ----------
    def _high_weight_nodes(self, threshold: float) -> List[Tuple[str, float]]:
        """返回 strength_score 超过阈值的高权重节点，按分数降序。"""
        g = self._owner.graph
        items = [(c, g.strength_score(c)) for c in g.snapshot_strength()]
        items = [(c, s) for c, s in items if s >= threshold]
        items.sort(key=lambda kv: kv[1], reverse=True)
        return items

    def _make_key(self, members: List[str]) -> str:
        return "⋈".join(sorted(members))

    def innovate_combine(self, threshold: float | None = None) -> int:
        """
        通路二：把高权重节点随机两两组合成"合成概念"。
        新权重 = 成员权重之和；写独立索引 combos；成员间互建弱连接。
        返回本次新生成的合成概念数。
        """
        if self._owner is None:
            return 0
        thr = config.DUAL_COMBINE_THRESHOLD if threshold is None else threshold
        with self._lock:
            high = self._high_weight_nodes(thr)
            if len(high) < config.DUAL_COMBINE_MIN_K:
                return 0
            created = 0
            # 高权重节点按分数加权采样，避免纯随机命中大量弱关联
            total = sum(s for _, s in high) or 1.0
            pool = [c for c, _ in high]
            weights = [s / total for _, s in high]
            k = config.DUAL_COMBINE_MAX_K
            for _ in range(config.DUAL_COMBINE_BATCH):
                if len(pool) < k:
                    break
                # 无放回抽 k 个成员（加权）
                chosen: List[str] = []
                remaining = list(pool)
                rem_w = list(weights)
                for _i in range(k):
                    if not remaining:
                        break
                    idx = random.choices(range(len(remaining)), weights=rem_w, k=1)[0]
                    chosen.append(remaining.pop(idx))
                    rem_w.pop(idx)
                if len(chosen) < config.DUAL_COMBINE_MIN_K:
                    continue
                key = self._make_key(chosen)
                if key in self.combos:
                    # 已存在：刷新成员（不重复叠加权重，避免复读式膨胀）
                    continue
                w = sum(self._owner.graph.strength_score(c) for c in chosen)
                cb = Combo(key=key, members=chosen, weight=w, pathway=2)
                # 与成员建弱连接（组合↔原概念），供"组合可相互关联"用
                for c in chosen:
                    cb.weak_links[c] = config.DUAL_WEAK_LINK
                self.combos[key] = cb
                created += 1
            # 容量上限：按 slope_utility 从低淘汰（负效用/冷门先淘汰）
            self._enforce_capacity()
            return created

    def _enforce_capacity(self) -> None:
        cap = config.DUAL_COMBO_CAPACITY
        if cap <= 0 or len(self.combos) <= cap:
            return
        ordered = sorted(self.combos.values(),
                         key=lambda cb: (cb.slope_utility, cb.use_count))
        drop_n = len(self.combos) - cap
        for cb in ordered[:drop_n]:
            self.combos.pop(cb.key, None)

    # ---------- slope_utility：边际效用反馈 ----------
    def record_feedback(self, combo_key: str, delta: float) -> None:
        """
        记录某合成概念被使用后「用户状态的斜率变化」delta（[-1,1]，正=变好）。
        slope_utility = 最近 N 次 delta 的移动平均（边际效用）。
        上层（assistant）在对话后，若动用了某组合，就把用户活跃度/情绪变化传入。
        """
        if not combo_key:
            return
        with self._lock:
            cb = self.combos.get(combo_key)
            if cb is None:
                return
            cb.utility_history.append(max(-1.0, min(1.0, delta)))
            if len(cb.utility_history) > config.DUAL_UTILITY_WINDOW:
                cb.utility_history = cb.utility_history[-config.DUAL_UTILITY_WINDOW:]
            cb.slope_utility = (sum(cb.utility_history)
                                / max(1, len(cb.utility_history)))
            cb.use_count += 1
            # 轮次：C++ 后端不暴露 graph.turn，回退到 owner 的 think 计数或时间戳
            turn = getattr(getattr(self._owner, "graph", None), "turn", None)
            if turn is None:
                turn = int(getattr(self._owner, "sleep_count", 0) * 10) + cb.use_count
            cb.last_used = int(turn)

    # ---------- 想象力出口：表层卡壳时调用 ----------
    def retrieve_creative(
        self,
        seeds: List[str],
        k: int = 3,
        context: List[str] | None = None,
    ) -> List[Tuple[str, float]]:
        """
        表层扩散激活思索不出答案时的"想象力出口"：
        返回与种子最相关、且 slope_utility 在当前情境下非负的合成概念（按相关性×效用排序）。
        context 为当前情境概念（用于二次过滤，暂以 slope_utility 全局值近似）。
        返回 [(组合key, 得分), ...]。
        """
        if self._owner is None or not self.combos:
            return []
        seed_set = set(seeds)
        scored: List[Tuple[str, float]] = []
        with self._lock:
            for key, cb in self.combos.items():
                # 负效用（该组合在此情境下有害）直接丢弃
                if cb.slope_utility < config.DUAL_UTILITY_MIN:
                    continue
                # 检索阈值：效用过低不返回
                if cb.slope_utility < config.DUAL_RETRIEVE_THRESHOLD:
                    continue
                # 相关性：成员与种子的重合度
                overlap = len(seed_set & set(cb.members))
                if overlap == 0:
                    # 无直接重合时，看弱连接到种子的程度
                    link_hit = sum(1 for s in seed_set if s in cb.weak_links)
                    if link_hit == 0:
                        continue
                    rel = 0.3 * link_hit / max(1, len(seed_set))
                else:
                    rel = overlap / max(1, len(seed_set))
                # 得分 = 相关性 × (1 + 效用加成) × 权重归一
                score = rel * (1.0 + max(0.0, cb.slope_utility)) * min(1.0, cb.weight / 4.0)
                scored.append((key, score))
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return scored[:k]

    # ---------- 组合之间的弱连接（两类压缩可相互关联） ----------
    def link_combos(self, a: str, b: str, amount: float | None = None) -> None:
        amt = config.DUAL_WEAK_LINK if amount is None else amount
        with self._lock:
            ca, cb = self.combos.get(a), self.combos.get(b)
            if ca is None or cb is None or a == b:
                return
            ca.weak_links[b] = ca.weak_links.get(b, 0.0) + amt
            cb.weak_links[a] = cb.weak_links.get(a, 0.0) + amt

    # ---------- 持久化 ----------
    def save(self, path: str = config.DUAL_COMBO_FILE) -> None:
        with self._lock:
            data = {"combos": {k: v.to_dict() for k, v in self.combos.items()}}
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, path)
            except Exception:
                pass

    def load(self, path: str = config.DUAL_COMBO_FILE) -> None:
        with self._lock:
            if not os.path.exists(path):
                self._loaded = True
                return
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.combos = {k: Combo.from_dict(v)
                               for k, v in data.get("combos", {}).items()}
            except Exception:
                self.combos = {}
            self._loaded = True

    def stats(self) -> dict:
        with self._lock:
            return {
                "combo_count": len(self.combos),
                "avg_utility": (sum(cb.slope_utility for cb in self.combos.values())
                                / max(1, len(self.combos))),
            }
