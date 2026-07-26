# -*- coding: utf-8 -*-
"""
储存量（脑容量）模块。

对应理论：
  - 储存量 = 有上限的信息单元库。每个信息单元(概念)带一个"统计计数"。
  - 统计 = 每次概念被激活/使用，计数增加；全局每轮缓慢衰减(遗忘)。
  - 容量满时淘汰"最弱"(计数最低)的概念，模拟脑容量有限、久不用则忘。

只负责"存什么、记多牢、忘什么"；关联结构在 assoc_graph 里。
"""
from __future__ import annotations
from typing import Dict, List, Iterable, Tuple
import math

import cl_config as config


class MemoryStore:
    def __init__(self, capacity: int = config.STORAGE_CAPACITY):
        self.capacity = capacity
        # concept -> count（统计量，可为小数因为会衰减）
        self.counts: Dict[str, float] = {}
        # concept -> 最近一次被激活的"逻辑时间"（用于近因，辅助淘汰）
        self.last_seen: Dict[str, int] = {}
        # concept -> 上一次 learn 周期里"信息增加量"(count 增量)；喂解锁优先级"信息量增加"项
        self.info_delta: Dict[str, float] = {}
        self._clock = 0

    # ---------- 写入 / 统计 ----------
    def observe(self, concept: str, amount: float = config.REINFORCE_NODE) -> None:
        """观察到一个概念：统计计数 += amount。"""
        if not concept:
            return
        self.counts[concept] = self.counts.get(concept, 0.0) + amount
        self.last_seen[concept] = self._clock

    def observe_many(self, concepts: Iterable[str], amount: float = config.REINFORCE_NODE) -> None:
        for c in concepts:
            self.observe(c, amount)

    def tick(self) -> List[str]:
        """推进一轮：逻辑时钟 +1，全局衰减(缓慢遗忘)，必要时淘汰。返回被淘汰概念。"""
        self._clock += 1
        decay = config.DECAY_PER_TURN
        for c in list(self.counts.keys()):
            self.counts[c] *= decay
        return self._enforce_capacity()

    # ---------- 读取 / 权重 ----------
    def count(self, concept: str) -> float:
        return self.counts.get(concept, 0.0)

    def salience(self, concept: str) -> float:
        """
        概念的内在显著度：统计越多越显著，但用 log 压缩(边际递减)。
        统计信息数量 -> 权重 的基础一环。
        """
        return math.log1p(max(0.0, self.counts.get(concept, 0.0)))

    def total_observations(self) -> float:
        """总统计量：用于温度调制(统计越多，偏向越强)。"""
        return sum(self.counts.values())

    def size(self) -> int:
        return len(self.counts)

    def contains(self, concept: str) -> bool:
        return concept in self.counts

    def recency(self, concept: str) -> float:
        """近因分数[0,1]：最近被激活->接近1，久未出现->趋近0。用于解锁优先级'最近'项。"""
        ls = self.last_seen.get(concept)
        if ls is None:
            return 0.0
        age = self._clock - ls
        return math.exp(-age / max(1e-6, config.RECENCY_TAU))

    def recent_info_increase(self, concept: str) -> float:
        """本 learn 周期该概念的信息增加量(>0 表示'信息量增加的词条')。"""
        return self.info_delta.get(concept, 0.0)

    def snapshot_counts(self) -> Dict[str, float]:
        """在 learn 统计++之前快照，用于事后算'信息增加量'。"""
        return dict(self.counts)

    def commit_deltas(self, prev_counts: Dict[str, float]) -> None:
        """统计++后调用：对比快照，回填每个概念的本轮信息增量(只记正增量)。"""
        self.info_delta = {}
        for c, cnt in self.counts.items():
            d = cnt - prev_counts.get(c, 0.0)
            if d > 1e-9:
                self.info_delta[c] = d

    def top(self, n: int = 20) -> List[Tuple[str, float]]:
        return sorted(self.counts.items(), key=lambda kv: kv[1], reverse=True)[:n]

    # ---------- 遗忘 / 容量 ----------
    def _enforce_capacity(self) -> List[str]:
        """超容量时，淘汰计数最低(且低于遗忘阈值优先)的概念。返回被淘汰列表。"""
        over = self.size() - self.capacity
        if over <= 0:
            return []
        # 优先淘汰计数最低的
        ordered = sorted(self.counts.items(), key=lambda kv: kv[1])
        evicted: List[str] = []
        for concept, cnt in ordered:
            if over <= 0:
                break
            del self.counts[concept]
            self.last_seen.pop(concept, None)
            evicted.append(concept)
            over -= 1
        return evicted

    # ---------- 持久化 ----------
    def to_dict(self) -> dict:
        return {
            "capacity": self.capacity,
            "counts": self.counts,
            "last_seen": self.last_seen,
            "info_delta": self.info_delta,
            "clock": self._clock,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryStore":
        obj = cls(capacity=d.get("capacity", config.STORAGE_CAPACITY))
        obj.counts = {k: float(v) for k, v in d.get("counts", {}).items()}
        obj.last_seen = {k: int(v) for k, v in d.get("last_seen", {}).items()}
        obj.info_delta = {k: float(v) for k, v in d.get("info_delta", {}).items()}
        obj._clock = int(d.get("clock", 0))
        return obj
