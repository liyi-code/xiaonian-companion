# -*- coding: utf-8 -*-
"""
主动找话题引擎（TopicEngine）—— 从"睡眠成果"里挑种子，绝不随机、绝不抓网。

设计目标（用户需求）：
  主动找话题的素材直接复用睡眠机制的成果，不做临时互联网抓取（不可控且蠢）。
  两个来源：
    来源A（创新组合）：combos.json 里"刚生成、slope_utility 高、但还没被调用过(use_count==0)"
                       的合成概念 —— 睡眠时碰撞出的新鲜种子。
    来源B（遗忘预警）：assoc_graph 里"权重正在快速衰减、但曾被反复提到的高频词" ——
                       趁彻底忘记前主动抢救这段记忆。
  生成策略：纯规则取权重最高的 1 个种子 + 套预设轻量话术模板，【不调用 LLM】；
            用户回复后，才把回复塞进 clayer 做一轮轻量联想 + LLM 续聊。

反馈闭环（"让生活越来越好" + "越来越"原则）：
  用户乐意聊（回复字数/情绪正增长）→ 给种子加权（强化）；
  用户无视或负反馈（"滚"）→ 拉黑该种子 + 缩短下次主动间隔（"知错就改，看脸色"）。

依赖：cl_config（常量）、Consciousness（通过 set_owner 注入弱引用）。
"""
from __future__ import annotations
from typing import Dict, List, Optional, Tuple
import random

import cl_config as config


class TopicEngine:
    """主动找话题引擎：纯规则选种子 + 模板话术 + 反馈闭环。"""

    def __init__(self):
        self._owner = None                       # 意识层弱引用（set_owner 注入）
        self._blacklist: set = set()             # 拉黑的种子（负反馈，永不再用）
        self._boost: Dict[str, float] = {}       # 正反馈加权：种子 -> 额外权重
        self.last_source: str = ""               # 上次用的来源（调试/闭环用）
        self.last_seed: str = ""                 # 上次用的种子（反馈闭环定位）

    def set_owner(self, consciousness) -> None:
        self._owner = consciousness

    # ---------- 来源B：遗忘预警 ----------
    def _forgetting_words(self) -> List[Tuple[str, float]]:
        """
        返回「正在快速衰减但曾高频」的词及其"抢救价值"。
        衰减判断：last_active 比当前最大 last_active 落后超过 TOPIC_SOURCE_B_GRACE（正在遗忘）。
        高频判断：assoc_count（关联的概念数）够大 = 曾反复被提到。
        抢救价值 = assoc_count（关联越多越值得救）× 衰减程度（越接近遗忘越紧急）。

        注意：C++ 后端不暴露 graph.turn，故用「last_active 相对最大值的落后轮次」代替
        绝对轮次，语义等价（最久没被调用的词最该抢救）。
        """
        g = self._owner.graph
        # 兼容 C++/Python 双后端：C++ 不暴露 last_active/assoc_count/turn 属性，
        # 但 to_dict() 两者都返回（含 turn），故统一从这里取。
        try:
            d = g.to_dict()
        except Exception:
            d = {}
        la_map = d.get("last_active", {}) or {}
        assoc_map = d.get("assoc_count", {}) or {}
        if not la_map:
            return []
        max_la = max(la_map.values())
        out: List[Tuple[str, float]] = []
        for c, strength in g.snapshot_strength().items():
            if strength <= 0:
                continue
            la = la_map.get(c)
            idle = (max_la - la) if la is not None else max_la
            if idle <= config.TOPIC_SOURCE_B_GRACE:
                continue
            assoc = assoc_map.get(c, 0.0)
            if assoc < config.TOPIC_SOURCE_B_ASSOC_MIN:
                continue
            # 抢救价值 = 关联数 × 衰减紧急性(闲置越多越急，上限钳制防爆炸)
            urgency = min(1.0, idle / max(1, config.TOPIC_SOURCE_B_GRACE * 3))
            value = assoc * (1.0 + urgency)
            out.append((c, value))
        out.sort(key=lambda kv: kv[1], reverse=True)
        return out

    # ---------- 来源A：创新组合（未用过的合成概念） ----------
    def _fresh_combos(self) -> List[Tuple[str, float]]:
        """返回「还没被用过(use_count==0)、slope_utility 达标、权重够」的合成概念。"""
        se = self._owner.sleep_engine
        out: List[Tuple[str, float]] = []
        for key, cb in se.combos.items():
            if cb.use_count > 0:
                continue
            if cb.slope_utility < config.TOPIC_SOURCE_A_MIN_UTILITY:
                continue
            if cb.weight < config.TOPIC_SOURCE_A_WEIGHT_MIN:
                continue
            if key in self._blacklist:
                continue
            out.append((key, cb.weight))
        out.sort(key=lambda kv: kv[1], reverse=True)
        return out

    # ---------- 选种子 + 生成话术（纯规则，不调 LLM） ----------
    def pick_topic(self) -> Optional[dict]:
        """
        从来源A（创新组合）优先、来源B（遗忘预警）兜底，取权重最高的 1 个种子。
        返回 {source, seed, other, text, seed_key}；无可用种子返回 None。
        """
        if self._owner is None:
            return None
        # 来源A 优先（新鲜碰撞的合成概念更符合"想象力"）
        fresh = self._fresh_combos()
        if fresh:
            key, weight = fresh[0]
            cb = self._owner.sleep_engine.combos[key]
            members = cb.members
            seed = members[0] if members else key
            other = members[1] if len(members) > 1 else ""
            self.last_source = "A"
            self.last_seed = key
            return self._render("A", key, seed, other)

        # 来源B 兜底（抢救即将遗忘的记忆）
        forgetting = self._forgetting_words()
        if forgetting:
            word, value = forgetting[0]
            # 找一个与它关联最强的"另一件事"作为 other（增强话术自然度）
            other = ""
            try:
                nb = self._owner.graph.strongest_neighbor(word)
                if nb and nb != word:
                    other = nb
            except Exception:
                other = ""
            self.last_source = "B"
            self.last_seed = word
            return self._render("B", word, word, other)

        return None

    def _render(self, source: str, seed_key: str, seed: str, other: str) -> dict:
        """套用预设话术模板（纯规则），{seed}/{other} 填充。"""
        tpl = random.choice(config.TOPIC_TEMPLATES)
        text = tpl.format(seed=seed, other=other or "别的事")
        return {
            "source": source,     # "A"=创新组合 "B"=遗忘预警
            "seed_key": seed_key,  # 反馈闭环定位（组合key 或 原词）
            "seed": seed,
            "other": other,
            "text": text,
        }

    # ---------- 反馈闭环 ----------
    def feedback(self, delta: float) -> None:
        """
        用户对小念主动发起话题后的反馈。
        delta ∈ [-1,1]：正=乐意聊（字数/情绪正增长），负=无视/骂（"滚"）。
        正反馈 → 给种子加权（boost）；负反馈 → 拉黑种子 + 标记需缩短下次主动间隔。
        返回是否发生负反馈（供上层缩短间隔用）。
        """
        if not self.last_seed:
            return
        if delta >= config.TOPIC_FEEDBACK_POSITIVE:
            # 正反馈：加权
            self._boost[self.last_seed] = self._boost.get(self.last_seed, 0.0) + 1.0
            # 若种子是合成概念，同步提升其 slope_utility（让它未来更易被想象力出口召回）
            if self.last_source == "A":
                try:
                    self._owner.sleep_engine.record_feedback(self.last_seed, 0.5)
                except Exception:
                    pass
        elif delta <= config.TOPIC_FEEDBACK_NEGATIVE:
            # 负反馈：拉黑 + 让该组合效用暴跌（下次想象力出口也不召回）
            self._blacklist.add(self.last_seed)
            if self.last_source == "A":
                try:
                    self._owner.sleep_engine.record_feedback(self.last_seed, -1.0)
                except Exception:
                    pass

    def negative_happened(self) -> bool:
        """上次反馈是否为负（供上层缩短下次主动间隔）。"""
        return self.last_seed in self._blacklist

    def stats(self) -> dict:
        return {
            "blacklist_size": len(self._blacklist),
            "boost_size": len(self._boost),
            "last_source": self.last_source,
            "last_seed": self.last_seed,
        }
