# -*- coding: utf-8 -*-
"""
链式解锁（关联图 + 扩散激活）模块。

对应理论：
  - 链式解锁：一个概念被想到后，会顺着关联"链"解锁一串相关概念。
  - 统计信息数量 -> 链式信息权重：边的强度来自"共现统计"。两个概念一起出现越多，
    它们之间的关联边越强，链式解锁时传导的能量越大。
  - 强度维度：每个概念有「强度」S(c) = K*(关联数 + 相似数)，单向正相关于它与多少词
    关联/相似；只影响新词自身，关联/相似词强度不变 -> 单个词强度是定值。多个词组成
    的"事件"强度是变量(词越多越大)。
  - 解锁优先级(信息量增加 > 强度 > 最近)：是否"运用"由匹配度门控。
  - 多跳传导带衰减，能量低于阈值就停(意识的边界)。

图结构：无向加权。edges[a][b] = 共现计数(统计量)。
传导时用"归一化后的边权重"当作传导系数。
"""
from __future__ import annotations
from typing import Dict, List, Tuple
import heapq
import os
import time

import cl_config as config

# A优化开关：设 PYCLAYER_DISABLE_SIM_CACHE=1 可禁用相似度/match 缓存（基准对照用，验证零偏差）
_SIM_CACHE_DISABLED = os.environ.get("PYCLAYER_DISABLE_SIM_CACHE") == "1"
from memory_store import MemoryStore


class AssocGraph:
    def __init__(self):
        # a -> {b: co_count}
        self.edges: Dict[str, Dict[str, float]] = {}
        # 强度维度（单向：只随"新词"诞生时其关联/相似规模而定，之后为定值）
        self.strength: Dict[str, float] = {}        # c -> 强度 S(c)
        self.assoc_count: Dict[str, float] = {}     # c -> 关联的概念数
        self.similar_count: Dict[str, float] = {}   # c -> 相似的概念数
        # ---------- 近因强度衰减（长期不用则忘） ----------
        self.last_active: Dict[str, int] = {}       # c -> 上次被"调取/激活"的逻辑轮次
        self.turn: int = 0                           # 逻辑时钟（每轮 learn 推进 +1）
        # ---------- 相似度缓存（A优化：Jaccard 对称且只依赖邻居成员集合，重复算极费） ----------
        # 失效时机：任何改变 self.edges 成员集合的操作（link / drop）调用 _invalidate_sim_cache。
        # decay 只改权重不改成员，故不清（安全、命中率高）。
        self._sim_cache: Dict[frozenset, float] = {}

    # ---------- 构建 / 统计 ----------
    def touch(self, concept: str, turn: int | None = None) -> None:
        """标记一个概念「此刻被调取/激活」，刷新其近因时间戳（供近因强度衰减用）。"""
        if not concept:
            return
        self.last_active[concept] = turn if turn is not None else self.turn

    def touch_many(self, concepts: List[str]) -> None:
        for c in concepts:
            self.touch(c)

    def link(self, a: str, b: str, amount: float = config.REINFORCE_EDGE) -> None:
        """强化 a、b 之间的关联(共现统计 += amount)。"""
        if not a or not b or a == b:
            return
        self.edges.setdefault(a, {})[b] = self.edges.setdefault(a, {}).get(b, 0.0) + amount
        self.edges.setdefault(b, {})[a] = self.edges.setdefault(b, {}).get(a, 0.0) + amount
        self._prune_node(a)
        self._prune_node(b)
        self._invalidate_sim_cache()   # A优化：边成员变化，相似度缓存失效

    def weaken(self, a: str, b: str, amount: float = config.REINFORCE_EDGE) -> None:
        """弱化 a、b 之间的关联(共现统计 -= amount，最低到 0 并清理)。"""
        if not a or not b or a == b:
            return
        for x, y in ((a, b), (b, a)):
            nbrs = self.edges.get(x)
            if not nbrs or y not in nbrs:
                continue
            nbrs[y] = max(0.0, nbrs[y] - amount)
            if nbrs[y] < 1e-4:
                del nbrs[y]
        self._prune_node(a)
        self._prune_node(b)
        self._invalidate_sim_cache()

    def link_group(self, concepts: List[str], amount: float = config.REINFORCE_EDGE) -> None:
        """一组同时出现的概念两两建立/强化关联(共现，袋式平权)。"""
        uniq = list(dict.fromkeys([c for c in concepts if c]))
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                self.link(uniq[i], uniq[j], amount)

    def link_sequence(
        self,
        concepts: List[str],
        window: int = config.PROXIMITY_WINDOW,
        base: float = config.REINFORCE_EDGE,
        decay: float = config.PROXIMITY_DECAY,
    ) -> None:
        """
        按"邻近"建边：保留词序信息 —— 相邻概念关联最强，距离越远越弱、超窗口不建。
        这比 link_group 的袋式平权更贴近语言结构（"下雨"和"晚上"该比跨句远词更紧）。
        amount(距离d) = base * decay^(d-1)，d=1(相邻)最强。
        """
        seq = [c for c in concepts if c]
        n = len(seq)
        for i in range(n):
            for d in range(1, window + 1):
                j = i + d
                if j >= n:
                    break
                if seq[i] == seq[j]:
                    continue
                self.link(seq[i], seq[j], base * (decay ** (d - 1)))

    def decay(self, factor: float = config.DECAY_PER_TURN) -> None:
        """关联也会随时间淡忘。同时推进逻辑时钟（近因强度衰减用）。"""
        self.turn += 1
        for a in list(self.edges.keys()):
            nbrs = self.edges[a]
            for b in list(nbrs.keys()):
                nbrs[b] *= factor
                if nbrs[b] < 1e-4:
                    del nbrs[b]
            if not nbrs:
                del self.edges[a]

    def _prune_node(self, a: str) -> None:
        nbrs = self.edges.get(a)
        if nbrs and len(nbrs) > config.EDGE_CAPACITY_PER_NODE:
            keep = sorted(nbrs.items(), key=lambda kv: kv[1], reverse=True)[: config.EDGE_CAPACITY_PER_NODE]
            self.edges[a] = dict(keep)

    def _invalidate_sim_cache(self) -> None:
        """A优化：任何改变 self.edges 成员集合的操作后调用，清空相似度缓存。"""
        self._sim_cache.clear()

    def drop(self, concept: str) -> None:
        """概念被遗忘淘汰时，连带清理它的边。"""
        for b in list(self.edges.get(concept, {}).keys()):
            self.edges.get(b, {}).pop(concept, None)
        self.edges.pop(concept, None)
        self.strength.pop(concept, None)
        self.assoc_count.pop(concept, None)
        self.similar_count.pop(concept, None)
        self.last_active.pop(concept, None)
        self._invalidate_sim_cache()   # A优化：边成员变化，相似度缓存失效

    # ---------- 全局节点封顶（防图无限膨胀 / 意大利面） ----------
    def node_count(self) -> int:
        """图中节点总数（关联边端点 ∪ 已定型强度）。"""
        nodes = set(self.edges.keys())
        nodes |= set(self.strength.keys())
        return len(nodes)

    def enforce_node_cap(self, cap: int) -> List[str]:
        """
        全局节点封顶：节点数超过 cap 时，按「总边权重(活跃度代理)」从低到高淘汰，
        连带清边与强度字段。最不活跃的概念先被遗忘，图始终保持有界、稀疏。
        cap<=0 表示不限制。返回被淘汰的概念列表。
        """
        if cap is None or cap <= 0:
            return []
        nodes = set(self.edges.keys()) | set(self.strength.keys())
        if len(nodes) <= cap:
            return []
        # 活跃度 = 该节点所有出边权重之和（边越多越重越活跃）
        activity = {n: sum(self.edges.get(n, {}).values()) for n in nodes}
        ordered = sorted(nodes, key=lambda n: activity[n])
        drop_n = len(nodes) - cap
        evicted: List[str] = []
        for n in ordered:
            if drop_n <= 0:
                break
            self.drop(n)
            evicted.append(n)
            drop_n -= 1
        return evicted

    # ---------- 近因强度衰减：长期未被调用的词，强度逐轮降，归零则遗忘 ----------
    def decay_strength_by_recency(
        self,
        enabled: bool = config.STRENGTH_RECENCY_ENABLED,
        decay: float = config.STRENGTH_RECENCY_DECAY,
        grace: int = config.STRENGTH_IDLE_GRACE,
        threshold: float = config.STRENGTH_FORGET_THRESHOLD,
    ) -> List[str]:
        """
        用户提出的"强度遗忘"机制：长时间没有被调取/激活的概念，其强度 S(c) 会慢慢降低，
        直到降到零被遗忘。

        实现：每个概念记录 last_active（最近一次被 think 的扩散激活 / learn 的使用概念
        刷新，见 touch()）。每轮对「闲置轮次 = turn - last_active」超过宽限期 grace 的概念，
        将其强度乘 decay 缓慢下滑；强度低于 threshold（约等于零）即 drop 遗忘，连带清边。
        这样不常用的冷门概念会自然沉底消失，图保持清爽、不至于意大利面；常用词因为
        last_active 不断被刷新而长期保持强连接。返回本轮被遗忘的概念列表。
        """
        if not enabled:
            return []
        forgotten: List[str] = []
        for c in list(self.strength.keys()):
            la = self.last_active.get(c)
            idle = (self.turn - la) if la is not None else self.turn
            if idle <= grace:
                continue
            self.strength[c] *= decay
            if self.strength[c] < threshold:
                self.drop(c)
                forgotten.append(c)
        return forgotten

    # ---------- 压缩整合（睡眠态的记忆自愈：弱词并入强词，降遍历耗时） ----------
    def benchmark_ms(self) -> float:
        """
        估量『每轮遍历记忆图』的耗时(ms)：只读模拟 decay / 近因强度衰减 / 节点封顶 三道
        O(N) 维护循环的代价（不修改任何数据）。乘以 SLEEP_COST_K 标定到真实耗时量级，
        供睡眠机制判断"逼近/超过 500ms"。
        """
        t0 = time.perf_counter()
        for a in self.edges:                      # 模拟 decay()：遍历所有边
            nbrs = self.edges[a]
            for b in nbrs:
                _ = nbrs[b] * 0.997
        for c in self.strength:                   # 模拟 decay_strength_by_recency()：遍历所有强度
            _ = self.strength[c] * 0.992
        for n in self.edges:                      # 模拟 enforce_node_cap()：汇总每个节点的边权
            total = 0.0
            for _w in self.edges[n].values():
                total += _w
        return (time.perf_counter() - t0) * 1000.0 * config.SLEEP_COST_K

    def strongest_neighbor(self, c: str) -> str | None:
        """返回 c 关联边权重最大的邻居（压缩整合时的"并入目标"）。"""
        nbrs = self.edges.get(c)
        if not nbrs:
            return None
        best, bw = None, -1.0
        for nb, w in nbrs.items():
            if w > bw:
                bw, best = w, nb
        return best

    def merge_into(self, c: str, target: str) -> None:
        """
        把概念 c 并入 target：c 的所有边（加权）转移到 target（c 与 target 之间的边不转移，
        避免自环），随后 drop(c) 清掉 c 自身及残留边。等于把"快要归零的弱词"整合进
        其最强相关词，节点/边数下降 -> 遍历耗时下降。
        """
        if not target or target == c:
            return
        for nb, w in list(self.edges.get(c, {}).items()):
            if nb == target:
                continue
            self.link(target, nb, w)
        self.drop(c)

    def consolidate(
        self,
        strength_max: float = config.CONSOLIDATE_STRENGTH_MAX,
        batch: int = config.CONSOLIDATE_BATCH,
    ) -> List[Tuple[str, str]]:
        """
        压缩整合：把『强度快要归零』的弱词并入其最强相关词（汇入相关新词），循环调用直至
        遍历耗时回落或无可合并弱词。候选 = 强度在 (0, strength_max) 的词（最弱、最接近被遗忘），
        按强度升序优先合并（越接近零的先整合）。返回 [(被合并词, 并入目标词), ...]。
        """
        cands = [c for c in self.strength if 0 < self.strength[c] < strength_max]
        cands.sort(key=lambda c: self.strength[c])
        merged: List[Tuple[str, str]] = []
        for c in cands:
            if len(merged) >= batch:
                break
            if self.strength.get(c, 0) >= strength_max:
                break
            tgt = self.strongest_neighbor(c)
            if not tgt or tgt == c:
                continue
            self.merge_into(c, tgt)
            merged.append((c, tgt))
        return merged

    # ---------- 边权重（统计 -> 权重） ----------
    def edge_weight(self, a: str, b: str, mem: MemoryStore) -> float:
        """
        传导系数：共现统计越高，边越强；但按端点频率归一化，
        避免高频"万金油"概念把所有能量都吸走(类似 TF-IDF 思想)。
        统计信息数量 -> 链式权重 的核心公式。
        """
        co = self.edges.get(a, {}).get(b, 0.0)
        if co <= 0:
            return 0.0
        # 归一化：共现 / (a频次 + b频次 - 共现)  近似 Jaccard，取值(0,1]
        ca = mem.count(a)
        cb = mem.count(b)
        denom = ca + cb - co
        if denom <= 0:
            return 0.0
        return co / denom

    def neighbors(self, a: str) -> List[Tuple[str, float]]:
        return list(self.edges.get(a, {}).items())

    # ---------- 强度维度（关联数 + 相似数，单向正相关） ----------
    def similarity(self, a: str, b: str) -> float:
        """两个概念的"相似度"：邻居集合的 Jaccard（共享关联 -> 彼此相似，是独立指标）。

        A优化：对称且只依赖邻居成员集合，结果加缓存（frozenset key），在 link/drop 改边时失效。
        不改变数学结果，仅避免重复计算（spread/match_degree/_compute_strength 大量重复调用）。
        """
        key = frozenset((a, b))
        if not _SIM_CACHE_DISABLED:
            cached = self._sim_cache.get(key)
            if cached is not None:
                return cached
        na = set(self.edges.get(a, {}).keys())
        nb = set(self.edges.get(b, {}).keys())
        if not na or not nb:
            res = 0.0
        else:
            inter = len(na & nb)
            union = len(na | nb)
            res = inter / union if union > 0 else 0.0
        if not _SIM_CACHE_DISABLED:
            self._sim_cache[key] = res
        return res

    def _compute_strength(self, w: str) -> None:
        """按"关联数 + 相似数"算 w 的强度(单向：只在此调用时设定 w 自身，不动他人)。"""
        nbrs = self.edges.get(w, {})
        # 关联数：边权重达阈值的邻居数
        assoc = sum(1 for _nb, wco in nbrs.items() if wco >= config.STRENGTH_ASSOC_MIN)
        # 相似数：以"邻居的邻居"为候选，Jaccard>=阈值的其他概念数
        candidates: set = set()
        for nb in nbrs:
            candidates |= set(self.edges.get(nb, {}).keys())
        candidates.discard(w)
        sim = 0
        for c in candidates:
            if self.similarity(w, c) >= config.SIM_THRESHOLD:
                sim += 1
        self.assoc_count[w] = assoc
        self.similar_count[w] = sim
        self.strength[w] = config.STRENGTH_K * (assoc + sim)

    def finalize_strength(self, new_words: List[str]) -> None:
        """
        为新词定强度(单向正相关的落点)：新词 w 的强度 = K*(它关联的旧词数 + 相似旧词数)。
        只设定 new_words 自身；与之关联的旧词强度不变 —— 满足"永远只影响新词"。
        已定型(已算过强度)的词不重算 -> 单个词强度是定值。
        """
        for w in new_words:
            if w and w not in self.strength:
                self._compute_strength(w)

    def ensure_strengths(self) -> None:
        """迁移/兜底：为图中尚无强度的概念一次性补算(视作新词)。之后均为定值。"""
        for c in list(self.edges.keys()):
            if c not in self.strength:
                self._compute_strength(c)

    def strength_of(self, c: str) -> float:
        return self.strength.get(c, 0.0)

    def strength_score(self, c: str) -> float:
        """强度归一化到[0,1]，喂给解锁能量。"""
        return min(1.0, self.strength_of(c) / max(1e-9, config.STRENGTH_REF))

    def event_strength(self, words: List[str]) -> float:
        """
        "事件"强度(变量)：多个词连起来组成的事件的强度。
        = 各词强度之和 * (1 + COMPOSE_BONUS*(n-1))；词越多(且越强)事件强度越大。
        """
        n = len(words)
        if n == 0:
            return 0.0
        s = sum(self.strength_of(w) for w in words)
        return s * (1.0 + config.COMPOSE_BONUS * (n - 1))

    def match_degree(self, c: str, seed_set: set) -> float:
        """
        "匹配度"：概念 c 与当前上下文(种子集)的契合程度，决定 c 是否被"运用"(解锁)。
        匹配度 = 关联匹配(到种子的最大边权重) * W + 相似匹配(到种子的最大相似度) * (1-W)。
        关联与相似是两条独立指标，此处线性融合。种子自身匹配度=1(必运用)。
        """
        if c in seed_set:
            return 1.0
        best_assoc = 0.0
        best_sim = 0.0
        for s in seed_set:
            co = self.edges.get(c, {}).get(s, 0.0)
            # 关联匹配：共现强度(隐含统计多少)，归一到[0,1]
            w_assoc = min(1.0, co / (co + 1.0)) if co > 0 else 0.0
            # 相似匹配：与种子的邻居集合 Jaccard
            w_sim = self.similarity(c, s)
            if w_assoc > best_assoc:
                best_assoc = w_assoc
            if w_sim > best_sim:
                best_sim = w_sim
        return config.MATCH_ASSOC_W * best_assoc + (1.0 - config.MATCH_ASSOC_W) * best_sim

    # ---------- 链式解锁（扩散激活 + 强度维度 + 分层优先级 + 匹配度门控） ----------
    def spread_activation(
        self,
        seeds: Dict[str, float],
        mem: MemoryStore,
        hops: int = config.SPREAD_HOPS,
        decay: float = config.SPREAD_DECAY,
        threshold: float = config.SPREAD_THRESHOLD,
        max_unlock: int = config.SPREAD_MAX_UNLOCK,
    ) -> Dict[str, float]:
        """
        从种子概念出发，沿关联链多跳解锁相关概念。

        解锁优先级严格分层（能量 = 传导能量 × (1 + 信息增权重·信息增 + 强度权重·强度 + 最近权重·最近)）：
          1) 信息量增加的词条（种子 / 本轮回填增量的概念）能量最高 -> 最先解锁
          2) 然后是强度大的词条
          3) 最后是最近的词条
        是否"运用"(解锁)由匹配度门控：非种子概念与当前上下文(种子)的匹配度低于
        MATCH_THRESHOLD 则不被解锁（不匹配就不运用）。

        用优先队列(能量大的先扩散)，保证在 max_unlock 限制下按上述优先级解锁。
        返回 {概念: 累计激活能量}（能量即该记忆在链条中所占的权重）。
        """
        seed_set = set(seeds.keys())
        activation: Dict[str, float] = {}
        heap: List[Tuple[float, int, str]] = []

        def priority_energy(c: str, base_e: float) -> float:
            # 分层优先级项：信息增 >> 强度 >> 最近
            info_b = 1.0 if c in seed_set else min(1.0, mem.recent_info_increase(c) / config.INFO_DELTA_REF)
            s = self.strength_score(c)
            r = mem.recency(c)
            return base_e * (1.0 + config.INFO_W * info_b + config.STRENGTH_W * s + config.RECENCY_W * r)

        for s, e in seeds.items():
            self.touch(s)   # 种子被"调取"，刷新近因
            pe = priority_energy(s, e)
            activation[s] = activation.get(s, 0.0) + pe
            heapq.heappush(heap, (-pe, 0, s))

        match_cache: Dict[str, float] | None = {} if not _SIM_CACHE_DISABLED else None   # A优化：spread 内 seed_set 固定，缓存 match_degree 避免重复算
        while heap and len(activation) < max_unlock:
            neg_e, hop, node = heapq.heappop(heap)
            energy = -neg_e
            if hop >= hops or energy < threshold:
                continue
            for nb, _co in self.neighbors(node):
                # 匹配度门控：非种子概念须与上下文匹配才被运用(解锁)
                if nb not in seed_set:
                    md = None if match_cache is None else match_cache.get(nb)
                    if md is None:
                        md = self.match_degree(nb, seed_set)
                        if match_cache is not None:
                            match_cache[nb] = md
                    if md < config.MATCH_THRESHOLD:
                        continue
                w = self.edge_weight(node, nb, mem)
                if w <= 0:
                    continue
                passed = energy * w * decay
                if passed < threshold:
                    continue
                pe = priority_energy(nb, passed)
                if pe > activation.get(nb, 0.0) + 1e-9:
                    activation[nb] = pe
                    self.touch(nb)   # 该概念被链式解锁"激活"，刷新近因
                    heapq.heappush(heap, (-pe, hop + 1, nb))

        return activation

    # ---------- 持久化 ----------
    def to_dict(self) -> dict:
        return {
            "edges": self.edges,
            "strength": self.strength,
            "assoc_count": self.assoc_count,
            "similar_count": self.similar_count,
            "last_active": self.last_active,
            "turn": self.turn,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AssocGraph":
        obj = cls()
        obj.edges = {a: {b: float(w) for b, w in nbrs.items()} for a, nbrs in d.get("edges", {}).items()}
        obj.strength = {k: float(v) for k, v in d.get("strength", {}).items()}
        obj.assoc_count = {k: float(v) for k, v in d.get("assoc_count", {}).items()}
        obj.similar_count = {k: float(v) for k, v in d.get("similar_count", {}).items()}
        obj.last_active = {k: int(v) for k, v in d.get("last_active", {}).items()}
        obj.turn = int(d.get("turn", 0))
        # 迁移：旧存档无强度数据 -> 一次性补算(之后均为定值)
        if not obj.strength and obj.edges:
            obj.ensure_strengths()
        return obj
