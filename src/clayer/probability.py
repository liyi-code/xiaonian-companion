# -*- coding: utf-8 -*-
"""
概率 + 伪随机组合 模块。

对应理论：
  - 权重 -> 概率：把每个被解锁概念的"权重"经 softmax 变成选取概率，
    权重占比大的，被调用/组合的概率就大(严格正相关)。
  - 统计信息数量变化 -> 偏向概率波动：softmax 的温度由"总统计量"调制。
    统计越多 -> 温度越低 -> 分布越尖 -> 偏向越强；统计少 -> 温度高 -> 波动大更随机。
  - 基础概率 / 最大概率：把每个概率 clamp 到 [BASE_PROB, MAX_PROB] 再归一化。
    基础概率保证弱项也有机会(灵感/发散)，最大概率防止强项独裁(避免僵化)。
  - 伪随机组合：按最终概率做加权无放回采样，抽出 K 个概念组成"这一念"。
"""
from __future__ import annotations
from typing import Dict, List, Tuple
import math
import random

import cl_config as config


def stat_modulated_temperature(total_observations: float) -> float:
    """
    统计量 -> 温度。见得越多，温度越低(偏向越强)，但有下限，永远保留波动。
        T = max(TEMP_MIN, TEMP_BASE / (1 + beta * log(1 + total)))
    """
    t = config.TEMP_BASE / (1.0 + config.TEMP_STAT_BETA * math.log1p(max(0.0, total_observations)))
    return max(config.TEMP_MIN, t)


def softmax(scores: Dict[str, float], temperature: float) -> Dict[str, float]:
    if not scores:
        return {}
    t = max(1e-6, temperature)
    mx = max(scores.values())
    exps = {k: math.exp((v - mx) / t) for k, v in scores.items()}
    z = sum(exps.values()) or 1.0
    return {k: v / z for k, v in exps.items()}


def clamp_and_renorm(probs: Dict[str, float],
                     base: float = config.BASE_PROB,
                     pmax: float = config.MAX_PROB) -> Dict[str, float]:
    """
    基础概率 & 最大概率钳制：每个概率被限制在 [eff_base, pmax]，且整体归一化到和为 1。
    用水填充(water-filling)迭代，保证 pmax 真正生效（不会被重新归一化顶破 -> 防独裁）。
    """
    if not probs:
        return {}
    n = len(probs)
    eff_base = min(base, pmax / n)  # 地板不能大到自己就超 1
    lo = eff_base
    hi = pmax

    x = {k: min(hi, max(lo, v)) for k, v in probs.items()}
    for _ in range(60):
        s = sum(x.values())
        if abs(s - 1.0) < 1e-6:
            break
        if s > 1.0:
            excess = s - 1.0
            movable = sum(max(0.0, x[k] - lo) for k in x)
            if movable <= 1e-12:
                break
            for k in x:
                x[k] = max(lo, x[k] - (x[k] - lo) * (excess / movable))
        else:
            deficit = 1.0 - s
            room = sum(hi - x[k] for k in x)
            if room <= 1e-12:
                break
            for k in x:
                x[k] = min(hi, x[k] + (hi - x[k]) * (deficit / room))
    return x


def entropy(probs: Dict[str, float]) -> float:
    """分布的香农熵(bits)。用于衡量当前意识是'专注'还是'发散'。"""
    h = 0.0
    for p in probs.values():
        if p > 0:
            h -= p * math.log2(p)
    return h


def normalized_entropy(probs: Dict[str, float]) -> float:
    """归一化熵[0,1]：0=完全专注(独一)，1=完全均匀发散。"""
    n = len(probs)
    if n <= 1:
        return 0.0
    return entropy(probs) / math.log2(n)


class ProbabilityEngine:
    """把'解锁能量+统计显著度'转成选取概率，并做伪随机加权组合采样。"""

    def __init__(self, seed=config.PRNG_SEED):
        self.rng = random.Random(seed)

    def build_distribution(
        self,
        activation: Dict[str, float],
        salience: Dict[str, float],
        total_observations: float,
    ) -> Tuple[Dict[str, float], float]:
        """
        综合权重 = 链式激活能量 * (1 + 统计显著度)。
        既看'当下被解锁得多强'(链式)，也看'长期见得多不多'(统计)。
        返回 (最终概率分布, 使用的温度)。
        """
        if not activation:
            return {}, config.TEMP_BASE
        raw: Dict[str, float] = {}
        for c, e in activation.items():
            sal = salience.get(c, 0.0)
            raw[c] = e * (1.0 + sal)

        temp = stat_modulated_temperature(total_observations)
        probs = softmax(raw, temp)
        probs = clamp_and_renorm(probs)
        return probs, temp

    def pick_k(self, n_candidates: int, focus: float = config.COMBINE_FOCUS) -> int:
        """决定这一念组合几个概念。专注度高 -> K 小而精；低 -> K 大而发散。"""
        lo, hi = config.COMBINE_MIN_K, config.COMBINE_MAX_K
        span = hi - lo
        k = int(round(hi - focus * span))
        k = max(lo, min(hi, k))
        return max(1, min(k, n_candidates))

    def combine(self, probs: Dict[str, float], k: int) -> List[Tuple[str, float]]:
        """
        伪随机组合：按概率做加权无放回采样，抽出 k 个概念。
        权重大的更可能先被抽中(调用并组合的概率大)，但基础概率保证弱项偶尔入选。
        返回 [(概念, 概率)] 按原概率降序。
        """
        if not probs:
            return []
        items = list(probs.items())
        pool = items[:]
        chosen: List[Tuple[str, float]] = []
        for _ in range(min(k, len(pool))):
            total = sum(p for _, p in pool)
            if total <= 0:
                break
            r = self.rng.random() * total
            acc = 0.0
            idx = 0
            for i, (_c, p) in enumerate(pool):
                acc += p
                if r <= acc:
                    idx = i
                    break
            chosen.append(pool.pop(idx))
        chosen.sort(key=lambda kv: kv[1], reverse=True)
        return chosen
